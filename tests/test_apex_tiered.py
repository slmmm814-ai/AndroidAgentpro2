"""Unit tests for agentpro.apex.tiered — fast/big model routing."""

from __future__ import annotations

import sys
import unittest
from typing import Any

sys.path.insert(0, "/root/AndroidAgentpro2")

from agentpro.apex.tiered import (  # noqa: E402
    ChatModel,
    OpenAIChatModel,
    TierConfig,
    TieredModels,
)


class _ScriptedModel:
    """Chat client that records the model name the router actually asked for."""

    def __init__(self, *, fail_model: str | None = None) -> None:
        self.calls: list[tuple[str, str, int, float]] = []
        self.fail_model = fail_model

    def chat(
        self,
        system: str,
        user: str,
        *,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        if self.fail_model == model:
            raise RuntimeError(f"forced failure on {model}")
        self.calls.append((model, user, max_tokens, temperature))
        return f"[{model}] {user}"


class TestRouting(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = TierConfig(fast_model="flash", big_model="pro")
        self.models = TieredModels(_ScriptedModel(), self.cfg)

    def test_big_purposes_route_big(self) -> None:
        for purpose in ("plan", "replan", "recover", "verify_goal", "summarize", "decompose"):
            self.assertEqual(self.models.route(purpose), "big", purpose)

    def test_fast_purposes_route_fast(self) -> None:
        for purpose in ("next_action", "classify", "extract", "label", "grounding"):
            self.assertEqual(self.models.route(purpose), "fast", purpose)

    def test_unknown_purpose_routes_big(self) -> None:
        self.assertEqual(self.models.route("something_new"), "big")

    def test_purpose_case_insensitive(self) -> None:
        self.assertEqual(self.models.route("PLAN"), "big")
        self.assertEqual(self.models.route("Next_Action"), "fast")

    def test_empty_purpose_routes_big(self) -> None:
        self.assertEqual(self.models.route(""), "big")


class TestAsk(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = TierConfig(fast_model="flash", big_model="pro")
        self.client = _ScriptedModel()
        self.models = TieredModels(self.client, self.cfg)

    def test_fast_purpose_uses_fast_model(self) -> None:
        out = self.models.ask("next_action", "sys", "do step")
        self.assertIn("[flash]", out)
        self.assertEqual(self.models.stats.fast_calls, 1)
        self.assertEqual(self.models.stats.big_calls, 0)

    def test_big_purpose_uses_big_model(self) -> None:
        out = self.models.ask("plan", "sys", "decompose goal")
        self.assertIn("[pro]", out)
        self.assertEqual(self.models.stats.big_calls, 1)
        self.assertEqual(self.models.stats.fast_calls, 0)

    def test_big_routes_are_recorded(self) -> None:
        self.models.ask("plan", "s", "u")
        self.models.ask("recover", "s", "u")
        self.assertEqual(self.models.stats.big_routes, ["plan", "recover"])

    def test_unknown_purpose_counts_as_big(self) -> None:
        self.models.ask("mystery", "s", "u")
        self.assertEqual(self.models.stats.big_calls, 1)


class TestFailover(unittest.TestCase):
    def test_fast_failure_falls_back_to_big(self) -> None:
        client = _ScriptedModel()
        client.fail_model = "flash"
        models = TieredModels(client, TierConfig("flash", "pro"))

        out = models.ask("next_action", "s", "u")

        self.assertIn("[pro]", out)
        self.assertEqual(models.stats.fast_failures, 1)
        self.assertEqual(models.stats.big_calls, 1)

    def test_fast_failure_without_fallback_raises(self) -> None:
        client = _ScriptedModel()
        client.fail_model = "flash"
        models = TieredModels(client, TierConfig("flash", "pro"), fallback_to_big=False)

        with self.assertRaises(RuntimeError):
            models.ask("next_action", "s", "u")
        self.assertEqual(models.stats.fast_failures, 1)
        self.assertEqual(models.stats.big_calls, 0)

    def test_big_failure_raises_and_counts(self) -> None:
        client = _ScriptedModel()
        client.fail_model = "pro"
        models = TieredModels(client, TierConfig("flash", "pro"))

        with self.assertRaises(RuntimeError):
            models.ask("plan", "s", "u")
        self.assertEqual(models.stats.big_failures, 1)


class TestForcedTiers(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _ScriptedModel()
        self.models = TieredModels(self.client, TierConfig("flash", "pro"))

    def test_ask_fast_forces_fast(self) -> None:
        out = self.models.ask_fast("s", "u")
        self.assertIn("[flash]", out)
        self.assertEqual(self.models.stats.fast_calls, 1)

    def test_ask_big_forces_big(self) -> None:
        out = self.models.ask_big("s", "u")
        self.assertIn("[pro]", out)
        self.assertEqual(self.models.stats.big_calls, 1)


class TestStats(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _ScriptedModel()
        self.models = TieredModels(self.client, TierConfig("flash", "pro"))

    def test_fast_share(self) -> None:
        self.models.ask("next_action", "s", "u")
        self.models.ask("next_action", "s", "u")
        self.models.ask("plan", "s", "u")

        self.assertEqual(self.models.stats.fast_share, 2 / 3)

    def test_total_calls_and_seconds(self) -> None:
        self.models.ask("next_action", "s", "u")
        self.models.ask("plan", "s", "u")

        self.assertEqual(self.models.stats.total_calls, 2)
        self.assertGreaterEqual(self.models.stats.total_seconds, 0.0)

    def test_avg_when_no_calls_is_zero(self) -> None:
        self.assertEqual(self.models.stats.avg_fast_seconds, 0.0)
        self.assertEqual(self.models.stats.avg_big_seconds, 0.0)
        self.assertEqual(self.models.stats.fast_share, 0.0)

    def test_summary_string(self) -> None:
        self.models.ask("next_action", "s", "u")
        s = self.models.summary()
        self.assertIn("fast=", s)
        self.assertIn("big=", s)
        self.assertIn("fast_share=", s)

    def test_to_dict_shape(self) -> None:
        d = self.models.stats.to_dict()
        for key in (
            "fast_calls",
            "big_calls",
            "fast_seconds",
            "big_seconds",
            "avg_fast_seconds",
            "avg_big_seconds",
            "fast_share",
            "fast_failures",
            "big_failures",
            "big_routes",
        ):
            self.assertIn(key, d)


class TestProtocolConformance(unittest.TestCase):
    def test_scripted_satisfies_protocol(self) -> None:
        m: ChatModel = _ScriptedModel()
        self.assertEqual(
            m.chat("s", "u", model="fast", max_tokens=100, temperature=0.0),
            "[fast] u",
        )


class TestTierConfig(unittest.TestCase):
    def test_defaults(self) -> None:
        cfg = TierConfig(fast_model="f", big_model="b")
        self.assertEqual(cfg.fast_max_tokens, 400)
        self.assertEqual(cfg.big_max_tokens, 1200)
        self.assertEqual(cfg.fast_temperature, 0.2)
        self.assertEqual(cfg.big_temperature, 0.3)


class TestModelNameReachesClient(unittest.TestCase):
    """The router must send the concrete model name, not just a token budget."""

    def test_fast_ask_sends_fast_model_name(self) -> None:
        client = _ScriptedModel()
        models = TieredModels(client, TierConfig("flash-9b", "pro-1"))
        models.ask("next_action", "s", "u")
        self.assertEqual(client.calls[0][0], "flash-9b")

    def test_big_ask_sends_big_model_name(self) -> None:
        client = _ScriptedModel()
        models = TieredModels(client, TierConfig("flash-9b", "pro-1"))
        models.ask("plan", "s", "u")
        self.assertEqual(client.calls[0][0], "pro-1")

    def test_tier_generation_settings_are_forwarded(self) -> None:
        client = _ScriptedModel()
        models = TieredModels(client, TierConfig("flash", "pro"))
        models.ask("next_action", "s", "u")
        _, _, max_tokens, temperature = client.calls[0]
        self.assertEqual(max_tokens, 400)
        self.assertAlmostEqual(temperature, 0.2)

    def test_failover_sends_big_model_name(self) -> None:
        client = _ScriptedModel()
        client.fail_model = "flash-9b"
        models = TieredModels(client, TierConfig("flash-9b", "pro-1"))
        out = models.ask("next_action", "s", "u")
        self.assertIn("[pro-1]", out)


class _RecordingClient:
    """Minimal OpenAI-compatible client double."""

    def __init__(self) -> None:
        self._model = "original"
        self.seen: list[dict[str, Any]] = []

    def complete(self, messages, *, json_mode=False, timeout=None,
                 max_tokens=None, temperature=None):
        self.seen.append({
            "model": self._model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": list(messages),
        })
        return f"reply from {self._model}"


class TestOpenAIChatModel(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _RecordingClient()
        self.adapter = OpenAIChatModel(self.client)

    def test_passes_model_and_generation_settings(self) -> None:
        out = self.adapter.chat("sys", "usr", model="pro-1", max_tokens=900, temperature=0.4)
        self.assertEqual(out, "reply from pro-1")
        call = self.client.seen[0]
        self.assertEqual(call["model"], "pro-1")
        self.assertEqual(call["max_tokens"], 900)
        self.assertAlmostEqual(call["temperature"], 0.4)

    def test_builds_system_and_user_messages(self) -> None:
        self.adapter.chat("sys", "usr", model="m", max_tokens=10, temperature=0.1)
        messages = self.client.seen[0]["messages"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1]["role"], "user")
        self.assertEqual(messages[1]["content"], "usr")

    def test_restores_client_model_after_call(self) -> None:
        self.adapter.chat("sys", "usr", model="temporary", max_tokens=10, temperature=0.1)
        self.assertEqual(self.client._model, "original")

    def test_rejects_client_without_complete(self) -> None:
        with self.assertRaises(TypeError):
            OpenAIChatModel(object())


if __name__ == "__main__":
    unittest.main()
