"""No provider key, hardware, or human fixtures: floor, transport, controls and prior-consent regression tests."""
import asyncio
import base64
import dataclasses
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
        self.valid_until = time.monotonic() + 100

    def require(self, scope="local_processing"):
        self.scopes.append(scope)
        if self.failed.is_set():
            raise vp.ConsentError("revoked")
        return {}


class FakeApi:
    def __init__(self):
        self.holder = None
        self.rows = []
        self.calls = []
        self.fail_renew = False

    async def request(self, method, path, body=None):
        self.calls.append((method, path, body))
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

    async def request_reply(self, note=None):
        self.replies.append(note)

    async def cancel(self):
        self.cancels += 1


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

    async def test_provider_cancel_error_after_manual_cancel_does_not_recancel(self):
        # Cancel/Hold after response.done while audio still plays: OpenAI answers response.cancel with
        # response_cancel_not_active; the agent is still active until the tick releases the floor, so it must not cancel again.
        room = make_room()
        agent = room.agents[0]
        await agent.begin("speak")
        await agent.event({"type": "response.created", "response": {"id": "r"}})
        await agent.event({"type": "response.output_audio.delta", "response_id": "r", "delta": base64.b64encode(b"\0\0").decode()})
        self.assertTrue(agent.player.playing)
        await agent.event({"type": "response.done", "response": {"id": "r", "output": [{"type": "message"}]}})
        self.assertTrue(agent.active)
        await agent.cancel()
        await agent.event({"type": "error", "error": {"code": "response_cancel_not_active"}})
        self.assertEqual(agent.provider.cancels, 1)

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
