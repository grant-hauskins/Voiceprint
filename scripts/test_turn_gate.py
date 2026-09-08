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
        self.assertEqual(tg.decide(state, 19, "silence", 16), "wait")   # hold is sticky
        state.manual = None
        self.assertEqual(tg.decide(state, 20, "silence", 16), "speak")   # released within the address window

    def test_direct_address_is_answered_once(self):
        self.state.note_utterance(utt("Ava, who is here?"), now=10)
        self.assertEqual(tg.decide(self.state, 10.5, "silence", 10), "speak")
        self.state.note_agent_spoke(now=13)
        self.assertEqual(tg.decide(self.state, 14, "silence", 10), "wait")     # same address, already answered
        self.state.note_utterance(utt("Ava, and Kyle?"), now=15)
        self.assertEqual(tg.decide(self.state, 15.5, "silence", 15), "speak")  # new address

    def test_agent_own_voice_is_ignored(self):
        state = tg.GateState(agent_names=("Ava",), agent_speaker_id="participant_3")
        state.note_utterance(utt("Ava, what time is it?", speaker="participant_3"), now=10)  # its own echo through the mic
        self.assertEqual(tg.decide(state, 12, "silence", 10), "wait")

    def test_override_respects_inhibitors_but_not_cooldown_or_address(self):
        """BUILD_SPEC_V3 §5: a verified override skips the queue, never the room's turn/overlap inhibitors."""
        self.state.note_agent_spoke(now=10)                                   # inside the 6 s balanced cooldown
        self.state.manual = "override"
        self.assertEqual(tg.decide(self.state, 11, "speaking", 10), "wait")   # a human turn is open
        self.assertEqual(self.state.manual, "override")                       # not consumed by an inhibitor
        self.assertEqual(tg.decide(self.state, 11, "overlap", 10), "wait")
        self.state.last_overlap_at = 10.5
        self.assertEqual(tg.decide(self.state, 11, "silence", 10), "wait")    # overlap hold
        self.assertEqual(tg.decide(self.state, 14, "silence", 10), "speak")   # no history, no address, cooldown ignored
        self.assertIsNone(self.state.manual)                                  # consumed once
        self.assertEqual(tg.decide(self.state, 14, "silence", 10), "wait")
        self.state.manual = "override"; self.state.others_speaking = True
        self.assertEqual(tg.decide(self.state, 30, "silence", 10), "wait")    # the floor still wins
        self.state.others_speaking = False; self.state.manual = "hold"
        self.assertEqual(tg.decide(self.state, 30, "silence", 10), "wait")

    def test_raise_hand_blocks_soft_opportunities_but_not_direct_address(self):
        """BUILD_SPEC_V3 §5: negotiation advocates raise a hand; direct address always overrides the default."""
        self.state.raise_hand = True
        self.state.note_utterance(utt("What should we order?"), now=10)
        self.assertEqual(tg.decide(self.state, 15, "silence", 10), "wait")    # would be "speak" in balanced mode
        self.state.eagerness = "eager"
        self.assertEqual(tg.decide(self.state, 15, "silence", 10), "wait")
        self.state.note_utterance(utt("Ava, what do you think?"), now=16)
        self.assertEqual(tg.decide(self.state, 16.5, "silence", 16), "speak")
        self.state.note_agent_spoke(now=17)
        self.state.note_utterance(utt("Ava, is that right?", label="low"), now=18)
        self.assertEqual(tg.decide(self.state, 18.5, "silence", 18), "clarify")

    def test_fuzzy_addressing_matches_similar_sounding_names(self):
        """docs/API.md "Fuzzy addressing": brian addresses Ryan, been never addresses Ben; exact forms still work."""
        self.assertEqual(tg.normalize_name("Phyllis"), "filis")
        self.assertEqual(tg.normalize_name("Jackie"), "jakie")
        self.assertTrue(tg.similar_name("brian", "Ryan")); self.assertTrue(tg.similar_name("Bryan", "Ryan"))
        self.assertTrue(tg.similar_name("Rian", "Ryan"))                      # exact after normalization, four letters
        self.assertFalse(tg.similar_name("been", "Ben"))                      # "ben" is three letters: never fuzzy
        self.assertFalse(tg.similar_name("Benn", "Ben")); self.assertTrue(tg.similar_name("ben", "Ben"))
        self.assertFalse(tg.similar_name("Lyle", "Kyle"))                     # 0.75, under the threshold
        self.assertTrue(tg.addressed("Brian, what do you think?", ("Ryan",)))
        self.assertTrue(tg.addressed("I think brian should answer", ("Ryan",)))
        self.assertFalse(tg.addressed("I have been there", ("Ben",)))
        self.assertFalse(tg.addressed("The avalanche was huge", ("Ava",)))
        # Ratio 0.80 cases sit below the 0.85 threshold: "Adrian" (adrian/rian) does not address Ryan, "great" and
        # "grand" do not address Grant, "mediate" does not address Mediator; brian/bryan (0.89) and kylie (0.89) still match.
        self.assertFalse(tg.similar_name("Adrian", "Ryan"))
        self.assertIsNone(tg.addressed_as("Adrian, are you there?", ("Ryan",)))
        self.assertFalse(tg.addressed("that's great", ("Grant",))); self.assertFalse(tg.addressed("grand idea", ("Grant",)))
        self.assertFalse(tg.addressed("let's mediate", ("Mediator",)))
        self.assertTrue(tg.addressed("Grant, thoughts?", ("Grant",))); self.assertTrue(tg.similar_name("Kylie", "Kyle"))
        self.assertTrue(tg.addressed("hey ryan can you summarize", ("Ryan",)))
        self.assertTrue(tg.addressed("ok Ryan, go", ("Ryan",)))
        self.assertTrue(tg.addressed("What about you, Ryan?", ("Ryan",)))
        self.assertEqual(tg.addressed_as("What about Ryan's idea?", ("Ryan",)), ("Ryan", "Ryan"))   # possessive splits off
        self.assertEqual(tg.addressed_as("hey ryan can you", ("Ryan",)), ("Ryan", "ryan"))          # the transcript's spelling
        self.assertEqual(tg.addressed_as("Brian, thoughts?", ("Kyle", "Ryan")), ("Ryan", "Brian"))
        self.assertEqual(tg.addressed_as("Ryan and brian", ("Ryan",)), ("Ryan", "Ryan"))            # exact wins over fuzzy
        self.assertIsNone(tg.addressed_as("nobody here", ("Ryan",)))
        self.assertIsNone(tg.addressed_as(None, ("Ryan",))); self.assertIsNone(tg.addressed_as("", ("Ryan",)))

    def test_fuzzy_address_to_another_agent_is_not_an_opening(self):
        kyle = tg.GateState(agent_names=("Kyle",), room_agent_names=("Kyle", "Ryan"))
        ryan = tg.GateState(agent_names=("Ryan",), room_agent_names=("Kyle", "Ryan"))
        for state in (kyle, ryan):
            state.note_utterance(utt("Brian, what do you think?"), now=10)
        self.assertEqual(tg.decide(kyle, 15, "silence", 10), "wait")          # a near-match for Ryan is not Kyle's turn
        self.assertEqual(tg.decide(ryan, 10.5, "silence", 10), "speak")       # Ryan is addressed through the misspelling
        ryan.note_utterance(utt("Ryan, is that right?", label="low"), now=16)
        self.assertEqual(tg.decide(ryan, 16.5, "silence", 16), "clarify")
        echo = tg.GateState(agent_names=("Ryan",), agent_speaker_id="participant_3")
        echo.note_utterance(utt("Brian, what time is it?", speaker="participant_3"), now=10)   # its own echo
        self.assertEqual(tg.decide(echo, 12, "silence", 10), "wait")

    def test_reviewed_label_counts_as_reliable(self):
        """docs/API.md "Transcript review": reviewed is a human label; the gate treats it like high and never clarifies."""
        self.state.note_utterance(utt("What should we order?", label="reviewed"), now=10)
        self.assertEqual(tg.decide(self.state, 11.5, "silence", 10), "speak")
        self.state.note_utterance(utt("Ava, is that right?", label="reviewed"), now=12)
        self.assertEqual(tg.decide(self.state, 12.5, "silence", 12), "speak")       # never "clarify"
        eager = tg.GateState(agent_names=("Ava",), eagerness="eager")
        eager.note_utterance(utt("Let's order pizza.", label="reviewed"), now=10)
        self.assertEqual(tg.decide(eager, 11.5, "silence", 10), "speak")
        eager.history[-1]["label"] = "low"
        self.assertEqual(tg.decide(eager, 11.5, "silence", 10), "wait")


if __name__ == "__main__":
    unittest.main()
