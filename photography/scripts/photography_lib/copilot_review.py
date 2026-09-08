"""Opt-in Copilot transport. Call only after confirming the complete frozen task.

The pinned SDK controls are not an OS sandbox or a remote-retention guarantee.
Credential/configuration startup reads still use the user's normal Copilot home.
Only session files and the child working directory are routed to owned storage.
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from io import BytesIO
import math
import logging
import os
from pathlib import Path
import re
import shutil
import sys
from time import perf_counter
from types import SimpleNamespace
from uuid import uuid4

from .config import PhotographyError, default_model_cache_root
from .review_provider import ReviewImage, ReviewReply, ReviewRequest
from .review_schema import PROVIDER_ID, RUNTIME_VERSION, SDK_VERSION


_IMAGE_ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,38}\Z")
_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                 "reasoning_tokens")
# Preserve ordinary credential-home locations, but not token, BYOK, runtime,
# instruction, Node preload, or telemetry overrides from the invoking shell.
_CHILD_ENV_KEYS = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE",
    "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
    "COPILOT_HOME",
    "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS", "DISPLAY", "WAYLAND_DISPLAY",
    "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
})


def _positive_int(value) -> bool:
    return type(value) is int and value > 0


def _nonnegative_number(value) -> bool:
    return ((type(value) is int and value >= 0)
            or (type(value) is float and math.isfinite(value) and value >= 0))


class _UsageTotals:
    """Sum per-call events, never context occupancy or cumulative snapshots.

    Pinned docs/features/usage-and-billing.md defines assistant.usage as one
    event per API call. A missing field in any call leaves its total unknown.
    """

    def __init__(self):
        self._seen = set()
        self._calls = []

    def record(self, event):
        if event.id in self._seen:
            return
        self._seen.add(event.id)
        data = event.data
        observed = {}
        for key in _TOKEN_FIELDS:
            value = getattr(data, key)
            if type(value) is int and value >= 0:
                observed[key] = value
        if data.copilot_usage is not None:
            value = data.copilot_usage.total_nano_aiu
            if _nonnegative_number(value):
                observed["credits"] = value
        self._calls.append(observed)

    def metadata(self) -> dict:
        result = {"usage_source": ("assistant.usage:per_call_sum" if self._calls
                                  else "assistant.usage:unavailable")}
        for key in (*_TOKEN_FIELDS, "credits"):
            if self._calls and all(key in call for call in self._calls):
                total = sum(call[key] for call in self._calls)
                if _nonnegative_number(total):
                    result[key] = total
        if "credits" in result:
            # Preserve the API's unit. Neither the premium multiplier `cost`
            # nor nano-AIU is silently converted to Azure credit or currency.
            result["credits_unit"] = "copilot_nano_aiu"
        return result


def _load_sdk():
    try:
        installed = version("github-copilot-sdk")
    except PackageNotFoundError:
        raise PhotographyError(
            "REVIEW_SDK_MISSING",
            "Install the optional photography/requirements-review.txt dependencies explicitly.",
        ) from None
    if installed != SDK_VERSION:
        raise PhotographyError("REVIEW_SDK_VERSION", f"Review requires github-copilot-sdk=={SDK_VERSION}.")
    try:
        from copilot import CopilotClient, RuntimeConnection
        from copilot._jsonrpc import JsonRpcError, ProcessExitedError
        from copilot._cli_download import get_cache_dir
        from copilot._cli_version import CLI_VERSION, get_runtime_platform
        from copilot.rpc import PermissionDecisionReject, SessionUpdateOptionsParams
        from copilot.session_events import (
            AssistantMessageData, AssistantUsageData, SessionErrorData, SessionIdleData, SessionMode,
        )
    except ImportError:
        raise PhotographyError("REVIEW_SDK_MISSING", "The pinned Copilot SDK installation is incomplete.") from None
    if CLI_VERSION != RUNTIME_VERSION:
        raise PhotographyError("REVIEW_RUNTIME_VERSION", "The installed SDK has an unexpected runtime pin.")
    return SimpleNamespace(
        CopilotClient=CopilotClient, PermissionDecisionReject=PermissionDecisionReject,
        RuntimeConnection=RuntimeConnection, get_cache_dir=get_cache_dir,
        get_runtime_platform=get_runtime_platform,
        SessionUpdateOptionsParams=SessionUpdateOptionsParams,
        AssistantUsageData=AssistantUsageData, SessionErrorData=SessionErrorData,
        AssistantMessageData=AssistantMessageData, SessionIdleData=SessionIdleData, SessionMode=SessionMode,
        JsonRpcError=JsonRpcError, ProcessExitedError=ProcessExitedError,
    )


def _installed_runtime(sdk) -> Path:
    # download-runtime provisions the wrapper, not necessarily the historical
    # copilot.exe alias. Never call ensure_runtime_wrapper or a download helper.
    try:
        pair = sdk.get_cache_dir(RUNTIME_VERSION) / "prebuilds" / sdk.get_runtime_platform()
        wrapper = pair / ("copilot-runtime.exe" if sys.platform == "win32" else "copilot-runtime")
        required = (wrapper, pair / "runtime.node", pair / ".hostless-runtime-assets-v2")
        complete = all(path.is_file() and path.stat().st_size > 0 for path in required)
    except (OSError, RuntimeError):
        complete = False
    if not complete:
        raise PhotographyError(
            "REVIEW_RUNTIME_MISSING",
            f"Explicitly provision Copilot runtime {RUNTIME_VERSION} before review; automatic download is disabled.",
        )
    return wrapper.resolve()


def _session_files(root: Path):
    from copilot.rpc import SessionFSReaddirWithTypesEntry, SessionFSReaddirWithTypesEntryType
    from copilot.session_fs_provider import SessionFsFileInfo, SessionFsProvider

    class OwnedSessionFiles(SessionFsProvider):
        def target(self, path: str) -> Path:
            if not isinstance(path, str) or "\0" in path:
                raise ValueError("Invalid session-state path.")
            requested = Path(path)
            if (not requested.is_absolute() or ".." in requested.parts
                    or any(":" in part for part in requested.parts[1:])):
                raise ValueError("Invalid session-state path.")
            target = requested.resolve()
            if not target.is_relative_to(root):
                raise ValueError("Session-state path escaped its owned directory.")
            return target

        async def read_file(self, path):
            return self.target(path).read_text(encoding="utf-8")

        async def write_file(self, path, content, mode=None):
            target = self.target(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        async def append_file(self, path, content, mode=None):
            target = self.target(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as stream:
                stream.write(content)

        async def exists(self, path):
            return self.target(path).exists()

        async def stat(self, path):
            target = self.target(path)
            info = target.stat()
            return SessionFsFileInfo(
                is_file=target.is_file(), is_directory=target.is_dir(), size=info.st_size,
                mtime=datetime.fromtimestamp(info.st_mtime, UTC),
                birthtime=datetime.fromtimestamp(info.st_ctime, UTC),
            )

        async def mkdir(self, path, recursive, mode=None):
            self.target(path).mkdir(parents=recursive, exist_ok=recursive)

        async def readdir(self, path):
            return sorted(entry.name for entry in self.target(path).iterdir())

        async def readdir_with_types(self, path):
            return [
                SessionFSReaddirWithTypesEntry(
                    name=entry.name,
                    type=(SessionFSReaddirWithTypesEntryType.DIRECTORY if entry.is_dir()
                          else SessionFSReaddirWithTypesEntryType.FILE),
                )
                for entry in sorted(self.target(path).iterdir())
            ]

        async def rm(self, path, recursive, force):
            target = self.target(path)
            try:
                if target.is_dir():
                    if recursive:
                        shutil.rmtree(target)
                    else:
                        target.rmdir()
                else:
                    target.unlink()
            except FileNotFoundError:
                if not force:
                    raise

        async def rename(self, src, dest):
            self.target(src).rename(self.target(dest))

    return OwnedSessionFiles()


def _validate_request(request: ReviewRequest) -> None:
    if (not isinstance(request, ReviewRequest)
            or not isinstance(request.model, str) or not request.model.strip()
            or len(request.model) > 160 or any(ord(char) < 32 for char in request.model)
            or request.model.casefold() == "auto"
            or not isinstance(request.prompt, str) or not request.prompt.strip()
            or not _positive_int(request.batch_size)
            or not _positive_int(request.max_image_bytes)
            or not isinstance(request.images, tuple)
            or not 1 <= len(request.images) <= request.batch_size):
        raise PhotographyError("REVIEW_REQUEST_INVALID", "Review requires an explicit model and a valid frozen batch.")
    from PIL import Image, UnidentifiedImageError

    seen = set()
    for image in request.images:
        if (not isinstance(image, ReviewImage) or not isinstance(image.image_id, str)
                or not _IMAGE_ID.fullmatch(image.image_id) or image.image_id in seen
                or not isinstance(image.data, bytes)
                or not 0 < len(image.data) <= request.max_image_bytes):
            raise PhotographyError("REVIEW_REQUEST_INVALID", "Review images must have unique opaque IDs and bounded JPEG bytes.")
        seen.add(image.image_id)
        try:
            with Image.open(BytesIO(image.data)) as preview:
                if preview.format != "JPEG":
                    raise ValueError("Not a JPEG.")
                preview.load()
        except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
            raise PhotographyError("REVIEW_IMAGE_INVALID", "Review accepts only valid saved JPEG previews.") from None


def _model_limits(model: dict, request: ReviewRequest) -> None:
    capabilities = model.get("capabilities")
    if not isinstance(capabilities, dict):
        raise PhotographyError("REVIEW_MODEL_UNSUPPORTED", "The selected model has no declared vision capabilities.")
    supports, limits = capabilities.get("supports"), capabilities.get("limits")
    vision = limits.get("vision") if isinstance(limits, dict) else None
    if (not isinstance(supports, dict) or supports.get("vision") is not True
            or not isinstance(vision, dict)
            or not _positive_int(vision.get("max_prompt_images"))
            or not _positive_int(vision.get("max_prompt_image_size"))
            or not isinstance(vision.get("supported_media_types"), list)
            or "image/jpeg" not in vision["supported_media_types"]):
        raise PhotographyError("REVIEW_MODEL_UNSUPPORTED", "The model must declare JPEG vision support and positive image limits.")
    if (request.batch_size > vision["max_prompt_images"]
            or request.max_image_bytes > vision["max_prompt_image_size"]):
        raise PhotographyError(
            "REVIEW_MODEL_LIMIT", "The model cannot accept the approved batch size or largest preview across this run.",
        )
    policy = model.get("policy")
    if policy is not None and (not isinstance(policy, dict) or policy.get("state") != "enabled"):
        raise PhotographyError("REVIEW_MODEL_UNSUPPORTED", "The selected model is not enabled by its declared policy.")


def _provider_error(stage, statuses):
    if 429 in statuses:
        code, message = "REVIEW_RATE_LIMITED", "Copilot rate-limited the review; it was not automatically retried."
    elif stage == "authentication" or any(code in (401, 403) for code in statuses):
        code, message = "REVIEW_AUTH_REQUIRED", "Copilot authentication failed. Check the intended local login explicitly."
    elif stage == "session_restrictions":
        code, message = "REVIEW_RESTRICTIONS_FAILED", "The required isolated session configuration failed."
    else:
        code, message = "REVIEW_PROVIDER_FAILED", "The Copilot review operation failed; it was not automatically retried."
    return PhotographyError(code, message, details={"stage": stage})


@contextmanager
def _provider_diagnostics():
    class SafeDiagnostic(logging.Filter):
        def filter(self, record):
            return logging.LogRecord(record.name, record.levelno, "", 0,
                                     "Copilot diagnostic; consult the structured review status for details.",
                                     (), None)

    sanitizer = SafeDiagnostic()
    loggers = [logging.getLogger(name) for name in ("copilot.client", "copilot.session", "copilot._jsonrpc")]
    for logger in loggers:
        logger.addFilter(sanitizer)
    try:
        yield
    finally:
        for logger in loggers:
            logger.removeFilter(sanitizer)


class CopilotReviewProvider:
    """A lazy synchronous facade; each operation owns a fresh client and session."""

    def __init__(self, *, timeout_seconds: float = 180, cleanup_timeout_seconds: float = 15,
                 state_directory: Path | None = None, expected_login: str | None = None):
        for value in (timeout_seconds, cleanup_timeout_seconds):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise PhotographyError("REVIEW_CONFIG_INVALID", "Review timeouts must be finite positive numbers.")
        if expected_login is not None and (not isinstance(expected_login, str) or not _LOGIN.fullmatch(expected_login)):
            raise PhotographyError("REVIEW_CONFIG_INVALID", "The expected Copilot login is invalid.")
        self.timeout_seconds = timeout_seconds
        self.cleanup_timeout_seconds = cleanup_timeout_seconds
        self.state_directory = state_directory
        self._login = expected_login

    def models(self) -> list[dict]:
        with _provider_diagnostics():
            return asyncio.run(self._operation(None))

    def review(self, request: ReviewRequest) -> ReviewReply:
        _validate_request(request)
        with _provider_diagnostics():
            return asyncio.run(self._operation(request))

    def _check_auth(self, auth) -> None:
        if (auth.isAuthenticated is not True or auth.authType != "user"
                or auth.host != "https://github.com"
                or not isinstance(auth.login, str) or not _LOGIN.fullmatch(auth.login)
                or (self._login is not None and auth.login.casefold() != self._login.casefold())):
            raise PhotographyError(
                "REVIEW_AUTH_REQUIRED",
                "Use the intended signed-in Copilot CLI account on github.com. "
                "Environment tokens, gh fallback, unknown sources, and account changes are not accepted; sign in explicitly outside review.",
            )
        self._login = auth.login

    async def _operation(self, request: ReviewRequest | None):
        started = perf_counter()
        client = session = owned = None
        owned_created = completed = False
        sdk_errors = ()
        session_id = str(uuid4())
        primary = None
        result = None
        stage = "sdk"
        usage = _UsageTotals()
        provider_statuses, reported_models, cleanup_errors = [], [], []
        reply_ready = asyncio.Event()
        reply_text = None
        accepting_reply = False

        async def cleanup_step(name, operation):
            try:
                await asyncio.wait_for(operation(), timeout=self.cleanup_timeout_seconds)
            except (OSError, RuntimeError, ValueError, ExceptionGroup, *sdk_errors, asyncio.CancelledError):
                # Never discard cleanup failures, or expose SDK exception text.
                cleanup_errors.append(name)

        try:
            sdk = _load_sdk()
            sdk_errors = (sdk.JsonRpcError, sdk.ProcessExitedError)
            runtime = _installed_runtime(sdk)
            stage = "owned_state"
            root = (Path(self.state_directory) if self.state_directory is not None
                    else default_model_cache_root().parent / "review-state")
            owned = root.resolve() / str(uuid4())
            owned.mkdir(parents=True, mode=0o700)
            owned_created = True
            work = owned / "work"
            work.mkdir(mode=0o700)
            session_state = owned / "session-state"
            session_state.mkdir(mode=0o700)
            env = {key: value for key, value in os.environ.items() if key.upper() in _CHILD_ENV_KEYS}
            env.update({"COPILOT_SKIP_CLI_DOWNLOAD": "1", "COPILOT_OTEL_ENABLED": "false",
                        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "false",
                        "TMP": str(work), "TEMP": str(work), "TMPDIR": str(work)})

            def create_files(created):
                nonlocal session
                if created.session_id != session_id:
                    raise PhotographyError("REVIEW_SESSION_INVALID", "The runtime changed the owned session identity.")
                session = created
                return _session_files(owned)

            def on_event(event):
                nonlocal reply_text
                data = event.data
                if isinstance(data, sdk.SessionErrorData):
                    provider_statuses.append(data.status_code)
                    reply_ready.set()
                elif isinstance(data, sdk.AssistantUsageData):
                    reported_models.append(data.model)
                    usage.record(event)
                elif accepting_reply and isinstance(data, sdk.AssistantMessageData):
                    reply_text = data.content
                elif (accepting_reply and isinstance(data, sdk.SessionIdleData)
                      and data.mode != sdk.SessionMode.AUTOPILOT):
                    reply_ready.set()

            async with asyncio.timeout(self.timeout_seconds):
                stage = "startup"
                client = sdk.CopilotClient(
                    connection=sdk.RuntimeConnection.for_stdio(path=str(runtime)),
                    working_directory=str(work), env=env, mode="copilot-cli",
                    use_logged_in_user=True, log_level="none",
                    builtin_plugin_directories=[], enable_remote_sessions=False,
                    session_fs={"initial_working_directory": str(work),
                                "session_state_path": str(session_state),
                                "conventions": "windows" if os.name == "nt" else "posix"},
                )
                await client.start()
                status = await client.get_status()
                if status.version != RUNTIME_VERSION:
                    raise PhotographyError("REVIEW_RUNTIME_VERSION", "The running Copilot runtime does not match the approved version.")
                stage = "authentication"
                self._check_auth(await client.get_auth_status())
                stage = "models"
                models = [model.to_dict() for model in await client.list_models()]
                if request is None:
                    result = models
                else:
                    matches = [model for model in models if model.get("id") == request.model]
                    if len(matches) != 1:
                        raise PhotographyError("REVIEW_MODEL_UNSUPPORTED", "The explicitly selected model is not uniquely available.")
                    _model_limits(matches[0], request)
                    stage = "session_restrictions"
                    session = await client.create_session(
                        session_id=session_id, model=request.model,
                        streaming=True,
                        on_permission_request=lambda _request, _invocation: sdk.PermissionDecisionReject(),
                        available_tools=[], tools=[], custom_agents=[], mcp_servers={},
                        working_directory=str(work), additional_directories=[],
                        enable_config_discovery=False, enable_on_demand_instruction_discovery=False,
                        skip_custom_instructions=True, enable_skills=False, included_builtin_skills=[],
                        skill_directories=[], plugin_directories=[], instruction_directories=[],
                        enable_file_hooks=False, enable_host_git_operations=False,
                        enable_session_store=False, memory={"enabled": False},
                        infinite_sessions={"enabled": False}, skip_embedding_retrieval=True,
                        embedding_cache_storage="in-memory", mcp_oauth_token_storage="in-memory",
                        enable_session_telemetry=False, custom_agents_local_only=True,
                        coauthor_enabled=False, manage_schedule_enabled=False,
                        enable_file_change_tracking=False, enable_mcp_apps=False,
                        system_message={"mode": "customize", "sections": {
                            "environment_context": {"action": "remove"},
                            "custom_instructions": {"action": "remove"},
                        }},
                        create_session_fs_handler=create_files, on_event=on_event,
                    )
                    applied = await session.rpc.options.update(sdk.SessionUpdateOptionsParams(
                        installed_plugins=[], included_builtin_agents=[], available_tools=[],
                        ask_user_disabled=True, continue_on_auto_mode=False,
                    ))
                    if applied.success is not True:
                        raise PhotographyError("REVIEW_RESTRICTIONS_FAILED", "The required session restrictions were not applied.")
                    stage = "authentication"
                    self._check_auth(await client.get_auth_status())
                    stage = "send"
                    if provider_statuses:
                        raise _provider_error(stage, provider_statuses)
                    accepting_reply = True
                    # Handle typed error events directly: send_and_wait rethrows
                    # session.error as a plain Exception containing provider text.
                    await session.send(
                        request.prompt, attachments=[
                            {"type": "blob", "data": base64.b64encode(image.data).decode("ascii"),
                             "mimeType": "image/jpeg", "displayName": image.image_id}
                            for image in request.images
                        ], agent_mode="interactive",
                    )
                    await reply_ready.wait()
                    if provider_statuses:
                        raise _provider_error(stage, provider_statuses)
                    if any(model != request.model for model in reported_models):
                        raise PhotographyError("REVIEW_MODEL_CHANGED", "Copilot reported usage for a different model than the approved selection.")
                    if not isinstance(reply_text, str) or not reply_text.strip():
                        raise PhotographyError("REVIEW_EMPTY_RESPONSE", "Copilot returned no review text.")
                    result = ReviewReply(reply_text, {
                        "provider": PROVIDER_ID, "model": request.model,
                        "sdk_version": SDK_VERSION, "runtime_version": RUNTIME_VERSION,
                        "request_attempts": 1,
                    })
            completed = True
        except PhotographyError as error:
            primary = error
        except TimeoutError:
            primary = PhotographyError("REVIEW_TIMEOUT", "The approved review operation timed out; it was not automatically retried.")
        except (asyncio.CancelledError, KeyboardInterrupt):
            primary = PhotographyError("REVIEW_CANCELLED", "The review operation was cancelled; remote work may have already occurred.")
        except (OSError, RuntimeError, ValueError, ExceptionGroup, *sdk_errors):
            primary = _provider_error(stage, provider_statuses)
        finally:
            if session is not None and not completed:
                await cleanup_step("session_abort", session.abort)
            if session is not None:
                # Detach while filesystem callback routing is still live.
                # Stop the runtime before deleting application-owned session data.
                await cleanup_step("session_disconnect", session.disconnect)
            if client is not None:
                await cleanup_step("client_stop", client.stop)
                if "client_stop" in cleanup_errors:
                    await cleanup_step("client_force_stop", client.force_stop)
            if owned_created:
                try:
                    shutil.rmtree(owned)
                except FileNotFoundError:
                    pass
                except OSError:
                    cleanup_errors.append("owned_state_remove")
        if cleanup_errors:
            details = {"cleanup_errors": cleanup_errors}
            if primary is None:
                primary = PhotographyError("REVIEW_CLEANUP_FAILED", "Review cleanup failed; inspect owned provider state before retrying.", details=details)
            else:
                primary.details = {**(primary.details or {}), **details}
        if primary is not None:
            primary.details = {**(primary.details or {}), "usage": usage.metadata(),
                               "elapsed_seconds": perf_counter() - started}
            raise primary from None
        if isinstance(result, ReviewReply):
            return ReviewReply(result.text, {
                **result.metadata, **usage.metadata(), "elapsed_seconds": perf_counter() - started,
            })
        return result
