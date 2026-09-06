"""Scripted-sequence tests for the turn-taking gate."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import turn_gate as tg  # noqa: E402


def utt(text, speaker="participant_1", label="high", candidates=None):
    return {"speaker_id": speaker, "text": text, "label": label, "candidates": candidates}


class GateTest(unittest.TestCase):
    def setUp(self):
        self.state = tg.GateState(agent_names=("Ava",), eagerness="balanced")

    def test_addressing_patterns(self):
        self.assertTrue(tg.addressed("Ava, what do you think?", ("Ava",)))
        self.assertTrue(tg.addressed("hey ava can you summarize", ("Ava",)))
        self.assertTrue(tg.addressed("What about you, Ava?", ("Ava",)))
        self.assertFalse(tg.addressed("The avalanche was huge", ("Ava",)))
        self.assertFalse(tg.addressed("", ("Ava",)))

    def test_waits_while_someone_is_talking_even_if_addressed(self):
        self.state.note_utterance(utt("Ava, what do you think?"), now=10)
        self.assertEqual(tg.decide(self.state, 10.5, "speaking", 10), "wait")
        self.assertEqual(tg.decide(self.state, 10.5, "silence", 10), "speak")

    def test_direct_address_with_unreliable_speaker_asks_for_clarification(self):
        self.state.note_utterance(utt("Ava, is that right?", label="low"), now=10)
        self.assertEqual(tg.decide(self.state, 11, "silence", 10), "clarify")

    def test_overlap_holds_the_agent_back(self):
        self.state.note_utterance(utt("both talking", speaker=None, label="overlap", candidates=["participant_1", "participant_2"]), now=10)
        self.state.note_utterance(utt("Ava, go ahead"), now=11)
        self.assertEqual(tg.decide(self.state, 12, "silence", 11), "wait")       # within OVERLAP_HOLD_S of the overlap
        self.assertEqual(tg.decide(self.state, 13.5, "silence", 11), "speak")

    def test_soft_opportunity_needs_question_high_label_and_silence(self):
        self.state.note_utterance(utt("What should we order?"), now=10)
        self.assertEqual(tg.decide(self.state, 10.5, "silence", 10), "wait")     # not enough silence yet
        self.assertEqual(tg.decide(self.state, 11.5, "silence", 10), "speak")
        self.state.history[-1]["label"] = "medium"
        self.assertEqual(tg.decide(self.state, 11.5, "silence", 10), "wait")     # balanced mode needs a high label
        self.state.history[-1]["label"] = "high"; self.state.history[-1]["text"] = "Let's order pizza."
        self.assertEqual(tg.decide(self.state, 15, "silence", 10), "wait")       # statement, not a question

    def test_cooldown_prevents_monologue_unless_addressed(self):
        self.state.note_utterance(utt("How far is it?"), now=10)
        self.state.note_agent_spoke(now=11)
        self.assertEqual(tg.decide(self.state, 13, "silence", 10), "wait")
        self.state.note_utterance(utt("Ava, and the time?"), now=13)
        self.assertEqual(tg.decide(self.state, 13.5, "silence", 13), "speak")

    def test_quiet_mode_only_speaks_when_addressed_and_manual_override_wins(self):
        state = tg.GateState(agent_names=("Ava",), eagerness="quiet")
        state.note_utterance(utt("Any ideas?"), now=10)
        self.assertEqual(tg.decide(state, 15, "silence", 10), "wait")
        state.manual = "speak"
        self.assertEqual(tg.decide(state, 15, "speaking", 10), "speak")
        state.manual = "hold"; state.note_utterance(utt("Ava, now?"), now=16)
        self.assertEqual(tg.decide(state, 17, "silence", 16), "wait")
        self.assertEqual(tg.decide(state, 30, "silence", 16), "wait")   # hold is sticky
        state.manual = None
        self.assertEqual(tg.decide(state, 30, "silence", 16), "speak")

    def test_agent_own_voice_is_ignored(self):
        state = tg.GateState(agent_names=("Ava",), agent_speaker_id="participant_3")
        state.note_utterance(utt("Ava, what time is it?", speaker="participant_3"), now=10)  # its own echo through the mic
        self.assertEqual(tg.decide(state, 12, "silence", 10), "wait")


if __name__ == "__main__":
    unittest.main()
