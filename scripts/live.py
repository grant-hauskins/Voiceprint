"""Enroll participants and stream a shared microphone to the Java API."""
import argparse
import base64
import json
import os
import queue
import time
import urllib.request
import uuid
from pathlib import Path


def api(base, path, body):
    headers = {"Content-Type": "application/json"}
    if os.environ.get("VOICEPRINT_API_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["VOICEPRINT_API_TOKEN"]
    request = urllib.request.Request(base + path, json.dumps(body).encode(), headers)
    with urllib.request.urlopen(request, timeout=40) as response:
        return json.load(response)


def run(args):
    import sounddevice as sd
    if not 2 <= len(args.names) <= 4:
        raise ValueError("Choose 2–4 participants")
    session = args.session or "live_" + uuid.uuid4().hex[:12]
    participants = []
    for index, name in enumerate(args.names, 1):
        input(f"{name}: press Enter, then speak alone for 8 seconds. ")
        audio = sd.rec(8 * 16000, samplerate=16000, channels=1, dtype="int16", device=args.device)
        sd.wait()
        participants.append({"id": f"participant_{index}", "name": name,
                             "opening_statement_audio": base64.b64encode(audio.astype("<i2").tobytes()).decode()})
    result = api(args.api, "/speaker/session/init", {"session_id": session, "sample_rate": 16000, "audio_format": "pcm_s16le", "participants": participants})
    print("Session", session, "ready:", ", ".join(f'{p["id"]}={p["name"]}' for p in participants))
    del participants  # Enrollment PCM is not written to disk.
    input("Press Enter to start the conversation. Ctrl+C ends the session. ")
    chunks = queue.Queue(maxsize=4)
    failed = []
    def callback(data, frames, timing, status):
        if status:
            failed.append("Microphone overflow or device error")
            raise sd.CallbackAbort
        try:
            chunks.put_nowait((bytes(data), time.monotonic()))
        except queue.Full:
            failed.append("Inference cannot keep up with capture; stopping instead of silently dropping audio")
            raise sd.CallbackAbort
    stream_file = None
    if args.events:
        args.events.parent.mkdir(parents=True, exist_ok=True)
        stream_file = args.events.open("w", encoding="utf-8")
    try:
        with sd.RawInputStream(samplerate=16000, channels=1, dtype="int16", blocksize=4000, device=args.device, callback=callback):
            for sequence in range(args.seconds * 4):
                if failed:
                    raise RuntimeError(failed[0])
                pcm, captured_at = chunks.get(timeout=3)
                attribution = api(args.api, "/speaker/session/" + session + "/audio", {"sequence": sequence, "audio_base64": base64.b64encode(pcm).decode()})
                elapsed = (time.monotonic() - captured_at) * 1000
                probability = attribution["confidence"]
                confidence = "unavailable" if probability is None else f"{probability:.0%}"
                print(f'{attribution["start_ms"] / 1000:6.2f}s  {attribution["speaker_id"] or "unknown":16} confidence={confidence:11} {attribution["status"]:10} {elapsed:.0f} ms  segment={attribution["segment_id"]}')
                if stream_file:
                    stream_file.write(json.dumps({"attribution": attribution, "capture_end_to_response_ms": elapsed}) + "\n")
                    stream_file.flush()
    except KeyboardInterrupt:
        print("Conversation stopped.")
    finally:
        if stream_file:
            stream_file.close()
        api(args.api, "/speaker/session/" + session + "/end", {})
        print("Session ended:", session)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8080")
    commands = parser.add_subparsers(dest="command", required=True)
    live = commands.add_parser("run")
    live.add_argument("--names", nargs="+", required=True)
    live.add_argument("--session")
    live.add_argument("--seconds", type=int, default=60)
    live.add_argument("--device", type=int)
    live.add_argument("--events", type=Path)
    correction = commands.add_parser("correct")
    correction.add_argument("session"); correction.add_argument("segment"); correction.add_argument("speaker")
    args = parser.parse_args()
    if args.command == "run":
        if not 1 <= args.seconds <= 3600:
            parser.error("seconds must be 1–3600")
        run(args)
    else:
        print(json.dumps(api(args.api, f"/speaker/session/{args.session}/correct", {"segment_id": args.segment, "actual_speaker": args.speaker}), indent=2))
