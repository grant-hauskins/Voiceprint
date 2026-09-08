"""BUILD_SPEC_V3 §5: every cell of the participation table, and how raise-hand changes the live gate."""
import unittest

import participation as pp
import turn_gate as tg


class ParticipationTableTest(unittest.TestCase):
    def test_every_cell_of_the_table(self):
        """§5 table: negotiation/advocate -> raise-hand; arbitration/arbitrator -> proactive; casual/any -> low threshold."""
        self.assertEqual(pp.default_mode("negotiation", "voice", "Synthetic One"), "raise_hand")
        self.assertEqual(pp.default_mode("negotiation", "arbitrator"), "proactive")
        self.assertEqual(pp.default_mode("negotiation", "voice", ""), "low_threshold")     # a voice agent with no principal
        self.assertEqual(pp.default_mode("casual", "voice", ""), "low_threshold")
        self.assertEqual(pp.default_mode("casual", "voice", "Synthetic One"), "low_threshold")
        self.assertEqual(pp.default_mode("casual", "arbitrator"), "low_threshold")
        with self.assertRaises(ValueError):
            pp.default_mode("debate", "voice")
        with self.assertRaises(ValueError):
            pp.default_mode("casual", "judge")
        self.assertEqual(pp.OVERRIDE_TAGS, ("OBJECTIVE_ACHIEVED", "REFOCUS_NEEDED"))

    def test_apply_mode_sets_raise_hand_only_for_advocates(self):
        state = tg.GateState(("Ava",))
        self.assertTrue(pp.apply_mode(state, "raise_hand").raise_hand)
        self.assertFalse(pp.apply_mode(state, "proactive").raise_hand)
        self.assertFalse(pp.apply_mode(state, "low_threshold").raise_hand)
        with self.assertRaises(ValueError):
            pp.apply_mode(state, "loud")


if __name__ == "__main__":
    unittest.main()
