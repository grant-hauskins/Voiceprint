"""Exercise the live API with paced public speech fixtures; NOT an accuracy benchmark.

Enrollment uses utterances 1–3; the stream uses disjoint utterances 4–6.
Raw fixtures stay in ignored data/. No batch-audio API is introduced.
"""
import argparse
import base64
import hashlib
import json
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import numpy as np
import soundfile as sf


def request(base, path, body=None):
    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(error.read().decode()) from error


def encode(samples):
    return base64.b64encode((np.clip(samples, -1, .999969) * 32768).astype("<i2").tobytes()).decode()


def main(args):
    def speech(speaker, utterances):
        arrays = []
        for utterance in utterances:
            data, rate = sf.read(args.fixtures / f"spk{speaker}_snt{utterance}.wav", dtype="float32")
            assert rate == 16000 and data.ndim == 1
            arrays.append(data)
        return np.concatenate(arrays)
    session = "smoke_" + uuid.uuid4().hex[:12]
    prefix = "/speaker/session/" + session
    result = request(args.api, "/speaker/session/init", {
        "session_id": session, "sample_rate": 16000, "audio_format": "pcm_s16le",
        "participants": [{"id": f"speaker_{i}", "name": f"Fixture speaker {i}", "opening_statement_audio": encode(speech(i, (1, 2, 3)))} for i in (1, 2)],
    })
    print("Enrollment:", json.dumps(result), flush=True)
    a, b = speech(1, (4, 5, 6)), speech(2, (4, 5, 6))
    # Match RMS before mixing so one speaker does not drown out the other.
    mixed_a = np.resize(a, 64000); mixed_b = np.resize(b, 64000)
    mixed = .10 * mixed_a / np.sqrt(np.mean(mixed_a ** 2)) + .10 * mixed_b / np.sqrt(np.mean(mixed_b ** 2))
    phases = [("speaker_1", a), ("speaker_2", b), ("overlap", mixed), ("silence", np.zeros(24000, dtype=np.float32))]
    rows, latencies, sequence, phase_start = [], [], 0, 0
    began = time.monotonic()
    for label, samples in phases:
        samples = np.pad(samples, (0, (-len(samples)) % 4000))
        for offset in range(0, len(samples), 4000):
            # Simulate capture: the complete chunk is available only at its end.
            deadline = began + (sequence + 1) * .25
            time.sleep(max(0, deadline - time.monotonic()))
            sent = time.monotonic()
            body = request(args.api, prefix + "/audio", {"sequence": sequence, "audio_base64": encode(samples[offset:offset + 4000])})
            received = time.monotonic()
            latencies.append((received - sent) * 1000)
            rows.append({"fixture_label": label, "stable_context": body["context_ms"] == 1500 and body["context_start_ms"] >= phase_start,
                         "capture_end_to_response_ms": (received - deadline) * 1000, "attribution": body})
            sequence += 1
        print("Phase", label, "completed", flush=True)
        phase_start = sequence * 250
    for speaker in ("speaker_1", "speaker_2"):
        assert any(r["fixture_label"] == speaker and r["stable_context"] and r["attribution"]["speaker_id"] == speaker for r in rows), "No match for " + speaker
    overlap_rows = [r for r in rows if r["fixture_label"] == "overlap" and r["stable_context"]]
    assert any(r["attribution"]["overlap"] == "detected" for r in overlap_rows), "Real model did not detect the mixed fixture"
    db = sqlite3.connect(args.db)
    eligible = db.execute("SELECT id,body,embedding FROM segments WHERE session_id=? AND eligible=1 AND speaker_id IS NOT NULL ORDER BY sequence", (session,)).fetchall()
    assert eligible, "No clean speech embedding available for correction"
    segment_id, body_json, embedding_json = eligible[0]
    body = json.loads(body_json)
    speaker = body["speaker_id"]
    before = db.execute("SELECT vector FROM profiles WHERE session_id=? AND id=?", (session, speaker)).fetchone()[0]
    correction = request(args.api, prefix + "/correct", {"segment_id": segment_id, "actual_speaker": speaker})
    assert correction["profile_updated"] is True
    after = db.execute("SELECT vector FROM profiles WHERE session_id=? AND id=?", (session, speaker)).fetchone()[0]
    assert before != after, "Correction did not change the stored profile"
    embedding = np.array(json.loads(embedding_json))
    similarity_before = float(embedding @ np.array(json.loads(before)))
    similarity_after = float(embedding @ np.array(json.loads(after)))
    assert similarity_after > similarity_before, "Corrected example did not move the profile toward the speaker"
    transcript = request(args.api, prefix + "/transcript?limit=200")["transcript"]
    assert len(transcript) == len(rows)
    assert request(args.api, prefix + "/current")["speaker_id"] is None
    request(args.api, prefix + "/end", {})
    assert request(args.api, prefix + "/current")["status"] == "ended"
    pure = [r for r in rows if r["fixture_label"].startswith("speaker_") and r["stable_context"] and r["attribution"]["status"] != "silence"]
    report = {
        "test_kind": "paced_public_fixtures_not_live_validation", "session_id": session,
        "model_id": body["model_id"], "stream_chunks": len(rows),
        "stable_non_silence_single_speaker_windows": len(pure),
        "fixture_speakers_identified": sorted({r["attribution"]["speaker_id"] for r in pure if r["attribution"]["speaker_id"] is not None}),
        "overlap_windows_flagged": sum(r["attribution"]["overlap"] == "detected" for r in overlap_rows),
        "overlap_windows_evaluated": len(overlap_rows),
        "single_speaker_overlap_flags": sum(r["attribution"]["overlap"] == "detected" for r in pure),
        "http_roundtrip_ms_p50": float(np.percentile(latencies, 50)), "http_roundtrip_ms_p95": float(np.percentile(latencies, 95)),
        "capture_end_to_response_ms_p95": float(np.percentile([r["capture_end_to_response_ms"] for r in rows], 95)),
        "initial_context_ms": 1500, "correction_profile_changed": before != after,
        "correction_example_similarity_before": similarity_before, "correction_example_similarity_after": similarity_after,
        "confidence_calibrated": False, "live_accuracy_validated": False,
        "fixture_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(args.fixtures.glob("*.wav"))},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"report": report, "events": rows}, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8080")
    parser.add_argument("--fixtures", type=Path, default=Path("data/fixtures"))
    parser.add_argument("--db", type=Path, default=Path("data/integration.sqlite"))
    parser.add_argument("--output", type=Path, default=Path("data/two-speaker-smoke.json"))
    main(parser.parse_args())
