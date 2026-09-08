"""Redaction guard conformance (every shared fixture case) and objective record helpers."""
import json
import unittest
from pathlib import Path

import objectives as ob

FIXTURE = Path(__file__).resolve().parents[1] / "evaluation" / "redaction_cases.json"


class RedactionFixtureTest(unittest.TestCase):
    def test_every_shared_case_matches_expected_and_hits(self):
        """BUILD_SPEC_V3 §3/§4: notes board never contains a private value; the fixture is shared with Redaction.java."""
        cases = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
        self.assertGreaterEqual(len(cases), 20)
        for case in cases:
            with self.subTest(case["name"]):
                self.assertEqual(ob.redact(case["text"], case["values"]), (case["expected"], case["hits"]))

    def test_value_classification(self):
        self.assertEqual(ob.numeric_value("$300,000"), 300000.0)
        self.assertEqual(ob.numeric_value("1.5M"), 1500000.0)
        self.assertEqual(ob.numeric_value("4.5 %"), 4.5)
        self.assertEqual(ob.numeric_value("12 thousand"), 12000.0)
        self.assertIsNone(ob.numeric_value("June 30"))
        self.assertIsNone(ob.numeric_value("300k firm"))

    def test_token_is_never_rematched_and_values_are_union(self):
        text, hits = ob.redact("300000 and three hundred thousand and the piano", ["300000", "the piano"])
        self.assertEqual((text, hits), ("[withheld] and [withheld] and [withheld]", 3))
        text, hits = ob.redact(text, ["300000", "the piano"])
        self.assertEqual(hits, 0)


class ObjectiveRecordTest(unittest.TestCase):
    API = {"session_id": "room", "objectives": [
        {"principal_id": "participant_1", "version": 2, "position": "wants to sell", "source": "typed", "trigger": "initial", "created_ms": 5,
         "constraints": [{"label": "floor", "value": "300000"}, {"label": "close by", "value": "June 30"}, {"label": "blank", "value": ""}]},
        {"principal_id": "participant_2", "version": 1, "position": "wants to buy", "source": "uploaded", "trigger": "initial", "created_ms": 6,
         "constraints": [{"label": "ceiling", "value": "320000"}, {"label": "floor", "value": "300000"}]},
        {"bogus": True}]}

    def test_parse_and_union(self):
        parsed = ob.parse_objectives(self.API)
        self.assertEqual(sorted(parsed), ["participant_1", "participant_2"])
        self.assertEqual(parsed["participant_1"].constraints, (("floor", "300000"), ("close by", "June 30")))
        self.assertEqual(parsed["participant_1"].version, 2)
        self.assertEqual(ob.constraint_values(parsed), ("300000", "June 30", "320000"))
        self.assertEqual(ob.parse_objectives({}), {})

    def test_render_marks_position_shareable_and_constraints_private(self):
        objective = ob.parse_objectives(self.API)["participant_1"]
        text = ob.render_objective_for_prompt(objective, "Synthetic One")
        self.assertIn("Position, which you may share: wants to sell", text)
        self.assertIn("NEVER state, quote, approximate or confirm", text)
        self.assertIn("- floor: 300000", text)
        self.assertIn("- close by: June 30", text)
        self.assertIn("without disclosing", text)
        bare = ob.Objective("participant_2", 1, "wants to buy", (), "typed", "initial", 0)
        self.assertNotIn("Private constraints", ob.render_objective_for_prompt(bare, "Synthetic Two"))


if __name__ == "__main__":
    unittest.main()
