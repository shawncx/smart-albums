"""Optional local Codex CLI adapter; Codex owns and reuses its saved login."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .analysis_schema import OUTPUT_SCHEMA, PROMPT, PROMPT_VERSION, SCHEMA_VERSION, fingerprint
from .config import PhotographyError
from .vision import ModelResult, ProviderError

CLI_PROMPT = PROMPT + "\nAnalyze only the attached preview. Do not use tools. Return only the schema's JSON object."


@dataclass(frozen=True)
class CodexConfig:
    model: str = "gpt-6-astra"
    language: str = "zh-CN"
    reasoning: str = "low"
    timeout: float = 240
    executable: str = "codex"

    def __post_init__(self):
        if not self.model.strip() or self.language not in ("zh-CN", "en"):
            raise PhotographyError("INVALID_CONFIG", "Provide a Codex model and supported language.")
        if self.reasoning not in ("low", "medium", "high", "xhigh") or not 1 <= self.timeout <= 600:
            raise PhotographyError("INVALID_CONFIG", "Invalid Codex reasoning level or timeout.")

    def profile(self):
        return {"provider": "codex-cli", "model": self.model, "language": self.language,
                "reasoning": self.reasoning, "input": "ingestion-jpeg-preview",
                "adapter_version": "codex-preview-v1", "prompt_version": PROMPT_VERSION,
                "prompt_hash": fingerprint(CLI_PROMPT), "schema_version": SCHEMA_VERSION,
                "schema_hash": fingerprint(OUTPUT_SCHEMA)}


class CodexCLIProvider:
    def __init__(self, config: CodexConfig | None = None):
        self.config = config or CodexConfig()
        self.executable = None
        self._ready = False

    def profile(self):
        return self.config.profile()

    @staticmethod
    def process_options():
        return {"capture_output": True, "encoding": "utf-8", "errors": "replace",
                "creationflags": subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0}

    def check_ready(self):
        if self._ready:
            return
        self.executable = shutil.which(self.config.executable)
        if not self.executable:
            raise ProviderError("CODEX_NOT_FOUND", "Install or expose the Codex CLI on PATH.", fatal=True)
        try:
            result = subprocess.run([self.executable, "login", "status"], timeout=15, **self.process_options())
        except (OSError, subprocess.TimeoutExpired):
            raise ProviderError("CODEX_LOGIN_UNAVAILABLE", "Could not check Codex login in this process environment.", fatal=True) from None
        if result.returncode != 0:
            raise ProviderError("CODEX_LOGIN_UNAVAILABLE", "Codex cannot access a saved login here. Use the normal logged-in user environment or sign in with Codex.", fatal=True)
        self._ready = True

    def analyze(self, preview: bytes) -> ModelResult:
        self.check_ready()
        with tempfile.TemporaryDirectory(prefix="photography-codex-") as temporary:
            work = Path(temporary).resolve()
            image = work / "preview.jpg"
            schema = work / "schema.json"
            output = work / "analysis.json"
            image.write_bytes(preview)
            schema.write_text(json.dumps(OUTPUT_SCHEMA), encoding="utf-8")
            # A single image in an isolated working directory. Auth stays in Codex's
            # normal store; no credential is read, copied or included in this package.
            command = [self.executable, "exec", "--ignore-user-config", "--ephemeral",
                       "--skip-git-repo-check", "--sandbox", "read-only", "--cd", str(work),
                       "--model", self.config.model, "--json", "--color", "never",
                       "--disable", "shell_tool", "--disable", "apps", "--disable", "browser_use",
                       "--disable", "computer_use", "-c", 'web_search="disabled"',
                       "-c", f'model_reasoning_effort="{self.config.reasoning}"',
                       "--image", str(image), "--output-schema", str(schema),
                       "--output-last-message", str(output), "-"]
            prompt = CLI_PROMPT + f"\nWrite descriptive values in {self.config.language}."
            try:
                result = subprocess.run(command, input=prompt, cwd=work, timeout=self.config.timeout,
                                        **self.process_options())
            except subprocess.TimeoutExpired:
                raise ProviderError("CODEX_TIMEOUT", "Codex exceeded the per-photo timeout; usage may be unavailable. No automatic retry was made.") from None
            except OSError:
                raise ProviderError("CODEX_EXEC_FAILED", "Codex could not start in the current environment.", fatal=True) from None
            if result.returncode != 0:
                raise ProviderError("CODEX_EXEC_FAILED", "Codex did not complete the photo request. Check login, model access and local runtime configuration; no automatic retry was made.", fatal=True)
            try:
                if output.stat().st_size > 1_048_576:
                    raise ValueError("oversized output")
                data = json.loads(output.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError, UnicodeError):
                raise ProviderError("INVALID_MODEL_OUTPUT", "Codex did not produce a valid structured result file.") from None
            usage = self.parse_usage(result.stdout)
            return ModelResult(data=data, model=self.config.model, usage=usage,
                               model_source="requested_cli_model", usage_scope="codex_turn")

    @staticmethod
    def parse_usage(events: str) -> dict | None:
        totals = {}
        for line in events.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get("type") != "turn.completed":
                continue
            usage = event.get("usage")
            if not isinstance(usage, dict):
                continue
            for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
                value = usage.get(key)
                if type(value) is int and value >= 0:
                    totals[key] = totals.get(key, 0) + value
        if "input_tokens" in totals and "output_tokens" in totals:
            totals["total_tokens"] = totals["input_tokens"] + totals["output_tokens"]
        return totals or None
