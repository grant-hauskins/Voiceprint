"""Enroll participants, stream a shared microphone to the Java API, and transcribe each finished turn.

Speaker identity comes from the Voiceprint API per 250 ms chunk. This client groups consecutive
same-speaker chunks into turns, transcribes each finished turn locally with faster-whisper, prints
"[Name m:ss.s-m:ss.s] words" and stores the line through POST /speaker/session/{id}/utterances so an
MCP client can read it with get_transcript. The API never transcribes audio itself.
"""
import argparse
import base64
import json
import os
import queue
import sys
import threading
import time
import urllib.request
import uuid
import wave
from pathlib import Path

CHUNK_BYTES = 8000          # 250 ms of mono 16 kHz PCM16
LEAD_CHUNKS = 4             # attribution lags speech onset by up to the 1.5 s context; include 1 s before
TAIL_CHUNKS = 1
GAP_CLOSE_CHUNKS = 3        # 750 ms without this speaker ends the turn
MIN_TURN_CHUNKS = 2
MAX_TURN_CHUNKS = 60        # split monologues at 15 s so text arrives while they are still talking


def api(base, path, body=None):
    headers = {"Content-Type": "application/json"}
    if os.environ.get("VOICEPRINT_API_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["VOICEPRINT_API_TOKEN"]
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(base + path, data, headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail); detail = f'{detail.get("error")}: {detail.get("message")}'
        except ValueError:
            pass
        raise RuntimeError(f"{path} -> HTTP {error.code} {detail}") from None


def clock(ms):
    return f"{ms // 60000}:{(ms // 1000) % 60:02d}.{(ms // 100) % 10}"


def read_wav(path):
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError(f"{path}: need mono 16 kHz 16-bit PCM WAV")
        return w.readframes(w.getnframes())


class Transcriber:
    """Runs faster-whisper on finished turns in a background thread and posts the text."""

    def __init__(self, base, session, names, model_name, log):
        from faster_whisper import WhisperModel
        self.model = WhisperModel(model_name, device="cpu", compute_type="int8")
        self.base, self.session, self.names, self.log = base, session, names, log
        self.jobs = queue.Queue()
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def submit(self, speaker_id, start_ms, end_ms, pcm):
        self.jobs.put((speaker_id, start_ms, end_ms, pcm))

    def finish(self, timeout=60):
        self.jobs.put(None)
        self.thread.join(timeout)

    def loop(self):
        import numpy as np
        while True:
            job = self.jobs.get()
            if job is None:
                return
            speaker_id, start_ms, end_ms, pcm = job
            try:
                audio = np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0
                segments, _ = self.model.transcribe(audio, language="en", beam_size=1, condition_on_previous_text=False,
                                                    vad_filter=True, no_speech_threshold=0.5)
                text = " ".join(s.text.strip() for s in segments).strip()
                if not text:
                    continue
                name = self.names.get(speaker_id, "unknown")
                print(f"[{name} {clock(start_ms)}-{clock(end_ms)}] {text}", flush=True)
                stored = api(self.base, f"/speaker/session/{self.session}/utterances",
                             {"speaker_id": speaker_id, "start_ms": start_ms, "end_ms": end_ms, "text": text, "source": "faster_whisper"})
                if self.log:
                    self.log.write(json.dumps({"utterance": stored, "text": text, "speaker_id": speaker_id}) + "\n"); self.log.flush()
            except Exception as error:  # keep streaming even if one transcription fails
                print(f"transcription failed for {clock(start_ms)}-{clock(end_ms)}: {error}", file=sys.stderr, flush=True)


class Turns:
    """Groups per-chunk attributions into speaker turns and hands finished turns to the transcriber."""

    def __init__(self, transcriber, verbose):
        self.transcriber, self.verbose = transcriber, verbose
        self.chunks = {}        # sequence -> pcm
        self.current = None     # {"speaker", "start", "last", "gap"}
        self.emitted_through = -1

    def observe(self, sequence, pcm, attribution):
        self.chunks[sequence] = pcm
        speaker = attribution.get("speaker_id")
        status = attribution.get("status")
        cur = self.current
        if speaker:
            if cur and cur["speaker"] == speaker:
                cur["last"], cur["gap"] = sequence, 0
                if cur["last"] - cur["start"] + 1 >= MAX_TURN_CHUNKS:
                    self.close(); self.current = {"speaker": speaker, "start": sequence + 1, "last": sequence, "gap": 0}
            else:
                if cur:
                    self.close()
                self.current = {"speaker": speaker, "start": sequence, "last": sequence, "gap": 0}
                if self.verbose:
                    print(f"  {clock(attribution['start_ms'])} {speaker} speaking", flush=True)
        elif cur:
            cur["gap"] += 1
            if cur["gap"] >= GAP_CLOSE_CHUNKS:
                self.close()
        for old in [s for s in self.chunks if s < sequence - MAX_TURN_CHUNKS - LEAD_CHUNKS - 8]:
            del self.chunks[old]

    def close(self):
        cur, self.current = self.current, None
        if not cur or cur["last"] < cur["start"] or cur["last"] - cur["start"] + 1 < MIN_TURN_CHUNKS:
            return
        first = max(cur["start"] - LEAD_CHUNKS, self.emitted_through + 1, 0)
        last = cur["last"] + TAIL_CHUNKS
        pcm = b"".join(self.chunks[s] for s in range(first, last + 1) if s in self.chunks)
        self.emitted_through = last
        if self.transcriber:
            self.transcriber.submit(cur["speaker"], first * 250, (last + 1) * 250, pcm)


def run(args):
    if not 2 <= len(args.names) <= 4:
        raise ValueError("Choose 2–4 participants")
    replay = args.stream_wav is not None
    if replay and len(args.enroll_wavs or []) != len(args.names):
        raise ValueError("--enroll-wavs needs one WAV per name when --stream-wav is used")
    transcriber = None
    if not args.no_transcribe:
        print(f"Loading faster-whisper {args.model} (first run downloads the model)...", flush=True)
    session = args.session or "live_" + uuid.uuid4().hex[:12]
    names = {f"participant_{i}": name for i, name in enumerate(args.names, 1)}
    while True:
        participants = []
        for index, name in enumerate(args.names, 1):
            pid = f"participant_{index}"
            if replay:
                pcm = read_wav(args.enroll_wavs[index - 1])
            else:
                import sounddevice as sd
                input(f"{name}: press Enter, then talk continuously for 8 seconds, close to the microphone. ")
                audio = sd.rec(8 * 16000, samplerate=16000, channels=1, dtype="int16", device=args.device)
                sd.wait(); pcm = audio.astype("<i2").tobytes()
                level = max(abs(int(audio.min())), int(audio.max()))
                print(f"  recorded {name}: peak level {level} of 32767" + ("  (too quiet: move closer or raise input gain)" if level < 1500 else ""), flush=True)
            participants.append({"id": pid, "name": name, "opening_statement_audio": base64.b64encode(pcm).decode()})
        try:
            api(args.api, "/speaker/session/init", {"session_id": session, "sample_rate": 16000, "audio_format": "pcm_s16le", "participants": participants})
            break
        except RuntimeError as error:
            if "422" not in str(error) or replay:
                raise
            print(f"Enrollment rejected: {error}", flush=True)
            print("One statement had silence, two voices, or a pause long enough to look like a speaker change. Recording everyone again.", flush=True)
    print("Session", session, "ready:", ", ".join(f"{pid}={name}" for pid, name in names.items()), flush=True)
    del participants  # Enrollment PCM is not written to disk.
    log = None
    if args.events:
        args.events.parent.mkdir(parents=True, exist_ok=True)
        log = args.events.open("w", encoding="utf-8")
    if not args.no_transcribe:
        transcriber = Transcriber(args.api, session, names, args.model, log)
    turns = Turns(transcriber, args.verbose)
    if not replay:
        input("Press Enter to start the conversation. Ctrl+C ends the session. ")
    chunks = queue.Queue(maxsize=4)
    failed = []

    def callback(data, frames, timing, status):
        if status:
            failed.append("Microphone overflow or device error"); raise sd.CallbackAbort
        try:
            chunks.put_nowait((bytes(data), time.monotonic()))
        except queue.Full:
            failed.append("Inference cannot keep up with capture; stopping instead of silently dropping audio"); raise sd.CallbackAbort

    def feed_file():
        pcm = read_wav(args.stream_wav)
        for offset in range(0, len(pcm) - CHUNK_BYTES + 1, CHUNK_BYTES):
            chunks.put((pcm[offset:offset + CHUNK_BYTES], time.monotonic()))
            if args.realtime:
                time.sleep(0.25)
        chunks.put(None)

    try:
        if replay:
            threading.Thread(target=feed_file, daemon=True).start()
            stream = None
        else:
            import sounddevice as sd
            stream = sd.RawInputStream(samplerate=16000, channels=1, dtype="int16", blocksize=4000, device=args.device, callback=callback)
            stream.start()
        try:
            for sequence in range(args.seconds * 4):
                if failed:
                    raise RuntimeError(failed[0])
                item = chunks.get(timeout=5)
                if item is None:
                    break
                pcm, captured_at = item
                attribution = api(args.api, f"/speaker/session/{session}/audio", {"sequence": sequence, "audio_base64": base64.b64encode(pcm).decode()})
                elapsed = (time.monotonic() - captured_at) * 1000
                turns.observe(sequence, pcm, attribution)
                if args.verbose:
                    print(f'{attribution["start_ms"] / 1000:6.2f}s  {attribution["speaker_id"] or "-":16} {attribution["status"]:10} {elapsed:.0f} ms  segment={attribution["segment_id"]}', flush=True)
                if log:
                    log.write(json.dumps({"attribution": attribution, "capture_end_to_response_ms": elapsed}) + "\n"); log.flush()
        finally:
            if stream:
                stream.stop(); stream.close()
    except KeyboardInterrupt:
        print("Conversation stopped.", flush=True)
    finally:
        turns.close()
        if transcriber:
            print("Finishing transcription...", flush=True)
            transcriber.finish()
        if log:
            log.close()
        api(args.api, f"/speaker/session/{session}/end", {})
        print("Session ended:", session, flush=True)
        print(f"Transcript: GET {args.api}/speaker/session/{session}/utterances  or MCP get_transcript(session_id=\"{session}\")", flush=True)


def show(args):
    print(api(args.api, f"/speaker/session/{args.session}/utterances?after_id={args.after}&limit=200")["text"], end="")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://127.0.0.1:8080")
    commands = parser.add_subparsers(dest="command", required=True)
    live = commands.add_parser("run", help="enroll, stream and transcribe")
    live.add_argument("--names", nargs="+", required=True)
    live.add_argument("--session")
    live.add_argument("--seconds", type=int, default=60)
    live.add_argument("--device", type=int, help="sounddevice input index")
    live.add_argument("--events", type=Path, help="JSONL log of attributions and utterances")
    live.add_argument("--model", default="base.en", help="faster-whisper model (tiny.en, base.en, small.en)")
    live.add_argument("--no-transcribe", action="store_true")
    live.add_argument("--verbose", action="store_true", help="print every 250 ms attribution")
    live.add_argument("--enroll-wavs", nargs="+", type=Path, help="replay mode: one enrollment WAV per name")
    live.add_argument("--stream-wav", type=Path, help="replay mode: conversation WAV instead of the microphone")
    live.add_argument("--realtime", action="store_true", help="replay at capture cadence")
    transcript = commands.add_parser("transcript", help="print stored utterances")
    transcript.add_argument("session"); transcript.add_argument("--after", type=int, default=0)
    correction = commands.add_parser("correct")
    correction.add_argument("session"); correction.add_argument("segment"); correction.add_argument("speaker")
    args = parser.parse_args()
    if args.command == "run":
        if not 1 <= args.seconds <= 3600:
            parser.error("seconds must be 1–3600")
        run(args)
    elif args.command == "transcript":
        show(args)
    else:
        print(json.dumps(api(args.api, f"/speaker/session/{args.session}/correct", {"segment_id": args.segment, "actual_speaker": args.speaker}), indent=2))
