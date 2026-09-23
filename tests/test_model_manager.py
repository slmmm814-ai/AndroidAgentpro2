from __future__ import annotations

import unittest

from agentpro.llm_planner import FakeLLMClient, LLMError
from agentpro.model_manager import ModelError, ModelManager


class FlakyClient:
    def __init__(self) -> None:
        self.attempts = 0

    def complete(self, messages, *, json_mode=False, timeout=30.0) -> str:
        self.attempts += 1
        raise LLMError(f"synthetic failure {self.attempts}")


class OkClient:
    def __init__(self, text: str = "OK") -> None:
        self._text = text

    def complete(self, messages, *, json_mode=False, timeout=30.0) -> str:
        return self._text


class ModelManagerTests(unittest.TestCase):
    def test_complete_success(self) -> None:
        mm = ModelManager(FakeLLMClient(script=["hello"]), retries=1)
        self.assertEqual(mm.complete([{"role": "user", "content": "hi"}]), "hello")
        self.assertEqual(mm.call_count, 1)
        self.assertTrue(mm.calls[0].ok)
        self.assertEqual(mm.calls[0].attempt, 1)

    def test_complete_exhausts_and_raises(self) -> None:
        flaky = FlakyClient()
        mm = ModelManager(flaky, retries=2)
        with self.assertRaises(ModelError):
            mm.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(mm.call_count, 3)

    def test_fallback_client_used(self) -> None:
        fallback = OkClient("fallback")
        mm = ModelManager(
            FlakyClient(),
            fallback_client=fallback,
            retries=1,
        )
        self.assertEqual(
            mm.complete([{"role": "user", "content": "hi"}]), "fallback"
        )

    def test_type_checks(self) -> None:
        with self.assertRaises(TypeError):
            ModelManager("not a client")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            ModelManager(OkClient(), retries=-1)
        with self.assertRaises(ValueError):
            ModelManager(OkClient(), timeout=0)

    def test_complete_json_validator_self_corrects(self) -> None:
        script = [
            '{"subgoals": []}',  # rejected: empty subgoals
            '{"subgoals": [{"text": "open app"}]}',  # accepted
        ]
        client = FakeLLMClient(script=script)
        mm = ModelManager(client, retries=0)

        def _validate(obj: dict) -> None:
            if not obj.get("subgoals"):
                raise ValueError("subgoals required")

        result = mm.complete_json(
            [{"role": "user", "content": "x"}],
            validator=_validate,
        )
        self.assertEqual(result["subgoals"][0]["text"], "open app")
        # second model input includes the corrective feedback message
        feedback = client.calls[1][-1]["content"]
        self.assertIn("rejected", feedback)

    def test_complete_json_gives_up(self) -> None:
        script = [
            "not json",  # parse error
            "still not json",
            "even worse",
        ]
        mm = ModelManager(FakeLLMClient(script=script), retries=0)
        with self.assertRaises(ModelError):
            mm.complete_json([{"role": "user", "content": "x"}])

    def test_require_validator_helper(self) -> None:
        with self.assertRaises(ValueError):
            ModelManager.require({"a": 1}, "a", "b")

    def test_wrong_root_type(self) -> None:
        mm = ModelManager(FakeLLMClient(script=["[1, 2]"]), retries=0)
        with self.assertRaises(ModelError):
            mm.complete_json([{"role": "user", "content": "x"}])


if __name__ == "__main__":
    unittest.main()