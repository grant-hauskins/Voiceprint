"""No provider key, hardware, or human fixtures: floor, transport, controls and prior-consent regression tests."""
import asyncio
import base64
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


if __name__ == "__main__":
    unittest.main()
