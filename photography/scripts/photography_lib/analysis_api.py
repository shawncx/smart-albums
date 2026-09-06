"""One-attempt OpenAI transport and shared single/multi-image request format."""
import base64
import json
import time
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener
from .vision import NoRedirect, ProviderError, OpenAIResponsesProvider
from .analysis_schema import PROMPT, OUTPUT_SCHEMA, object_schema, validate_analysis
from .config import PhotographyError


class RequestFailure(ProviderError):
    def __init__(self, code, message, *, retryable=False, uncertain=False, wait=0, fatal=False, usage=None):
        super().__init__(code, message, fatal=fatal)
        self.retryable, self.uncertain, self.wait, self.usage = retryable, uncertain, wait, usage


def token_usage(envelope):
    usage = envelope.get("usage") if isinstance(envelope, dict) else None
    if not isinstance(usage, dict):
        return None
    result = {k: v for k, v in usage.items() if k in ("input_tokens", "output_tokens", "total_tokens") and type(v) is int and v >= 0}
    details = usage.get("input_tokens_details")
    if isinstance(details, dict) and type(details.get("cached_tokens")) is int and 0 <= details["cached_tokens"] <= result.get("input_tokens", 0):
        result["cached_input_tokens"] = details["cached_tokens"]
    return result or None


def retry_delay(headers):
    value = headers.get("Retry-After", "0")
    try:
        return max(0, float(value))
    except ValueError:
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return 0


class OpenAIHTTP:
    def __init__(self, provider):
        self.provider = provider
        self.headers = {}

    def call(self, path, *, method="GET", data=None, content_type="application/json", raw=False, max_bytes=4_194_304):
        self.provider.check_ready()
        config = self.provider.config
        body = json.dumps(data, ensure_ascii=False).encode("utf-8") if isinstance(data, dict) else data
        request = Request(config.base_url + path, data=body, method=method,
            headers={"Authorization": "Bearer " + config.api_key, "Content-Type": content_type})
        try:
            with build_opener(NoRedirect()).open(request, timeout=config.timeout) as response:
                content = response.read(max_bytes + 1)
                self.headers = {k.lower(): v for k, v in response.headers.items() if k.lower().startswith("x-ratelimit-")}
            if len(content) > max_bytes:
                raise RequestFailure("RESPONSE_TOO_LARGE", "The response exceeded the local size limit.")
            return content if raw else json.loads(content)
        except HTTPError as exc:
            status, delay = exc.code, retry_delay(exc.headers)
            quota = False
            try:
                err = json.loads(exc.read(8192)).get("error", {})
                quota = isinstance(err, dict) and err.get("code") in ("insufficient_quota", "billing_hard_limit_reached")
            except (ValueError, OSError, AttributeError):
                pass
            finally:
                exc.close()
            raise RequestFailure("QUOTA_EXHAUSTED" if quota else "RATE_LIMITED" if status == 429 else "MODEL_HTTP_ERROR",
                f"Model service returned HTTP {status}.", retryable=(status == 429 or status >= 500) and not quota,
                fatal=quota or status in (400, 401, 403, 404), wait=delay,
                uncertain=method == "POST" and status >= 500) from None
        except (URLError, TimeoutError, OSError):
            raise RequestFailure("MODEL_NETWORK_ERROR", "No definitive response was received.",
                uncertain=method == "POST", retryable=method != "POST") from None
        except (ValueError, UnicodeError):
            raise RequestFailure("INVALID_MODEL_OUTPUT", "Service returned invalid JSON.", uncertain=method == "POST") from None


def build_payload(config, images):
    """images is an ordered list of (opaque photo ID, JPEG bytes)."""
    multi = len(images) > 1
    schema = OUTPUT_SCHEMA
    content = []
    for pid, data in images:
        content.extend([{"type": "input_text", "text": "Photo ID: " + pid if multi else "Describe this photograph's preview."},
            {"type": "input_image", "detail": config.detail,
             "image_url": "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")}])
    instructions = PROMPT + f"\nWrite descriptive values in {config.language}."
    if multi:
        schema = object_schema({"photos": {"type": "array", "minItems": len(images), "maxItems": len(images),
            "items": object_schema({"photo_id": {"type": "string", "enum": [pid for pid, _ in images]}, "analysis": OUTPUT_SCHEMA})}})
        instructions += "\nApply the observations separately to each labeled photo. Return each photo ID exactly once; never mix observations between images."
    return {"model": config.model, "store": False, "instructions": instructions,
        "input": [{"role": "user", "content": content}],
        "text": {"format": {"type": "json_schema", "name": "photo_analyses" if multi else "photo_analysis", "strict": True, "schema": schema}},
        "max_output_tokens": config.max_output_tokens * len(images)}


def parse_results(envelope, ids):
    usage = token_usage(envelope)
    try:
        output = OpenAIResponsesProvider._parse(envelope)
    except ProviderError as exc:
        raise RequestFailure(exc.code, str(exc), usage=usage) from None
    output.usage = usage
    if len(ids) == 1:
        try:
            validate_analysis(output.data)
            return {ids[0]: output}, {}, usage
        except PhotographyError as exc:
            raise RequestFailure(exc.code, str(exc), usage=usage) from None
    data = output.data
    if not isinstance(data, dict) or set(data) != {"photos"} or not isinstance(data["photos"], list):
        raise RequestFailure("INVALID_MODEL_OUTPUT", "Expected a per-photo result list.", usage=usage)
    values, errors = {}, {}
    seen = set()
    for item in data["photos"]:
        if not isinstance(item, dict) or set(item) != {"photo_id", "analysis"} or item.get("photo_id") not in ids:
            raise RequestFailure("RESULT_MAPPING_ERROR", "Result contains an unknown photo or invalid mapping.", usage=usage)
        pid = item["photo_id"]
        if pid in seen:
            values.pop(pid, None)
            errors[pid] = {"code": "DUPLICATE_RESULT", "message": "Repeated photo ID in model output."}
            continue
        seen.add(pid)
        try:
            validate_analysis(item["analysis"])
            from dataclasses import replace
            values[pid] = replace(output, data=item["analysis"], usage=None, usage_scope="shared_provider_request")
        except PhotographyError as exc:
            errors[pid] = exc.to_dict()
    for pid in ids:
        if pid not in values and pid not in errors:
            errors[pid] = {"code": "MISSING_RESULT", "message": "No result for this photo."}
    return values, errors, usage
