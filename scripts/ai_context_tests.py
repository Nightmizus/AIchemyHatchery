"""Offline regressions: .venv/bin/python scripts/ai_context_tests.py"""
import os
import json
from contextlib import contextmanager
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deepseek_harness_adapter as dsh
import server


def event(kind, **data):
    return {"type": kind, "data": data}


class ContextTests(unittest.TestCase):
    def test_last_call_is_not_cumulative_billing(self):
        events = [
            event("request/context", contextWindow=1000000),
            event("assistant/message", usage={"inputTokens": 400, "cacheReadTokens": 600, "outputTokens": 50}),
            event("assistant/message", usage={"inputTokens": 700, "cacheReadTokens": 800, "outputTokens": 100}),
        ]
        self.assertEqual(dsh._context_usage(events), {"usedTokens": 1600, "contextWindow": 1000000})
        self.assertEqual(dsh._token_usage(events), {"inputTokens": 2500, "outputTokens": 150})

    def test_stream_and_message_report_same_usage_once(self):
        usage = {"inputTokens": 120, "cacheReadTokens": 80, "outputTokens": 30}
        events = [event("request/context", contextWindow=64000),
                  event("assistant/chunk", chunk={"type": "usage", "usage": usage}),
                  event("assistant/message", usage=usage)]
        self.assertEqual(dsh._context_usage(events), {"usedTokens": 230, "contextWindow": 64000})

    def test_missing_usage_stays_unknown(self):
        self.assertIsNone(dsh._context_usage([event("request/context", contextWindow=1000000),
                                             event("assistant/message", usage={})]))

    def test_legacy_client_cannot_override_model_or_reasoning(self):
        handler = object.__new__(server.AIchemyHatcheryHandler)
        # 旧客户端仍会带 sessionId；单会话模型下它被忽略，返回值里没有会话。
        handler.read_json = lambda: {"prompt": "hello", "context": {}, "model": "kimi-k2.6",
                                     "reasoningEffort": "off", "sessionId": "a" * 32}
        prepared = handler.prepare_ai_run()
        self.assertEqual(prepared, ("hello", {}, True, [], [], [], [], "full"))

    def test_deepseek_key_update_preserves_other_settings_and_redacts_response(self):
        class Connection:
            @contextmanager
            def cursor(self):
                yield None

        @contextmanager
        def database(autocommit=False):
            yield Connection()

        handler = object.__new__(server.AIchemyHatcheryHandler)
        handler.require_console_user = lambda admin: {"id": "fake-admin"}
        handler.read_json = lambda: {"deepseekApiKey": "sk-local-test-1234"}
        responses = []
        handler.send_json = responses.append
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, 'ROOT', Path(tmp)), \
                patch.dict(os.environ, {'MIAODA_LLM': 'deepseek'}, clear=True), \
                patch.object(server, 'neon_db', database), patch.object(server, 'audit_event') as audit:
            env_file = Path(tmp) / '.env'
            env_file.write_text('MIAODA_LLM=deepseek\nKIMI_API_KEY=other-existing-key\n')
            handler.handle_admin_ai_config_update()
            self.assertIn('KIMI_API_KEY=other-existing-key', env_file.read_text())
            self.assertIn('DEEPSEEK_API_KEY=sk-local-test-1234', env_file.read_text())
            self.assertEqual(responses[0]['deepseekKeyMask'], '···1234')
            self.assertNotIn('sk-local-test-1234', json.dumps(responses))
            self.assertNotIn('sk-local-test-1234', str(audit.call_args))

    def test_non_admin_cannot_update_key(self):
        handler = object.__new__(server.AIchemyHatcheryHandler)
        handler.require_console_user = lambda admin: None
        with patch.object(server, 'persist_env_value') as persist:
            handler.handle_admin_ai_config_update()
        persist.assert_not_called()

    def test_native_route_launches_with_gateway_and_server_model(self):
        calls = []
        notifications = [
            {"method": "session.event", "params": {"event": event("agent/inbox/spliced", inserted=[{"id": "message-1"}])}},
            {"method": "session.event", "params": {"event": event("request/context", contextWindow=1000000)}},
            {"method": "session.event", "params": {"event": event("assistant/message", usage={"inputTokens": 60, "outputTokens": 10}, message={"content": [{"type": "text", "text": '{"summary":"ok"}'}]})}},
            {"method": "session.event", "params": {"event": event("turn/end", reason={"kind": "completed"})}},
            {"method": "session.status", "params": {"status": "idle"}},
        ]

        class Runtime:
            def __init__(self, args, cwd, env, timeout, secrets):
                calls.append((args, env))

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def request(self, method, payload, timeout):
                calls.append((method, payload))
                if method == "session/prompt":
                    self.session_id = payload["sessionId"]
                    return {"messageId": "message-1"}

            def next_notification(self, deadline):
                notification = notifications.pop(0)
                notification["params"]["sessionId"] = self.session_id
                return notification

        env = {"MIAODA_LLM": "deepseek", "DEEPSEEK_API_KEY": "test-key-only",
               "DEEPSEEK_MODEL": "DeepSeek-V4.1-Flash", "DEEPSEEK_REASONING_EFFORT": "high",
               "HATCHERY_DEEPSEEK_BASE_URL": "https://gateway.example/v1/"}
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, env, clear=True), \
                patch.object(dsh, "_JsonRpcRuntime", Runtime), \
                patch.object(dsh, "resolve_harness_binary", return_value=Path('/fake/dsh')):
            root = Path(tmp)
            (root / 'deepseek_harness').mkdir()
            for filename in ['site-agent.patch.yml', 'site-tools.mjs']:
                (root / 'deepseek_harness' / filename).touch()
            progress = []
            result = dsh.run_deepseek_harness(root=root, prompt='hello', context={}, attachments=[],
                                               workspace=root, preview_port=4182, progress=progress.append)
        self.assertEqual(calls[0][1]["DEEPSEEK_BASE_URL"], "https://gateway.example/v1")
        self.assertEqual(calls[0][1]["DEEPSEEK_SEARCH_BASE_URL"], "https://gateway.example/v1")
        self.assertEqual(calls[0][1]["HATCHERY_DEEPSEEK_SEARCH_MODEL"], "DeepSeek-V4.1-Flash")
        self.assertEqual(calls[0][1]["HATCHERY_DEEPSEEK_SEARCH_API_KEY"], "test-key-only")
        self.assertEqual(calls[1][1]["provider"], "deepseek-official")
        self.assertEqual(calls[1][1]["model"], "DeepSeek-V4.1-Flash")
        self.assertEqual(calls[1][1]["reasoningEffort"], "high")
        self.assertEqual(result["contextUsage"], {"usedTokens": 70, "contextWindow": 1000000})
        self.assertEqual(progress[-1]["contextUsage"], result["contextUsage"])


if __name__ == '__main__':
    unittest.main()
