import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import score_run as scorer


FIXTURE = Path(__file__).resolve().parents[1] / "evaluation" / "synthetic_regression_runs.jsonl"


def score(rows, **kwargs):
    return scorer.score("unit", [dict(row, _line=index) for index, row in enumerate(rows, 1)], **kwargs)


def event(kind, agent="Ava", **fields):
    return {"agent": agent, "openai": dict(type=kind, **fields)}


def tool(iid="tool1", agent="Ava", output="ok", error=None):
    return event("response.output_item.done", agent, response_id="tool-response", item={
        "id": iid, "type": "mcp_call", "name": "get_transcript", "output": output, "error": error})


def audio(rid="reply1", agent="Ava"):
    return event("response.output_audio.delta", agent, response_id=rid)


def playback(action, agent, response_id, when, offset=0, basis="played"):
    return {"agent": agent, "monotonic_ms": when, "playback": {"action": action, "response_id": response_id,
            "audio_timeline_ms": offset, "timing_basis": basis}}


class SyntheticRegressionTests(unittest.TestCase):
    def test_last_three_match_measured_counts(self):
        runs = scorer.read_runs(FIXTURE)
        self.assertEqual(len(runs), 3)
        expected = [(2, 2, 2, 3, 4261), (6, 4, 4, 7, 17298), (5, 5, 4, 5, 14149)]
        for (key, rows), (replies, calls, ready, utterances, tokens) in zip(runs.items(), expected):
            result = scorer.score(key, rows)
            agent = result["agents"]["Ava"]
            self.assertEqual(agent["replies"], replies)
            self.assertEqual(agent["get_transcript_calls"], calls)
            self.assertEqual(agent["tool_before_reply"], scorer.fraction(ready, replies))
            self.assertEqual(result["utterances"], utterances)
            self.assertEqual(agent["tokens"]["total_tokens"], tokens)
            self.assertIsNone(result["floor"]["violations"])
            self.assertIsNone(result["ground_truth"])

    def test_final_preamble_precedes_tool(self):
        result = scorer.score("final", list(scorer.read_runs(FIXTURE).values())[-1])
        reply = next(row for row in result["reply_evidence"] if row["response_id"] == "fixture_3_reply_1")
        call = next(row for row in result["tool_evidence"] if row["response_id"] == reply["response_id"])
        self.assertFalse(reply["tool_before_speech"])
        self.assertLess(reply["source_line"], call["source_line"])

    def test_sanitizer_does_not_copy_configuration(self):
        for rows in scorer.read_runs(FIXTURE).values():
            for row in rows:
                payload = row.get("openai", {}).get("session", {})
                self.assertLessEqual(set(payload), {"instructions"})
                self.assertNotIn("authorization", json.dumps(row).lower())
                self.assertNotIn("server_url", json.dumps(row).lower())


class EvidenceTests(unittest.TestCase):
    def test_allowlisted_tool_result_does_not_need_content(self):
        item = {"id": "safe", "type": "mcp_call", "name": "get_transcript", "succeeded": True, "output_bytes": 47}
        result = score([event("response.output_item.done", item=item), audio()])
        self.assertEqual(result["agents"]["Ava"]["tool_before_reply"], scorer.fraction(1, 1))
        self.assertEqual(result["provider_tool_output_bytes"]["p50"], 47)

    def test_aggregate_has_no_subject_or_room_identifiers(self):
        result = score([dict(tool(), session_id="private_room"), audio()], names={"private_subject": "Private Person"})
        output = json.dumps(scorer.aggregate(result))
        for forbidden in ("private_room", "private_subject", "Private Person", "reply1", "tool1", "Ava"):
            self.assertNotIn(forbidden, output)
        self.assertIn("Agent 1", output)

    def test_ephemeral_summary_does_not_modify_log_or_write_result(self):
        before = FIXTURE.read_bytes()
        summary = scorer.summarize_ephemeral(FIXTURE)
        self.assertIn("5 replies", summary)
        self.assertNotIn("fixture_3_reply_1", summary)
        self.assertEqual(FIXTURE.read_bytes(), before)

    def test_ram_summary_scores_no_file(self):
        self.assertIn("1 replies", scorer.summarize_events([tool(), audio()]))

    def test_tool_result_is_fresh_and_agent_specific(self):
        result = score([tool(), audio(), audio("reply2"), audio("ben1", "Ben")])
        self.assertEqual(result["agents"]["Ava"]["tool_before_reply"], scorer.fraction(1, 2))
        self.assertEqual(result["agents"]["Ben"]["tool_before_reply"], scorer.fraction(0, 1))

    def test_failed_or_unfinished_call_not_credited(self):
        result = score([tool(output=None), tool("tool2", error={"message": "failed"}), audio()])
        self.assertEqual(result["agents"]["Ava"]["tool_before_reply"], scorer.fraction(0, 1))
        self.assertEqual(result["agents"]["Ava"]["failed_tool_calls"], 1)

    def test_result_after_speech_not_backdated(self):
        result = score([audio(), tool(), event("response.output_audio_transcript.done", response_id="reply1", transcript="hi")])
        self.assertEqual(result["agents"]["Ava"]["tool_before_reply"], scorer.fraction(0, 1))

    def test_transcript_alone_cannot_prove_audio_start(self):
        result = score([tool(), event("response.output_audio_transcript.done", response_id="reply1", transcript="hi")])
        self.assertEqual(result["agents"]["Ava"]["reply_timing_missing"], 1)
        self.assertIsNone(result["agents"]["Ava"]["tool_before_reply"]["ratio"])

    def test_repeated_events_and_multiple_message_parts_are_one_response(self):
        done = event("response.done", response={"id": "reply1", "usage": {"total_tokens": 10}})
        result = score([tool(), tool(), audio(), audio(),
                        event("response.output_audio_transcript.done", response_id="reply1", item_id="m1", transcript="part1"),
                        event("response.output_audio_transcript.done", response_id="reply1", item_id="m2", transcript="part2"), done, done])
        self.assertEqual(result["agents"]["Ava"]["replies"], 1)
        self.assertEqual(result["agents"]["Ava"]["get_transcript_calls"], 1)
        self.assertEqual(result["agents"]["Ava"]["tokens"]["total_tokens"], 10)

    def test_utf8_size_and_measured_tool_latency(self):
        begin = dict(event("response.mcp_call.in_progress", item_id="tool1"), monotonic_ms=100)
        finish = dict(tool(output="é"), monotonic_ms=175)
        result = score([begin, finish])
        self.assertEqual(result["provider_tool_output_bytes"]["p50"], 2)
        self.assertEqual(result["provider_tool_latency_ms"]["p50"], 75)

    def test_played_intervals_measure_cross_agent_overlap(self):
        result = score([playback("started", "Ava", "a1", 100), playback("started", "Ben", "b1", 150),
                        playback("drained", "Ava", "a1", 200), playback("drained", "Ben", "b1", 300)])
        self.assertEqual(result["floor"]["violations"], 1)
        self.assertEqual(result["floor"]["observed_pairs"][0]["overlap_ms"], 50)

    def test_unclosed_queued_and_missing_playback_are_not_zero_violations(self):
        for rows in ([playback("started", "Ava", "a1", 100)],
                     [playback("started", "Ava", "a1", 100, basis="queued")],
                     [audio("b1", "Ben"), playback("started", "Ava", "a1", 100), playback("drained", "Ava", "a1", 200)]):
            self.assertIsNone(score(rows)["floor"]["violations"])

    def test_overlap_timing_uses_same_audio_timeline(self):
        result = score([{"attribution": {"overlap": "detected", "start_ms": 1000, "end_ms": 1500}},
                        audio(), playback("started", "Ava", "reply1", 5000, 4000)])
        self.assertEqual(result["agents"]["Ava"]["replies_within_3s_of_detected_overlap"], scorer.fraction(1, 1))

    def test_ground_truth_accuracy_overlap_and_named_handoff(self):
        turns = [{"speaker_id": "p1", "speaker_name": "Grant", "label": "high", "text": "hello"},
                 {"speaker_id": None, "label": "overlap", "text": "both"},
                 {"speaker_id": "p3", "speaker_name": "Ava", "source": "agent", "text": "Ben, over to you."},
                 {"speaker_id": "p4", "speaker_name": "Ben", "source": "agent", "text": "Thanks, Ava."}]
        result = score([{"utterance": value} for value in turns], truth=["Grant", "OVERLAP", "AGENT:Ava", "AGENT:Ben"])
        self.assertEqual(result["ground_truth"]["per_speaker"]["Grant"], scorer.fraction(1, 1))
        self.assertEqual(result["ground_truth"]["overlap_recall"], scorer.fraction(1, 1))
        self.assertEqual(result["ground_truth"]["named_agent_handoffs"], scorer.fraction(1, 1))
        with self.assertRaisesRegex(ValueError, "alignment mismatch"):
            score([{"utterance": turns[0]}], truth=[])

    def test_parallel_provider_sessions_share_explicit_run_id(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "log.jsonl"
            rows = [dict(event("session.created", agent), run_id="room-run") for agent in ("Ava", "Ben")]
            path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            self.assertEqual(list(scorer.read_runs(path)), ["room-run"])

    def test_cli_appends_valid_json_and_does_not_write_on_truth_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "runs.jsonl"
            with contextlib.redirect_stdout(io.StringIO()):
                scorer.main([str(FIXTURE), "--run", "legacy-3", "--output", str(output)])
            saved = output.read_text(encoding="utf-8")
            self.assertEqual(json.loads(saved)["agents"]["Agent 1"]["replies"], 5)
            turns = Path(directory) / "turns.txt"
            turns.write_text("Grant\n", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                scorer.main([str(FIXTURE), "--run", "legacy-3", "--turns", str(turns), "--output", str(output)])
            self.assertEqual(output.read_text(encoding="utf-8"), saved)


if __name__ == "__main__":
    unittest.main()
