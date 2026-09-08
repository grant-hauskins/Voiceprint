import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import score_run as scorer


FIXTURE = Path(__file__).resolve().parents[1] / "evaluation" / "synthetic_regression_runs.jsonl"
V3_FIXTURE = Path(__file__).resolve().parents[1] / "evaluation" / "v3_events_sample.jsonl"


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


def arbitrator(action, agent="Mediator", **fields):
    value = {"action": action, "trigger": None, "tag": None, "confirmed": None, "ingested_rows": None,
             "generations": None, "tier": None, "redactions": None}
    value.update(fields)
    return {"agent": agent, "session_id": "synthetic_room", "arbitrator": value}


def arbitration_run(ingested, generations):
    """Cumulative counters as the runtime emits them: N ingested rows with generations spread across them."""
    rows = []
    every = max(1, ingested // generations) if generations else ingested + 1
    generated = 0
    for line in range(1, ingested + 1):
        rows.append(arbitrator("ingested", ingested_rows=line))
        if line % every == 0 and generated < generations:
            generated += 1
            rows.append(arbitrator("generated", trigger="contribution", generations=generated))
    return rows


def guard(action, stage, redactions=1, agent="Ava"):
    return {"agent": agent, "guard": {"action": action, "stage": stage, "redactions": redactions}}


def summary(action, attempts=1, board_rows=3, transcript_rows=61, reason=None):
    value = {"action": action, "attempts": attempts, "board_rows": board_rows, "transcript_rows": transcript_rows}
    if reason:
        value["reason"] = reason
    return {"agent": "Mediator", "summary": value}


class ArbitrationTests(unittest.TestCase):
    """HANDOFF_PROMPT §K rows owned by ws/eval; synthetic rows shaped like EventLog's v3 kinds."""

    def test_legacy_runs_keep_zero_blocks(self):
        result = score([tool(), audio()])
        self.assertEqual(result["arbitrator"], {"generations": 0, "ingested_rows": 0, "generation_ratio": scorer.fraction(0, 0),
                                                "posts": {"board": 0, "raw": 0}, "post_redactions": 0,
                                                "overrides": {"claimed": 0, "confirmed": 0, "rejected": 0}})
        self.assertEqual(result["guard"], {"spoken_cuts": 0, "stored_utterance_redactions": 0, "board_redactions": 0,
                                           "raw_redactions": 0, "summary_redactions": 0, "events": 0})
        self.assertEqual(result["summary"], {"saved": False, "attempts": None, "board_rows": None, "transcript_rows": None,
                                             "skipped_reason": None, "failed": False})
        rendered = scorer.render_aggregate(scorer.aggregate(result))
        self.assertIn("Arbitrator: 0 generations over 0 ingested rows (ratio NA); posts board 0 raw 0; overrides 0 claimed, 0 confirmed, 0 rejected.", rendered)
        self.assertIn("Guard: 0 spoken cuts; redactions stored 0, board 0, raw 0, summary 0.", rendered)
        self.assertIn("Summary: not recorded.", rendered)
        self.assertFalse([w for w in result["warnings"] if "Arbitrator" in w or "Override" in w or "summary" in w or "leak guard" in w])

    def test_arbitrator_does_not_generate_on_every_line(self):
        result = score(arbitration_run(30, 4))
        self.assertEqual(result["arbitrator"]["generation_ratio"], scorer.fraction(4, 30))
        self.assertFalse([w for w in result["warnings"] if "every ingested row" in w])
        self.assertIn("Arbitrator: 4 generations over 30 ingested rows (ratio 0.133)", scorer.render_aggregate(scorer.aggregate(result)))
        noisy = score(arbitration_run(5, 5))
        self.assertEqual(noisy["arbitrator"]["generation_ratio"]["ratio"], 1.0)
        self.assertIn("Arbitrator generated on every ingested row: BUILD_SPEC_V3 §5.1 expects far fewer generations than lines.", noisy["warnings"])

    def test_generations_without_ingested_rows_are_not_a_ratio(self):
        result = score([arbitrator("generated", trigger="manual", generations=1)])
        self.assertIsNone(result["arbitrator"]["generation_ratio"]["ratio"])
        self.assertFalse([w for w in result["warnings"] if "every ingested row" in w])

    def test_override_tags_verified_not_self_judged(self):
        rows = [arbitrator("override_claimed", trigger="OBJECTIVE_ACHIEVED", tag="OBJECTIVE_ACHIEVED"),
                arbitrator("override_confirmed", trigger="OBJECTIVE_ACHIEVED", tag="OBJECTIVE_ACHIEVED", confirmed=True),
                arbitrator("override_claimed", trigger="REFOCUS_NEEDED", tag="REFOCUS_NEEDED"),
                arbitrator("override_rejected", trigger="REFOCUS_NEEDED", tag="REFOCUS_NEEDED", confirmed=False)]
        result = score(rows)
        self.assertEqual(result["arbitrator"]["overrides"], {"claimed": 2, "confirmed": 1, "rejected": 1})
        self.assertNotIn("Override confirmed without a recorded claim", result["warnings"])
        self.assertIn("overrides 2 claimed, 1 confirmed, 1 rejected.", scorer.render_aggregate(scorer.aggregate(result)))
        unclaimed = score([arbitrator("override_confirmed", tag="OBJECTIVE_ACHIEVED", confirmed=True)])
        self.assertEqual(unclaimed["arbitrator"]["overrides"]["confirmed"], 1)
        self.assertIn("Override confirmed without a recorded claim", unclaimed["warnings"])
        # One claim cannot cover two verdicts.
        double = score([arbitrator("override_claimed", tag="OBJECTIVE_ACHIEVED"),
                        arbitrator("override_rejected", tag="OBJECTIVE_ACHIEVED", confirmed=False),
                        arbitrator("override_confirmed", tag="OBJECTIVE_ACHIEVED", confirmed=True)])
        self.assertIn("Override confirmed without a recorded claim", double["warnings"])

    def test_posts_are_counted_by_tier_with_redactions(self):
        result = score([arbitrator("posted", trigger="contribution", tier="board", redactions=1),
                        arbitrator("posted", trigger="contribution", tier="board", redactions=0),
                        arbitrator("posted", trigger="manual", tier="raw", redactions=2)])
        self.assertEqual(result["arbitrator"]["posts"], {"board": 2, "raw": 1})
        self.assertEqual(result["arbitrator"]["post_redactions"], 3)

    def test_notes_board_never_contains_a_private_value_guard_tallies_per_stage(self):
        result = score([guard("cut", "spoken_delta"), guard("cut", "spoken_delta"), guard("redacted", "stored_utterance", 2),
                        guard("redacted", "board", 1, "Mediator"), guard("redacted", "raw", 3, "Ben"), guard("redacted", "summary", 1, "Mediator")])
        self.assertEqual(result["guard"], {"spoken_cuts": 2, "stored_utterance_redactions": 2, "board_redactions": 1,
                                           "raw_redactions": 3, "summary_redactions": 1, "events": 6})
        self.assertIn("Advocate speech was cut by the leak guard 2 times: audio before the cut may have been heard.", result["warnings"])
        self.assertIn("Guard: 2 spoken cuts; redactions stored 2, board 1, raw 3, summary 1.", scorer.render_aggregate(scorer.aggregate(result)))
        self.assertFalse([w for w in score([guard("redacted", "board")])["warnings"] if "leak guard" in w])

    def test_summary_saved_before_end_never_on_withdrawal(self):
        saved = score([summary("saved")])
        self.assertEqual(saved["summary"], {"saved": True, "attempts": 1, "board_rows": 3, "transcript_rows": 61,
                                            "skipped_reason": None, "failed": False})
        self.assertIn("Summary: saved after 1 attempt (3 board rows, 61 transcript rows).", scorer.render_aggregate(scorer.aggregate(saved)))
        self.assertFalse([w for w in saved["warnings"] if "summary attempt failed" in w])
        for reason in ("not_negotiation", "consent_failed", "not_operator_end", "scope_missing"):
            skipped = score([summary("skipped", attempts=0, board_rows=0, transcript_rows=0, reason=reason)])
            self.assertEqual(skipped["summary"]["skipped_reason"], reason)
            self.assertFalse(skipped["summary"]["saved"])
            self.assertIn(f"Summary: skipped ({reason}).", scorer.render_aggregate(scorer.aggregate(skipped)))
        failed = score([summary("failed", attempts=3)])
        self.assertTrue(failed["summary"]["failed"])
        self.assertFalse(failed["summary"]["saved"])
        self.assertIn("Summary: failed after 3 attempts.", scorer.render_aggregate(scorer.aggregate(failed)))
        self.assertIn("A summary attempt failed: check that End conversation blocked on the write (BUILD_SPEC_V3 §6).", failed["warnings"])
        retried = score([summary("failed", attempts=1), summary("saved", attempts=2)])
        self.assertEqual((retried["summary"]["saved"], retried["summary"]["failed"], retried["summary"]["attempts"]), (True, True, 2))
        self.assertIn("Summary: saved after 2 attempts (3 board rows, 61 transcript rows).", scorer.render_aggregate(scorer.aggregate(retried)))

    def test_markdown_details_show_arbitration_table(self):
        rendered = scorer.render_markdown(score(arbitration_run(30, 4) + [guard("cut", "spoken_delta"), summary("saved")]))
        self.assertIn("| Arbitrator generations / ingested rows | 4/30 (13.3%) |", rendered)
        self.assertIn("| Guard spoken cuts | 1 |", rendered)
        self.assertIn("| Summary | saved after 1 attempt (3 board rows, 61 transcript rows) |", rendered)

    def test_aggregate_schema_3_has_v3_blocks_and_no_identifiers_or_text(self):
        rows = arbitration_run(30, 4) + [guard("cut", "spoken_delta"), summary("saved"),
                                         dict(tool(agent="Mediator"), session_id="synthetic_room", participant_id="participant_3"),
                                         {"utterance": {"utterance_id": 9, "speaker_id": "participant_1", "speaker_name": "Ava",
                                                        "source": "agent", "label": "agent", "text": "synthetic spoken words"}}]
        output = json.dumps(scorer.aggregate(score(rows)))
        parsed = json.loads(output)
        self.assertEqual(parsed["schema_version"], 3)
        self.assertEqual(set(parsed), {"schema_version", "scope", "agents", "floor_violations", "server_mcp_call_samples",
                                       "arbitrator", "guard", "summary"})
        self.assertEqual(parsed["arbitrator"]["generation_ratio"], scorer.fraction(4, 30))
        for forbidden in ("synthetic_room", "participant_1", "participant_3", "Ava", "Mediator", "unit", "synthetic spoken words",
                          "\"text\"", "utterance_id", "tool1", "trigger", "tag"):
            self.assertNotIn(forbidden, output)

    def test_ram_summary_scores_v3_kinds(self):
        rendered = scorer.summarize_events(arbitration_run(30, 4) + [
            arbitrator("override_claimed", tag="OBJECTIVE_ACHIEVED"), arbitrator("override_confirmed", tag="OBJECTIVE_ACHIEVED", confirmed=True),
            guard("redacted", "board", 1, "Mediator"), summary("saved")])
        self.assertIn("Arbitrator: 4 generations over 30 ingested rows (ratio 0.133); posts board 0 raw 0; overrides 1 claimed, 1 confirmed, 0 rejected.", rendered)
        self.assertIn("Guard: 0 spoken cuts; redactions stored 0, board 1, raw 0, summary 0.", rendered)
        self.assertIn("Summary: saved after 1 attempt (3 board rows, 61 transcript rows).", rendered)

    def test_v3_fixture_exercises_every_new_kind(self):
        runs = scorer.read_runs(V3_FIXTURE)
        self.assertEqual(list(runs), ["synthetic-v3"])
        rows = runs["synthetic-v3"]
        seen = {row["arbitrator"]["action"] for row in rows if "arbitrator" in row}
        self.assertEqual(seen, {"ingested", "generated", "posted", "override_claimed", "override_confirmed", "override_rejected", "skipped"})
        self.assertEqual({row["guard"]["stage"] for row in rows if "guard" in row}, {"spoken_delta", "stored_utterance", "board", "raw", "summary"})
        self.assertEqual([row["summary"]["action"] for row in rows if "summary" in row], ["failed", "saved"])
        for row in rows:
            self.assertEqual(row["session_id"], "synthetic_room")
            self.assertNotIn("text", json.dumps(row))
        result = scorer.score("synthetic-v3", rows)
        self.assertEqual(result["arbitrator"]["generation_ratio"], scorer.fraction(4, 30))
        self.assertEqual(result["arbitrator"]["posts"], {"board": 2, "raw": 1})
        self.assertEqual(result["arbitrator"]["overrides"], {"claimed": 2, "confirmed": 1, "rejected": 1})
        self.assertEqual(result["guard"]["events"], 5)
        self.assertEqual(result["summary"]["attempts"], 2)
        self.assertNotIn("Override confirmed without a recorded claim", result["warnings"])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            results = scorer.main(["--no-append", str(V3_FIXTURE)])
        self.assertEqual(len(results), 1)
        self.assertIn("Arbitrator: 4 generations over 30 ingested rows (ratio 0.133); posts board 2 raw 1; overrides 2 claimed, 1 confirmed, 1 rejected.", buffer.getvalue())
        self.assertIn("Summary: saved after 2 attempts (2 board rows, 30 transcript rows).", buffer.getvalue())
        self.assertNotIn("synthetic_room", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
