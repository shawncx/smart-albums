"""Public settings only. Authentication is resolved by the executing process."""
import math
import os
import re
from .config import PhotographyError
from .vision import AnalysisConfig, OpenAIResponsesProvider
from .codex_vision import CodexConfig, CodexCLIProvider

DEFAULTS = dict(provider="openai", model=None, base_url="https://api.openai.com/v1",
    key_env="OPENAI_API_KEY", language="zh-CN", detail="high", reasoning="low", timeout=None,
    retries=2, max_output_tokens=2400, wait_preference="immediate", mode="auto",
    images_per_request=1, concurrency=1, rpm=None, tpm=None, input_tokens_per_photo=None,
    input_price=None, output_price=None, queue_tokens=None, max_wait_seconds=60,
    batch_max_requests=1000, batch_max_bytes=100_000_000)


def resolve_settings(saved=None, overrides=None):
    overrides = {k: v for k, v in (overrides or {}).items() if v is not None}
    if set(overrides) - set(DEFAULTS):
        raise PhotographyError("INVALID_CONFIG", "Unknown analysis setting.")
    base = {**DEFAULTS, **(saved or {})}
    # A model/endpoint from a different channel must never silently carry over.
    if overrides.get("provider", base["provider"]) != base["provider"]:
        for key in ("model", "base_url", "timeout", "key_env"):
            base[key] = DEFAULTS[key]
    result = {**base, **overrides}
    if not result["model"]:
        result["model"] = (CodexConfig.model if result["provider"] == "codex" else
                            os.environ.get("PHOTOGRAPHY_MODEL") or AnalysisConfig.model)
    if not saved and "base_url" not in overrides:
        result["base_url"] = os.environ.get("OPENAI_BASE_URL") or result["base_url"]
    choices = {"provider": ("openai", "codex"), "language": ("zh-CN", "en"),
        "detail": ("low", "high", "auto"), "reasoning": ("low", "medium", "high", "xhigh"),
        "mode": ("auto", "immediate", "batch"), "wait_preference": ("immediate", "flexible")}
    for key, allowed in choices.items():
        if result[key] not in allowed:
            raise PhotographyError("INVALID_CONFIG", f"Unsupported {key}.")
    bounds = {"images_per_request": (1, 4), "concurrency": (1, 8), "retries": (0, 3),
        "max_output_tokens": (512, 8000), "max_wait_seconds": (0, 300),
        "batch_max_requests": (1, 50000), "batch_max_bytes": (1000, 200_000_000)}
    for key, (low, high) in bounds.items():
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise PhotographyError("INVALID_CONFIG", f"Invalid {key}.")
    for key in ("rpm", "tpm", "queue_tokens", "input_tokens_per_photo", "input_price", "output_price"):
        value = result[key]
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value <= 0):
            raise PhotographyError("INVALID_CONFIG", f"{key} must be positive and finite.")
    if result["tpm"] and not result["input_tokens_per_photo"]:
        raise PhotographyError("INVALID_CONFIG", "A token rate limit needs an input-token estimate for admission scheduling.")
    if result["queue_tokens"] and not result["input_tokens_per_photo"]:
        raise PhotographyError("INVALID_CONFIG", "A Batch queue budget needs an input-token estimate.")
    if not isinstance(result["key_env"], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", result["key_env"]):
        raise PhotographyError("INVALID_CONFIG", "Use an environment variable name, not a credential.")
    make_provider(result)  # Validate configuration, without checking login or sending requests.
    return result


def make_provider(settings):
    if settings["provider"] == "codex":
        return CodexCLIProvider(CodexConfig(model=settings["model"], language=settings["language"],
            reasoning=settings["reasoning"], timeout=settings["timeout"] or 240))
    return OpenAIResponsesProvider(AnalysisConfig(model=settings["model"], language=settings["language"],
        detail=settings["detail"], base_url=settings["base_url"],
        api_key=os.environ.get(settings["key_env"], ""), timeout=settings["timeout"] or 60,
        max_output_tokens=settings["max_output_tokens"], retries=0))
