"""Client library for the Voiceprint API: enrollment, chunk streaming, turn grouping, local ASR.

Speaker identity comes from the API per 250 ms chunk. `Turns` groups consecutive same-speaker chunks into
utterances, tracks similarity/margin/overlap/abstention per utterance, and isolates overlap segments
(two people talking at once) as their own rows with both candidate ids. `Transcriber` runs faster-whisper
on each finished segment and stores the text through POST /speaker/session/{id}/utterances.
The API never transcribes audio itself.
"""
import base64
import json
import os
import queue
import sys
import threading
import time
import urllib.request
import wave
from collections import Counter

CHUNK_BYTES = 8000          # 250 ms of mono 16 kHz PCM16
CHUNK_MS = 250
LEAD_CHUNKS = 4             # attribution lags speech onset by up to the 1.5 s context; include 1 s before
TAIL_CHUNKS = 1
GAP_CLOSE_CHUNKS = 3        # 750 ms without this speaker ends the turn
MIN_TURN_CHUNKS = 2
MAX_TURN_CHUNKS = 60        # split monologues at 15 s so text arrives while they are still talking
OVERLAP_OPEN_CHUNKS = 2     # consecutive overlap chunks that open an overlap segment
OVERLAP_CLOSE_CHUNKS = 2    # consecutive clear chunks that close it


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


def format_line(utterance, names):
    """Terminal line for a finished utterance, mirroring the API's compact transcript format."""
    span = f'{clock(utterance["start_ms"])}-{clock(utterance["end_ms"])}'
    if utterance.get("speaker_id") is None and utterance.get("candidates"):
        who = "OVERLAP " + "+".join(names.get(c, c) for c in utterance["candidates"])
    else:
        who = names.get(utterance.get("speaker_id"), "unknown")
    label = utterance.get("label")
    # "#id" appears only once the row is stored, i.e. visible to agents via get_transcript under that same id.
    stored = f"#{utterance['utterance_id']} " if utterance.get("utterance_id") is not None else "   "
    return f"{stored}[{who} {span}{' ' + label if label else ''}] {utterance['text']}"


class Transcriber:
    """Runs faster-whisper on finished segments in a background thread and posts the text."""

    def __init__(self, base, session, names, model_name="base.en", log=None, on_utterance=None):
        from faster_whisper import WhisperModel
        self.model = WhisperModel(model_name, device="cpu", compute_type="int8")
        self.base, self.session, self.names, self.log, self.on_utterance = base, session, names, log, on_utterance
        self.jobs = queue.Queue()
        self.in_flight = 0
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def idle(self):
        """True when nothing is queued or being transcribed; the gate waits for this before letting the agent speak."""
        return self.jobs.empty() and self.in_flight == 0

    def submit(self, utterance, pcm):
        self.jobs.put((utterance, pcm))

    def finish(self, timeout=60):
        self.jobs.put(None)
        self.thread.join(timeout)

    def transcribe(self, pcm):
        import numpy as np
        audio = np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0
        segments, _ = self.model.transcribe(audio, language="en", beam_size=1, condition_on_previous_text=False,
                                            vad_filter=True, no_speech_threshold=0.5)
        return " ".join(s.text.strip() for s in segments).strip()

    def loop(self):
        while True:
            job = self.jobs.get()
            if job is None:
                return
            utterance, pcm = job
            self.in_flight += 1
            try:
                text = self.transcribe(pcm)
                if not text:
                    continue
                utterance = dict(utterance, text=text, source="faster_whisper")
                stored = api(self.base, f"/speaker/session/{self.session}/utterances", utterance)
                utterance["utterance_id"] = stored["utterance_id"]; utterance["label"] = stored.get("label")
                print(format_line(utterance, self.names), flush=True)
                if self.log:
                    self.log.write(json.dumps({"utterance": utterance}) + "\n"); self.log.flush()
                if self.on_utterance:
                    self.on_utterance(utterance)
            except Exception as error:  # keep streaming even if one transcription fails
                print(f'transcription failed for {clock(utterance["start_ms"])}-{clock(utterance["end_ms"])}: {error}', file=sys.stderr, flush=True)
            finally:
                self.in_flight -= 1


class Turns:
    """Groups per-chunk attributions into speaker turns and overlap segments; emits finished segments via `sink(utterance, pcm)`."""

    def __init__(self, sink, verbose=False):
        self.sink, self.verbose = sink, verbose
        self.chunks = {}            # sequence -> pcm
        self.current = None         # open turn
        self.overlap = None         # open overlap segment
        self.overlap_run = 0
        self.emitted_through = -1

    def observe(self, sequence, pcm, attribution):
        self.chunks[sequence] = pcm
        speaker = attribution.get("speaker_id")
        status = attribution.get("status")
        overlapping = attribution.get("overlap") == "detected"
        if overlapping:
            self.overlap_run += 1
            if self.overlap:
                self.overlap["last"], self.overlap["clear_run"] = sequence, 0
                self._tally(attribution)
            elif self.overlap_run >= OVERLAP_OPEN_CHUNKS:
                self._close_turn(tail=0)  # the overlap audio owns these chunks
                self.overlap = {"start": sequence - self.overlap_run + 1, "last": sequence, "clear_run": 0, "votes": Counter()}
                self._tally(attribution)
                if self.verbose:
                    print(f"  !! overlap from {clock(attribution['start_ms'] - (self.overlap_run - 1) * CHUNK_MS)}", flush=True)
            elif self.current:
                self.current["overlap_n"] += 1; self.current["total_n"] += 1; self.current["gap"] += 1
            self._prune(sequence)
            return
        self.overlap_run = 0
        if self.overlap:
            self.overlap["clear_run"] += 1
            if self.overlap["clear_run"] >= OVERLAP_CLOSE_CHUNKS:
                self._close_overlap()
        cur = self.current
        if speaker:
            if cur and cur["speaker"] == speaker:
                cur["last"], cur["gap"] = sequence, 0
                self._stat(cur, attribution)
                if cur["last"] - cur["start"] + 1 >= MAX_TURN_CHUNKS:
                    self._close_turn()
                    self.current = self._new_turn(speaker, sequence + 1)
                    self.current["last"] = sequence
            else:
                self._close_turn()
                self.current = self._new_turn(speaker, sequence)
                self._stat(self.current, attribution)
                if self.verbose:
                    print(f"  {clock(attribution['start_ms'])} {speaker} speaking", flush=True)
        elif cur:
            cur["gap"] += 1
            if status not in ("silence", "buffering"):
                cur["abstain_n"] += 1; cur["total_n"] += 1
            if cur["gap"] >= GAP_CLOSE_CHUNKS:
                self._close_turn()
        self._prune(sequence)

    def flush(self):
        self._close_overlap(); self._close_turn()

    # -- internals -------------------------------------------------------------------------------------------------
    @staticmethod
    def _new_turn(speaker, start):
        return {"speaker": speaker, "start": start, "last": start, "gap": 0, "sims": [], "margins": [], "overlap_n": 0, "abstain_n": 0, "total_n": 0}

    @staticmethod
    def _stat(turn, attribution):
        turn["total_n"] += 1
        if attribution.get("similarity") is not None:
            turn["sims"].append(attribution["similarity"]); turn["margins"].append(attribution.get("margin") or 0.0)

    def _tally(self, attribution):
        for candidate in (attribution.get("candidates") or [])[:2]:
            self.overlap["votes"][candidate["speaker_id"]] += 1

    def _prune(self, sequence):
        for old in [s for s in self.chunks if s < sequence - MAX_TURN_CHUNKS - LEAD_CHUNKS - 8]:
            del self.chunks[old]

    def _audio(self, first, last):
        return b"".join(self.chunks[s] for s in range(first, last + 1) if s in self.chunks)

    def _close_turn(self, tail=TAIL_CHUNKS):
        cur, self.current = self.current, None
        if not cur or cur["last"] < cur["start"] or cur["last"] - cur["start"] + 1 < MIN_TURN_CHUNKS:
            return
        first = max(cur["start"] - LEAD_CHUNKS, self.emitted_through + 1, 0)
        last = cur["last"] + tail
        self.emitted_through = last
        total = max(cur["total_n"], 1)
        utterance = {
            "speaker_id": cur["speaker"], "start_ms": first * CHUNK_MS, "end_ms": (last + 1) * CHUNK_MS,
            "similarity": round(sum(cur["sims"]) / len(cur["sims"]), 4) if cur["sims"] else None,
            "margin": round(sum(cur["margins"]) / len(cur["margins"]), 4) if cur["margins"] else None,
            "overlap_ratio": round(cur["overlap_n"] / total, 3), "abstain_ratio": round(cur["abstain_n"] / total, 3),
        }
        self.sink(utterance, self._audio(first, last))

    def _close_overlap(self):
        seg, self.overlap = self.overlap, None
        if not seg:
            return
        first = max(seg["start"] - 1, self.emitted_through + 1, 0)
        last = seg["last"] + TAIL_CHUNKS
        self.emitted_through = last
        candidates = [speaker for speaker, _ in seg["votes"].most_common(2)]
        utterance = {"speaker_id": None, "start_ms": first * CHUNK_MS, "end_ms": (last + 1) * CHUNK_MS,
                     "similarity": None, "margin": None, "overlap_ratio": 1.0, "abstain_ratio": 0.0, "candidates": candidates}
        self.sink(utterance, self._audio(first, last))


class Stream:
    """Feeds ordered 250 ms chunks to the API and routes attributions to `Turns` and callbacks."""

    def __init__(self, base, session, turns, log=None, on_chunk=None, verbose=False):
        self.base, self.session, self.turns, self.log, self.on_chunk, self.verbose = base, session, turns, log, on_chunk, verbose
        self.sequence = 0
        self.latencies = []

    def feed(self, pcm, captured_at=None):
        attribution = api(self.base, f"/speaker/session/{self.session}/audio", {"sequence": self.sequence, "audio_base64": base64.b64encode(pcm).decode()})
        elapsed = None if captured_at is None else (time.monotonic() - captured_at) * 1000
        self.turns.observe(self.sequence, pcm, attribution)
        if self.verbose:
            print(f'{attribution["start_ms"] / 1000:6.2f}s  {attribution["speaker_id"] or "-":16} {attribution["status"]:10} {attribution.get("overlap"):9} {"" if elapsed is None else f"{elapsed:.0f} ms"}  segment={attribution["segment_id"]}', flush=True)
        if self.log:
            self.log.write(json.dumps({"attribution": attribution, "capture_end_to_response_ms": elapsed}) + "\n"); self.log.flush()
        if self.on_chunk:
            self.on_chunk(pcm, attribution)
        self.sequence += 1
        return attribution

    def end(self):
        self.turns.flush()
        return api(self.base, f"/speaker/session/{self.session}/end", {})


def enroll(base, session, names, record, replay_wavs=None):
    """Enroll `names` (list) and return {participant_id: name}. `record(name)` returns 8 s of PCM16 from the mic."""
    ids = {f"participant_{i}": name for i, name in enumerate(names, 1)}
    while True:
        participants = []
        for index, (pid, name) in enumerate(ids.items()):
            pcm = read_wav(replay_wavs[index]) if replay_wavs else record(name)
            participants.append({"id": pid, "name": name, "opening_statement_audio": base64.b64encode(pcm).decode()})
        try:
            api(base, "/speaker/session/init", {"session_id": session, "sample_rate": 16000, "audio_format": "pcm_s16le", "participants": participants})
            return ids
        except RuntimeError as error:
            if "422" not in str(error) or replay_wavs:
                raise
            print(f"Enrollment rejected: {error}", flush=True)
            print("One statement had silence, two voices, or a pause long enough to look like a speaker change. Recording everyone again.", flush=True)


def record_from_mic(device):
    def record(name):
        import sounddevice as sd
        input(f"{name}: press Enter, then talk continuously for 8 seconds, close to the microphone. ")
        audio = sd.rec(8 * 16000, samplerate=16000, channels=1, dtype="int16", device=device)
        sd.wait()
        level = max(abs(int(audio.min())), int(audio.max()))
        print(f"  recorded {name}: peak level {level} of 32767" + ("  (too quiet: move closer or raise input gain)" if level < 1500 else ""), flush=True)
        return audio.astype("<i2").tobytes()
    return record


class Microphone:
    """Shared-microphone capture into a bounded queue of (pcm, captured_at); stops instead of dropping audio."""

    def __init__(self, device=None):
        self.device = device
        self.chunks = queue.Queue(maxsize=4)
        self.failed = []
        self.stream = None

    def _callback(self, data, frames, timing, status):
        import sounddevice as sd
        if status:
            self.failed.append("Microphone overflow or device error"); raise sd.CallbackAbort
        try:
            self.chunks.put_nowait((bytes(data), time.monotonic()))
        except queue.Full:
            self.failed.append("Inference cannot keep up with capture; stopping instead of silently dropping audio"); raise sd.CallbackAbort

    def __enter__(self):
        import sounddevice as sd
        self.stream = sd.RawInputStream(samplerate=16000, channels=1, dtype="int16", blocksize=CHUNK_BYTES // 2, device=self.device, callback=self._callback)
        self.stream.start()
        return self

    def __exit__(self, *exc):
        if self.stream:
            self.stream.stop(); self.stream.close()

    def get(self, timeout=5):
        if self.failed:
            raise RuntimeError(self.failed[0])
        return self.chunks.get(timeout=timeout)


def file_chunks(path, realtime=False):
    pcm = read_wav(path)
    for offset in range(0, len(pcm) - CHUNK_BYTES + 1, CHUNK_BYTES):
        yield pcm[offset:offset + CHUNK_BYTES], time.monotonic()
        if realtime:
            time.sleep(CHUNK_MS / 1000)
