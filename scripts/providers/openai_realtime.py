"""Extracted OpenAI transport. No response instructions overrides or automatic VAD replies."""
import asyncio
import base64
import json
import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from realtime_openai import REALTIME_URL, instructions, persona
import voiceprint_client as vp


def participant_url(url, participant_id):
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "participant_id"]
    query.append(("participant_id", participant_id))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


class OpenAIRealtime:
    def __init__(self, config, session_id, names, participant_id, mcp_url, mcp_token=None, connector=None, consent=None):
        self.config, self.session_id, self.names = config, session_id, names
        self.participant_id, self.mcp_url, self.mcp_token = participant_id, mcp_url, mcp_token
        self.connector, self.ws = connector, None
        self.consent = consent

    async def authorized(self):
        await asyncio.to_thread(vp.require_consent, self.consent, "openai_audio")
        await asyncio.to_thread(vp.require_consent, self.consent, "hosted_mcp")

    async def _send(self, event):
        await self.ws.send(json.dumps(event))

    async def connect(self):
        await self.authorized()
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("Set OPENAI_API_KEY in this shell before starting the runtime")
        if self.connector is None:
            import websockets
            self.connector = websockets.connect
        self.ws = await self.connector(f"{REALTIME_URL}?model={self.config.model}",
                                       additional_headers={"Authorization": "Bearer " + key}, max_size=None)
        tool = {"type": "mcp", "server_label": "voiceprint",
                "server_url": participant_url(self.mcp_url, self.participant_id),
                "allowed_tools": ["get_transcript", "get_current_speaker"], "require_approval": "never"}
        if self.mcp_token:
            tool["authorization"] = self.mcp_token
        prompt = instructions(self.config.name, self.session_id, self.names)
        flavor = persona(self.config.name, getattr(self.config, "speaks_for", ""), self.config.instructions_extra)
        if flavor:
            prompt += "\n\n" + flavor
        await self._send({"type": "session.update", "session": {
            "type": "realtime", "model": self.config.model, "output_modalities": ["audio"],
            "instructions": prompt,
            "audio": {"input": {"format": {"type": "audio/pcm", "rate": 24000},
                                "turn_detection": {"type": "server_vad", "create_response": False, "interrupt_response": False},
                                "transcription": {"model": "gpt-4o-mini-transcribe", "language": "en"}},
                      "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": self.config.voice}},
            "tools": [tool]}})
        deadline = asyncio.get_running_loop().time() + 20
        while True:
            event = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=max(.01, deadline - asyncio.get_running_loop().time())))
            if event.get("type") == "error":
                raise RuntimeError("OpenAI rejected the session configuration; provider error code: " +
                                   str(event.get("error", {}).get("code", "unknown")))
            if event.get("type") == "session.updated":
                session = event.get("session", {})
                registered = session.get("tools", [])
                vad = session.get("audio", {}).get("input", {}).get("turn_detection") or {}
                if not any(t.get("type") == "mcp" and t.get("server_label") == "voiceprint" for t in registered):
                    raise RuntimeError("session.updated has no voiceprint MCP tool; refusing to run")
                if vad.get("create_response") is not False or vad.get("interrupt_response") is not False:
                    raise RuntimeError("session.updated did not confirm manual response control; refusing to run")
                break
        await self._message(f"(system) This room's Voiceprint session_id is {self.session_id}. Participants: " +
                            ", ".join(f"{n} (id {pid})" for pid, n in self.names.items()) + ".")

    async def _message(self, text):
        await self._send({"type": "conversation.item.create", "item": {"type": "message", "role": "user",
                           "content": [{"type": "input_text", "text": text}]}})

    async def send_audio(self, pcm24k):
        await self.authorized()
        await self._send({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm24k).decode()})

    async def request_reply(self, note=None):
        await self.authorized()
        if note is not None:
            await self._message(note)
        await self._send({"type": "response.create"})

    async def cancel(self):
        await self._send({"type": "response.cancel"})

    async def events(self):
        async for raw in self.ws:
            yield json.loads(raw)

    async def close(self):
        if self.ws is not None:
            await self.ws.close()
