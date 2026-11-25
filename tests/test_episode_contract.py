"""Newcomer API regressions for authorization, exact U accounting and receipt versions."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from episode_contract import sample, trajectory
from integrity import receipt_format


def decision(at, action="B"):
    return {"observation": {"timeNs": at, "legalActions": ["A", "B"]},
            "selected": action, "policyVersion": "fixture"}


class EpisodeContractTests(unittest.TestCase):
    rows = [{"start_ns": 10, "end_ns": 20, "unmet": ["left", "right"], "U_service_ns": 20},
            {"start_ns": 20, "end_ns": 40, "unmet": ["left"], "U_service_ns": 20}]

    def test_same_timestamp_prefix_and_half_open_window(self):
        decisions = [decision(t) for t in (8, 15, 15, 28, 40)]
        decisions[1].update(behaviorProbability=.25, oldLogProbability=-1.3862943611198906)
        result = trajectory(self.rows, decisions, 40e-9)
        self.assertEqual(result["prefixUnweightedServiceNs"], 10)
        self.assertEqual([s["rewardUnweightedServiceNs"] for s in result["steps"]], [0, -18, -12])
        self.assertEqual([s["terminal"] for s in result["steps"]], [False, False, True])
        self.assertEqual(result["steps"][0]["behaviorProbability"], .25)
        self.assertNotIn("behaviorProbability", result["steps"][1])
        self.assertFalse(any("weight" in k.lower() and "unweighted" not in k.lower()
                             for s in result["steps"] for k in s))

    def test_empty_decision_window_is_unassigned(self):
        value = trajectory(self.rows, [], 40e-9)
        self.assertEqual((value["prefixUnweightedServiceNs"], value["steps"]), (40, []))

    def test_invalid_clock_and_action_are_rejected(self):
        with self.assertRaises(ValueError):
            trajectory(self.rows, [decision(15, "X")], 40e-9)
        with self.assertRaises(ValueError):
            trajectory([self.rows[1], self.rows[0]], [], 40e-9)
        with self.assertRaises(ValueError):
            trajectory(self.rows, [decision(25), decision(12)], 40e-9)

    def test_training_split_checked_before_starting_worker(self):
        with patch("episode_contract._run") as launch:
            with self.assertRaises(ValueError):
                sample("09", "unused")
            launch.assert_not_called()

    def test_receipt_formats(self):
        self.assertEqual(receipt_format({"mode":"workorder"}), "b03.packet.run-files.v1")
        self.assertEqual(receipt_format({"mode":"workorder","receiptContract":"b03.packet.workorder.v2"}),
                         "b03.packet.run-files.v2")
        for case in ({"mode":"workorder","receiptContract":"unknown"},
                     {"mode":"workorder","implementationSha256":{"src/episode_contract.py":"registered"}}):
            with self.assertRaises(ValueError):
                receipt_format(case)


if __name__ == "__main__":
    unittest.main()
