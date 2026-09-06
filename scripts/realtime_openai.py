"""OpenAI Realtime voice agent that knows who is talking, via Voiceprint's public MCP endpoint.

One shared microphone feeds two consumers: Voiceprint (16 kHz chunks, speaker attribution, local ASR) and the
OpenAI Realtime session (24 kHz audio). The provider's VAD only segments audio; it never auto-responds.
The application-level gate in turn_gate.py decides when to send response.create, using speaker labels,
overlap rows and silence from Voiceprint. The model reads the transcript itself with the get_transcript MCP tool.

Usage (PowerShell), with the worker, API and `scripts\\dev.ps1 tunnel` already running:
  $env:OPENAI_API_KEY = "..."
  .venv\\Scripts\\python.exe scripts\\realtime_openai.py --names Grant Kyle --mcp-url https://<name>.trycloudflare.com/mcp --device 1

Keys while running: SPACE = one reply now, H = hold/release (sticky mute; also cuts off a reply), C = cancel current reply, Q = quit.
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
        f"You cannot tell voices apart yourself; the get_transcript tool (session_id \"{session_id}\") tells you who said what, "
        "with a label per line: high and medium mean the name is reliable, low means unsure, OVERLAP means two people at once. "
        "Before every reply call get_transcript with the after_id from your last call (0 the first time). "
        "'Who is in the room' means the enrolled people listed above; the transcript lines carry their names. "
        "Judge only by the newest lines; older OVERLAP or low lines are history. If the newest line is low or OVERLAP, ask who spoke. "
        "Address people by name. Keep every reply under two sentences. Call tools silently: never say that you are checking, "
        "looking, or pulling anything up; just call get_transcript and then answer."
    )


HOSTAPI_PREFERENCE = ("MME", "Windows DirectSound", "Windows WASAPI", "Windows WDM-KS")   # MME resamples for us; WDM-KS is picky


def resolve_device(spec, kind):
    """
    spec: None, an index, or comma-separated preferences (index or case-insensitive name substring), first present wins.
    Bluetooth 'Hands-Free' endpoints (the low-quality call profile that also hijacks the mic) are skipped unless
    named by index. Returns a device index, or None for the Windows default.
    """
    import sounddevice as sd
    if spec is None or str(spec).strip() == "":
        return None
    apis, devices = sd.query_hostapis(), sd.query_devices()
    key = "max_output_channels" if kind == "output" else "max_input_channels"
    for pref in [x.strip() for x in str(spec).split(",") if x.strip()]:
        if pref.isdigit():
            i = int(pref)
            if i < len(devices) and devices[i][key] > 0:
                return i
            continue
        matches = [i for i, d in enumerate(devices) if d[key] > 0 and pref.lower() in d["name"].lower() and "hands-free" not in d["name"].lower()]
        if matches:
            def rank(i):
                name = apis[devices[i]["hostapi"]]["name"]
                return HOSTAPI_PREFERENCE.index(name) if name in HOSTAPI_PREFERENCE else 99
            return sorted(matches, key=rank)[0]
    return None


def describe_device(index, kind):
    import sounddevice as sd
    if index is None:
        index = sd.default.device[1 if kind == "output" else 0]
        return f"Windows default: {sd.query_devices(index)['name']}"
    return f"{index}: {sd.query_devices(index)['name']}"


def resample_16k_to_24k(pcm16k):
    import numpy as np
    from scipy.signal import resample_poly
    x = np.frombuffer(pcm16k, dtype="<i2").astype("float32")
    y = resample_poly(x, 3, 2)
    return np.clip(y, -32768, 32767).astype("<i2").tobytes()


class Player:
    """Plays the agent's audio (24 kHz PCM16) and reports when playback is idle."""

    def __init__(self, device=None, tail_s=0.4):
        import sounddevice as sd
        self.queue = []
        self.tail_s = tail_s      # keep "busy" this long after the last buffer; raise for Bluetooth latency
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
        return pending > 0 or time.monotonic() - self.last_audio_at < self.tail_s


async def main(args):
    # Keep this established command as a compatibility entry point. The guarded runtime
    # imports this file's earned audio helpers and exact instructions. The legacy raw
    # logger/capture implementation is replaced by the guarded shared path.
    from agent_runtime import AgentConfig, main as shared_main, parser as shared_parser
    guarded = shared_parser().parse_args([])
    for key in ("api", "names", "session", "device", "output_device", "mute_tail", "model", "verbose", "mcp_url"):
        setattr(guarded, key, getattr(args, key))
    guarded.contacts = getattr(args, "contacts", None)
    guarded.mcp_token = args.mcp_token
    guarded.events = args.events
    guarded.configs = [AgentConfig(args.agent_name, "openai_realtime", args.realtime_model,
                                  args.voice, args.eagerness, args.output_device or "HD 4.40,BenQ")]
    return await shared_main(guarded)



if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://127.0.0.1:8080")
    parser.add_argument("--mcp-url", required=False, help="public https URL of the Voiceprint MCP endpoint (â€¦/mcp)")
    parser.add_argument("--mcp-token")
    parser.add_argument("--names", nargs="+", required=True, help="human participants to enroll")
    parser.add_argument("--contacts", nargs="+", help="one typed email/phone per full name; otherwise prompt")
    parser.add_argument("--agent-name", default="Ava")
    parser.add_argument("--eagerness", choices=["quiet", "balanced", "eager"], default="balanced")
    parser.add_argument("--session")
    parser.add_argument("--device", default="Razer Seiren", help="microphone: index or comma-separated name preferences (default: Razer Seiren)")
    parser.add_argument("--output-device", default="HD 4.40,BenQ", help="speaker: index or comma-separated name preferences, first present wins (default: HD 4.40 BT, then BenQ monitor, else Windows default)")
    parser.add_argument("--mute-tail", type=float, default=0.4, help="seconds to keep the mic muted after the agent's audio drains; use ~0.8 for Bluetooth")
    parser.add_argument("--model", default="base.en", help="faster-whisper model")
    parser.add_argument("--realtime-model", default="gpt-realtime-2.1")
    parser.add_argument("--voice", default="marin")
    parser.add_argument("--events", type=Path, help="disabled by privacy policy; aggregate scoring uses bounded memory")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--list-devices", action="store_true", help="print microphone/speaker indexes and exit")
    parsed = parser.parse_args()
    if parsed.list_devices:
        import sounddevice as sd
        print(sd.query_devices()); sys.exit(0)
    if not parsed.mcp_url:
        parser.error("--mcp-url is required (the public https URL from scripts\\dev.ps1 tunnel, ending in /mcp)")
    asyncio.run(main(parsed))
