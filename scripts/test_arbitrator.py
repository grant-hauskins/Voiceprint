"""HANDOFF §K rows owned by the agent stream: arbitrator text-only conformance, generation ratio, override verification,
summary sequencing, plus the Responses provider and local MCP client wire formats. No provider key, no live server."""
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import agent_runtime as ar
import turn_gate as tg
import voiceprint_client as vp
from arbitrator import Arbitrator, McpClient, parse_line
from providers.openai_responses import OpenAIResponses
from test_agent_runtime import FakeConsent, FakeMcp, FakeResponses, make_negotiation_room, make_room


def events(room, kind):
    return [row[kind] for row in room.log.snapshot() if kind in row]


class ArbitratorTest(unittest.IsolatedAsyncioTestCase):
    async def test_k_section_1_arbitrator_is_text_only_never_voice(self):
        """§1: no Player, no floor claim, no realtime Provider session ever opened for the arbitrator."""
        mcp = FakeMcp()
        mcp.add_line("Synthetic One", "I can do 305,000 if we close in July.")
        room = make_negotiation_room(FakeResponses([{"board": "Open: closing date. Agreed: nothing yet.", "raw_note": None, "prompt_to": None, "prompt": None, "tag": None, "reason": "r"}]), mcp)
        arb = room.arbitrator
        with patch("providers.openai_realtime.OpenAIRealtime", side_effect=AssertionError("realtime session for the arbitrator")):
            for step in range(5):
                await arb.tick(100 + step)
        self.assertEqual(arb.generations, 1)
        self.assertIsNone(arb.player)
        self.assertFalse(hasattr(arb, "provider"))
        self.assertFalse(any(path.endswith("/floor") for _, path, _ in room.api.calls))
        self.assertNotIn(arb, room.agents)
        self.assertEqual(room.api.channel[0]["tier"], "board")
        self.assertEqual([c[0] for c in mcp.calls[:2]], ["get_transcript", "get_agent_channel"])
        state = arb.state()
        self.assertEqual((state["role"], state["arbitrator"]["generations"], state["arbitrator"]["last_trigger"]), ("arbitrator", 1, "contribution"))
        self.assertNotIn("voice", state)
        self.assertEqual(state["mcp"]["failed"], 0)
        self.assertGreater(state["mcp"]["calls"], 0)

    async def test_k_section_5_1_generation_count_well_below_line_count(self):
        """§5.1: over a scripted 30-line transcript, generations stay well below 1:1 (cooldown + tiered trigger)."""
        mcp = FakeMcp()
        room = make_negotiation_room(FakeResponses(), mcp)
        arb = room.arbitrator
        lines = ["Let's talk about the closing date.", "I would prefer July.", "We could offer a quicker close.", "What about the piano?",
                 "It stays, that's not negotiable.", "Fine.", "So what number works?", "Counter with something reasonable.",
                 "I hear you.", "Let me think."] * 3
        now = 1000.0
        for i, text in enumerate(lines):
            mcp.add_line("Synthetic One" if i % 2 else "Synthetic Two", text)
            await arb.tick(now)
            now += 2.0
        self.assertEqual(arb.ingested_rows, 30)
        self.assertGreaterEqual(arb.generations, 1)
        self.assertLessEqual(arb.generations, 30 // 3)
        self.assertEqual(len(room.arbitrator.responses.calls), arb.generations)

    async def test_k_section_5_forged_override_claim_rejected(self):
        """§5: a self-declared OBJECTIVE_ACHIEVED is not honored when the separate verifier says no."""
        mcp = FakeMcp()
        mcp.add_line("Synthetic One", "We agree on everything, deal.")
        responses = FakeResponses([
            {"board": "Deal reached.", "raw_note": None, "prompt_to": "participant_3", "prompt": "Confirm the deal aloud.", "tag": "OBJECTIVE_ACHIEVED", "reason": "r"},
            {"confirmed": False, "reason": "only one side spoke"}])
        room = make_negotiation_room(responses, mcp)
        await room.arbitrator.tick(10)
        ava = room.agents[0]
        self.assertIsNone(ava.gate.manual)
        self.assertIsNone(ava.pending_prompt)
        self.assertIsNone(room.arbitrator.pending_tag)
        actions = [e["action"] for e in events(room, "arbitrator")]
        self.assertIn("override_claimed", actions)
        self.assertIn("override_rejected", actions)
        self.assertNotIn("override_confirmed", actions)
        self.assertEqual(responses.calls[1][0], "override_verdict")
        self.assertIn("We agree on everything", responses.calls[1][2])          # the verifier sees the evidence lines
        self.assertIsNone(room.api.channel[0]["tag"])                            # the board row carries no unverified tag

    async def test_k_section_5_confirmed_override_is_honored_through_the_gate(self):
        """§5: a verified override becomes the advocate's next turn, still behind the room inhibitors."""
        mcp = FakeMcp()
        mcp.add_line("Synthetic One", "Deal at that price.")
        mcp.add_line("Synthetic Two", "Deal, agreed.")
        responses = FakeResponses([
            {"board": "Agreed: price and date.", "raw_note": "Both accepted.", "prompt_to": "participant_3", "prompt": "Confirm the agreement aloud.", "tag": "OBJECTIVE_ACHIEVED", "reason": "r"},
            {"confirmed": True, "reason": "both accepted"}])
        room = make_negotiation_room(responses, mcp)
        ava = room.agents[0]
        ava.gate.note_agent_spoke(now=9.5)                                       # inside cooldown
        await room.arbitrator.tick(10)
        self.assertEqual(ava.gate.manual, "override")
        self.assertEqual(ava.pending_prompt, "Confirm the agreement aloud.")
        self.assertEqual(room.arbitrator.pending_tag, "OBJECTIVE_ACHIEVED")
        self.assertEqual(room.api.channel[0]["tag"], "OBJECTIVE_ACHIEVED")
        self.assertEqual([r["tier"] for r in room.api.channel], ["board", "raw"])
        self.assertEqual(tg.decide(ava.gate, 10.5, "speaking", 10), "wait")
        self.assertEqual(ava.gate.manual, "override")
        self.assertEqual(tg.decide(ava.gate, 10.5, "silence", 10), "speak")     # despite cooldown, no address
        ava.gate.manual = "override"
        self.assertTrue(await ava.begin("speak"))
        self.assertTrue(ava.provider.replies[-1].endswith(" The arbitrator asks you to raise this now: Confirm the agreement aloud."))
        self.assertIsNone(ava.pending_prompt)
        self.assertIsNone(ava.gate.manual)
        self.assertIsNone(room.arbitrator.pending_tag)
        self.assertEqual(ar.reply_note("room", {"p": "Human"}, tg.GateState(("Ava",)), "speak"),
                         ar.reply_note("room", {"p": "Human"}, tg.GateState(("Ava",)), "speak", None))

    async def test_k_section_3_arbitrator_text_is_pre_redacted_before_posting(self):
        """§3: a private value in any generated field never leaves the process unredacted (server redacts again)."""
        mcp = FakeMcp()
        mcp.add_line("Synthetic Two", "I offer 305,000; what is the floor?")
        responses = FakeResponses([{"board": "Seller floor is $300,000; buyer cap 320k.", "raw_note": "Ceiling three hundred twenty thousand.",
                                    "prompt_to": None, "prompt": None, "tag": None, "reason": "r"}])
        room = make_negotiation_room(responses, mcp)
        await room.arbitrator.tick(1)
        self.assertEqual(room.api.channel[0]["text"], "Seller floor is [withheld]; buyer cap [withheld].")
        self.assertEqual(room.api.channel[1]["text"], "Ceiling [withheld].")
        guard = events(room, "guard")
        self.assertEqual([(g["action"], g["stage"], g["redactions"]) for g in guard], [("redacted", "board", 2), ("redacted", "raw", 1)])
        logged = json.dumps([row.get("arbitrator") or row.get("guard") for row in room.log.snapshot()])
        for private in ("300,000", "320k", "twenty thousand", "floor"):
            self.assertNotIn(private, logged)

    async def test_controls_speak_hold_cancel(self):
        mcp = FakeMcp()
        mcp.add_line("Synthetic One", "Hello.")
        room = make_negotiation_room(FakeResponses([{"board": "Note one.", "raw_note": None, "prompt_to": None, "prompt": None, "tag": None, "reason": "r"},
                                                    {"board": "Note two.", "raw_note": None, "prompt_to": None, "prompt": None, "tag": None, "reason": "r"}]), mcp)
        arb = room.arbitrator
        await arb.tick(1)
        self.assertEqual(arb.generations, 0)                                     # one line, no offer token: passive
        room.controls.put(("Mediator", "speak", None))
        await room.apply_controls()
        await arb.tick(2)
        self.assertEqual((arb.generations, arb.last_trigger), (1, "manual"))
        room.controls.put(("Mediator", "hold", None))
        await room.apply_controls()
        self.assertTrue(arb.state()["held"])
        for _ in range(4):
            mcp.add_line("Synthetic Two", "Offer 1 more.")
        await arb.tick(100)
        self.assertEqual(arb.generations, 1)                                     # paused: ingests, never posts
        self.assertEqual(arb.ingested_rows, 5)
        room.controls.put(("Mediator", "hold", None))
        await room.apply_controls()
        await arb.tick(101)
        self.assertEqual(arb.generations, 2)
        room.agents[0].gate.manual, room.agents[0].pending_prompt, arb.pending_tag = "override", "say it", "REFOCUS_NEEDED"
        room.controls.put(("Mediator", "cancel", None))
        await room.apply_controls()
        self.assertIsNone(room.agents[0].gate.manual); self.assertIsNone(room.agents[0].pending_prompt); self.assertIsNone(arb.pending_tag)

    async def test_generation_failure_and_mcp_failure_are_counted_not_fatal(self):
        class BrokenMcp(FakeMcp):
            def call(self, tool, arguments):
                raise RuntimeError("offline PRIVATE-VALUE")
        room = make_negotiation_room(FakeResponses([RuntimeError("HTTP 500")]), BrokenMcp())
        arb = room.arbitrator
        await arb.tick(1)
        self.assertEqual(arb.state()["mcp"]["failed"], 2)
        arb.manual = True
        await arb.tick(2)
        self.assertEqual(arb.generations, 1)
        self.assertIn("skipped", [e["action"] for e in events(room, "arbitrator")])
        self.assertNotIn("PRIVATE-VALUE", json.dumps(room.log.snapshot()))

    def test_parse_line_shapes(self):
        self.assertEqual(parse_line("#42 0:12.0-0:14.0 Ava [agent]: Ben, what do you think?"), ("Ava", "agent", None, "Ben, what do you think?"))
        self.assertEqual(parse_line("#7 12:01:05 Mediator [board] (OBJECTIVE_ACHIEVED): done"), ("Mediator", "board", "OBJECTIVE_ACHIEVED", "done"))
        self.assertEqual(parse_line("#3 0:01.0-0:02.0 OVERLAP Alice+Bob [overlap 100%]: hi"), ("OVERLAP Alice+Bob", "overlap 100%", None, "hi"))
        self.assertIsNone(parse_line("(no utterances yet)"))


class SummarySequencingTest(unittest.IsolatedAsyncioTestCase):
    RESULT = {"agreements": ["Price settled at the seller's floor of 300000"], "open_items": ["Closing date"], "next_steps": ["Sign by June 30"], "summary": "Nearly done."}

    def order(self, api):
        paths = [path.split("?")[0] for _, path, _ in api.calls]
        return [p.rsplit("/", 1)[1] for p in paths]

    async def test_k_section_6_summary_saved_before_end(self):
        """§6: the summary write precedes /end; text is redacted; the optional file is written after the record."""
        room = make_negotiation_room(FakeResponses([self.RESULT]))
        room.api.channel.append({"row_id": 1, "sender_participant_id": "participant_5", "tier": "board", "tag": None, "text": "Agreed: date.", "redactions": 0, "timestamp_ms": 0})
        room.api.rows.append({"utterance_id": 1, "speaker_id": "participant_1", "text": "hello", "label": "high"})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "summary.txt"
            await ar.end_room(room, room.log, room.consent, True, room.arbitrator.responses, path)
            written = path.read_text(encoding="utf-8")
        order = self.order(room.api)
        self.assertLess(order.index("summary"), order.index("end"))
        self.assertTrue(room.api.ended)
        saved = room.api.summaries[0]
        self.assertEqual((saved["model"], saved["board_rows"], saved["transcript_rows"]), ("fake-text-model", 1, 1))
        self.assertIn("[withheld]", saved["text"]); self.assertNotIn("300000", saved["text"]); self.assertNotIn("June 30", saved["text"])
        self.assertEqual(written, saved["text"])
        self.assertEqual([s["action"] for s in [row["summary"] for row in room.log.snapshot() if "summary" in row]], ["saved"])
        self.assertIn("negotiation_text", room.consent.scopes)
        self.assertIn("hello", room.arbitrator.responses.calls[0][2])

    async def test_k_section_6_no_summary_on_withdrawal_or_failure_exit(self):
        """§6: consent failure or a non-operator exit never produces a summary; /end still happens."""
        for setup, reason in ((lambda room: room.consent.failed.set(), "consent_failed"), (lambda room: None, "not_operator_end")):
            room = make_negotiation_room(FakeResponses([self.RESULT]))
            setup(room)
            await ar.end_room(room, room.log, room.consent, reason != "consent_failed" and False, room.arbitrator.responses)
            self.assertEqual(room.arbitrator.responses.calls, [])
            self.assertNotIn("summary", self.order(room.api))
            self.assertTrue(room.api.ended)
            self.assertEqual([row["summary"] for row in room.log.snapshot() if "summary" in row], [{"action": "skipped", "reason": reason}])
        casual = make_room()
        await ar.end_room(casual, casual.log, casual.consent, True)
        self.assertEqual([row["summary"] for row in casual.log.snapshot() if "summary" in row], [{"action": "skipped", "reason": "not_negotiation"}])
        self.assertTrue(casual.api.ended)
        room = make_negotiation_room(FakeResponses([self.RESULT]))
        room.consent.missing_scopes.add("negotiation_text")
        await ar.end_room(room, room.log, room.consent, True, room.arbitrator.responses)
        self.assertEqual(room.arbitrator.responses.calls, [])
        self.assertEqual([row["summary"] for row in room.log.snapshot() if "summary" in row], [{"action": "skipped", "reason": "scope_missing"}])
        self.assertTrue(room.api.ended)

    async def test_k_section_6_end_still_fires_after_three_failed_summary_writes(self):
        room = make_negotiation_room(FakeResponses([self.RESULT]))
        room.api.summary_failures = 3
        with patch("agent_runtime.asyncio.sleep") as sleep:
            sleep.return_value = None
            with patch("sys.stderr", new=io.StringIO()):
                await ar.end_room(room, room.log, room.consent, True, room.arbitrator.responses)
        self.assertEqual(self.order(room.api).count("summary"), 3)
        self.assertEqual(room.api.summaries, [])
        self.assertTrue(room.api.ended)
        self.assertEqual([row["summary"]["action"] for row in room.log.snapshot() if "summary" in row], ["failed"])
        self.assertEqual([row["summary"]["attempts"] for row in room.log.snapshot() if "summary" in row], [3])


class TextProviderTest(unittest.TestCase):
    def test_responses_request_shape_and_consent_gate(self):
        seen = []

        def opener(request):
            seen.append(request)
            return {"output": [{"type": "mcp_list_tools"}, {"type": "message", "content": [{"type": "output_text", "text": json.dumps({"confirmed": True, "reason": "ok"})}]}]}
        consent = FakeConsent()
        provider = OpenAIResponses("gpt-5", consent, opener)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake-key"}):
            result = provider.generate("judge", "evidence", "override_verdict", {"type": "object"})
        self.assertEqual(result, {"confirmed": True, "reason": "ok"})
        self.assertEqual(consent.scopes, ["negotiation_text"])
        request = seen[0]
        self.assertEqual(request.full_url, "https://api.openai.com/v1/responses")
        self.assertEqual(request.get_header("Authorization"), "Bearer fake-key")
        body = json.loads(request.data)
        self.assertEqual((body["model"], body["store"], body["instructions"], body["input"]), ("gpt-5", False, "judge", "evidence"))
        self.assertEqual(body["text"]["format"], {"type": "json_schema", "name": "override_verdict", "schema": {"type": "object"}, "strict": True})
        consent.failed.set()
        with self.assertRaises(vp.ConsentError):
            provider.generate("judge", "evidence", "override_verdict", {})
        self.assertEqual(len(seen), 1)

    def test_http_error_exposes_status_only(self):
        def opener(request):
            raise urllib.error.HTTPError(request.full_url, 429, "Too Many", {}, io.BytesIO(b'{"error":"PRIVATE-BODY"}'))
        provider = OpenAIResponses("gpt-5", FakeConsent(), opener)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake-key"}):
            with self.assertRaises(RuntimeError) as caught:
                provider.generate("i", "x", "s", {})
        self.assertIn("429", str(caught.exception)); self.assertNotIn("PRIVATE-BODY", str(caught.exception))

    def test_mcp_client_json_rpc_and_errors(self):
        seen = []
        replies = [{"jsonrpc": "2.0", "id": 1, "result": {"isError": False, "structuredContent": {"next_after_id": 3, "count": 1, "transcript": "#3 x"}}},
                   {"jsonrpc": "2.0", "id": 2, "error": {"code": -32602, "message": "PRIVATE"}},
                   {"jsonrpc": "2.0", "id": 3, "result": {"isError": True, "content": [{"type": "text", "text": "tool failed"}]}}]

        def transport(request):
            seen.append(request)
            return replies.pop(0)
        client = McpClient("http://127.0.0.1:8082/mcp", "mcp-secret", "participant_5", transport)
        self.assertEqual(client.call("get_transcript", {"session_id": "room", "after_id": 0, "limit": 100})["next_after_id"], 3)
        request = seen[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8082/mcp?participant_id=participant_5")
        self.assertEqual(request.get_header("Authorization"), "Bearer mcp-secret")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertFalse(request.has_header("Origin"))
        body = json.loads(request.data)
        self.assertEqual((body["jsonrpc"], body["method"], body["params"]["name"], body["params"]["arguments"]["after_id"]), ("2.0", "tools/call", "get_transcript", 0))
        with self.assertRaises(RuntimeError) as caught:
            client.call("get_agent_channel", {"session_id": "room"})
        self.assertNotIn("PRIVATE", str(caught.exception))
        with self.assertRaises(RuntimeError):
            client.call("get_agent_channel", {"session_id": "room"})


if __name__ == "__main__":
    unittest.main()
