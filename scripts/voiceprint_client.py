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
from collections import Counter, deque
from pathlib import Path

CHUNK_BYTES = 8000          # 250 ms of mono 16 kHz PCM16
CHUNK_MS = 250
LEAD_CHUNKS = 4             # attribution lags speech onset by up to the 1.5 s context; include 1 s before
TAIL_CHUNKS = 1
GAP_CLOSE_CHUNKS = 3        # 750 ms without this speaker ends the turn
MIN_TURN_CHUNKS = 2
MAX_TURN_CHUNKS = 60        # split monologues at 15 s so text arrives while they are still talking
OVERLAP_OPEN_CHUNKS = 2     # consecutive overlap chunks that open an overlap segment
OVERLAP_CLOSE_CHUNKS = 2    # consecutive clear chunks that close it


class ConsentError(RuntimeError):
    """The room no longer has authoritative prior permission for this operation."""


class ConsentGuard:
    """Fresh API checks before protected actions; authorization failures latch until restart."""
    def __init__(self, base, session, hosted=False):
        self.base, self.session, self.hosted = base, session, hosted
        self.epoch = None
        self.failed = threading.Event()
        self.valid_until = 0

    def deny(self):
        self.valid_until = 0
        self.failed.set()

    def require(self, scope="local_processing"):
        if self.failed.is_set():
            raise ConsentError("Room authorization stopped; capture and disclosure are blocked")
        try:
            state = api(self.base, f"/speaker/session/{self.session}/consent")
            epoch = (state.get("policy_version"), state.get("consent_method_version"), state.get("roster_version"))
            people = state.get("participants") or []
            scopes = state.get("scopes") or {}
            if (state.get("session_id") != self.session or state.get("allowed") is not True
                    or state.get("state") != "active" or not all(epoch)
                    or not people or any(p.get("bipa_consent_granted") is not True for p in people)
                    or scopes.get("local_processing") is not True or scopes.get(scope) is not True
                    or state.get("retention_deadline_ms", 0) <= time.time() * 1000
                    or (self.epoch is not None and epoch != self.epoch)):
                raise ConsentError("Prior current written release and disclosure permission are required for every human")
            if self.hosted and (scopes.get("openai_audio") is not True or scopes.get("hosted_mcp") is not True):
                raise ConsentError("Hosted audio and MCP require every release and reviewed vendor configuration")
            self.epoch = epoch
            self.valid_until = time.monotonic() + 1.0
            return state
        except Exception as error:
            self.deny()
            reason = str(error) if isinstance(error, ConsentError) else "consent status request failed"
            raise ConsentError(f"Consent unavailable, revoked, expired, or changed; stopped all protected processing ({reason})") from None


def require_consent(consent, scope="local_processing"):
    if consent is None:
        raise ConsentError("An authoritative ConsentGuard is required before reading or capturing audio")
    return consent.require(scope)


def prepare_room(base, session, names, contacts=None, hosted=False, stop=None):
    """Collect roster text only. The actual people sign in the GUI while the mic stays closed.
    `stop` is an optional threading.Event that abandons the wait (the room is left pending for the API sweeper)."""
    if not api_token():
        raise ConsentError("VOICEPRINT_API_TOKEN must authenticate the operator before the consent flow")
    notice = api(base, "/privacy/notice")
    if not notice.get("configured"):
        raise ConsentError("Configure the controller name, address, and email in the API before consent")
    vendors = notice.get("vendors") or {}
    if hosted and not (vendors.get("openai_reviewed") is True and vendors.get("cloudflare_reviewed") is True):
        # Fail before anyone signs: the API would compute the hosted scopes as false no matter what people check.
        raise ConsentError("Hosted agents are blocked until the operator has reviewed the OpenAI and Cloudflare account settings and set "
                           "VOICEPRINT_OPENAI_REVIEWED=true and VOICEPRINT_CLOUDFLARE_REVIEWED=true in data\\launcher.env (then restart)")
    if contacts is not None and len(contacts) != len(names):
        raise ValueError("Provide one --contacts entry per full participant name")
    contacts = contacts or [input(f"{name}: type your email or phone (unverified): ").strip() for name in names]
    if any(not name.strip() for name in names) or any(not contact.strip() for contact in contacts):
        raise ValueError("Full typed names and contacts must be nonempty")
    api(base, "/privacy/rooms", {"session_id": session, "purpose_id": "live_conversation_v1",
        "participants": [{"id": f"participant_{i}", "name": name, "contact": contact}
                         for i, (name, contact) in enumerate(zip(names, contacts), 1)]})
    print(f"Pending session {session}. Each person must personally sign at {base}/ui. Microphone remains closed.", flush=True)
    while True:
        state = api(base, f"/speaker/session/{session}/consent")
        if state.get("state") in ("revoked", "destroying", "destroyed", "legacy_blocked"):
            raise ConsentError("This room cannot be authorized; start a new consent room")
        if state.get("allowed"):
            scopes = state.get("scopes") or {}
            if hosted and not (scopes.get("openai_audio") is True and scopes.get("hosted_mcp") is True):
                raise ConsentError("Everyone signed, but at least one release left an optional disclosure box unchecked, so the room "
                                   "cannot use OpenAI audio or hosted MCP. End this room and start again with both disclosure boxes checked")
            guard = ConsentGuard(base, session, hosted)
            guard.require("openai_audio" if hosted else "local_processing")
            return guard
        if stop is not None and stop.is_set():
            raise ConsentError("Stopped while waiting for written releases")
        time.sleep(.5)


def api_token():
    value = os.environ.get("VOICEPRINT_API_TOKEN")
    if value:
        return value
    path = Path(__file__).resolve().parents[1] / "data" / "api-token.txt"
    return path.read_text(encoding="utf-8").strip() if path.exists() else None


def api(base, path, body=None):
    headers = {"Content-Type": "application/json"}
    bearer = api_token()
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    if path == "/speaker/session/init" and body:
        headers["X-Voiceprint-Session"] = body["session_id"]
    if path == "/privacy/rooms" and body is not None:
        # The API accepts pending-room creation only from its exact local origin; the runtime
        # is that local operator process. Releases are still signed only by each person in the GUI.
        headers["Origin"] = base
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(base + path, data, headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=2 if path.endswith("/consent") else 40) as response:
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


def read_wav(path, consent=None):
    require_consent(consent)
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

    def __init__(self, base, session, names, model_name="base.en", log=None, on_utterance=None, consent=None):
        require_consent(consent)
        self.consent = consent
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
        require_consent(self.consent)
        self.jobs.put((utterance, pcm))

    def discard(self):
        while True:
            try:
                self.jobs.get_nowait()
            except queue.Empty:
                break

    def finish(self, timeout=60):
        self.jobs.put(None)
        self.thread.join(timeout)
        if self.thread.is_alive():
            self.consent.deny()
            self.discard()
            print("Transcription did not drain before shutdown; later persistence is blocked.", file=sys.stderr, flush=True)

    def transcribe(self, pcm):
        require_consent(self.consent)
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
                require_consent(self.consent)
                stored = api(self.base, f"/speaker/session/{self.session}/utterances", utterance)
                utterance["utterance_id"] = stored["utterance_id"]; utterance["label"] = stored.get("label")
                print(f"Stored human turn #{utterance['utterance_id']} [{utterance.get('label')}]; text available in local GUI.", flush=True)
                if self.log:
                    self.log.write(json.dumps({"utterance": utterance}) + "\n"); self.log.flush()
                if self.on_utterance:
                    self.on_utterance(utterance)
            except ConsentError:
                self.discard()
                return
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

    def __init__(self, base, session, turns, log=None, on_chunk=None, verbose=False, consent=None):
        require_consent(consent)
        self.consent = consent
        self.base, self.session, self.turns, self.log, self.on_chunk, self.verbose = base, session, turns, log, on_chunk, verbose
        self.sequence = 0
        self.latencies = deque(maxlen=120)

    def feed(self, pcm, captured_at=None):
        started = time.monotonic()
        require_consent(self.consent)
        authorized = time.monotonic()
        attribution = api(self.base, f"/speaker/session/{self.session}/audio", {"sequence": self.sequence, "audio_base64": base64.b64encode(pcm).decode()})
        responded = time.monotonic()
        elapsed = None if captured_at is None else (responded - captured_at) * 1000
        self.turns.observe(self.sequence, pcm, attribution)
        if self.verbose:
            print(f'{attribution["start_ms"] / 1000:6.2f}s  {attribution["speaker_id"] or "-":16} {attribution["status"]:10} {attribution.get("overlap"):9} {"" if elapsed is None else f"{elapsed:.0f} ms"}  segment={attribution["segment_id"]}', flush=True)
        if self.log:
            self.log.write(json.dumps({"attribution": attribution, "capture_end_to_response_ms": elapsed}) + "\n"); self.log.flush()
        if self.on_chunk:
            self.on_chunk(pcm, attribution)
        finished = time.monotonic()
        self.latencies.append({
            "queue_ms": None if captured_at is None else (started - captured_at) * 1000,
            "consent_ms": (authorized - started) * 1000,
            "api_ms": (responded - authorized) * 1000,
            "consumer_ms": (finished - responded) * 1000,
            "processing_ms": (finished - started) * 1000,
        })
        self.sequence += 1
        return attribution

    def timing_summary(self):
        """Bounded numeric diagnostics only; no audio, identity, or transcript content."""
        if not self.latencies:
            return "Audio timing: no completed chunks."
        parts = []
        for key in ("processing_ms", "queue_ms", "consent_ms", "api_ms", "consumer_ms"):
            values = sorted(row[key] for row in self.latencies if row[key] is not None)
            if values:
                parts.append(f"{key.removesuffix('_ms')} avg={sum(values) / len(values):.0f} max={max(values):.0f} ms")
        return f"Audio timing (last {len(self.latencies)} chunks; {CHUNK_MS} ms/chunk budget): " + "; ".join(parts)

    def end(self):
        self.turns.flush()
        return api(self.base, f"/speaker/session/{self.session}/end", {})


def enroll(base, session, names, record, replay_wavs=None, consent=None, on_reject=None):
    """Enroll `names` (list) and return {participant_id: name}. `record(name)` returns 8 s of PCM16 from the mic.
    `on_reject(message)` is told when the API rejects the statements and everyone is recorded again."""
    ids = {f"participant_{i}": name for i, name in enumerate(names, 1)}
    require_consent(consent)
    while True:
        participants = []
        for index, (pid, name) in enumerate(ids.items()):
            require_consent(consent)
            pcm = read_wav(replay_wavs[index], consent) if replay_wavs else record(name)
            participants.append({"id": pid, "name": name, "opening_statement_audio": base64.b64encode(pcm).decode()})
        try:
            require_consent(consent)
            api(base, "/speaker/session/init", {"session_id": session, "sample_rate": 16000, "audio_format": "pcm_s16le", "participants": participants})
            return ids
        except RuntimeError as error:
            if "422" not in str(error) or replay_wavs:
                raise
            message = "One statement had silence, two voices, or a pause long enough to look like a speaker change. Recording everyone again."
            print(f"Enrollment rejected: {error}", flush=True)
            print(message, flush=True)
            if on_reject is not None:
                on_reject(f"Enrollment rejected ({error}). {message}")


def record_from_mic(device, consent=None):
    def record(name):
        import numpy as np
        require_consent(consent)
        input(f"{name}: after your written release, press Enter, then speak for 8 seconds, starting: "
              f"I, {name}, consent to Voiceprint collecting my voiceprint for identifying consenting speakers and providing a speaker-attributed transcript during the current room conversation today. ")
        with Microphone(device, consent=consent) as capture:
            pcm = b"".join(capture.get()[0] for _ in range(32))
        require_consent(consent)
        audio = np.frombuffer(pcm, dtype="<i2")
        level = max(abs(int(audio.min())), int(audio.max()))
        print(f"  recorded {name}: peak level {level} of 32767" + ("  (too quiet: move closer or raise input gain)" if level < 1500 else ""), flush=True)
        return pcm
    return record


class Microphone:
    """Shared-microphone capture into a bounded queue of (pcm, captured_at); stops instead of dropping audio."""

    def __init__(self, device=None, consent=None):
        self.device = device
        self.consent = consent
        self.chunks = queue.Queue(maxsize=4)
        self.failed = []
        self.stream = None

    def _callback(self, data, frames, timing, status):
        import sounddevice as sd
        if self.consent is None or self.consent.failed.is_set() or time.monotonic() >= self.consent.valid_until:
            self.failed.append("Consent stale or revoked; capture stopped")
            raise sd.CallbackAbort
        if status:
            self.failed.append("Microphone overflow or device error"); raise sd.CallbackAbort
        try:
            self.chunks.put_nowait((bytes(data), time.monotonic()))
        except queue.Full:
            self.failed.append(f"Audio processing fell behind capture: {self.chunks.maxsize} queued chunks "
                               f"({self.chunks.maxsize * CHUNK_MS} ms buffer full); stopped to avoid an audio gap")
            raise sd.CallbackAbort

    def __enter__(self):
        require_consent(self.consent)
        import sounddevice as sd
        self.stream = sd.RawInputStream(samplerate=16000, channels=1, dtype="int16", blocksize=CHUNK_BYTES // 2, device=self.device, callback=self._callback)
        self.stream.start()
        return self

    def __exit__(self, *exc):
        if self.stream:
            self.stream.stop(); self.stream.close()

    def get(self, timeout=5):
        try:
            require_consent(self.consent)
        except ConsentError:
            if self.stream:
                self.stream.abort()
            while not self.chunks.empty():
                self.chunks.get_nowait()
            raise
        if self.failed:
            raise RuntimeError(self.failed[0])
        return self.chunks.get(timeout=timeout)


def file_chunks(path, realtime=False, consent=None):
    pcm = read_wav(path, consent)
    for offset in range(0, len(pcm) - CHUNK_BYTES + 1, CHUNK_BYTES):
        require_consent(consent)
        yield pcm[offset:offset + CHUNK_BYTES], time.monotonic()
        if realtime:
            time.sleep(CHUNK_MS / 1000)
