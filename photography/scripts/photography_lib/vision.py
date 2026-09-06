"""Small OpenAI Responses adapter. Credentials and raw errors are never persisted."""
from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass, field
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .analysis_schema import OUTPUT_SCHEMA, PROMPT, PROMPT_VERSION, SCHEMA_VERSION, fingerprint
from .config import PhotographyError


@dataclass(frozen=True)
class AnalysisConfig:
    model: str = "gpt-5.4-mini-2026-03-17"
    api_key: str = field(default="", repr=False, compare=False)
    base_url: str = "https://api.openai.com/v1"
    language: str = "zh-CN"
    detail: str = "high"
    max_output_tokens: int = 2400
    timeout: float = 60
    retries: int = 2

    def __post_init__(self):
        parts = urlsplit(self.base_url)
        local_http = parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1")
        if (parts.scheme != "https" and not local_http) or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise PhotographyError("INVALID_CONFIG", "Model base URL must be HTTPS (HTTP is allowed only for local testing), without credentials or query parameters.")
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))
        if not isinstance(self.model, str) or not self.model.strip():
            raise PhotographyError("INVALID_CONFIG", "A model name is required.")
        if self.language not in ("zh-CN", "en") or self.detail not in ("low", "high", "auto"):
            raise PhotographyError("INVALID_CONFIG", "Unsupported analysis language or image detail.")
        if not 1 <= self.timeout <= 300 or not 0 <= self.retries <= 3 or not 512 <= self.max_output_tokens <= 8000:
            raise PhotographyError("INVALID_CONFIG", "Invalid model timeout, retries or output limit.")

    @classmethod
    def from_env(cls, **overrides):
        values = {"api_key": os.environ.get("OPENAI_API_KEY", ""),
                  "model": os.environ.get("PHOTOGRAPHY_MODEL") or cls.model,
                  "base_url": os.environ.get("OPENAI_BASE_URL") or cls.base_url}
        values.update({key: value for key, value in overrides.items() if value is not None})
        return cls(**values)

    def profile(self) -> dict:
        return {"provider": "openai-responses", "base_url": self.base_url, "model": self.model,
                "language": self.language, "detail": self.detail,
                "max_output_tokens": self.max_output_tokens,
                "prompt_version": PROMPT_VERSION, "prompt_hash": fingerprint(PROMPT),
                "schema_version": SCHEMA_VERSION, "schema_hash": fingerprint(OUTPUT_SCHEMA)}


@dataclass
class ModelResult:
    data: dict
    model: str
    response_id: str | None = None
    usage: dict | None = None
    model_source: str = "provider_response"
    usage_scope: str = "provider_request"


class VisionProvider(Protocol):
    def profile(self) -> dict: ...
    def check_ready(self) -> None: ...
    def analyze(self, preview: bytes) -> ModelResult: ...


class ProviderError(PhotographyError):
    def __init__(self, code, message, *, fatal=False):
        super().__init__(code, message)
        self.fatal = fatal


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward the API credential or photograph to a redirected host.
        return None


class OpenAIResponsesProvider:
    def __init__(self, config: AnalysisConfig | None = None):
        self.config = config or AnalysisConfig.from_env()
        self.opener = build_opener(NoRedirect())

    def profile(self):
        return self.config.profile()

    def check_ready(self):
        if not self.config.api_key.strip():
            raise ProviderError("MODEL_CREDENTIAL_MISSING", "Set OPENAI_API_KEY in the local environment to enable image analysis.", fatal=True)

    def analyze(self, preview: bytes) -> ModelResult:
        self.check_ready()
        payload = {
            "model": self.config.model, "store": False,
            "instructions": PROMPT + f"\nWrite descriptive values in {self.config.language}.",
            "input": [{"role": "user", "content": [
                {"type": "input_text", "text": "Describe this photograph's preview."},
                {"type": "input_image", "detail": self.config.detail,
                 "image_url": "data:image/jpeg;base64," + base64.b64encode(preview).decode("ascii")},
            ]}],
            "text": {"format": {"type": "json_schema", "name": "photo_analysis",
                                 "strict": True, "schema": OUTPUT_SCHEMA}},
            "max_output_tokens": self.config.max_output_tokens,
        }
        request = Request(self.config.base_url + "/responses",
                          data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                          headers={"Authorization": "Bearer " + self.config.api_key,
                                   "Content-Type": "application/json"}, method="POST")
        for attempt in range(self.config.retries + 1):
            try:
                with self.opener.open(request, timeout=self.config.timeout) as response:
                    raw = response.read(1_048_577)
                if len(raw) > 1_048_576:
                    raise ProviderError("INVALID_MODEL_OUTPUT", "Model response exceeds the size limit.")
                try:
                    envelope = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise ProviderError("INVALID_MODEL_OUTPUT", "Model returned an invalid JSON response.") from None
                return self._parse(envelope)
            except HTTPError as exc:
                status = exc.code
                exc.close()
                if (status == 429 or 500 <= status < 600) and attempt < self.config.retries:
                    time.sleep(2 ** attempt)
                    continue
                fatal = status in (400, 401, 403, 404)
                code = "MODEL_AUTH_FAILED" if status in (401, 403) else "MODEL_HTTP_ERROR"
                raise ProviderError(code, f"Model service returned HTTP {status}; check credentials, model access or request configuration.", fatal=fatal) from None
            except (URLError, TimeoutError, OSError):
                if attempt < self.config.retries:
                    time.sleep(2 ** attempt)
                    continue
                raise ProviderError("MODEL_NETWORK_ERROR", "Model service could not be reached within the retry limit.") from None
        raise AssertionError("Unreachable retry state")

    @staticmethod
    def _parse(envelope) -> ModelResult:
        if not isinstance(envelope, dict) or not isinstance(envelope.get("output"), list):
            raise ProviderError("INVALID_MODEL_OUTPUT", "Model response has no valid output list.")
        texts = []
        for item in envelope["output"]:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                raise ProviderError("INVALID_MODEL_OUTPUT", "Model response has invalid message content.")
            for part in content:
                if not isinstance(part, dict):
                    raise ProviderError("INVALID_MODEL_OUTPUT", "Model response has invalid content.")
                if part.get("type") == "refusal":
                    raise ProviderError("MODEL_REFUSED", "The model declined to analyze this image.")
                if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                    texts.append(part["text"])
        if envelope.get("status") != "completed":
            raise ProviderError("MODEL_INCOMPLETE", "Model output was incomplete; no analysis was saved.")
        if len(texts) != 1 or not isinstance(envelope.get("model"), str):
            raise ProviderError("INVALID_MODEL_OUTPUT", "Expected one structured output and a returned model name.")
        try:
            data = json.loads(texts[0])
        except ValueError:
            raise ProviderError("INVALID_MODEL_OUTPUT", "Model output is not a JSON object.") from None
        # Persist only known token counters, never arbitrary provider response fields.
        usage = envelope.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        usage = {key: value for key, value in usage.items()
                 if key in ("input_tokens", "output_tokens", "total_tokens")
                 and type(value) is int and value >= 0}
        response_id = envelope.get("id")
        return ModelResult(data=data, model=envelope["model"],
                           response_id=response_id if isinstance(response_id, str) else None,
                           usage=usage or None)
