"""OpenAI Realtime voice agent that knows who is talking, via Voiceprint's public MCP endpoint.

One shared microphone feeds two consumers: Voiceprint (16 kHz chunks, speaker attribution, local ASR) and the
OpenAI Realtime session (24 kHz audio). The provider's VAD only segments audio; it never auto-responds.
The application-level gate in turn_gate.py decides when to send response.create, using speaker labels,
overlap rows and silence from Voiceprint. The model reads the transcript itself with the get_transcript MCP tool.

Usage (PowerShell), with the worker, API and `scripts\\dev.ps1 tunnel` already running:
  $env:OPENAI_API_KEY = "..."
  .venv\\Scripts\\python.exe scripts\\realtime_openai.py --names Grant Kyle --mcp-url https://<name>.trycloudflare.com/mcp --device 1

Keys while running: SPACE = let the agent speak now, H = hold (skip the next opportunity), Q = quit.
"""
import argparse
import asyncio
import base64
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import turn_gate as tg  # noqa: E402
import voiceprint_client as vp  # noqa: E402

REALTIME_URL = "wss://api.openai.com/v1/realtime"


def instructions(agent_name, session_id, names):
    people = ", ".join(f"{n} (id {pid})" for pid, n in names.items())
    return (
        f"You are {agent_name}, one participant in a spoken group conversation with {people}. "
        f"You cannot tell voices apart yourself. Before every reply call get_transcript with session_id \"{session_id}\" "
        "and the after_id you received last time (0 the first time) to learn who said what. "
        "Address people by name. If the latest lines are marked OVERLAP or low, say you are not sure who spoke and ask, "
        "instead of guessing. Keep every reply under two sentences. Do not narrate tool use."
    )


def resample_16k_to_24k(pcm16k):
    import numpy as np
    from scipy.signal import resample_poly
    x = np.frombuffer(pcm16k, dtype="<i2").astype("float32")
    y = resample_poly(x, 3, 2)
    return np.clip(y, -32768, 32767).astype("<i2").tobytes()


class Player:
    """Plays the agent's audio (24 kHz PCM16) and reports when playback is idle."""

    def __init__(self, device=None):
        import sounddevice as sd
        self.queue = []
        self.lock = threading.Lock()
        self.stream = sd.RawOutputStream(samplerate=24000, channels=1, dtype="int16", device=device, blocksize=1200, callback=self._callback)
        self.stream.start()
        self.last_audio_at = 0

    def _callback(self, out, frames, timing, status):
        need = frames * 2
        with self.lock:
            buf = b"".join(self.queue); self.queue = [buf[need:]] if len(buf) > need else []
        chunk = buf[:need]
        if chunk:
            self.last_audio_at = time.monotonic()
        out[:] = chunk + b"\x00" * (need - len(chunk))

    def play(self, pcm):
        with self.lock:
            self.queue.append(pcm)

    def flush(self):
        with self.lock:
            self.queue = []

    def busy(self):
        with self.lock:
            pending = sum(len(b) for b in self.queue)
        return pending > 0 or time.monotonic() - self.last_audio_at < 0.3


async def main(args):
    import websockets
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        sys.exit("Set OPENAI_API_KEY in the environment first.")
    mcp_token = args.mcp_token or os.environ.get("VOICEPRINT_MCP_TOKEN")
    if not mcp_token:
        path = Path(__file__).resolve().parents[1] / "data" / "mcp-token.txt"
        mcp_token = path.read_text(encoding="utf-8").strip() if path.exists() else None

    session_id = args.session or "agent_" + uuid.uuid4().hex[:10]
    print(f"Loading faster-whisper {args.model}...", flush=True)
    names = vp.enroll(args.api, session_id, args.names, vp.record_from_mic(args.device))
    print("Session", session_id, "ready:", ", ".join(f"{pid}={name}" for pid, name in names.items()), flush=True)

    log = open(args.events, "a", encoding="utf-8") if args.events else None
    state = tg.GateState(agent_names=(args.agent_name,), eagerness=args.eagerness)
    last_end = {"at": None}
    status = {"current": "silence"}

    def on_utterance(utterance):
        state.note_utterance(utterance)
        last_end["at"] = time.monotonic()

    transcriber = vp.Transcriber(args.api, session_id, names, args.model, log, on_utterance=on_utterance)
    loop = asyncio.get_running_loop()
    audio_out = asyncio.Queue()

    def on_chunk(pcm, attribution):
        st = attribution.get("status")
        if attribution.get("overlap") == "detected":
            status["current"] = "overlap"
        elif attribution.get("speaker_id") or st == "unknown":
            status["current"] = "speaking"
        else:
            status["current"] = "silence"
        if not muted["now"]:
            loop.call_soon_threadsafe(audio_out.put_nowait, pcm)

    stream = vp.Stream(args.api, session_id, vp.Turns(transcriber.submit, args.verbose), log, on_chunk=on_chunk, verbose=args.verbose)
    player = Player(args.output_device)
    stop = threading.Event()

    muted = {"now": False}

    def mic_thread():
        # Half-duplex: PortAudio has no echo cancellation, so while the agent's audio is playing the shared
        # microphone is replaced by silence. Voiceprint keeps a continuous timeline (silence chunks) and the
        # agent's own voice never reaches Voiceprint or OpenAI's input buffer. Human speech during the agent's
        # reply is lost for that moment; the trade-off is a clean transcript with no self-echo.
        try:
            with vp.Microphone(args.device) as mic:
                while not stop.is_set():
                    pcm, captured_at = mic.get()
                    muted["now"] = player.busy()
                    if muted["now"]:
                        pcm = b"\x00" * len(pcm)
                    stream.feed(pcm, captured_at)
        except Exception as error:
            print("microphone loop stopped:", error, file=sys.stderr, flush=True)
            stop.set()

    def key_thread():
        try:
            import msvcrt
        except ImportError:
            return
        while not stop.is_set():
            if msvcrt.kbhit():
                ch = msvcrt.getwch().lower()
                if ch == " ":
                    state.manual = "speak"; print("  [manual: speak]", flush=True)
                elif ch == "h":
                    state.manual = "hold"; print("  [manual: hold]", flush=True)
                elif ch == "q":
                    stop.set()
            time.sleep(0.05)

    headers = {"Authorization": "Bearer " + key}
    async with websockets.connect(f"{REALTIME_URL}?model={args.realtime_model}", additional_headers=headers, max_size=None) as ws:
        tool = {"type": "mcp", "server_label": "voiceprint", "server_url": args.mcp_url, "allowed_tools": ["get_transcript", "get_current_speaker"],
                "require_approval": "never", "server_description": "Who said what in this room, with confidence labels."}
        if mcp_token:
            tool["authorization"] = mcp_token
        await ws.send(json.dumps({"type": "session.update", "session": {
            "type": "realtime", "model": args.realtime_model, "output_modalities": ["audio"],
            "instructions": instructions(args.agent_name, session_id, names),
            "audio": {"input": {"format": {"type": "audio/pcm", "rate": 24000},
                                "turn_detection": {"type": "server_vad", "create_response": False, "interrupt_response": False},
                                "transcription": {"model": "gpt-4o-mini-transcribe", "language": "en"}},
                      "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": args.voice}},
            "tools": [tool]}}))
        print("Realtime session configured. Press Enter to start the conversation; SPACE = speak now, H = hold, Q = quit.", flush=True)
        input()
        threading.Thread(target=mic_thread, daemon=True).start()
        threading.Thread(target=key_thread, daemon=True).start()
        responding = {"active": False}

        async def sender():
            while not stop.is_set():
                pcm = await audio_out.get()
                await ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": base64.b64encode(resample_16k_to_24k(pcm)).decode()}))

        async def receiver():
            async for raw in ws:
                event = json.loads(raw); kind = event.get("type")
                if log:
                    log.write(json.dumps({"openai": event if kind != "response.output_audio.delta" else {"type": kind}}) + "\n"); log.flush()
                if kind == "response.output_audio.delta":
                    player.play(base64.b64decode(event["delta"]))
                elif kind == "response.output_audio_transcript.done":
                    print(f"[{args.agent_name} said] {event.get('transcript')}", flush=True)
                elif kind == "response.output_item.done" and event.get("item", {}).get("type") == "mcp_call":
                    item = event["item"]
                    print(f"  mcp_call {item.get('name')}({item.get('arguments')}) -> {('ERROR ' + str(item.get('error'))) if item.get('error') else 'ok'}", flush=True)
                elif kind == "response.done":
                    responding["active"] = False; state.note_agent_spoke()
                elif kind == "error":
                    print("OpenAI error:", event.get("error"), file=sys.stderr, flush=True)
                if stop.is_set():
                    break

        async def gate_loop():
            while not stop.is_set():
                await asyncio.sleep(0.2)
                if responding["active"] or player.busy():
                    continue
                decision = tg.decide(state, time.monotonic(), status["current"], last_end["at"])
                if decision == "wait":
                    continue
                responding["active"] = True
                extra = "" if decision == "speak" else " The last attribution was uncertain: ask who just spoke before answering."
                print(f"  [gate: {decision}]", flush=True)
                await ws.send(json.dumps({"type": "response.create", "response": {"instructions": "Respond now." + extra}}))

        tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver()), asyncio.create_task(gate_loop())]
        try:
            while not stop.is_set():
                await asyncio.sleep(0.2)
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            for t in tasks:
                t.cancel()
    stream.end()
    transcriber.finish()
    if log:
        log.close()
    print("Session ended:", session_id, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://127.0.0.1:8080")
    parser.add_argument("--mcp-url", required=False, help="public https URL of the Voiceprint MCP endpoint (…/mcp)")
    parser.add_argument("--mcp-token")
    parser.add_argument("--names", nargs="+", required=True, help="human participants to enroll")
    parser.add_argument("--agent-name", default="Ava")
    parser.add_argument("--eagerness", choices=["quiet", "balanced", "eager"], default="balanced")
    parser.add_argument("--session")
    parser.add_argument("--device", type=int, help="microphone index")
    parser.add_argument("--output-device", type=int, help="speaker index")
    parser.add_argument("--model", default="base.en", help="faster-whisper model")
    parser.add_argument("--realtime-model", default="gpt-realtime-2.1")
    parser.add_argument("--voice", default="marin")
    parser.add_argument("--events", type=Path, default=Path("data/realtime-events.jsonl"))
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--list-devices", action="store_true", help="print microphone/speaker indexes and exit")
    parsed = parser.parse_args()
    if parsed.list_devices:
        import sounddevice as sd
        print(sd.query_devices()); sys.exit(0)
    asyncio.run(main(parsed))
