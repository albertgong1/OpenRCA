"""Prompt-cache breakpoints added by the API router; no network calls."""

import copy
import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


def _import_router(model):
    with tempfile.TemporaryDirectory() as config_dir:
        config_path = Path(config_dir) / "config.json"
        config_path.write_text(json.dumps({"gemini3pro": {"api_key": "test-only", "model": model}}))
        with mock.patch.dict(os.environ, {"API_CONFIG_PATH": str(config_path)}):
            import rca.api_router as router
            return importlib.reload(router)


CACHE = {"type": "ephemeral"}


def _breakpoints(messages):
    return [
        i for i, m in enumerate(messages)
        if isinstance(m["content"], list) and any("cache_control" in b for b in m["content"])
    ]


class CacheBreakpointTest(unittest.TestCase):
    def setUp(self):
        self.router = _import_router("anthropic-claude-4.6-sonnet")
        self.history = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "objective"},
            {"role": "assistant", "content": "step 1"},
            {"role": "user", "content": "observation 1"},
        ]
        self.note = [{"role": "user", "content": "continue"}]

    def test_marks_last_two_messages_only(self):
        marked = self.router._with_cache_breakpoints(self.history + self.note)
        self.assertEqual(_breakpoints(marked), [3, 4])
        self.assertEqual(marked[3]["content"], [{"type": "text", "text": "observation 1", "cache_control": CACHE}])
        self.assertEqual(marked[0], self.history[0])

    def test_does_not_mutate_input(self):
        messages = self.history + self.note
        before = copy.deepcopy(messages)
        self.router._with_cache_breakpoints(messages)
        self.assertEqual(messages, before)

    def test_breakpoints_never_accumulate_across_calls(self):
        # The agent extends the same history between calls; markers must not pile up
        # past Anthropic's 4-breakpoint limit.
        history = list(self.history)
        for step in range(10):
            marked = self.router._with_cache_breakpoints(history + self.note)
            self.assertLessEqual(len(_breakpoints(marked)), 2)
            history.extend([{"role": "assistant", "content": f"step {step}"},
                            {"role": "user", "content": f"obs {step}"}])

    def test_history_prefix_matches_next_call(self):
        # The cached prefix of one call (history ending at its last real message)
        # must be the start of the next call's messages.
        first = self.router._with_cache_breakpoints(self.history + self.note)
        grown = self.history + [{"role": "assistant", "content": "step 2"},
                                {"role": "user", "content": "observation 2"}]
        self.assertEqual(first[3]["content"][0]["text"], grown[3]["content"])

    def test_list_content_and_empty_content(self):
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]},
            {"role": "assistant", "content": ""},
        ]
        marked = self.router._with_cache_breakpoints(messages)
        self.assertEqual(marked[0]["content"][1]["cache_control"], CACHE)
        self.assertNotIn("cache_control", marked[0]["content"][0])
        self.assertNotIn("cache_control", messages[0]["content"][1])
        self.assertEqual(marked[1], {"role": "assistant", "content": ""})

    def test_request_marked_only_for_anthropic_models(self):
        for model, expected in [("anthropic-claude-4.6-sonnet", [1, 2]), ("openai-gpt-5.5", [])]:
            router = _import_router(model)
            client = mock.MagicMock()
            client.chat.completions.create.return_value.choices = [mock.MagicMock()]
            with mock.patch("openai.OpenAI", return_value=client):
                router.get_chat_completion(self.history[:2] + self.note)
            sent = client.chat.completions.create.call_args.kwargs["messages"]
            self.assertEqual(_breakpoints(sent), expected, model)


if __name__ == "__main__":
    unittest.main()
