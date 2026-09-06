"""Unit tests for turn grouping and overlap segmentation (no API, no microphone)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import voiceprint_client as vp  # noqa: E402


def chunk(sequence, speaker=None, status=None, overlap="clear", similarity=None, margin=None, candidates=None):
    if status is None:
        status = "tentative" if speaker else "unknown"
    return {"sequence": sequence, "start_ms": sequence * 250, "speaker_id": speaker, "status": status, "overlap": overlap,
            "similarity": similarity, "margin": margin, "candidates": candidates or []}


class TurnsTest(unittest.TestCase):
    def setUp(self):
        self.out = []
        self.turns = vp.Turns(lambda utterance, pcm: self.out.append((utterance, len(pcm))))

    def feed(self, sequence, **kw):
        self.turns.observe(sequence, b"x" * vp.CHUNK_BYTES, chunk(sequence, **kw))

    def test_single_speaker_turn_with_stats_and_lead_audio(self):
        for s in range(0, 5):
            self.feed(s, status="buffering" if s < 5 else None)
        for s in range(5, 13):
            self.feed(s, speaker="a", similarity=0.6 + 0.01 * (s % 2), margin=0.3)
        for s in range(13, 16):
            self.feed(s, status="silence")
        self.assertEqual(len(self.out), 1)
        u, audio = self.out[0]
        self.assertEqual(u["speaker_id"], "a")
        self.assertEqual(u["start_ms"], (5 - vp.LEAD_CHUNKS) * 250)
        self.assertEqual(u["end_ms"], (12 + vp.TAIL_CHUNKS + 1) * 250)
        self.assertAlmostEqual(u["similarity"], 0.605, places=3)
        self.assertEqual(u["margin"], 0.3)
        self.assertEqual(u["overlap_ratio"], 0.0)
        self.assertEqual(u["abstain_ratio"], 0.0)
        self.assertEqual(audio, (12 + vp.TAIL_CHUNKS - (5 - vp.LEAD_CHUNKS) + 1) * vp.CHUNK_BYTES)

    def test_speaker_change_closes_turn_and_abstentions_are_counted(self):
        for s in range(0, 6):
            self.feed(s, speaker="a", similarity=0.5, margin=0.2)
        self.feed(6, status="unknown")             # speaker_change_in_context style abstention
        self.feed(7, status="unknown")
        for s in range(8, 14):
            self.feed(s, speaker="b", similarity=0.7, margin=0.4)
        self.turns.flush()
        self.assertEqual([u["speaker_id"] for u, _ in self.out], ["a", "b"])
        first = self.out[0][0]
        self.assertEqual(first["abstain_ratio"], round(2 / 8, 3))
        second = self.out[1][0]
        self.assertEqual(second["start_ms"], (first["end_ms"] // 250) * 250)  # audio never overlaps the previous row

    def test_overlap_burst_becomes_its_own_row_with_two_candidates(self):
        for s in range(0, 6):
            self.feed(s, speaker="a", similarity=0.6, margin=0.3)
        cands = [{"speaker_id": "a", "similarity": 0.4}, {"speaker_id": "b", "similarity": 0.35}]
        for s in range(6, 10):
            self.feed(s, status="overlap", overlap="detected", candidates=cands)
        for s in range(10, 16):
            self.feed(s, speaker="b", similarity=0.65, margin=0.35)
        self.turns.flush()
        kinds = [(u["speaker_id"], u.get("candidates")) for u, _ in self.out]
        self.assertEqual(kinds, [("a", None), (None, ["a", "b"]), ("b", None)])
        overlap = self.out[1][0]
        self.assertEqual(overlap["overlap_ratio"], 1.0)
        self.assertEqual(overlap["start_ms"], 6 * 250)  # first overlap chunk, right after turn a
        self.assertEqual(overlap["end_ms"], (9 + vp.TAIL_CHUNKS + 1) * 250)
        self.assertEqual(self.out[2][0]["start_ms"], overlap["end_ms"])

    def test_single_overlap_chunk_inside_turn_only_raises_ratio(self):
        for s in range(0, 4):
            self.feed(s, speaker="a", similarity=0.6, margin=0.3)
        self.feed(4, status="overlap", overlap="detected")
        for s in range(5, 9):
            self.feed(s, speaker="a", similarity=0.6, margin=0.3)
        self.turns.flush()
        self.assertEqual(len(self.out), 1)
        self.assertEqual(self.out[0][0]["overlap_ratio"], round(1 / 9, 3))

    def test_long_monologue_is_split(self):
        for s in range(0, vp.MAX_TURN_CHUNKS + 10):
            self.feed(s, speaker="a", similarity=0.6, margin=0.3)
        self.turns.flush()
        self.assertEqual(len(self.out), 2)

    def test_format_line(self):
        names = {"participant_1": "Grant", "participant_2": "Kyle"}
        self.assertEqual(vp.format_line({"speaker_id": "participant_1", "start_ms": 12500, "end_ms": 18000, "text": "hi", "label": "high", "utterance_id": 7}, names), "#7 [Grant 0:12.5-0:18.0 high] hi")
        self.assertEqual(vp.format_line({"speaker_id": None, "candidates": ["participant_1", "participant_2"], "start_ms": 0, "end_ms": 1500, "text": "x", "label": "overlap"}, names), "   [OVERLAP Grant+Kyle 0:00.0-0:01.5 overlap] x")


if __name__ == "__main__":
    unittest.main()
