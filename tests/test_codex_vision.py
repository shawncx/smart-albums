from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "photography" / "scripts"))
from photography_lib.codex_vision import CodexConfig, CodexCLIProvider
from photography_lib.vision import ProviderError


class CodexAdapterTests(unittest.TestCase):
    def test_ready_only_checks_login_and_is_cached(self):
        provider = CodexCLIProvider()
        with patch("photography_lib.codex_vision.shutil.which", return_value="codex.exe"), \
             patch("photography_lib.codex_vision.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "Logged in using ChatGPT")) as run:
            provider.check_ready()
            provider.check_ready()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0], ["codex.exe", "login", "status"])

    def test_missing_cli_or_login_is_clear_and_does_not_invoke_model(self):
        with patch("photography_lib.codex_vision.shutil.which", return_value=None):
            with self.assertRaises(ProviderError) as error:
                CodexCLIProvider().check_ready()
            self.assertEqual(error.exception.code, "CODEX_NOT_FOUND")
        with patch("photography_lib.codex_vision.shutil.which", return_value="codex.exe"), \
             patch("photography_lib.codex_vision.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "private error")):
            with self.assertRaises(ProviderError) as error:
                CodexCLIProvider().check_ready()
            self.assertEqual(error.exception.code, "CODEX_LOGIN_UNAVAILABLE")
            self.assertNotIn("private error", str(error.exception))

    def test_command_inputs_isolation_structured_result_and_usage(self):
        provider = CodexCLIProvider(CodexConfig(model="test-model"))
        provider._ready, provider.executable = True, "codex.exe"
        locations = []
        def run(command, **options):
            self.assertEqual(command[:2], ["codex.exe", "exec"])
            self.assertIn("--ephemeral", command)
            self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
            image = Path(command[command.index("--image") + 1])
            locations.append(image.parent)
            self.assertEqual(image.read_bytes(), b"preview-bytes")
            self.assertEqual(str(image.parent), str(options["cwd"]))
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text('{"observation":"fixture"}', encoding="utf-8")
            events = json.dumps({"type": "turn.completed", "usage": {
                "input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 20}})
            return subprocess.CompletedProcess(command, 0, events, "")
        with patch("photography_lib.codex_vision.subprocess.run", side_effect=run):
            output = provider.analyze(b"preview-bytes")
        self.assertEqual(output.data, {"observation": "fixture"})
        self.assertEqual(output.model_source, "requested_cli_model")
        self.assertEqual(output.usage_scope, "codex_turn")
        self.assertEqual(output.usage["total_tokens"], 120)
        self.assertFalse(locations[0].exists())

    def test_usage_missing_is_unknown_and_cached_input_is_not_added_twice(self):
        self.assertIsNone(CodexCLIProvider.parse_usage("not json\n{}\n[]"))
        events = '\n'.join(json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 20}}) for _ in range(2))
        self.assertEqual(CodexCLIProvider.parse_usage(events), {
            "input_tokens": 200, "cached_input_tokens": 160, "output_tokens": 40, "total_tokens": 240})

    def test_failure_or_timeout_is_not_automatically_retried(self):
        provider = CodexCLIProvider()
        provider._ready, provider.executable = True, "codex.exe"
        for result in (subprocess.TimeoutExpired("codex", 1), subprocess.CompletedProcess([], 1, "", "secret")):
            with self.subTest(result=type(result).__name__):
                kwargs = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
                with patch("photography_lib.codex_vision.subprocess.run", **kwargs) as run:
                    with self.assertRaises(ProviderError) as error:
                        provider.analyze(b"preview")
                    self.assertEqual(run.call_count, 1)
                    self.assertNotIn("secret", str(error.exception))


if __name__ == "__main__":
    unittest.main()
