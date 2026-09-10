"""No live credentials, runtime process, downloads, or inference in these tests."""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from importlib.metadata import PackageNotFoundError, version
import inspect
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
from threading import Barrier
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from PIL import Image

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "photography" / "scripts"))
from photography_lib import copilot_review as adapter
from photography_lib.config import PhotographyError
from photography_lib.review_provider import ReviewImage, ReviewReply, ReviewRequest


def jpeg():
    stream = BytesIO()
    Image.new("RGB", (4, 3), "blue").save(stream, format="JPEG")
    return stream.getvalue()


def request(**changes):
    return replace(ReviewRequest(
        model="vision-model", prompt="Return strict JSON for image-1.",
        images=(ReviewImage("image-1", jpeg()),), batch_size=4, max_image_bytes=4096,
    ), **changes)


def model():
    return {"id": "vision-model", "name": "Vision model", "capabilities": {
        "supports": {"vision": True},
        "limits": {"vision": {"max_prompt_images": 4, "max_prompt_image_size": 4096,
                             "supported_media_types": ["image/jpeg"]}},
    }}


class FakePermissionReject:
    kind = "reject"


class FakeOptions:
    def __init__(self, *, installed_plugins, included_builtin_agents, available_tools,
                 ask_user_disabled, continue_on_auto_mode):
        self.installed_plugins = installed_plugins
        self.included_builtin_agents = included_builtin_agents
        self.available_tools = available_tools
        self.ask_user_disabled = ask_user_disabled
        self.continue_on_auto_mode = continue_on_auto_mode


class FakeErrorData:
    def __init__(self, status_code):
        self.status_code = status_code


class FakeMessageData:
    def __init__(self, content, *, chunk_count=None):
        self.content = content
        self.chunk_count = chunk_count


class FakeIdleData:
    mode = "interactive"


class FakeUsageData:
    model = "vision-model"
    input_tokens = 12
    output_tokens = 7
    cache_read_tokens = None
    cache_write_tokens = None
    reasoning_tokens = 3
    copilot_usage = None
    cost = 1.0

    def __init__(self, **values):
        self.__dict__.update(values)


class Harness:
    def __init__(self):
        self.calls, self.clients, self.sessions = [], [], []
        self.failures = {}
        self.catalogue = [model()]
        self.auth = SimpleNamespace(isAuthenticated=True, authType="user",
                                    host="https://github.com", login="approved-user")
        self.runtime_version = adapter.RUNTIME_VERSION
        self.raw_reply = '```untrusted output; the domain must reject this```'
        self.options_applied = True
        self.status_code = None
        self.hang_send = False
        self.usage_events = [SimpleNamespace(id=uuid4(), data=FakeUsageData())]
        self.other_events = []
        harness = self

        class Client:
            def __init__(self, **kwargs):
                harness.record("construct")
                self.kwargs = kwargs
                harness.clients.append(self)

            async def start(self):
                harness.record("start")

            async def get_status(self):
                harness.record("status")
                return SimpleNamespace(version=harness.runtime_version)

            async def get_auth_status(self):
                harness.record("auth")
                return harness.auth

            async def list_models(self):
                harness.record("models")
                return [SimpleNamespace(to_dict=lambda m=item: deepcopy(m)) for item in harness.catalogue]

            async def create_session(self, **kwargs):
                harness.calls.append("create")
                if not Path(kwargs["working_directory"]).is_dir():
                    raise RuntimeError("Session working directory must exist on the runtime host.")
                created = Session(kwargs)
                kwargs["create_session_fs_handler"](created)
                if "create" in harness.failures:
                    raise harness.failures["create"]
                return created

            async def delete_session(self, session_id):
                raise AssertionError("Default-home deletion must not handle custom session files")

            async def stop(self):
                harness.record("stop")

            async def force_stop(self):
                harness.record("force_stop")

        class Session:
            def __init__(self, kwargs):
                self.kwargs = kwargs
                self.session_id = kwargs["session_id"]
                self.rpc = SimpleNamespace(options=SimpleNamespace(update=self.update))
                harness.sessions.append(self)

            async def update(self, options):
                harness.record("update")
                self.options = options
                return SimpleNamespace(success=harness.options_applied)

            async def send(self, prompt, *, attachments, agent_mode):
                harness.calls.append("send")
                self.sent = {"prompt": prompt, "attachments": attachments, "agent_mode": agent_mode}
                for event in [*harness.usage_events, *harness.other_events]:
                    self.kwargs["on_event"](event)
                if harness.status_code:
                    self.kwargs["on_event"](SimpleNamespace(data=FakeErrorData(harness.status_code)))
                if harness.hang_send:
                    await asyncio.sleep(10)
                if "send" in harness.failures:
                    raise harness.failures["send"]
                if harness.raw_reply is not None:
                    self.kwargs["on_event"](SimpleNamespace(data=FakeMessageData(harness.raw_reply)))
                self.kwargs["on_event"](SimpleNamespace(data=FakeIdleData()))
                return "fake-message-id"

            async def abort(self):
                harness.record("abort")

            async def disconnect(self):
                harness.record("disconnect")

        self.sdk = SimpleNamespace(
            CopilotClient=Client,
            RuntimeConnection=SimpleNamespace(for_stdio=lambda *, path: SimpleNamespace(path=path)),
            PermissionDecisionReject=FakePermissionReject,
            SessionUpdateOptionsParams=FakeOptions,
            SessionErrorData=FakeErrorData, AssistantUsageData=FakeUsageData,
            AssistantMessageData=FakeMessageData, SessionIdleData=FakeIdleData,
            SessionMode=SimpleNamespace(AUTOPILOT="autopilot"),
            JsonRpcError=RuntimeError, ProcessExitedError=RuntimeError,
        )

    def record(self, name):
        self.calls.append(name)
        if name in self.failures:
            raise self.failures[name]


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.root = PROJECT / "tests" / (".review-provider-" + uuid4().hex)
        self.root.mkdir()
        self.addCleanup(lambda: shutil.rmtree(self.root) if self.root.exists() else None)
        self.harness = Harness()
        self.provider = adapter.CopilotReviewProvider(state_directory=self.root / "state")
        for target, value in (("_load_sdk", self.harness.sdk),
                              ("_installed_runtime", self.root / "runtime.exe"),
                              ("_session_files", object())):
            mocked = patch.object(adapter, target, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)

    def error(self, code, operation=None):
        with self.assertRaises(PhotographyError) as caught:
            (operation or (lambda: self.provider.review(request())))()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_interfaces_are_frozen_and_synchronous(self):
        for value, attr, change in ((request(), "model", "other"),
                                    (ReviewImage("a", b"1"), "data", b"2"),
                                    (ReviewReply("text", {}), "text", "other")):
            with self.subTest(type=type(value)), self.assertRaises(FrozenInstanceError):
                setattr(value, attr, change)
        self.assertFalse(inspect.iscoroutinefunction(self.provider.review))
        self.assertFalse(inspect.iscoroutinefunction(self.provider.models))

    def test_module_import_and_construction_do_not_touch_sdk_files_or_network(self):
        code = """
import builtins, pathlib, sys
from unittest.mock import patch
sys.path.insert(0, str(pathlib.Path.cwd() / 'photography' / 'scripts'))
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'copilot' or name.startswith('copilot.'):
        raise AssertionError('SDK imported')
    return original(name, *args, **kwargs)
with patch('builtins.__import__', guarded), patch('pathlib.Path.mkdir', side_effect=AssertionError('write')), patch('socket.socket', side_effect=AssertionError('network')):
    from photography_lib.copilot_review import CopilotReviewProvider
    CopilotReviewProvider()
assert 'copilot' not in sys.modules
"""
        result = subprocess.run([sys.executable, "-c", code], cwd=PROJECT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.harness.calls, [])

    def test_exact_jpeg_blobs_prompt_order_raw_reply_and_usage(self):
        frozen = request(images=(ReviewImage("image-2", jpeg()), ReviewImage("image-1", jpeg())))
        result = self.provider.review(frozen)
        self.assertEqual(result.text, self.harness.raw_reply)
        sent = self.harness.sessions[0].sent
        self.assertEqual(sent["prompt"], frozen.prompt)
        self.assertEqual(sent["agent_mode"], "interactive")
        self.assertEqual([entry["displayName"] for entry in sent["attachments"]], ["image-2", "image-1"])
        for entry, image in zip(sent["attachments"], frozen.images):
            self.assertEqual(set(entry), {"type", "data", "mimeType", "displayName"})
            self.assertEqual(entry["type"], "blob")
            self.assertEqual(entry["mimeType"], "image/jpeg")
            self.assertEqual(base64.b64decode(entry["data"], validate=True), image.data)
        self.assertEqual(result.metadata["request_attempts"], 1)
        self.assertEqual(result.metadata["usage_source"], "assistant.usage:per_call_sum")
        self.assertEqual(result.metadata["input_tokens"], 12)
        self.assertEqual(result.metadata["output_tokens"], 7)
        self.assertEqual(result.metadata["reasoning_tokens"], 3)
        self.assertNotIn("usage", result.metadata)
        self.assertNotIn("cache_read_tokens", result.metadata)
        self.assertNotIn("credits", result.metadata)
        self.assertGreaterEqual(result.metadata["elapsed_seconds"], 0)
        self.assertEqual(self.harness.calls[-2:], ["disconnect", "stop"])
        self.assertEqual(list((self.root / "state").iterdir()), [])

    def test_every_request_has_fresh_client_auth_models_and_session(self):
        self.provider.review(request())
        self.provider.review(request())
        self.assertEqual(len(self.harness.clients), 2)
        self.assertEqual(self.harness.calls.count("models"), 2)
        self.assertEqual(self.harness.calls.count("auth"), 4)
        self.assertNotEqual(self.harness.sessions[0].session_id, self.harness.sessions[1].session_id)
        workdirs = [client.kwargs["working_directory"] for client in self.harness.clients]
        self.assertEqual(len(set(workdirs)), 2)
        self.assertTrue(all(not Path(path).exists() for path in workdirs))

    def test_concurrent_calls_keep_clients_sessions_and_state_isolated(self):
        started = Barrier(5, timeout=10)
        original_record = self.harness.record
        def record(name):
            original_record(name)
            if name == "start":
                started.wait()
        with patch.object(self.harness, "record", side_effect=record), ThreadPoolExecutor(max_workers=5) as pool:
            replies = list(pool.map(self.provider.review, [request(prompt=f"Review batch {i}") for i in range(5)]))
        self.assertEqual(len(replies), 5)
        self.assertTrue(all(reply.text == self.harness.raw_reply for reply in replies))
        self.assertTrue(all(reply.metadata["request_attempts"] == 1 for reply in replies))
        self.assertEqual(len(self.harness.clients), 5)
        self.assertEqual(len({client.kwargs["working_directory"] for client in self.harness.clients}), 5)
        self.assertEqual(len({session.session_id for session in self.harness.sessions}), 5)
        self.assertEqual({session.sent["prompt"] for session in self.harness.sessions},
                         {f"Review batch {i}" for i in range(5)})
        self.assertEqual(self.harness.calls.count("auth"), 10)
        self.assertEqual(self.harness.calls.count("stop"), 5)
        self.assertEqual(list((self.root / "state").iterdir()), [])

    def test_models_authenticate_and_stop_without_creating_session(self):
        result = self.provider.models()
        self.assertEqual(result, self.harness.catalogue)
        self.assertEqual(self.harness.calls, ["construct", "start", "status", "auth", "models", "stop"])

    def test_all_restrictions_precede_send_and_permission_always_rejects(self):
        self.provider.review(request())
        options = self.harness.sessions[0].kwargs
        for key in ("available_tools", "tools", "custom_agents", "additional_directories",
                    "included_builtin_skills", "skill_directories", "plugin_directories",
                    "instruction_directories"):
            self.assertEqual(options[key], [], key)
        for key in ("enable_config_discovery", "enable_on_demand_instruction_discovery",
                    "enable_skills", "enable_file_hooks", "enable_host_git_operations",
                    "enable_session_store", "enable_session_telemetry", "coauthor_enabled",
                    "manage_schedule_enabled", "enable_file_change_tracking", "enable_mcp_apps"):
            self.assertIs(options[key], False, key)
        for key in ("skip_custom_instructions", "custom_agents_local_only", "skip_embedding_retrieval"):
            self.assertIs(options[key], True, key)
        for key in ("memory", "infinite_sessions"):
            self.assertEqual(options[key], {"enabled": False})
        self.assertEqual(options["embedding_cache_storage"], "in-memory")
        self.assertEqual(options["mcp_servers"], {})
        self.assertIs(options["streaming"], True)
        self.assertEqual(options["system_message"], {"mode": "customize", "sections": {
            "environment_context": {"action": "remove"}, "custom_instructions": {"action": "remove"}}})
        self.assertEqual(options["on_permission_request"](object(), object()).kind, "reject")
        self.assertEqual(self.harness.sessions[0].options.installed_plugins, [])
        self.assertEqual(self.harness.sessions[0].options.included_builtin_agents, [])
        self.assertLess(self.harness.calls.index("update"), self.harness.calls.index("send"))
        client = self.harness.clients[0].kwargs
        self.assertEqual(client["mode"], "copilot-cli")
        self.assertIs(client["use_logged_in_user"], True)
        self.assertNotIn("base_directory", client)
        self.assertNotIn("github_token", client)
        self.assertNotIn("telemetry", client)
        self.assertEqual(client["env"]["COPILOT_OTEL_ENABLED"], "false")
        work = Path(client["working_directory"])
        self.assertEqual(client["session_fs"]["session_state_path"], str(work.parent / "session-state"))
        self.assertEqual(client["session_fs"]["initial_working_directory"], str(work))
        self.assertEqual(client["session_fs"]["conventions"], "windows" if os.name == "nt" else "posix")
        self.assertEqual(options["working_directory"], str(work))

    def test_provider_diagnostic_filter_sanitizes_and_restores_logging(self):
        logger = logging.getLogger("copilot._jsonrpc")
        original = list(logger.filters)
        with self.assertLogs(logger, level="WARNING") as captured:
            with adapter._provider_diagnostics():
                logger.warning("SECRET_MARKER %s", "sensitive diagnostic",
                               exc_info=(RuntimeError, RuntimeError("SECRET_MARKER"), None))
            logger.warning("restored message")
        self.assertNotIn("SECRET_MARKER", captured.output[0])
        self.assertNotIn("sensitive diagnostic", captured.output[0])
        self.assertIn("structured review status", captured.output[0])
        self.assertIn("restored message", captured.output[1])
        self.assertEqual(logger.filters, original)

    def test_child_environment_excludes_override_credentials_without_mutating_parent(self):
        overrides = {
            "COPILOT_GITHUB_TOKEN": "SECRET_MARKER", "GH_TOKEN": "SECRET_MARKER",
            "GITHUB_TOKEN": "SECRET_MARKER", "GITHUB_COPILOT_API_TOKEN": "SECRET_MARKER",
            "COPILOT_SDK_AUTH_TOKEN": "SECRET_MARKER", "GH_ENTERPRISE_TOKEN": "SECRET_MARKER",
            "GITHUB_ENTERPRISE_TOKEN": "SECRET_MARKER", "OPENAI_API_KEY": "SECRET_MARKER",
            "ANTHROPIC_API_KEY": "SECRET_MARKER", "COPILOT_API_URL": "https://untrusted.invalid",
            "COPILOT_CLI_PATH": "untrusted.exe", "NODE_OPTIONS": "--require=untrusted",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "https://untrusted.invalid",
            "HOME": str(self.root / "normal-home"),
            "COPILOT_HOME": str(self.root / "existing-copilot-home"),
        }
        with patch.dict(os.environ, overrides):
            before = dict(os.environ)
            self.provider.review(request())
            self.assertEqual(dict(os.environ), before)
        child = self.harness.clients[0].kwargs["env"]
        self.assertEqual(child["HOME"], overrides["HOME"])
        self.assertEqual(child["COPILOT_HOME"], overrides["COPILOT_HOME"])
        self.assertEqual(child["COPILOT_SKIP_CLI_DOWNLOAD"], "1")
        self.assertNotIn("SECRET_MARKER", json.dumps(child))
        for key in set(overrides) - {"HOME", "COPILOT_HOME"}:
            self.assertNotIn(key, child)

    def test_provider_reported_model_change_cannot_become_a_success(self):
        with patch.object(FakeUsageData, "model", "not-approved"):
            self.error("REVIEW_MODEL_CHANGED")
        self.assertEqual(self.harness.calls[-3:], ["abort", "disconnect", "stop"])

    def test_usage_totals_sum_distinct_per_call_events_once_without_context_occupancy(self):
        first = SimpleNamespace(id=uuid4(), data=FakeUsageData(
            input_tokens=100, output_tokens=20, cache_read_tokens=30, cache_write_tokens=5,
            reasoning_tokens=7, copilot_usage=SimpleNamespace(total_nano_aiu=1_250_000_000.0)))
        second = SimpleNamespace(id=uuid4(), data=FakeUsageData(
            input_tokens=200, output_tokens=40, cache_read_tokens=60, cache_write_tokens=15,
            reasoning_tokens=11, copilot_usage=SimpleNamespace(total_nano_aiu=2_500_000_000.0)))
        self.harness.usage_events = [first, second, first]
        self.harness.other_events = [SimpleNamespace(id=uuid4(), data=SimpleNamespace(
            current_tokens=10000, token_limit=20000, input_tokens=9000, output_tokens=999,
            total_nano_aiu=100_000_000_000))]
        metadata = self.provider.review(request()).metadata
        self.assertEqual(metadata["usage_source"], "assistant.usage:per_call_sum")
        self.assertEqual(metadata["input_tokens"], 300)
        self.assertEqual(metadata["output_tokens"], 60)
        self.assertEqual(metadata["cache_read_tokens"], 90)
        self.assertEqual(metadata["cache_write_tokens"], 20)
        self.assertEqual(metadata["reasoning_tokens"], 18)
        self.assertEqual(metadata["credits"], 3_750_000_000)
        self.assertEqual(metadata["credits_unit"], "copilot_nano_aiu")
        self.assertNotIn("cost", metadata)
        self.assertNotIn("total_tokens", metadata)
        self.assertTrue(all(not isinstance(value, (list, dict)) for value in metadata.values()))

    def test_usage_missing_fields_do_not_fabricate_zero_or_partial_totals(self):
        self.harness.usage_events = [
            SimpleNamespace(id=uuid4(), data=FakeUsageData(
                input_tokens=10, output_tokens=5, cache_read_tokens=2, cache_write_tokens=1,
                copilot_usage=SimpleNamespace(total_nano_aiu=100.0))),
            SimpleNamespace(id=uuid4(), data=FakeUsageData(
                input_tokens=None, output_tokens=7, cache_read_tokens=None, cache_write_tokens=None)),
        ]
        metadata = self.provider.review(request()).metadata
        self.assertEqual(metadata["output_tokens"], 12)
        for key in ("input_tokens", "cache_read_tokens", "cache_write_tokens", "credits", "credits_unit"):
            self.assertNotIn(key, metadata)
        self.harness.usage_events = []
        metadata = self.provider.review(request()).metadata
        self.assertEqual(metadata["usage_source"], "assistant.usage:unavailable")
        for key in (*adapter._TOKEN_FIELDS, "credits", "credits_unit"):
            self.assertNotIn(key, metadata)

    def test_response_completion_diagnostics_are_content_free_and_separate_from_usage(self):
        self.harness.usage_events = [SimpleNamespace(id=uuid4(), data=FakeUsageData(
            finish_reason="length", max_output_tokens=4096))]
        self.harness.other_events = [SimpleNamespace(data=FakeMessageData("PRIVATE_TEXT", chunk_count=2))]
        reply = self.provider.review(request())
        metadata = reply.diagnostics
        self.assertEqual(metadata["response_finish_reason"], "length")
        self.assertEqual(metadata["response_finish_reason_calls"], 1)
        self.assertEqual(metadata["response_usage_calls"], 1)
        self.assertEqual(metadata["response_output_limit_calls"], 1)
        self.assertEqual(metadata["response_max_output_tokens"], 4096)
        self.assertEqual(metadata["response_message_events"], 2)
        self.assertEqual(metadata["response_max_chunk_count"], 2)
        self.assertTrue(metadata["response_idle_observed"])
        self.assertNotIn("PRIVATE_TEXT", json.dumps(metadata))
        self.assertNotIn("response_finish_reason", reply.metadata)

    def test_unknown_missing_and_mixed_completion_reasons_are_not_claimed_as_normal_stops(self):
        for reasons, expected in (
            ([], "unavailable"), ([None], "unavailable"), (["stop", None], "unavailable"),
            (["stop", "length"], "mixed"), (["PRIVATE_PROVIDER_TEXT"], "other"),
        ):
            self.harness.usage_events = [SimpleNamespace(id=uuid4(), data=FakeUsageData(
                finish_reason=reason, max_output_tokens=None)) for reason in reasons]
            with self.subTest(reasons=reasons):
                metadata = self.provider.review(request()).diagnostics
                self.assertEqual(metadata["response_finish_reason"], expected)
                self.assertNotIn("response_max_output_tokens", metadata)
                self.assertNotIn("PRIVATE_PROVIDER_TEXT", json.dumps(metadata))
        self.harness.usage_events = [
            SimpleNamespace(id=uuid4(), data=FakeUsageData(finish_reason="stop", max_output_tokens=4096)),
            SimpleNamespace(id=uuid4(), data=FakeUsageData(finish_reason="stop", max_output_tokens=8192)),
        ]
        self.assertNotIn("response_max_output_tokens", self.provider.review(request()).diagnostics)

    def test_usage_rejects_invalid_numeric_metrics_and_preserves_explicit_zero(self):
        for value in (-1, True, float("nan"), float("inf"), "100"):
            self.harness.usage_events = [SimpleNamespace(id=uuid4(), data=FakeUsageData(
                input_tokens=value, output_tokens=value, cache_read_tokens=value,
                cache_write_tokens=value, reasoning_tokens=value,
                copilot_usage=SimpleNamespace(total_nano_aiu=value)))]
            with self.subTest(value=value):
                metadata = self.provider.review(request()).metadata
                for key in (*adapter._TOKEN_FIELDS, "credits", "credits_unit"):
                    self.assertNotIn(key, metadata)
        self.harness.usage_events = [SimpleNamespace(id=uuid4(), data=FakeUsageData(
            input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0,
            reasoning_tokens=0, copilot_usage=SimpleNamespace(total_nano_aiu=0.0)))]
        metadata = self.provider.review(request()).metadata
        for key in (*adapter._TOKEN_FIELDS, "credits"):
            self.assertEqual(metadata[key], 0)
        self.assertEqual(metadata["credits_unit"], "copilot_nano_aiu")

    def test_elapsed_time_is_measured_through_cleanup_not_inferred_from_tokens(self):
        with patch.object(adapter, "perf_counter", side_effect=[100.0, 112.5]):
            metadata = self.provider.review(request()).metadata
        self.assertEqual(metadata["elapsed_seconds"], 12.5)
        self.assertEqual(self.harness.calls[-2:], ["disconnect", "stop"])

    def test_unknown_unacceptable_or_changed_auth_fails_before_image_send(self):
        cases = [{"authType": source} for source in (None, "env", "gh-cli", "token", "hmac", "api-key", "oauth", "unknown")]
        cases += [{"isAuthenticated": False}, {"host": None}, {"host": "github.com"},
                  {"host": "https://other.invalid"}, {"login": None}, {"login": ""}, {"login": "bad/user"}]
        for changes in cases:
            with self.subTest(changes=changes):
                current = self.harness.auth
                self.harness.auth = SimpleNamespace(**{**vars(current), **changes})
                self.error("REVIEW_AUTH_REQUIRED")
                self.harness.auth = current
        self.assertNotIn("send", self.harness.calls)
        self.provider.review(request())
        self.harness.auth.login = "different-account"
        self.error("REVIEW_AUTH_REQUIRED")
        self.assertEqual(self.harness.calls.count("send"), 1)

    def test_expected_login_and_runtime_version_are_checked(self):
        self.provider = adapter.CopilotReviewProvider(state_directory=self.root / "state", expected_login="someone-else")
        self.error("REVIEW_AUTH_REQUIRED")
        self.harness.runtime_version = "unexpected"
        self.error("REVIEW_RUNTIME_VERSION")
        self.assertNotIn("create", self.harness.calls)

    def test_unknown_model_vision_limits_or_policy_never_create_session(self):
        candidates = []
        for key in ("max_prompt_images", "max_prompt_image_size", "supported_media_types"):
            candidate = model()
            del candidate["capabilities"]["limits"]["vision"][key]
            candidates.append(candidate)
        for key, value in (("max_prompt_images", True), ("max_prompt_images", 0),
                           ("max_prompt_images", 4.0), ("max_prompt_image_size", "4096"),
                           ("supported_media_types", ["image/png"])):
            candidate = model()
            candidate["capabilities"]["limits"]["vision"][key] = value
            candidates.append(candidate)
        for candidate in (model(), model(), model()):
            candidates.append(candidate)
        candidates[-3]["capabilities"]["supports"]["vision"] = False
        candidates[-2]["id"] = "not-selected"
        candidates[-1]["policy"] = {"state": "disabled"}
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                self.harness.catalogue = [candidate]
                self.error("REVIEW_MODEL_UNSUPPORTED")
        self.assertNotIn("create", self.harness.calls)

    def test_whole_run_batch_size_and_maximum_bytes_not_just_final_batch_are_checked(self):
        candidate = model()
        candidate["capabilities"]["limits"]["vision"]["max_prompt_images"] = 2
        self.harness.catalogue = [candidate]
        self.error("REVIEW_MODEL_LIMIT")
        candidate["capabilities"]["limits"]["vision"]["max_prompt_images"] = 4
        candidate["capabilities"]["limits"]["vision"]["max_prompt_image_size"] = 2048
        self.assertLess(len(request().images[0].data), 2048)
        self.error("REVIEW_MODEL_LIMIT")
        self.assertNotIn("create", self.harness.calls)

    def test_model_discovery_is_not_cached_between_batches(self):
        self.provider.review(request())
        self.harness.catalogue[0]["capabilities"]["limits"]["vision"]["max_prompt_images"] = 2
        self.error("REVIEW_MODEL_LIMIT")
        self.assertEqual(self.harness.calls.count("send"), 1)
        self.assertEqual(self.harness.calls.count("models"), 2)

    def test_invalid_requests_fail_before_sdk_load(self):
        invalid = [
            request(model="auto"), request(batch_size=True), request(batch_size=0),
            request(max_image_bytes=10), request(images=()), request(prompt=" "),
            request(images=(ReviewImage("../private.jpg", jpeg()),)),
            request(images=(ReviewImage("image-1", jpeg()), ReviewImage("image-1", jpeg()))),
        ]
        with patch.object(adapter, "_load_sdk", side_effect=AssertionError("SDK must remain untouched")):
            for frozen in invalid:
                with self.subTest(request=frozen.model):
                    self.error("REVIEW_REQUEST_INVALID", lambda: self.provider.review(frozen))
            for data in (b"not jpeg", jpeg()[:100]):
                self.error("REVIEW_IMAGE_INVALID", lambda: self.provider.review(request(images=(ReviewImage("image-1", data),))))
            png = BytesIO()
            Image.new("RGB", (1, 1)).save(png, "PNG")
            self.error("REVIEW_IMAGE_INVALID", lambda: self.provider.review(request(images=(ReviewImage("image-1", png.getvalue()),))))

    def test_false_options_ack_aborts_and_disconnects_without_send(self):
        self.harness.options_applied = False
        self.error("REVIEW_RESTRICTIONS_FAILED")
        self.assertNotIn("send", self.harness.calls)
        self.assertEqual(self.harness.calls[-3:], ["abort", "disconnect", "stop"])

    def test_create_failure_after_registration_still_disconnects_and_cleans_owned_session(self):
        self.harness.failures["create"] = RuntimeError("SECRET_MARKER")
        self.error("REVIEW_RESTRICTIONS_FAILED")
        self.assertEqual(self.harness.calls[-3:], ["abort", "disconnect", "stop"])

    def test_timeout_aborts_then_disconnects_before_stopping_without_retry(self):
        self.harness.hang_send = True
        self.provider.timeout_seconds = .025
        self.error("REVIEW_TIMEOUT")
        self.assertEqual(self.harness.calls[-4:], ["send", "abort", "disconnect", "stop"])
        self.assertEqual(self.harness.calls.count("send"), 1)

    def test_cancellation_aborts_then_disconnects_before_stopping(self):
        self.harness.failures["send"] = asyncio.CancelledError()
        self.error("REVIEW_CANCELLED")
        self.assertEqual(self.harness.calls[-3:], ["abort", "disconnect", "stop"])

    def test_empty_response_fails_but_non_json_is_left_for_domain_parser(self):
        for text in (None, "", "  "):
            self.harness.raw_reply = text
            self.error("REVIEW_EMPTY_RESPONSE")
        self.harness.raw_reply = "plain prose"
        self.assertEqual(self.provider.review(request()).text, "plain prose")

    def test_rate_limit_and_auth_failures_are_classified_from_typed_events_not_raw_text(self):
        self.harness.failures["send"] = RuntimeError("SECRET_MARKER")
        for status, code in ((429, "REVIEW_RATE_LIMITED"), (401, "REVIEW_AUTH_REQUIRED"), (500, "REVIEW_PROVIDER_FAILED")):
            self.harness.status_code = status
            error = self.error(code)
            self.assertNotIn("SECRET_MARKER", json.dumps(error.to_dict()))
            self.assertNotIn("SECRET_MARKER", "".join(traceback.format_exception(error)))

    def test_cleanup_failures_remain_visible_without_masking_primary_or_leaking_secrets(self):
        self.harness.failures = {key: RuntimeError("SECRET_MARKER") for key in ("send", "abort", "disconnect", "stop", "force_stop")}
        error = self.error("REVIEW_PROVIDER_FAILED")
        self.assertEqual(error.details["cleanup_errors"], ["session_abort", "session_disconnect", "client_stop", "client_force_stop"])
        self.assertNotIn("SECRET_MARKER", json.dumps(error.to_dict()))
        self.assertEqual(self.harness.calls[-4:], ["abort", "disconnect", "stop", "force_stop"])

    def test_cleanup_failure_after_success_is_not_reported_as_success(self):
        self.harness.failures["disconnect"] = RuntimeError("SECRET_MARKER")
        self.error("REVIEW_CLEANUP_FAILED")
        self.assertEqual(self.harness.calls[-2:], ["disconnect", "stop"])

    def test_owned_state_cleanup_failure_is_visible(self):
        original = shutil.rmtree
        with patch.object(adapter.shutil, "rmtree", side_effect=PermissionError("SECRET_MARKER")):
            error = self.error("REVIEW_CLEANUP_FAILED")
        self.assertEqual(error.details["cleanup_errors"], ["owned_state_remove"])
        self.assertNotIn("SECRET_MARKER", json.dumps(error.to_dict()))
        original(self.root / "state")

    def test_start_failure_still_stops_owned_client(self):
        self.harness.failures["start"] = RuntimeError("SECRET_MARKER")
        self.error("REVIEW_PROVIDER_FAILED")
        self.assertEqual(self.harness.calls[-1], "stop")
        self.assertNotIn("disconnect", self.harness.calls)


class RuntimePreflightTests(unittest.TestCase):
    def setUp(self):
        self.root = PROJECT / "tests" / (".review-runtime-" + uuid4().hex)
        self.root.mkdir()
        self.addCleanup(lambda: shutil.rmtree(self.root))

    def test_missing_runtime_fails_before_client_construction_and_never_downloads(self):
        harness = Harness()
        harness.sdk.get_cache_dir = lambda _version: self.root
        harness.sdk.get_runtime_platform = lambda: "platform"
        with patch.object(adapter, "_load_sdk", return_value=harness.sdk):
            with self.assertRaises(PhotographyError) as caught:
                adapter.CopilotReviewProvider(state_directory=self.root / "state").review(request())
        self.assertEqual(caught.exception.code, "REVIEW_RUNTIME_MISSING")
        self.assertEqual(harness.calls, [])
        self.assertFalse((self.root / "state").exists())

    def test_complete_preprovisioned_wrapper_needs_no_legacy_alias(self):
        pair = self.root / "prebuilds" / "platform"
        pair.mkdir(parents=True)
        wrapper = pair / ("copilot-runtime.exe" if sys.platform == "win32" else "copilot-runtime")
        for path in (wrapper, pair / "runtime.node", pair / ".hostless-runtime-assets-v2"):
            path.write_bytes(b"installed-test-fixture")
        sdk = SimpleNamespace(get_cache_dir=lambda _version: self.root,
                              get_runtime_platform=lambda: "platform")
        self.assertEqual(adapter._installed_runtime(sdk), wrapper.resolve())
        (pair / "runtime.node").write_bytes(b"")
        with self.assertRaises(PhotographyError):
            adapter._installed_runtime(sdk)

    def test_missing_or_wrong_sdk_version_never_imports_or_provisions_runtime(self):
        for value, code in ((PackageNotFoundError(), "REVIEW_SDK_MISSING"), ("other", "REVIEW_SDK_VERSION")):
            with patch.object(adapter, "version", side_effect=value if isinstance(value, Exception) else None,
                              return_value=value):
                with self.assertRaises(PhotographyError) as caught:
                    adapter._load_sdk()
            self.assertEqual(caught.exception.code, code)


try:
    PINNED_SDK_INSTALLED = version("github-copilot-sdk") == adapter.SDK_VERSION
except PackageNotFoundError:
    PINNED_SDK_INSTALLED = False


@unittest.skipUnless(PINNED_SDK_INSTALLED, "Optional pinned SDK not installed.")
class InstalledSdkContractTests(unittest.TestCase):
    def test_real_session_error_events_are_sanitized_and_persist_failed_state(self):
        from copilot.session_events import SessionErrorData
        from photography_lib import review
        from tests.test_review_execution import ReviewFixture
        for status, expected in ((429, "REVIEW_RATE_LIMITED"), (401, "REVIEW_AUTH_REQUIRED"),
                                 (None, "REVIEW_PROVIDER_FAILED")):
            with self.subTest(status=status):
                case = ProviderTests("test_exact_jpeg_blobs_prompt_order_raw_reply_and_usage")
                fixture = ReviewFixture()
                case.setUp()
                fixture.setUp()
                try:
                    case.harness.sdk.SessionErrorData = SessionErrorData
                    case.harness.other_events = [SimpleNamespace(data=SessionErrorData.from_dict({
                        "errorType": "provider_error", "message": "SECRET_PROVIDER_MESSAGE", "statusCode": status,
                    }))]
                    plan = review.create_plan(fixture.ids[:1], store=fixture.store, model="vision-model")
                    result = review.execute_plan(plan["run_id"], store=fixture.store, confirm=plan["digest"],
                                                 provider_factory=lambda: case.provider)
                    self.assertEqual(result["status"], "failed")
                    self.assertEqual(result["error"]["code"], expected)
                    self.assertEqual(result["batches"][0]["status"], "failed")
                    self.assertNotIn("SECRET_PROVIDER_MESSAGE", json.dumps(result))
                    self.assertFalse(result["results"])
                    self.assertEqual(case.harness.calls[-3:], ["abort", "disconnect", "stop"])
                    self.assertEqual(result["batches"][0]["metadata"]["input_tokens"], 12)
                finally:
                    fixture.doCleanups()
                    case.doCleanups()

    def test_actual_usage_event_types_use_per_call_copilot_nano_aiu_not_multiplier(self):
        from copilot.session_events import AssistantUsageData
        data = AssistantUsageData.from_dict({
            "model": "vision-model", "inputTokens": 123, "outputTokens": 45,
            "cacheReadTokens": 0, "cacheWriteTokens": 4, "reasoningTokens": 5,
            "cost": 3.0, "copilotUsage": {"totalNanoAiu": 1250000000.0},
            "finishReason": "stop", "maxOutputTokens": 4096,
        })
        totals = adapter._UsageTotals()
        totals.record(SimpleNamespace(id=uuid4(), data=data))
        self.assertEqual(totals.metadata(), {
            "usage_source": "assistant.usage:per_call_sum", "input_tokens": 123,
            "output_tokens": 45, "cache_read_tokens": 0, "cache_write_tokens": 4,
            "reasoning_tokens": 5, "credits": 1250000000.0, "credits_unit": "copilot_nano_aiu",
        })
        self.assertEqual(totals.response_diagnostics(), {
            "response_finish_reason": "stop", "response_finish_reason_calls": 1,
            "response_usage_calls": 1, "response_output_limit_calls": 0, "response_max_output_tokens": 4096,
        })

    def test_installed_sdk_imports_and_type_construction_do_not_download(self):
        from copilot import _cli_download
        with patch.object(_cli_download, "ensure_runtime_wrapper", side_effect=AssertionError("download")):
            sdk = adapter._load_sdk()
            connection = sdk.RuntimeConnection.for_stdio(path="explicit-runtime.exe")
        self.assertEqual(connection.path, "explicit-runtime.exe")
        self.assertEqual(sdk.PermissionDecisionReject().to_dict(), {"kind": "reject"})
        options = sdk.SessionUpdateOptionsParams(
            installed_plugins=[], included_builtin_agents=[], available_tools=[],
            ask_user_disabled=True, continue_on_auto_mode=False,
        ).to_dict()
        self.assertEqual(options["installedPlugins"], [])
        self.assertEqual(options["availableTools"], [])

    def test_every_mocked_call_binds_to_actual_pinned_keyword_api(self):
        from copilot import CopilotClient, RuntimeConnection
        from copilot.generated.rpc import OptionsApi
        from copilot.session import CopilotSession
        case = ProviderTests("test_exact_jpeg_blobs_prompt_order_raw_reply_and_usage")
        case.setUp()
        try:
            case.provider.review(request())
            client, session = case.harness.clients[0], case.harness.sessions[0]
            inspect.signature(CopilotClient).bind(**client.kwargs)
            inspect.signature(CopilotClient.create_session).bind(object(), **session.kwargs)
            inspect.signature(CopilotSession.send).bind(object(), **session.sent)
            inspect.signature(CopilotSession.disconnect).bind(object())
            inspect.signature(OptionsApi.update).bind(object(), session.options)
            inspect.signature(RuntimeConnection.for_stdio).bind(path="explicit-runtime.exe")
        finally:
            case.doCleanups()

    def test_real_sdk_serializes_restrictions_without_starting_a_runtime(self):
        from copilot import CopilotClient, RuntimeConnection
        from copilot.rpc import SessionUpdateOptionsParams
        from copilot import _cli_download
        case = ProviderTests("test_exact_jpeg_blobs_prompt_order_raw_reply_and_usage")
        case.setUp()
        calls = []

        class Wire:
            async def request(self, method, params, **kwargs):
                calls.append((method, params))
                if method == "session.create":
                    return {"sessionId": params["sessionId"]}
                if method == "session.send":
                    return {"messageId": "owned-message"}
                if method in ("session.options.update", "session.detach"):
                    return {"success": True}
                raise AssertionError("Unexpected RPC: " + method)

            async def stop(self):
                calls.append(("stop", {}))

        try:
            case.provider.review(request())
            fake_client, fake_session = case.harness.clients[0], case.harness.sessions[0]
            options = {**fake_client.kwargs, "connection": RuntimeConnection.for_stdio(path="explicit-runtime.exe")}

            async def wire_exercise():
                with patch.object(_cli_download, "ensure_runtime_wrapper", side_effect=AssertionError("download")), \
                        patch.object(CopilotClient, "start", side_effect=AssertionError("runtime startup")):
                    client = CopilotClient(**options)
                    client._client = Wire()
                    session = await client.create_session(**fake_session.kwargs)
                    ack = await session.rpc.options.update(SessionUpdateOptionsParams(**vars(fake_session.options)))
                    self.assertTrue(ack.success)
                    await session.send(fake_session.sent["prompt"], attachments=fake_session.sent["attachments"])
                    await session.disconnect()
                    await client.stop()

            asyncio.run(wire_exercise())
            payload = next(params for method, params in calls if method == "session.create")
            for key in ("enableConfigDiscovery", "enableOnDemandInstructionDiscovery", "enableSkills",
                        "enableFileHooks", "enableHostGitOperations", "enableSessionStore",
                        "enableSessionTelemetry"):
                self.assertIs(payload[key], False, key)
            self.assertEqual(payload["availableTools"], [])
            self.assertEqual(payload["memory"], {"enabled": False})
            self.assertEqual(payload["infiniteSessions"], {"enabled": False})
            self.assertEqual(payload["systemMessage"], fake_session.kwargs["system_message"])
            patches = [params for method, params in calls if method == "session.options.update"]
            self.assertEqual(patches[-1]["installedPlugins"], [])
            sent = next(params for method, params in calls if method == "session.send")
            self.assertEqual(sent["attachments"], fake_session.sent["attachments"])
            self.assertEqual([method for method, _ in calls][-2:], ["session.detach", "stop"])
        finally:
            case.doCleanups()

    def test_owned_session_files_use_real_abc_and_reject_traversal(self):
        from copilot.session_fs_provider import SessionFsProvider, create_session_fs_adapter
        from copilot.rpc import SessionFSReadFileRequest, SessionFSWriteFileRequest
        root = PROJECT / "tests" / (".review-files-" + uuid4().hex)
        root.mkdir()
        self.addCleanup(lambda: shutil.rmtree(root))
        files = adapter._session_files(root.resolve())
        self.assertIsInstance(files, SessionFsProvider)

        async def exercise():
            state = root / "session-state"
            events = str(state / "events.jsonl")
            await files.mkdir(str(state), True)
            await files.write_file(events, "owned")
            await files.append_file(events, "-only")
            self.assertEqual(await files.read_file(events), "owned-only")
            self.assertTrue((await files.stat(events)).is_file)
            self.assertEqual(await files.readdir(str(state)), ["events.jsonl"])
            self.assertEqual(len(await files.readdir_with_types(str(state))), 1)
            await files.rename(events, str(state / "renamed"))
            self.assertTrue(await files.exists(str(state / "renamed")))
            handler = create_session_fs_adapter(files)
            result = await handler.write_file(SessionFSWriteFileRequest(
                session_id="owned", path=str(state / "typed"), content="through RPC"))
            self.assertIsNone(result)
            result = await handler.read_file(SessionFSReadFileRequest(session_id="owned", path=str(state / "typed")))
            self.assertEqual(result.content, "through RPC")
            for path in ("../outside", "/session-state/../../outside", "C:\\private", "/x\0x",
                         str(state / ".." / ".." / "outside"), str(state / "events:stream")):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    await files.write_file(path, "must not escape")
            await files.rm(str(state), True, False)
            self.assertFalse(await files.exists(str(state)))

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
