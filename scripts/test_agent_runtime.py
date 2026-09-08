"""No provider key, hardware, or human fixtures: floor, transport, controls and prior-consent regression tests."""
import asyncio
import base64
import dataclasses
import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import agent_runtime as ar
import turn_gate as tg
import voiceprint_client as vp
from providers import UnavailableProvider
from providers.openai_realtime import OpenAIRealtime, participant_url


class FakeConsent:
    def __init__(self):
        self.failed = threading.Event()
        self.scopes = []
        self.missing_scopes = set()
        self.valid_until = time.monotonic() + 100

    def require(self, scope="local_processing"):
        self.scopes.append(scope)
        if self.failed.is_set():
            raise vp.ConsentError("revoked")
        if scope in self.missing_scopes:
            raise vp.ConsentError(f"scope {scope} not released")
        return {}


class FakeApi:
    def __init__(self):
        self.holder = None
        self.rows = []
        self.calls = []
        self.fail_renew = False
        self.objectives = []            # GET .../objectives rows
        self.objectives_status = None   # e.g. 403 to refuse the read
        self.channel = []               # stored agent_channel rows
        self.summaries = []
        self.summary_failures = 0       # POST .../summary raises this many times first
        self.ended = False

    async def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path.endswith("/objectives") and method == "GET":
            if self.objectives_status:
                error = RuntimeError(f"HTTP {self.objectives_status}")
                error.status = self.objectives_status
                raise error
            return {"session_id": "room", "objectives": list(self.objectives)}
        if path.endswith("/agent_channel") and method == "POST":
            row = {"row_id": len(self.channel) + 1, "sender_participant_id": body["sender_participant_id"], "tier": body["tier"],
                   "tag": body.get("tag"), "text": body["text"], "redactions": 0, "timestamp_ms": 0}
            self.channel.append(row)
            return {"session_id": "room", "row_id": row["row_id"], "tier": row["tier"], "redactions": 0, "text": row["text"]}
        if "/agent_channel?" in path and method == "GET":
            after = int(path.split("after_id=")[1].split("&")[0])
            tier = path.split("tier=")[1].split("&")[0] if "tier=" in path else "all"
            rows = [r for r in self.channel if r["row_id"] > after and (tier == "all" or r["tier"] == tier)]
            return {"session_id": "room", "rows": rows, "next_after_id": rows[-1]["row_id"] if rows else after, "revealed": False,
                    "text": "\n".join(f"#{r['row_id']} 00:00:00 X [{r['tier']}]: {r['text']}" for r in rows)}
        if path.endswith("/summary") and method == "POST":
            if self.summary_failures:
                self.summary_failures -= 1
                raise RuntimeError("summary store unavailable")
            self.summaries.append(body)
            return {"session_id": "room", "created_ms": 1, "retention_deadline_ms": 2}
        if path.endswith("/end") and method == "POST":
            self.ended = True
            return {}
        if path.endswith("/floor") and method == "POST":
            pid = body["participant_id"]
            if self.fail_renew:
                raise RuntimeError("offline")
            granted = self.holder in (None, pid)
            if granted:
                self.holder = pid
            now = int(time.time() * 1000)
            return {"granted": granted, "held_by": self.holder, "server_time_ms": now, "expires_at_ms": now + 15000}
        if "/floor?" in path and method == "DELETE":
            released = self.holder == path.split("participant_id=")[1]
            if released:
                self.holder = None
            return {"released": released, "held_by": self.holder}
        if path.endswith("/participants"):
            return {"participant": body}
        if path.endswith("/utterances"):
            row = dict(body, utterance_id=len(self.rows) + 1, label="agent")
            self.rows.append(row)
            return row
        if "/utterances?" in path:
            after = int(path.split("after_id=")[1].split("&")[0])
            rows = [r for r in self.rows if r["utterance_id"] > after]
            return {"utterances": rows, "next_after_id": rows[-1]["utterance_id"] if rows else after}
        raise AssertionError((method, path, body))


class FakeProvider:
    def __init__(self):
        self.replies = []
        self.cancels = 0
        self.prompts = []
        self.objective = None
        self.principal_name = None

    async def request_reply(self, note=None):
        self.replies.append(note)

    async def cancel(self):
        self.cancels += 1

    async def update_instructions(self, prompt):
        self.prompts.append(prompt)


class FakeResponses:
    """Scripted Responses API: each generate() pops the next result (a dict, or an exception to raise)."""
    def __init__(self, results=()):
        self.results = list(results)
        self.calls = []
        self.model = "fake-text-model"

    def generate(self, instructions, input_text, schema_name, schema):
        self.calls.append((schema_name, instructions, input_text))
        if not self.results:
            return {"board": None, "raw_note": None, "prompt_to": None, "prompt": None, "tag": None, "reason": "nothing new"}
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeMcp:
    """Local MCP endpoint double: compact transcript/channel lines served by cursor, like McpServer's structuredContent."""
    def __init__(self):
        self.transcript = []      # (id, line)
        self.channel = []
        self.calls = []

    def add_line(self, speaker, text, label="high"):
        uid = len(self.transcript) + 1
        self.transcript.append((uid, f"#{uid} 0:{uid:02d}.0-0:{uid + 1:02d}.0 {speaker} [{label}]: {text}"))
        return uid

    def add_note(self, sender, text, tier="raw"):
        rid = len(self.channel) + 1
        self.channel.append((rid, f"#{rid} 12:00:{rid:02d} {sender} [{tier}]: {text}"))
        return rid

    def call(self, tool, arguments):
        self.calls.append((tool, arguments))
        rows, key = (self.transcript, "transcript") if tool == "get_transcript" else (self.channel, "channel")
        after = arguments.get("after_id", 0)
        page = [(i, line) for i, line in rows if i > after][:arguments.get("limit", 100)]
        return {"session_id": arguments["session_id"], "next_after_id": page[-1][0] if page else after, "count": len(page),
                key: "\n".join(line for _, line in page) or "(no utterances yet)"}


class FakePlayer:
    def __init__(self):
        self.playing = False
        self.chunks = []

    def busy(self):
        return self.playing

    def play(self, pcm):
        self.chunks.append(pcm)
        self.playing = True

    def flush(self):
        self.playing = False
        self.chunks.clear()


def make_room():
    configs = [ar.AgentConfig("Ava"), ar.AgentConfig("Ben", voice="cedar", eagerness="quiet")]
    room = ar.Room(configs, "room", {"participant_1": "Human", "participant_3": "Ava", "participant_4": "Ben"},
                   FakeApi(), ar.EventLog(None, "room"), FakeConsent())
    room.agents = [ar.Agent(c, f"participant_{i}", FakeProvider(), FakePlayer(), room) for c, i in zip(configs, (3, 4))]
    return room


OBJECTIVE_ROWS = [
    {"principal_id": "participant_1", "version": 1, "position": "wants to sell the property", "source": "typed", "trigger": "initial", "created_ms": 1,
     "constraints": [{"label": "floor", "value": "300000"}, {"label": "close by", "value": "June 30"}]},
    {"principal_id": "participant_2", "version": 1, "position": "wants to buy the property", "source": "typed", "trigger": "initial", "created_ms": 2,
     "constraints": [{"label": "ceiling", "value": "320000"}]}]


def make_negotiation_room(responses=None, mcp=None):
    """Two humans, two advocates (Ava for One, Ben for Two) and a text-only arbitrator, with objectives already read."""
    import objectives as ob
    from arbitrator import Arbitrator
    configs = [ar.AgentConfig("Ava", speaks_for="Synthetic One"), ar.AgentConfig("Ben", voice="cedar", speaks_for="Synthetic Two"),
               ar.with_role(ar.AgentConfig("Mediator"), "arbitrator")]
    names = {"participant_1": "Synthetic One", "participant_2": "Synthetic Two", "participant_3": "Ava", "participant_4": "Ben", "participant_5": "Mediator"}
    api = FakeApi()
    api.objectives = [dict(row) for row in OBJECTIVE_ROWS]
    room = ar.Room(configs, "room", names, api, ar.EventLog(None, "room"), FakeConsent(), "negotiation")
    room.objectives = ob.parse_objectives({"objectives": api.objectives})
    room.agents = [ar.Agent(c, f"participant_{i}", FakeProvider(), FakePlayer(), room) for c, i in zip(configs[:2], (3, 4))]
    room.arbitrator = Arbitrator(configs[2], "participant_5", room, responses or FakeResponses(), mcp or FakeMcp(), poll_s=0)
    room.arbitrator.objectives = room.objectives
    return room


class RuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_floor_first_manual_denial_preserves_request(self):
        room = make_room()
        ava, ben = room.agents
        room.api.holder = ben.participant_id
        ava.gate.manual = "speak"
        await ava.tick(time.monotonic())
        self.assertEqual(ava.gate.manual, "speak")
        self.assertEqual(ava.provider.replies, [])
        room.api.holder = None
        await ava.tick(time.monotonic())
        self.assertEqual(room.api.holder, ava.participant_id)
        self.assertEqual(len(ava.provider.replies), 1)

    async def test_two_simultaneous_claims_have_one_winner(self):
        room = make_room()
        result = await asyncio.gather(*(a.begin("speak") for a in room.agents))
        self.assertEqual(sum(result), 1)
        self.assertEqual(sum(len(a.provider.replies) for a in room.agents), 1)

    async def test_transcriber_idle_before_manual_speak(self):
        room = make_room()
        room.transcriber_idle = lambda: False
        room.agents[0].gate.manual = "speak"
        await room.agents[0].tick(time.monotonic())
        self.assertEqual(room.api.calls, [])

    async def test_tool_continuation_waits_completion_and_keeps_floor(self):
        room = make_room()
        agent = room.agents[0]
        await agent.begin("speak")
        await agent.event({"type": "response.mcp_call.in_progress", "response_id": "r1"})
        await agent.event({"type": "response.done", "response": {"id": "r1", "output": [{"type": "mcp_call"}]}})
        await agent.tick(time.monotonic())
        self.assertEqual(len(agent.provider.replies), 1)
        self.assertEqual(room.api.holder, agent.participant_id)
        await agent.event({"type": "response.mcp_call.completed", "response_id": "r1"})
        await agent.tick(time.monotonic())
        self.assertEqual(agent.provider.replies[-1], None)
        self.assertEqual(len(agent.provider.replies), 2)
        self.assertTrue(agent.floor_owned)

    async def test_provider_declared_mcp_outcomes_are_tracked_without_content(self):
        room = make_room()
        agent = room.agents[0]
        await agent.begin("speak")
        self.assertEqual(agent.state()["mcp"], {"list_tools": "unknown", "tools": [], "calls": 0, "failed": 0, "last_error": None})
        await agent.event({"type": "response.output_item.done", "response_id": "r1", "item": {"type": "mcp_list_tools", "status": "failed", "error": {"type": "http_error", "code": 502, "message": "Bad gateway☃"}}})
        mcp = agent.state()["mcp"]
        self.assertEqual(mcp["list_tools"], "failed"); self.assertIn("502", mcp["last_error"]); self.assertNotIn("☃", mcp["last_error"])
        await agent.event({"type": "response.output_item.done", "response_id": "r1", "item": {"type": "mcp_list_tools", "status": "completed", "tools": [{"name": "get_transcript"}, {"name": "get_current_speaker"}, {"name": "secret_tool"}]}})
        mcp = agent.state()["mcp"]
        self.assertEqual((mcp["list_tools"], mcp["tools"], mcp["last_error"]), ("ok", ["get_transcript", "get_current_speaker"], None))
        await agent.event({"type": "response.output_item.done", "response_id": "r1", "item": {"type": "mcp_call", "name": "get_transcript", "arguments": "{\"session_id\":\"room\"}", "output": "#1 lines"}})
        await agent.event({"type": "response.output_item.done", "response_id": "r1", "item": {"type": "mcp_call", "name": "get_transcript", "error": "Tool call failed: 403"}})
        mcp = agent.state()["mcp"]
        self.assertEqual((mcp["calls"], mcp["failed"], mcp["last_error"]), (2, 1, "Tool call failed: 403"))
        self.assertNotIn("#1 lines", json.dumps(agent.state()))          # outputs never reach the control API

    async def test_floor_released_only_after_done_and_drain(self):
        room = make_room()
        agent = room.agents[0]
        await agent.begin("speak")
        await agent.event({"type": "response.output_audio.delta", "response_id": "r", "delta": base64.b64encode(b"\0\0").decode()})
        await agent.event({"type": "response.done", "response": {"id": "r", "output": [{"type": "message"}]}})
        await agent.tick(time.monotonic())
        self.assertTrue(agent.floor_owned)
        agent.player.playing = False
        await agent.tick(time.monotonic())
        self.assertIsNone(room.api.holder)
        self.assertFalse(agent.active)

    async def test_renewal_loss_cancels_and_ignores_late_audio(self):
        room = make_room()
        agent = room.agents[0]
        await agent.begin("speak")
        await agent.event({"type": "response.created", "response": {"id": "r"}})
        room.api.fail_renew = True
        agent.renew_at = 0
        await agent.tick(time.monotonic())
        await agent.event({"type": "response.output_audio.delta", "response_id": "r", "delta": "AAA="})
        self.assertEqual(agent.provider.cancels, 1)
        self.assertEqual(agent.player.chunks, [])
        self.assertFalse(agent.floor_owned)

    async def test_cancel_resets_pending_tool_continuation(self):
        room = make_room()
        agent = room.agents[0]
        await agent.begin("speak")
        agent.continue_pending = True
        await agent.cancel()
        await agent.tick(time.monotonic())
        self.assertFalse(agent.continue_pending)
        self.assertEqual(len(agent.provider.replies), 1)
        self.assertIsNone(room.api.holder)

    async def test_begin_names_the_transcript_spelling_when_addressed_inexactly(self):
        configs = [ar.AgentConfig("Ryan")]
        room = ar.Room(configs, "room", {"participant_1": "Human", "participant_3": "Ryan"}, FakeApi(), ar.EventLog(None, "room"), FakeConsent())
        ryan = ar.Agent(configs[0], "participant_3", FakeProvider(), FakePlayer(), room)
        room.agents = [ryan]
        ryan.gate.note_utterance({"speaker_id": "participant_1", "label": "high", "text": "Brian, what do you think?"}, 10)
        self.assertEqual(tg.decide(ryan.gate, 10.5, "silence", 10), "speak")
        self.assertTrue(await ryan.begin("speak"))
        self.assertTrue(ryan.provider.replies[0].endswith(" The transcript wrote your name as 'Brian'; that line is addressed to you."))
        await ryan.release()
        ryan.active = False
        ryan.gate.note_utterance({"speaker_id": "participant_1", "label": "high", "text": "Ryan, and the time?"}, 20)
        self.assertTrue(await ryan.begin("speak"))
        self.assertNotIn("wrote your name", ryan.provider.replies[1])

    async def test_reviewed_rows_reach_the_gate_as_reliable_human_lines(self):
        room = make_room()
        row = {"speaker_id": "participant_1", "text": "Ava, what should we order?", "label": "reviewed", "original_text": "Eva, what should we order?",
               "original_speaker_id": "participant_2", "reviewed_ms": 5, "source": "human", "start_ms": 0, "end_ms": 900}
        room.api.rows.append(dict(row, utterance_id=1))
        await room.poll_bus()
        ava = room.agents[0]
        self.assertEqual(ava.gate.history[-1]["label"], "reviewed")
        self.assertEqual(tg.decide(ava.gate, ava.gate.history[-1]["seen_at"] + 0.5, "silence", ava.gate.history[-1]["seen_at"]), "speak")

    async def test_agent_utterance_only_reaches_gate_through_stored_bus(self):
        room = make_room()
        agent = room.agents[0]
        await agent.begin("speak")
        room.timeline_ms = 1000
        await agent.event({"type": "response.created", "response": {"id": "r"}})
        room.timeline_ms = 2000
        event = {"type": "response.output_audio_transcript.done", "response_id": "r", "item_id": "item", "transcript": "Ben, what do you think?"}
        await agent.event(event)
        self.assertEqual(room.agents[1].gate.history, [])
        await room.poll_bus()
        await room.poll_bus()
        await agent.event(event)
        self.assertEqual(len(room.agents[1].gate.history), 1)
        self.assertEqual(len(room.api.rows), 1)
        self.assertEqual(room.api.rows[0]["source"], "agent")
        self.assertEqual((room.api.rows[0]["start_ms"], room.api.rows[0]["end_ms"]), (1000, 2000))

    async def test_controls_hold_speak_and_eagerness(self):
        room = make_room()
        room.controls.put(("Ben", "hold", None))
        await room.apply_controls()
        self.assertTrue(room.agents[1].state()["held"])
        room.controls.put(("Ben", "speak", None))
        room.controls.put(("Ben", "eagerness", "eager"))
        await room.apply_controls()
        self.assertFalse(room.agents[1].state()["held"])
        self.assertEqual(room.agents[1].gate.manual, "speak")
        self.assertEqual(room.agents[1].gate.eagerness, "eager")

    async def test_k_section_4_guard_cuts_spoken_leak_and_redacts_stored_text(self):
        """§4 (runtime mirror): an advocate's streamed transcript that spells its own constraint is cut and never stored;
        a finished transcript is stored redacted. The other side's values are not held at all."""
        room = make_negotiation_room()
        ava, ben = room.agents
        self.assertEqual(ava.guard_values, ("300000", "June 30"))
        self.assertEqual(ben.guard_values, ("320000",))
        self.assertTrue(ava.gate.raise_hand)                                     # negotiation advocate: raise-hand by default
        await ava.begin("speak")
        await ava.event({"type": "response.created", "response": {"id": "r1"}})
        await ava.event({"type": "response.output_audio.delta", "response_id": "r1", "delta": base64.b64encode(b"\0\0").decode()})
        self.assertTrue(ava.player.playing)
        await ava.event({"type": "response.output_audio_transcript.delta", "response_id": "r1", "item_id": "i", "delta": "Our floor is three hundred"})
        self.assertEqual(ava.provider.cancels, 0)
        await ava.event({"type": "response.output_audio_transcript.delta", "response_id": "r1", "item_id": "i", "delta": " thousand dollars."})
        self.assertEqual(ava.provider.cancels, 1)
        self.assertFalse(ava.player.playing)
        await ava.event({"type": "response.output_audio_transcript.done", "response_id": "r1", "item_id": "i", "transcript": "Our floor is three hundred thousand dollars."})
        self.assertEqual(room.api.rows, [])
        guard = [row["guard"] for row in room.log.snapshot() if "guard" in row]
        self.assertEqual(guard, [{"action": "cut", "stage": "spoken_delta", "redactions": 1}])
        self.assertNotIn("three hundred", json.dumps(room.log.snapshot()))
        room.api.holder = None
        await ava.tick(time.monotonic()); ava.active = False; ava.final_done = False
        await ava.begin("speak")
        await ava.event({"type": "response.created", "response": {"id": "r2"}})
        await ava.event({"type": "response.output_audio_transcript.delta", "response_id": "r2", "item_id": "j", "delta": "We could close by June 15, 320000 is fine."})
        self.assertEqual(ava.provider.cancels, 1)                                # the other side's value is not Ava's to guard
        await ava.event({"type": "response.output_audio_transcript.done", "response_id": "r2", "item_id": "j", "transcript": "$300,000 by June 30 then."})
        self.assertEqual(room.api.rows[0]["text"], "[withheld] by [withheld] then.")
        guard = [row["guard"] for row in room.log.snapshot() if "guard" in row]
        self.assertEqual(guard[-1], {"action": "redacted", "stage": "stored_utterance", "redactions": 2})
        casual = make_room().agents[0]
        self.assertEqual(casual.guard_values, ()); self.assertFalse(casual.gate.raise_hand)

    async def test_objective_version_change_reprompts_only_that_advocate(self):
        room = make_negotiation_room()
        ava, ben = room.agents
        room.api.objectives[0] = dict(OBJECTIVE_ROWS[0], version=2, constraints=[{"label": "floor", "value": "310000"}], trigger="offer heard")
        await room.refresh_objectives()
        self.assertEqual(len(ava.provider.prompts), 1)
        self.assertIn("310000", ava.provider.prompts[0]); self.assertNotIn("300000", ava.provider.prompts[0])
        self.assertIn("Synthetic One's personal agent", ava.provider.prompts[0])
        from realtime_openai import SHARED_INSTRUCTIONS
        self.assertIn(SHARED_INSTRUCTIONS, ava.provider.prompts[0])
        self.assertEqual(ava.guard_values, ("310000",))
        self.assertEqual(ben.provider.prompts, [])
        self.assertEqual(room.arbitrator.objectives["participant_1"].version, 2)
        await room.refresh_objectives()
        self.assertEqual(len(ava.provider.prompts), 1)                           # same version: no re-prompt
        room.api.objectives_status = 403
        room.objectives_forbidden = False
        await room.refresh_objectives()
        self.assertTrue(room.objectives_forbidden)
        self.assertNotIn("310000", json.dumps(room.log.snapshot()))

    async def test_arbitrator_state_and_routing_in_room(self):
        room = make_negotiation_room()
        state = room.state()
        self.assertEqual(state["conversation_type"], "negotiation")
        self.assertEqual([a["name"] for a in state["agents"]], ["Ava", "Ben", "Mediator"])
        self.assertEqual([a["role"] for a in state["agents"]], ["voice", "voice", "arbitrator"])
        self.assertEqual(sorted(state["agents"][2]["arbitrator"]), ["cooldown_until_ms", "generations", "ingested_rows", "last_trigger", "paused", "pending_tag"])
        self.assertNotIn("voice", state["agents"][2])
        self.assertFalse(room.request_override("participant_9", "x", "REFOCUS_NEEDED"))
        room.agents[1].gate.manual = "hold"
        self.assertFalse(room.request_override("Ben", "x", "REFOCUS_NEEDED"))     # hold is the operator's decision


class ControlsEndToEndTest(unittest.IsolatedAsyncioTestCase):
    """Hold, Cancel and Speak from the console travel the real path: loopback HTTP -> room.controls -> apply_controls ->
    Agent.cancel/tick (floor, provider, player) or Arbitrator.control."""

    def start_server(self, room):
        self.server = ar.control_server(room, port=0, token="test-bearer")
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def post(self, name, action):
        request = urllib.request.Request(f"{self.base}/agents/{name}/control", json.dumps({"action": action}).encode(),
                                         {"Authorization": "Bearer test-bearer"}, method="POST")
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, json.load(response)

    async def mid_reply(self, agent):
        """A reply in flight: floor held, response created, audio playing."""
        self.assertTrue(await agent.begin("speak"))
        await agent.event({"type": "response.created", "response": {"id": "r1"}})
        await agent.event({"type": "response.output_audio.delta", "response_id": "r1", "delta": base64.b64encode(b"\x01\x02" * 400).decode()})
        self.assertTrue(agent.active and agent.floor_owned and agent.player.busy() and agent.player.chunks)
        self.assertEqual(agent.room.api.holder, agent.participant_id)

    def address(self, agent, text, now):
        agent.gate.note_utterance({"speaker_id": "participant_1", "label": "high", "text": text}, now)
        agent.room.last_end_at = now

    async def test_hold_button_cuts_the_reply_and_mutes_until_pressed_again(self):
        room = make_room()
        self.start_server(room)
        ava = room.agents[0]
        await self.mid_reply(ava)
        status, body = self.post("Ava", "hold")
        self.assertEqual(status, 200)
        await room.apply_controls()
        self.assertEqual(ava.provider.cancels, 1)
        self.assertEqual(ava.player.chunks, []); self.assertFalse(ava.player.busy())
        self.assertEqual(ava.gate.manual, "hold"); self.assertTrue(ava.state()["held"])
        self.assertTrue(ava.floor_owned)                                     # released by the next tick, not by cancel itself
        await ava.tick(time.monotonic())
        self.assertFalse(ava.active); self.assertFalse(ava.floor_owned); self.assertIsNone(room.api.holder)
        self.assertIn(("DELETE", room.path + "/floor?participant_id=participant_3", None), room.api.calls)
        now = time.monotonic()
        self.address(ava, "Ava, what do you think?", now)
        await ava.tick(now + 1)
        self.assertEqual(len(ava.provider.replies), 1)                       # still held: no new reply
        self.assertTrue(ava.state()["held"])
        status, body = self.post("Ava", "hold")
        self.assertEqual((status, body["agent"]["held"]), (200, True))         # state before the queued toggle applies
        await room.apply_controls()
        self.assertIsNone(ava.gate.manual); self.assertFalse(ava.state()["held"])
        self.assertEqual(ava.provider.cancels, 1)                            # releasing hold cancels nothing
        await ava.tick(now + 1.5)
        self.assertEqual(len(ava.provider.replies), 2)                       # the standing address is answered once released
        self.assertEqual([e["control"]["action"] for e in room.log.snapshot() if "control" in e], ["cancel", "hold", "hold"])

    async def test_cancel_button_cuts_the_reply_and_leaves_the_gate_open(self):
        room = make_room()
        self.start_server(room)
        ava = room.agents[0]
        await self.mid_reply(ava)
        self.assertEqual(self.post("Ava", "cancel")[0], 200)
        await room.apply_controls()
        self.assertEqual(ava.provider.cancels, 1)
        self.assertEqual(ava.player.chunks, []); self.assertFalse(ava.player.busy())
        self.assertIsNone(ava.gate.manual); self.assertFalse(ava.state()["held"])
        await ava.tick(time.monotonic())
        self.assertFalse(ava.active); self.assertFalse(ava.floor_owned); self.assertIsNone(room.api.holder)
        await ava.event({"type": "response.done", "response_id": "r1", "response": {"id": "r1", "status": "cancelled", "output": []}})
        self.assertEqual(ava.provider.cancels, 1)                            # the cancelled response's tail is ignored
        now = time.monotonic()
        self.address(ava, "Ava, are you there?", now)
        await ava.tick(now + 1)
        self.assertEqual(len(ava.provider.replies), 2)
        self.assertTrue(ava.active and ava.floor_owned)
        self.assertEqual(room.api.holder, ava.participant_id)

    async def test_hold_and_cancel_while_idle_are_harmless(self):
        room = make_room()
        self.start_server(room)
        ava = room.agents[0]
        self.assertEqual(self.post("Ava", "cancel")[0], 200)
        self.assertEqual(self.post("Ava", "hold")[0], 200)
        self.assertEqual(self.post("Ava", "hold")[0], 200)
        await room.apply_controls()
        self.assertEqual(ava.provider.cancels, 0)
        self.assertFalse(ava.active); self.assertFalse(ava.floor_owned); self.assertIsNone(ava.gate.manual)
        self.assertFalse(any(path.endswith("/floor") for _, path, _ in room.api.calls))
        now = time.monotonic()
        self.address(ava, "Ava, hello?", now)
        await ava.tick(now + 1)
        self.assertEqual(len(ava.provider.replies), 1)                       # an idle cancel never blocks the next reply

    async def test_arbitrator_hold_pauses_cancel_drops_override_speak_generates(self):
        room = make_negotiation_room(FakeResponses(), FakeMcp())
        self.start_server(room)
        arb, ava = room.arbitrator, room.agents[0]
        for text in ("Let's talk about the closing date.", "I would prefer July.", "We could offer a quicker close."):
            arb.mcp.add_line("Synthetic One", text)
        self.assertEqual(self.post("Mediator", "hold")[0], 200)
        await room.apply_controls()
        self.assertTrue(arb.paused and arb.state()["held"])
        await arb.tick(100.0)
        self.assertEqual((arb.generations, arb.ingested_rows), (0, 3))       # ingested, never generated while held
        self.assertEqual(self.post("Mediator", "hold")[0], 200)
        await room.apply_controls()
        self.assertFalse(arb.paused)
        await arb.tick(101.0)
        self.assertEqual(arb.generations, 1)                                 # the three new lines were still pending
        self.assertTrue(room.request_override("participant_3", "raise the date", "OBJECTIVE_ACHIEVED"))
        arb.pending_tag = "OBJECTIVE_ACHIEVED"
        self.assertEqual((ava.gate.manual, ava.pending_prompt), ("override", "raise the date"))
        self.assertEqual(self.post("Mediator", "cancel")[0], 200)
        await room.apply_controls()
        self.assertIsNone(arb.pending_tag); self.assertIsNone(ava.gate.manual); self.assertIsNone(ava.pending_prompt)
        await arb.tick(102.0)
        self.assertEqual(arb.generations, 1)                                 # inside the 20 s cooldown
        self.assertEqual(self.post("Mediator", "speak")[0], 200)
        await room.apply_controls()
        await arb.tick(103.0)
        self.assertEqual((arb.generations, arb.last_trigger), (2, "manual"))  # speak ignores the cooldown
        self.assertEqual([e["control"]["action"] for e in room.log.snapshot() if "control" in e], ["hold", "hold", "override", "cancel", "speak"])

    async def test_cancel_after_the_response_finished_does_not_loop_on_the_provider_error(self):
        """response.cancel after response.done draws an error (response_cancel_not_active); it must not trigger another cancel."""
        room = make_room()
        ava = room.agents[0]
        await self.mid_reply(ava)
        await ava.event({"type": "response.done", "response_id": "r1",
                         "response": {"id": "r1", "status": "completed", "output": [{"type": "message"}]}})
        self.assertTrue(ava.active and ava.final_done and ava.player.busy())    # audio still playing after the model finished
        room.controls.put(("Ava", "cancel", None))
        await room.apply_controls()
        self.assertEqual(ava.provider.cancels, 1)
        with patch("sys.stderr", new=io.StringIO()):
            await ava.event({"type": "error", "error": {"type": "invalid_request_error", "code": "response_cancel_not_active"}})
            await ava.event({"type": "error", "error": {"type": "invalid_request_error", "code": "response_cancel_not_active"}})
        self.assertEqual(ava.provider.cancels, 1)                            # no second response.cancel, no ping-pong
        await ava.tick(time.monotonic())
        self.assertFalse(ava.active); self.assertFalse(ava.floor_owned); self.assertIsNone(room.api.holder)
        with patch("sys.stderr", new=io.StringIO()):
            await ava.event({"type": "error", "error": {"code": "response_cancel_not_active"}})
        self.assertEqual(ava.provider.cancels, 1)
        now = time.monotonic()
        self.address(ava, "Ava, one more?", now + 10)
        await ava.tick(now + 11)
        self.assertEqual(len(ava.provider.replies), 2)                       # not stuck: a fresh address is answered


class GateAndConfigTest(unittest.TestCase):
    def test_persona_prompt_and_toml_fields(self):
        from realtime_openai import persona
        self.assertEqual(persona("Ava"), "")
        text = persona("Ben", "Grant Hauskins", "Answer in haiku.")
        self.assertIn("Grant Hauskins's personal agent", text); self.assertIn("Do not speak for anyone else", text); self.assertTrue(text.endswith("Answer in haiku."))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agents.toml"
            path.write_text('[[agents]]\nname = "Ava"\nspeaks_for = "Grant Hauskins"\ninstructions_extra = "Be terse."\n', encoding="utf-8")
            config = ar.load_config(path)[0]
            self.assertEqual((config.speaks_for, config.instructions_extra), ("Grant Hauskins", "Be terse."))
            path.write_text('[[agents]]\nname = "Ava"\nspeaks_for = 3\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                ar.load_config(path)

    def test_default_configuration(self):
        configs = ar.load_config(Path(ar.__file__).with_name("agents.toml"))
        self.assertEqual([(c.name, c.voice, c.eagerness) for c in configs], [("Ava", "marin", "balanced"), ("Ben", "cedar", "quiet")])
        self.assertEqual([c.role for c in configs], ["voice", "voice"])

    def test_uniform_instruction_layer_and_objective_injection(self):
        """BUILD_SPEC_V3 §0 #1: every agent gets the shared layer; only an advocate gets its own objective."""
        import objectives as ob
        from realtime_openai import SHARED_INSTRUCTIONS, compose_prompt, instructions, persona
        self.assertEqual(persona("Ava"), "")
        objective = ob.Objective("participant_1", 3, "wants to sell", (("floor", "300000"),), "typed", "initial", 0)
        text = persona("Ava", "Synthetic One", "Be terse.", objective, "Synthetic One")
        self.assertIn("Be terse.", text); self.assertTrue(text.endswith("proposal instead."))
        self.assertIn("- floor: 300000", text); self.assertIn("NEVER state", text)
        config = ar.AgentConfig("Ava", speaks_for="Synthetic One")
        prompt = compose_prompt(config, "room", {"participant_1": "Synthetic One"}, objective, "Synthetic One")
        room_rules = instructions("Ava", "room", {"participant_1": "Synthetic One"})
        self.assertTrue(prompt.startswith(room_rules + "\n\n" + SHARED_INSTRUCTIONS + "\n\n"))
        self.assertIn("get_agent_channel", SHARED_INSTRUCTIONS); self.assertIn("post_agent_channel", SHARED_INSTRUCTIONS)
        plain = compose_prompt(ar.AgentConfig("Ava"), "room", {})
        self.assertTrue(plain.endswith(SHARED_INSTRUCTIONS))

    def test_role_configuration_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agents.toml"
            path.write_text('[[agents]]\nname = "Mediator"\nrole = "arbitrator"\nprovider = "openai_responses"\nmodel = "gpt-5"\n', encoding="utf-8")
            config = ar.load_config(path)[0]
            self.assertEqual((config.role, config.provider, config.model), ("arbitrator", "openai_responses", "gpt-5"))
            for content in ('[[agents]]\nname = "Mediator"\nrole = "arbitrator"\n',                                   # realtime provider for an arbitrator
                            '[[agents]]\nname = "Ava"\nprovider = "openai_responses"\n',                              # text provider for a voice agent
                            '[[agents]]\nname = "Mediator"\nrole = "judge"\n',
                            '[[agents]]\nname = "Mediator"\nrole = "arbitrator"\nprovider = "openai_responses"\nspeaks_for = "Grant"\n'):
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    ar.load_config(path)
        arbitrator = ar.with_role(ar.AgentConfig("Mediator"), "arbitrator")
        self.assertEqual((arbitrator.provider, arbitrator.model), ("openai_responses", "gpt-5"))
        back = ar.with_role(arbitrator, "voice")
        self.assertEqual((back.provider, back.model), ("openai_realtime", "gpt-realtime-2.1"))
        self.assertEqual(ar.persona_record(arbitrator)["role"], "arbitrator")
        with tempfile.TemporaryDirectory() as tmp, patch.object(ar, "PERSONA_DIR", Path(tmp)):
            ar.save_personas([arbitrator])
            self.assertEqual(ar.load_personas()["Mediator"]["role"], "arbitrator")
            merged = ar.merge_personas([ar.AgentConfig("Ava")])
            self.assertEqual([(c.name, c.role, c.provider) for c in merged], [("Ava", "voice", "openai_realtime"), ("Mediator", "arbitrator", "openai_responses")])

    def test_room_validation_for_conversation_types(self):
        ava, ben = ar.AgentConfig("Ava", speaks_for="One"), ar.AgentConfig("Ben", speaks_for="Two")
        mediator = ar.with_role(ar.AgentConfig("Mediator"), "arbitrator")
        ar.validate_room("casual", [ava, ben]); ar.validate_room("negotiation", [ava, ben, mediator])
        for conversation_type, configs in (("casual", [ava, mediator]), ("negotiation", [ava, ben]), ("negotiation", [ava, mediator]),
                                           ("negotiation", [ava, ar.AgentConfig("Ben", speaks_for="one"), mediator]),
                                           ("negotiation", [ava, ar.AgentConfig("Ben"), mediator]),
                                           ("negotiation", [ava, ben, mediator, ar.with_role(ar.AgentConfig("Judge"), "arbitrator")]),
                                           ("negotiation", [ava, ben, ar.AgentConfig("Cy"), mediator]), ("debate", [ava])):
            with self.assertRaises(ValueError, msg=(conversation_type, configs)):
                ar.validate_room(conversation_type, configs)

    def test_duplicate_names_and_unknown_fields_rejected(self):
        for content in ('[[agents]]\nname="Ava"\n[[agents]]\nname="ava"', '[[agents]]\nname="Ava"\nsecret="bad"'):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.toml"
                path.write_text(content)
                with self.assertRaises(ValueError):
                    ar.load_config(path)

    def test_named_handoff_once_and_no_unaddressed_agent_loop(self):
        room = make_room()
        ava, ben = [a.gate for a in room.agents]
        row = {"speaker_id": "participant_3", "source": "agent", "label": "agent", "text": "Ben, thoughts?"}
        ava.note_utterance(row, 10)
        ben.note_utterance(row, 10)
        self.assertEqual(tg.decide(ava, 11, "silence", 10), "wait")
        self.assertEqual(tg.decide(ben, 11, "silence", 10), "speak")
        ben.note_agent_spoke(12)
        self.assertEqual(tg.decide(ben, 20, "silence", 10), "wait")
        ava.note_utterance({**row, "speaker_id": "participant_4", "text": "I agree."}, 21)
        self.assertEqual(tg.decide(ava, 30, "silence", 21), "wait")

    def test_human_address_to_ben_does_not_trigger_ava(self):
        state = make_room().agents[0].gate
        state.note_utterance({"speaker_id": "participant_1", "label": "high", "text": "Ben, any ideas?"}, 10)
        self.assertEqual(tg.decide(state, 15, "silence", 10), "wait")
        state.manual = "speak"
        state.others_speaking = True
        self.assertEqual(tg.decide(state, 15, "silence", 10), "wait")
        self.assertEqual(state.manual, "speak")

    def test_exact_nudge_wording(self):
        state = tg.GateState(("Ava",))
        state.note_utterance({"speaker_id": "p", "label": "high"})
        expected = "(system) Voiceprint session_id is room. People in this room: Human. The most recent line was spoken by Human (label high). Call get_transcript with after_id from your last call, then answer that person by name. Only the newest line's label matters; earlier OVERLAP or low lines are history, not a reason to refuse. Labels high and medium are reliable enough to name the speaker."
        self.assertEqual(ar.reply_note("room", {"p": "Human"}, state, "speak"), expected)

    def test_misspelled_address_note_and_reviewed_label_note(self):
        """docs/API.md "Fuzzy addressing": an inexact address appends one sentence naming the transcript's spelling."""
        state = tg.GateState(("Ryan",))
        state.note_utterance({"speaker_id": "p", "label": "high", "text": "Brian, what do you think?"})
        plain = ar.reply_note("room", {"p": "Human"}, state, "speak")
        self.assertTrue(plain.endswith("Labels high and medium are reliable enough to name the speaker."))
        self.assertEqual(ar.misheard_name(state, "Ryan"), "Brian")
        noted = ar.reply_note("room", {"p": "Human"}, state, "speak", None, ar.misheard_name(state, "Ryan"))
        self.assertEqual(noted, plain + " The transcript wrote your name as 'Brian'; that line is addressed to you.")
        self.assertTrue(ar.reply_note("room", {"p": "Human"}, state, "speak", "raise the date", "Brian")
                        .endswith("addressed to you. The arbitrator asks you to raise this now: raise the date"))
        state.note_utterance({"speaker_id": "p", "label": "high", "text": "ryan, and the time?"})
        self.assertIsNone(ar.misheard_name(state, "Ryan"))                    # exact spelling, case aside
        state.note_utterance({"speaker_id": "p", "label": "high", "text": "Let's move on."})
        self.assertIsNone(ar.misheard_name(state, "Ryan"))                    # newest human line decides
        state.note_utterance({"speaker_id": "p", "label": "high", "text": "Brian?"})
        state.note_utterance({"speaker_id": "participant_9", "source": "agent", "label": "agent", "text": "I agree."})
        self.assertEqual(ar.misheard_name(state, "Ryan"), "Brian")            # agent rows are skipped
        reviewed = tg.GateState(("Ava",))
        reviewed.note_utterance({"speaker_id": "p", "label": "reviewed", "text": "Ava, go on."})
        self.assertTrue(ar.reply_note("room", {"p": "Human"}, reviewed, "speak").endswith(
            "(label reviewed). Call get_transcript with after_id from your last call, then answer that person by name. "
            "Only the newest line's label matters; earlier OVERLAP or low lines are history, not a reason to refuse. "
            "Labels high and medium are reliable enough to name the speaker. "
            "Label reviewed means the operator confirmed that speaker; treat it as reliable."))

    def test_instruction_layers_mention_misspelling_and_reviewed(self):
        from realtime_openai import SHARED_INSTRUCTIONS, instructions
        self.assertIn("machine-generated", SHARED_INSTRUCTIONS)
        self.assertIn("similar-sounding name", SHARED_INSTRUCTIONS)
        self.assertIn("reviewed means the operator confirmed the speaker", instructions("Ava", "room", {"p": "Human"}))


class AdapterTest(unittest.IsolatedAsyncioTestCase):
    async def test_transport_configuration_and_bare_continuation(self):
        ws = Mock()
        ws.send, ws.close = AsyncMock(), AsyncMock()
        ws.recv = AsyncMock(return_value=json.dumps({"type": "session.updated", "session": {
            "tools": [{"type": "mcp", "server_label": "voiceprint"}],
            "audio": {"input": {"turn_detection": {"create_response": False, "interrupt_response": False}}}}}))
        connector = AsyncMock(return_value=ws)
        adapter = OpenAIRealtime(ar.AgentConfig("Ava"), "room", {"p": "Human"}, "a", "https://example.test/mcp", "secret", connector, FakeConsent())
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake-key"}):
            await adapter.connect()
        await adapter.request_reply("unchanged note")
        await adapter.request_reply()
        await adapter.cancel()
        await adapter.send_audio(b"\0\0")
        sent = [json.loads(c.args[0]) for c in ws.send.call_args_list]
        self.assertFalse(sent[0]["session"]["audio"]["input"]["turn_detection"]["create_response"])
        self.assertIn("participant_id=a", sent[0]["session"]["tools"][0]["server_url"])
        self.assertEqual(sent[3], {"type": "response.create"})
        self.assertEqual(sent[4], {"type": "response.create"})
        self.assertEqual(sent[5], {"type": "response.cancel"})

    async def test_provider_connection_blocked_before_consent(self):
        connector = AsyncMock()
        adapter = OpenAIRealtime(ar.AgentConfig("Ava"), "room", {}, "a", "https://example.test/mcp", connector=connector)
        with self.assertRaises(vp.ConsentError):
            await adapter.connect()
        connector.assert_not_called()

    async def test_revocation_blocks_audio_and_requests(self):
        consent = FakeConsent()
        adapter = OpenAIRealtime(ar.AgentConfig("Ava"), "room", {}, "a", "https://example.test/mcp", consent=consent)
        adapter.ws = Mock(send=AsyncMock())
        consent.failed.set()
        with self.assertRaises(vp.ConsentError):
            await adapter.send_audio(b"\0\0")
        with self.assertRaises(vp.ConsentError):
            await adapter.request_reply("hello")
        adapter.ws.send.assert_not_called()

    async def test_stubs_have_same_interface_and_fail_clearly(self):
        for provider in ("xai_speech", "gemini_live"):
            stub = UnavailableProvider(provider)
            for method in ("connect", "send_audio", "request_reply", "cancel", "events", "close"):
                self.assertTrue(callable(getattr(stub, method)))
            with self.assertRaises(NotImplementedError):
                await stub.connect()


class PrivacyTest(unittest.TestCase):
    def state(self):
        return {"session_id": "room", "allowed": True, "state": "active", "policy_version": "v1",
                "consent_method_version": "m1", "roster_version": 1,
                "participants": [{"id": "p", "bipa_consent_granted": True}],
                "scopes": {"local_processing": True, "openai_audio": True, "hosted_mcp": True},
                "retention_deadline_ms": int(time.time() * 1000) + 60000}

    def test_missing_unsigned_revoked_stale_wrong_room_fail_closed(self):
        variants = [{}, {"allowed": False}, {"state": "revoked"}, {"session_id": "wrong"},
                    {"retention_deadline_ms": 1}, {"participants": [{"bipa_consent_granted": False}]}]
        for changes in variants:
            state = self.state() if changes else {}
            state.update(changes)
            guard = vp.ConsentGuard("http://127.0.0.1:8080", "room")
            with patch.object(vp, "api", return_value=state):
                with self.assertRaises(vp.ConsentError):
                    guard.require()
            self.assertTrue(guard.failed.is_set())

    def test_policy_change_and_outage_latch_failure(self):
        for second in ({**self.state(), "policy_version": "v2"}, RuntimeError("offline")):
            guard = vp.ConsentGuard("local", "room")
            with patch.object(vp, "api", side_effect=[self.state(), second]):
                guard.require()
                with self.assertRaises(vp.ConsentError):
                    guard.require()
            with patch.object(vp, "api") as api:
                with self.assertRaises(vp.ConsentError):
                    guard.require()
                api.assert_not_called()

    def test_local_consent_does_not_authorize_hosted(self):
        state = self.state()
        state["scopes"]["hosted_mcp"] = False
        with patch.object(vp, "api", return_value=state):
            with self.assertRaises(vp.ConsentError):
                vp.ConsentGuard("local", "room", hosted=True).require()

    def test_audio_ingestion_blocked_before_open_or_model(self):
        with patch.object(vp.wave, "open") as opened:
            with self.assertRaises(vp.ConsentError):
                vp.read_wav("missing.wav")
            opened.assert_not_called()
        with self.assertRaises(vp.ConsentError):
            vp.Microphone().__enter__()
        with self.assertRaises(vp.ConsentError):
            vp.Transcriber("local", "room", {})
        with self.assertRaises(vp.ConsentError):
            vp.Stream("local", "room", Mock())

    def test_logger_allowlist_and_destruction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            with self.assertRaises(vp.ConsentError):
                ar.EventLog(path, "room")
            self.assertFalse(path.exists())
            log = ar.EventLog(None, "room", secrets=("never-key",))
            log.emit("openai", {"type": "session.updated", "session": {"authorization": "never-key", "instructions": "private words"}})
            log.emit("openai", {"type": "response.done", "response": {"id": "r", "output": [{"id": "i", "type": "mcp_call", "name": "get_transcript", "arguments": "private-name", "output": "private words"}]}})
            log.emit("utterance", {"utterance_id": 1, "text": "private words", "speaker_id": "private-name", "source": "agent"})
            rows = log.snapshot()
            text = json.dumps(rows)
            self.assertNotIn("private", text)
            self.assertNotIn("never-key", text)
            item = rows[1]["openai"]["response"]["output"][0]
            self.assertTrue(item["succeeded"])
            self.assertEqual(item["output_bytes"], 13)
            log.close()
            self.assertEqual(log.snapshot(), [])

    def test_metadata_buffer_bounded_and_reports_eviction(self):
        log = ar.EventLog(None, "room", max_records=2)
        for _ in range(3):
            log.emit("runtime", {"action": "test"})
        self.assertEqual(len(log.snapshot()), 2)
        self.assertEqual(log.dropped, 1)

    def test_v3_event_kinds_keep_only_allowlisted_fields(self):
        log = ar.EventLog(None, "room")
        log.emit("arbitrator", {"action": "posted", "trigger": "contribution", "tag": None, "confirmed": None, "ingested_rows": 4, "generations": 1,
                                "tier": "board", "redactions": 0, "text": "PRIVATE-VALUE", "board": "PRIVATE-VALUE", "prompt": "PRIVATE-VALUE"}, "Mediator", "participant_5")
        log.emit("guard", {"action": "cut", "stage": "spoken_delta", "redactions": 1, "value": "PRIVATE-VALUE", "transcript": "PRIVATE-VALUE"})
        log.emit("summary", {"action": "saved", "attempts": 1, "board_rows": 2, "transcript_rows": 9, "reason": None, "text": "PRIVATE-VALUE"})
        log.emit("control", {"action": "override", "value": "OBJECTIVE_ACHIEVED", "held": False, "prompt": "PRIVATE-VALUE"})
        log.emit("openai", {"type": "response.output_audio_transcript.delta", "response_id": "r", "delta": "PRIVATE-VALUE"})
        rows = log.snapshot()
        self.assertNotIn("PRIVATE-VALUE", json.dumps(rows))
        self.assertEqual(rows[0]["arbitrator"], {"action": "posted", "trigger": "contribution", "tag": None, "confirmed": None, "ingested_rows": 4, "generations": 1, "tier": "board", "redactions": 0})
        self.assertEqual(rows[1]["guard"], {"action": "cut", "stage": "spoken_delta", "redactions": 1})
        self.assertEqual(rows[2]["summary"], {"action": "saved", "attempts": 1, "board_rows": 2, "transcript_rows": 9, "reason": None})
        self.assertEqual(rows[3]["control"]["value"], "OBJECTIVE_ACHIEVED")
        item = {"type": "mcp_call", "name": "post_agent_channel", "arguments": "PRIVATE-VALUE", "output": "ok"}
        self.assertEqual(ar.EventLog.tool_evidence(item)["name"], "post_agent_channel")
        self.assertEqual(ar.EventLog.tool_evidence(dict(item, name="delete_everything"))["name"], "other")


class ControlsHttpTest(unittest.TestCase):
    def setUp(self):
        self.room = make_room()
        self.server = ar.control_server(self.room, port=0, token="test-bearer")
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def request(self, path="/agents", method="GET", body=None, headers=None):
        headers = {"Authorization": "Bearer test-bearer", **(headers or {})}
        raw = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, raw, headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, json.load(response), response.headers
        except urllib.error.HTTPError as error:
            return error.code, json.load(error), error.headers

    def test_state_and_control_contract(self):
        status, body, _ = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(body["session_id"], "room")
        self.assertEqual(body["agents"][1]["name"], "Ben")
        status, body, _ = self.request("/agents/Ben/control", "POST", {"action": "speak"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(self.room.controls.get_nowait(), ("Ben", "speak", None))

    def test_host_origin_auth_and_preflight(self):
        self.assertEqual(self.request(headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request(headers={"Origin": "https://example.test"})[0], 403)
        self.assertEqual(self.request(headers={"Authorization": "wrong"})[0], 401)
        status, _, headers = self.request(method="OPTIONS", headers={"Origin": "http://127.0.0.1:8080", "Authorization": ""})
        self.assertEqual(status, 200)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "http://127.0.0.1:8080")
        self.assertNotEqual(headers["Access-Control-Allow-Origin"], "*")

    def test_invalid_control_and_unknown_agent(self):
        self.assertEqual(self.request("/agents/Ben/control", "POST", {"action": "eagerness", "value": "pushy"})[0], 400)
        self.assertEqual(self.request("/agents/Nobody/control", "POST", {"action": "speak"})[0], 404)

    def test_arbitrator_state_and_controls_over_http(self):
        room = make_negotiation_room()
        server = ar.control_server(room, port=0, token="test-bearer")
        base, self.base = self.base, f"http://127.0.0.1:{server.server_port}"
        try:
            status, body, _ = self.request()
            self.assertEqual((status, body["conversation_type"]), (200, "negotiation"))
            mediator = body["agents"][2]
            self.assertEqual((mediator["name"], mediator["role"], mediator["provider"], mediator["model"]), ("Mediator", "arbitrator", "openai_responses", "gpt-5"))
            self.assertNotIn("voice", mediator); self.assertEqual(mediator["arbitrator"]["paused"], False)
            self.assertEqual(body["agents"][0]["role"], "voice")
            self.assertEqual(self.request("/agents/Mediator/control", "POST", {"action": "eagerness", "value": "eager"})[0], 400)
            for action in ("speak", "hold", "cancel"):
                status, body, _ = self.request("/agents/Mediator/control", "POST", {"action": action})
                self.assertEqual((status, body["agent"]["role"]), (200, "arbitrator"))
            self.assertEqual([room.controls.get_nowait() for _ in range(3)], [("Mediator", a, None) for a in ("speak", "hold", "cancel")])
        finally:
            self.base = base
            server.shutdown(); server.server_close()


class LifecycleHttpTest(unittest.TestCase):
    """The GUI drives setup, enrollment, start and stop through the same loopback control server."""

    def setUp(self):
        self.runtime = ar.Runtime(gui=True, mcp_url="https://example.invalid/mcp", api_token="operator-token")
        self.server = ar.control_server(self.runtime, port=0, token="operator-token")
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.saved_key = os.environ.pop("OPENAI_API_KEY", None)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        os.environ.pop("OPENAI_API_KEY", None)
        if self.saved_key is not None:
            os.environ["OPENAI_API_KEY"] = self.saved_key

    def request(self, path, method="GET", body=None, headers=None, auth=True):
        headers = {**({"Authorization": "Bearer operator-token"} if auth else {}), **(headers or {})}
        raw = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, raw, headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def test_bootstrap_hands_token_only_to_gui_runtime_from_local_origin(self):
        status, body = self.request("/bootstrap", headers={"Origin": "http://127.0.0.1:8080"}, auth=False)
        self.assertEqual((status, body["api_token"], body["phase"]), (200, "operator-token", "setup"))
        self.assertEqual(self.request("/bootstrap", headers={"Origin": "https://example.test"}, auth=False)[0], 403)
        self.assertEqual(self.request("/bootstrap", headers={"Host": "evil.example"}, auth=False)[0], 403)
        plain = ar.control_server(ar.Runtime(gui=False, api_token="operator-token"), port=0, token="operator-token")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{plain.server_port}/bootstrap", timeout=2) as response:
                self.assertIsNone(json.load(response)["api_token"])
        finally:
            plain.shutdown(); plain.server_close()
        self.assertEqual(self.request("/agents", auth=False)[0], 401)

    def test_setup_validation_and_key_kept_in_process_only(self):
        status, body = self.request("/agents")
        self.assertTrue(body["needs_openai_key"]); self.assertEqual(body["phase"], "setup"); self.assertEqual(body["agents"], [])
        people = [{"name": "Synthetic One", "contact": "one@example.invalid"}, {"name": "Synthetic Two", "contact": "555-0100"}]
        self.assertEqual(self.request("/setup", "POST", {"participants": people})[0], 409)          # key required
        self.assertEqual(self.request("/setup", "POST", {"participants": [], "openai_api_key": "x" * 40})[0], 409)
        self.assertEqual(self.request("/setup", "POST", {"participants": people[:1], "openai_api_key": "x" * 40})[0], 409)   # API needs 2-4 humans
        self.assertEqual(self.request("/setup", "POST", {"participants": [people[0], people[0]], "openai_api_key": "x" * 40})[0], 409)
        self.assertEqual(self.request("/setup", "POST", {"participants": people, "openai_api_key": "short"})[0], 409)
        self.assertEqual(self.request("/setup", "POST", {"participants": people, "openai_api_key": "k" * 40, "session_id": "bad id"})[0], 409)
        self.assertEqual(self.request("/setup", "POST", {"participants": people, "openai_api_key": "k" * 40, "session_id": "room_1"})[0], 200)
        self.assertEqual(os.environ["OPENAI_API_KEY"], "k" * 40)
        self.assertEqual(self.runtime.wait_setup(), {"names": ["Synthetic One", "Synthetic Two"], "contacts": ["one@example.invalid", "555-0100"], "session_id": "room_1"})
        self.assertFalse(self.request("/agents")[1]["needs_openai_key"])
        self.runtime.set_phase("consent")
        self.assertEqual(self.request("/setup", "POST", {"participants": people})[0], 409)        # only during setup

    def test_setup_builds_separate_agents_and_persists_the_panel(self):
        configs = [ar.AgentConfig("Ava", instructions_extra="from toml"), ar.AgentConfig("Ben", voice="cedar", output_device="Speakers X")]
        with tempfile.TemporaryDirectory() as tmp, patch.object(ar, "PERSONA_DIR", Path(tmp)):
            ar.save_personas([dataclasses.replace(configs[0], instructions_extra="")])                  # an explicitly cleared default
            runtime = ar.Runtime(gui=True, mcp_url="https://example.invalid/mcp", api_token="operator-token", configs=configs)
            shown = runtime.state()
            self.assertEqual(shown["agent_configs"][0]["instructions"], "")                              # cleared stays cleared, TOML does not come back
            self.assertEqual(shown["voices"][0], "alloy"); self.assertEqual(shown["max_agents"], ar.MAX_AGENTS)
            server = ar.control_server(runtime, port=0, token="operator-token")
            try:
                base = self.base; self.base = f"http://127.0.0.1:{server.server_port}"
                people = [{"name": "Synthetic One", "contact": "one@example.invalid"}, {"name": "Synthetic Two", "contact": "555-0100"}]
                key = {"openai_api_key": "k" * 40}
                bad = [[], [{"name": "Ben", "speaks_for": "Nobody"}], [{"name": "Ben", "instructions": "x" * 6001}], [{"name": "Ben", "voice": "robot"}],
                       [{"name": "Synthetic One"}], [{"name": "Ben"}, {"name": "ben"}], [{"name": f"A{i}"} for i in range(ar.MAX_AGENTS + 1)]]
                for agents in bad:
                    self.assertEqual(self.request("/setup", "POST", {"participants": people, "agents": agents, **key})[0], 409, agents)
                self.assertIsNone(runtime.session_configs)
                big = "\u00e9" * 6000                                                                  # non-ASCII at the limit, two agents: well over 8 KiB
                status, _ = self.request("/setup", "POST", {"participants": people, "agents": [
                    {"name": "Ben", "speaks_for": "synthetic two", "voice": "sage", "eagerness": "eager", "instructions": "Only words that start with A.\r\n\x00Be brief."},
                    {"name": "Cy", "instructions": big}], **key})
                self.assertEqual(status, 200)
                self.base = base
            finally:
                server.shutdown(); server.server_close()
            runtime.wait_setup()
            active = {c.name: c for c in runtime.active_configs()}
            self.assertEqual(sorted(active), ["Ben", "Cy"])                                              # Ava was removed from this room
            self.assertEqual((active["Ben"].speaks_for, active["Ben"].voice, active["Ben"].eagerness, active["Ben"].instructions_extra, active["Ben"].output_device),
                             ("synthetic two", "sage", "eager", "Only words that start with A.\nBe brief.", "Speakers X"))
            self.assertEqual((active["Cy"].instructions_extra, active["Cy"].speaks_for, active["Cy"].provider), (big, "", "openai_realtime"))
            files = sorted(p.name for p in Path(tmp).glob("*.json"))
            self.assertEqual(len(files), 2); self.assertTrue(any(f.startswith("Ben-") for f in files)); self.assertTrue(any(f.startswith("Cy-") for f in files))
            self.assertNotEqual(ar.persona_path("Agent One"), ar.persona_path("Agent_One"))              # distinct names never share a file
            fresh = ar.Runtime(gui=True, configs=configs)
            self.assertEqual([(c.name, c.voice, c.instructions_extra[:4]) for c in fresh.configs], [("Ava", "marin", "from"), ("Ben", "sage", "Only"), ("Cy", "marin", "\u00e9\u00e9\u00e9\u00e9")])
            self.assertNotIn("Only words", json.dumps(runtime.bootstrap()))

    def test_setup_validation_for_negotiation_rooms(self):
        """docs/API.md V3 /setup: 2-4 humans, exactly two advocates for different people, exactly one arbitrator; no arbitrator in casual."""
        people = [{"name": "Synthetic One", "contact": "one@example.invalid"}, {"name": "Synthetic Two", "contact": "555-0100"}]
        key = {"openai_api_key": "k" * 40}
        ava, ben = {"name": "Ava", "speaks_for": "Synthetic One"}, {"name": "Ben", "speaks_for": "Synthetic Two"}
        mediator = {"name": "Mediator", "role": "arbitrator"}
        with tempfile.TemporaryDirectory() as tmp, patch.object(ar, "PERSONA_DIR", Path(tmp)):
            rejected = [
                {"participants": people, "conversation_type": "debate", "agents": [ava, ben, mediator], **key},
                {"participants": people, "conversation_type": "casual", "agents": [ava, ben, mediator], **key},          # arbitrator in casual
                {"participants": people, "conversation_type": "negotiation", "agents": [ava, ben], **key},               # no arbitrator
                {"participants": people, "conversation_type": "negotiation", "agents": [ava, mediator], **key},          # one advocate
                {"participants": people, "conversation_type": "negotiation", "agents": [ava, dict(ben, speaks_for="Synthetic One"), mediator], **key},
                {"participants": people, "conversation_type": "negotiation", "agents": [ava, {"name": "Ben"}, mediator], **key},
                {"participants": people, "conversation_type": "negotiation", "agents": [ava, ben, mediator, {"name": "Judge", "role": "arbitrator"}], **key},
                {"participants": people, "conversation_type": "negotiation", "agents": [ava, ben, dict(mediator, speaks_for="Synthetic One")], **key},
                {"participants": people, "conversation_type": "negotiation", "agents": [ava, ben, dict(mediator, role="referee")], **key},
                {"participants": people, "conversation_type": "negotiation", **key},                                     # agents.toml has no arbitrator
                {"participants": people[:1], "conversation_type": "negotiation", "agents": [ava, ben, mediator], **key},
            ]
            for body in rejected:
                self.assertEqual(self.request("/setup", "POST", body)[0], 409, body)
            self.assertEqual(self.runtime.state()["conversation_type"], "casual")
            status, _ = self.request("/setup", "POST", {"participants": people, "conversation_type": "negotiation", "agents": [ava, ben, mediator], **key})
            self.assertEqual(status, 200)
            self.runtime.wait_setup()
            self.assertEqual(self.runtime.state()["conversation_type"], "negotiation")
            configs = {c.name: c for c in self.runtime.active_configs()}
            self.assertEqual((configs["Mediator"].role, configs["Mediator"].provider, configs["Mediator"].model), ("arbitrator", "openai_responses", "gpt-5"))
            self.assertEqual([c["role"] for c in self.runtime.state()["agent_configs"]], ["voice", "voice", "arbitrator"])
            self.assertEqual(sorted(self.runtime.state()["roles"]), ["arbitrator", "voice"])

    def test_enrollment_start_and_stop_follow_phases(self):
        self.runtime.participants = [{"id": "participant_1", "name": "Synthetic One"}]
        self.assertEqual(self.request("/enrollment/record", "POST", {"participant_id": "participant_1"})[0], 409)
        self.assertEqual(self.request("/start", "POST", {})[0], 409)
        self.runtime.set_phase("enrollment")
        with self.runtime.lock:
            self.runtime.awaiting = "participant_1"; self.runtime.enrollment["participant_1"] = {"state": "waiting", "peak": None}
        body = self.request("/agents")[1]
        self.assertEqual(body["awaiting"], "participant_1"); self.assertEqual(body["participants"][0]["enrollment"]["state"], "waiting")
        self.assertEqual(self.request("/enrollment/record", "POST", {"participant_id": "participant_2"})[0], 409)
        self.assertEqual(self.request("/enrollment/record", "POST", {"participant_id": "participant_1"})[0], 200)
        self.assertEqual(self.runtime.records.get(timeout=1), "participant_1")
        self.runtime.set_phase("ready")
        self.assertFalse(self.runtime.start.is_set())
        self.assertEqual(self.request("/start", "POST", {})[0], 200)
        self.assertTrue(self.runtime.start.is_set())
        self.assertEqual(self.request("/stop", "POST", {})[0], 200)
        self.assertTrue(self.runtime.stop.is_set())
        self.assertEqual(self.request("/agents/Ava/control", "POST", {"action": "speak"})[0], 404)   # no room yet

    def test_recorder_waits_for_trigger_and_reports_level(self):
        self.runtime.participants = [{"id": "participant_1", "name": "Synthetic One"}]
        consent = FakeConsent()
        chunk = b"\x10\x27" * 2000   # 0x2710 = 10000 peak
        capture = Mock(); capture.get.return_value = (chunk, 0.0)
        capture.__enter__ = Mock(return_value=capture); capture.__exit__ = Mock(return_value=False)
        with patch.object(vp, "Microphone", return_value=capture):
            record = self.runtime.recorder(None, consent, console=False)
            result = {}
            worker = threading.Thread(target=lambda: result.setdefault("pcm", record("Synthetic One")))
            worker.start()
            for _ in range(50):
                if self.runtime.awaiting == "participant_1":
                    break
                time.sleep(.02)
            self.assertEqual(self.runtime.state()["participants"][0]["enrollment"]["state"], "waiting")
            self.assertFalse(capture.__enter__.called)                     # microphone closed until triggered
            self.runtime.request_record("participant_1")
            worker.join(5)
        self.assertEqual(len(result["pcm"]), 32 * len(chunk))
        state = self.runtime.state()["participants"][0]["enrollment"]
        self.assertEqual((state["state"], state["peak"]), ("recorded", 10000))
        self.assertIsNone(self.runtime.state()["awaiting"])
        self.runtime.reset_enrollment("Enrollment rejected: try again")
        self.assertEqual(self.runtime.state()["participants"][0]["enrollment"]["state"], "rejected")

    def test_enrollment_state_carries_profile_seeded_from_the_init_response(self):
        """V3.1: init returns participants[{id, profile_seeded}]; vp.enroll still returns the ids dict and hands the body
        to the runtime, so GET /agents participants[].enrollment shows which retained voiceprint seeded the profile."""
        self.runtime.participants = [{"id": "participant_1", "name": "Grant"}, {"id": "participant_2", "name": "Kyle"}]
        self.runtime.enrollment["participant_1"] = {"state": "recorded", "peak": 9000}
        init = {"session_id": "room", "participants": [{"id": "participant_1", "profile_seeded": True}, {"id": "participant_2", "profile_seeded": False}]}
        seen = []
        with patch("voiceprint_client.api", return_value=init) as api:
            ids = vp.enroll("http://127.0.0.1:8080", "room", ["Grant", "Kyle"], lambda name: b"\x00" * 32000, None, FakeConsent(),
                            on_response=lambda body: (seen.append(body), self.runtime.note_enrolled(body)))
        self.assertEqual(ids, {"participant_1": "Grant", "participant_2": "Kyle"})
        self.assertEqual(seen, [init])
        self.assertEqual(api.call_args[0][1], "/speaker/session/init")
        status, body = self.request("/agents")
        enrollment = {p["id"]: p["enrollment"] for p in body["participants"]}
        self.assertEqual((status, enrollment["participant_1"]), (200, {"state": "recorded", "peak": 9000, "profile_seeded": True}))
        self.assertEqual(enrollment["participant_2"], {"state": "recorded", "peak": None, "profile_seeded": False})
        with patch("voiceprint_client.api", return_value={"session_id": "room"}):
            self.assertEqual(vp.enroll("http://127.0.0.1:8080", "room", ["Grant"], lambda name: b"", None, FakeConsent()), {"participant_1": "Grant"})
        self.runtime.note_enrolled({"session_id": "room"})                    # an older API without participants[] changes nothing
        self.assertTrue(self.runtime.state()["participants"][0]["enrollment"]["profile_seeded"])

    def test_stop_unblocks_setup_and_enrollment_waits(self):
        self.runtime.request_stop()
        with self.assertRaisesRegex(RuntimeError, "Stopped before setup"):
            self.runtime.wait_setup()
        self.runtime.participants = [{"id": "participant_1", "name": "Synthetic One"}]
        with patch.object(vp, "Microphone") as microphone:
            with self.assertRaisesRegex(RuntimeError, "Stopped during enrollment"):
                self.runtime.recorder(None, FakeConsent(), console=False)("Synthetic One")
            microphone.assert_not_called()

    REVIEWED = {"configured": True, "vendors": {"openai_reviewed": True, "cloudflare_reviewed": True}}

    def test_prepare_room_wait_abandons_on_stop(self):
        stop = threading.Event(); stop.set()
        responses = iter([self.REVIEWED, {"session_id": "room"}, {"state": "pending", "allowed": False}])
        with patch.object(vp, "api_token", return_value="operator-token"), patch.object(vp, "api", side_effect=lambda *a, **k: next(responses)):
            with self.assertRaisesRegex(vp.ConsentError, "Stopped while waiting"):
                vp.prepare_room("http://127.0.0.1:1", "room", ["Synthetic One"], ["one@example.invalid"], True, stop)

    def test_prepare_room_refuses_hosted_room_before_vendor_review_and_names_missing_scopes(self):
        calls = []
        unreviewed = {"configured": True, "vendors": {"openai_reviewed": True, "cloudflare_reviewed": False}}
        with patch.object(vp, "api_token", return_value="operator-token"), patch.object(vp, "api", side_effect=lambda base, path, body=None: (calls.append(path), unreviewed)[1]):
            with self.assertRaisesRegex(vp.ConsentError, "VOICEPRINT_CLOUDFLARE_REVIEWED=true"):
                vp.prepare_room("http://127.0.0.1:1", "room", ["Synthetic One"], ["one@example.invalid"], True)
        self.assertEqual(calls, ["/privacy/notice"])                      # no room was created
        signed_without_scopes = {"state": "active", "allowed": True, "scopes": {"local_processing": True, "openai_audio": False, "hosted_mcp": False}}
        responses = iter([self.REVIEWED, {"session_id": "room"}, signed_without_scopes])
        with patch.object(vp, "api_token", return_value="operator-token"), patch.object(vp, "api", side_effect=lambda *a, **k: next(responses)):
            with self.assertRaisesRegex(vp.ConsentError, "disclosure box unchecked"):
                vp.prepare_room("http://127.0.0.1:1", "room", ["Synthetic One"], ["one@example.invalid"], True)

    def test_guard_keeps_the_specific_reason(self):
        guard = vp.ConsentGuard("http://127.0.0.1:1", "room", hosted=True)
        state = {"session_id": "room", "allowed": True, "state": "active", "policy_version": "p", "consent_method_version": "m", "roster_version": 1,
                 "participants": [{"bipa_consent_granted": True}], "scopes": {"local_processing": True, "openai_audio": False, "hosted_mcp": False},
                 "retention_deadline_ms": (time.time() + 60) * 1000}
        with patch.object(vp, "api", return_value=state):
            with self.assertRaisesRegex(vp.ConsentError, "reviewed vendor configuration"):
                guard.require("local_processing")
        self.assertTrue(guard.failed.is_set())


if __name__ == "__main__":
    unittest.main()
