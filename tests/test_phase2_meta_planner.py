from __future__ import annotations

import unittest

from agentpro import (
    ActionType,
    AgentAction,
    AgentContext,
    AgentState,
    FSMLimits,
)
from agentpro.meta_planner import (
    MetaPlanner,
    NoValidPlanError,
    PlannerValidationError,
    StaticCandidateProvider,
    action_type_counts,
    make_candidate,
)


class TestMetaPlanner(unittest.TestCase):
    def setUp(self) -> None:
        self.context = AgentContext(goal="open application")

    def test_selects_highest_deterministic_score(self) -> None:
        planner = MetaPlanner()

        short = make_candidate(
            "short",
            [AgentAction(ActionType.TAP, {"x": 10, "y": 10})],
            confidence=0.90,
            risk=0.10,
        )

        long = make_candidate(
            "long",
            [
                AgentAction(ActionType.BACK),
                AgentAction(ActionType.TAP, {"x": 10, "y": 10}),
            ],
            confidence=0.70,
            risk=0.20,
        )

        decision = planner.select([long, short], self.context)

        self.assertEqual(decision.selected.name, "short")
        self.assertTrue(decision.evaluation.valid)
        self.assertEqual(decision.evaluation.candidate_name, "short")

    def test_rejects_low_confidence(self) -> None:
        planner = MetaPlanner(min_confidence=0.80)

        candidate = make_candidate(
            "weak",
            [AgentAction(ActionType.TAP, {"x": 1, "y": 1})],
            confidence=0.50,
            risk=0.10,
        )

        with self.assertRaises(NoValidPlanError):
            planner.select([candidate], self.context)

    def test_rejects_high_risk(self) -> None:
        planner = MetaPlanner(max_risk=0.30)

        candidate = make_candidate(
            "risky",
            [AgentAction(ActionType.TAP, {"x": 1, "y": 1})],
            confidence=0.95,
            risk=0.80,
        )

        with self.assertRaises(NoValidPlanError):
            planner.select([candidate], self.context)

    def test_rejects_plan_over_action_limit(self) -> None:
        planner = MetaPlanner(
            FSMLimits(max_actions=2),
            max_plan_actions=2,
        )

        candidate = make_candidate(
            "too-long",
            [
                AgentAction(ActionType.TAP, {"x": 1, "y": 1}),
                AgentAction(ActionType.TAP, {"x": 2, "y": 2}),
                AgentAction(ActionType.TAP, {"x": 3, "y": 3}),
            ],
            confidence=1.0,
            risk=0.0,
        )

        with self.assertRaises(NoValidPlanError):
            planner.select([candidate], self.context)

    def test_remaining_action_budget_is_enforced(self) -> None:
        context = AgentContext(
            goal="limited task",
            action_count=1,
        )

        planner = MetaPlanner(
            FSMLimits(max_actions=2),
            max_plan_actions=2,
        )

        candidate = make_candidate(
            "budget-overflow",
            [
                AgentAction(ActionType.TAP, {"x": 1, "y": 1}),
                AgentAction(ActionType.TAP, {"x": 2, "y": 2}),
            ],
            confidence=1.0,
            risk=0.0,
        )

        with self.assertRaises(NoValidPlanError):
            planner.select([candidate], context)

    def test_confirmation_penalty_is_deterministic(self) -> None:
        planner = MetaPlanner()

        normal = make_candidate(
            "normal",
            [AgentAction(ActionType.TAP, {"x": 1, "y": 1})],
            confidence=0.80,
            risk=0.10,
        )

        confirmed = make_candidate(
            "confirmed",
            [
                AgentAction(
                    ActionType.TAP,
                    {"x": 1, "y": 1},
                    requires_confirmation=True,
                )
            ],
            confidence=0.80,
            risk=0.10,
        )

        normal_eval = planner.evaluate(normal, self.context)
        confirmed_eval = planner.evaluate(confirmed, self.context)

        self.assertGreater(
            normal_eval.score,
            confirmed_eval.score,
        )

    def test_static_provider_returns_candidates(self) -> None:
        candidate = make_candidate(
            "tap",
            [AgentAction(ActionType.TAP, {"x": 5, "y": 5})],
            confidence=0.9,
            risk=0.1,
        )

        provider = StaticCandidateProvider([candidate])
        result = provider.generate(self.context, None)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "tap")

    def test_plan_returns_selected_actions(self) -> None:
        planner = MetaPlanner()

        candidate = make_candidate(
            "tap",
            [AgentAction(ActionType.TAP, {"x": 5, "y": 5})],
            confidence=0.9,
            risk=0.1,
        )

        provider = StaticCandidateProvider([candidate])

        actions = planner.plan(
            self.context,
            None,
            provider,
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action_type, ActionType.TAP)

    def test_empty_candidates_are_rejected(self) -> None:
        planner = MetaPlanner()

        with self.assertRaises(NoValidPlanError):
            planner.select([], self.context)

    def test_invalid_candidate_type_is_rejected(self) -> None:
        planner = MetaPlanner()

        with self.assertRaises(PlannerValidationError):
            planner.select(["invalid"], self.context)  # type: ignore[list-item]

    def test_action_type_counts(self) -> None:
        actions = [
            AgentAction(ActionType.TAP, {"x": 1, "y": 1}),
            AgentAction(ActionType.TAP, {"x": 2, "y": 2}),
            AgentAction(ActionType.BACK),
        ]

        counts = action_type_counts(actions)

        self.assertEqual(counts[ActionType.TAP], 2)
        self.assertEqual(counts[ActionType.BACK], 1)

    def test_tie_breaking_is_stable(self) -> None:
        planner = MetaPlanner()

        first = make_candidate(
            "alpha",
            [AgentAction(ActionType.TAP, {"x": 1, "y": 1})],
            confidence=0.8,
            risk=0.2,
        )

        second = make_candidate(
            "beta",
            [AgentAction(ActionType.TAP, {"x": 2, "y": 2})],
            confidence=0.8,
            risk=0.2,
        )

        decision_one = planner.select(
            [second, first],
            self.context,
        )
        decision_two = planner.select(
            [first, second],
            self.context,
        )

        self.assertEqual(decision_one.selected.name, "alpha")
        self.assertEqual(decision_two.selected.name, "alpha")

    def test_terminal_context_is_still_valid_for_evaluation(self) -> None:
        context = AgentContext(
            goal="completed",
            state=AgentState.SUCCESS,
        )

        planner = MetaPlanner()

        candidate = make_candidate(
            "candidate",
            [AgentAction(ActionType.FINISH)],
            confidence=1.0,
            risk=0.0,
        )

        evaluation = planner.evaluate(candidate, context)

        self.assertTrue(evaluation.valid)


    def test_duplicate_candidate_names_do_not_hide_rejected_evaluation(self) -> None:
        planner = MetaPlanner(max_risk=0.30)

        first = make_candidate(
            "same-name",
            [AgentAction(ActionType.TAP, {"x": 1, "y": 1})],
            confidence=0.90,
            risk=0.10,
        )

        second = make_candidate(
            "same-name",
            [AgentAction(ActionType.BACK)],
            confidence=0.40,
            risk=0.90,
        )

        decision = planner.select([first, second], self.context)

        self.assertEqual(decision.selected.name, "same-name")
        self.assertEqual(len(decision.rejected), 1)
        self.assertEqual(decision.rejected[0].candidate_name, "same-name")
        self.assertFalse(decision.rejected[0].valid)


if __name__ == "__main__":
    unittest.main()
