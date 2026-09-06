# API contract v0.1

All endpoints use JSON over loopback HTTP. `VOICEPRINT_API_TOKEN`, when set, requires `Authorization: Bearer ...`. Errors have `error` and `message`. Times ending in `_ms` are milliseconds. `timestamp_ms` is server Unix time; `start_ms` and `end_ms` are offsets on the session's accepted audio timeline. Network pauses do not create fabricated audio. IDs contain letters, digits, underscores or hyphens, up to 80 characters.

## Enrollment

`POST /speaker/session/init` returns 201:

```json
{
  "session_id": "conv_123",
  "sample_rate": 16000,
  "audio_format": "pcm_s16le",
  "participants": [
    {"id": "grant", "name": "Grant", "opening_statement_audio": "BASE64_RAW_PCM"},
    {"id": "kyle", "name": "Kyle", "opening_statement_audio": "BASE64_RAW_PCM"}
  ]
}
```

Each opening statement is 5–15 seconds of **mono signed 16-bit little-endian PCM at 16 kHz**, with no WAV header. Enrollment is atomic: all profiles persist or none do. Silence, overlap and detected speaker changes are rejected. Name and identity are explicitly supplied by the client; a voice embedding is not proof of identity. A repeated session ID returns 409. Up to 32 active sessions may exist, but inference is serialized in this spike.

## Live chunks

`POST /speaker/session/{id}/audio`:

```json
{"sequence": 0, "audio_base64": "BASE64_8000_BYTES", "text": "Optional external transcript for this chunk"}
```

Send exactly 8000 bytes (250 ms). Sequence numbers start at zero. Wait for acknowledgment before retrying a failed request. Retries with the same sequence and payload return the persisted result; changed payloads and skipped sequences return 409. The stream uses bounded rolling memory. Worker failure returns 503 without advancing the sequence or accepting the chunk. A microphone client should stop if it can no longer keep up; it must not silently drop samples.

The first five chunks return `buffering`. Subsequent chunks use a 1.5 second context. After a process restart or long capture gap the context must warm up again. `context_start_ms` states the span used by the embedding; the match cannot be assumed to describe just the final 250 ms when speaker changes occur.

A typical uncalibrated response includes:

```json
{
  "segment_id": "opaque-uuid",
  "session_id": "conv_123",
  "sequence": 5,
  "start_ms": 1250,
  "end_ms": 1500,
  "context_start_ms": 0,
  "speaker_id": "grant",
  "confidence": null,
  "confidence_kind": "uncalibrated",
  "similarity": 0.72,
  "margin": 0.21,
  "uncertain": true,
  "trusted": false,
  "status": "tentative",
  "overlap": "clear",
  "uncertainty_reasons": ["uncalibrated_confidence"]
}
```

Responses also include the model version, server timestamp, inference duration, candidate similarities, source and optional text. `inference_ms` includes analysis and matching, not capture, HTTP queuing or persistence. The client measures capture-to-response separately.

An eligible calibration supplies a probability in `[0,1]` for the **top-ranked identity being correct**, plus `calibration_id`. Probabilities below `.60` are uncertain. Unavailable calibration yields null, never a synthetic percentage. Overlap, speaker changes, weak matches (cosine < `.25`) and ambiguous matches (margin < `.05`) suppress a single-speaker decision. These initial rejection thresholds are engineering defaults, not empirically validated operating points.

`overlap: "detected"` comes from acoustic model inference. Candidate identities are similarity rankings, **not independently verified identities of the overlapping voices**. Their confidence remains null and they are uncertain. Speaker separation is not performed.

## Reads and corrections

- `GET /speaker/session/{id}/current`: current attribution, or `waiting`, `stale`, `ended`, `inference_error`. These unavailable states have no speaker and no confidence. Current data expires after 1500 ms without a fresh accepted chunk.
- `GET /speaker/session/{id}/profiles`: participant IDs, names and model ID. Embeddings are not exposed.
- `GET /speaker/session/{id}/transcript?speaker_id=grant&after_sequence=-1&limit=100`: ordered segment history, optionally filtered to one speaker. `next_after_sequence` is the next-page cursor. Text is null unless supplied by an external ASR client. Filtering uses the latest corrected identity.
- `GET /speaker/session/{id}/corrections?after_id=0&limit=100`: append-only audit, including previous/actual speaker, timestamp and whether a profile was updated. `next_after_id` is the cursor.
- `POST /speaker/session/{id}/end` with `{}`: stop accepting new audio; retain history.
- `DELETE /speaker/session/{id}`: delete session profiles, history, correction examples and audit; clear its audio buffer.

Both paged reads accept limits 1–200. Statement and segment IDs are scoped to their session. Corrections work during or after a conversation:

```json
{"segment_id": "UUID_FROM_ATTRIBUTION", "actual_speaker": "kyle"}
```

Send this to `POST /speaker/session/{id}/correct`. A segment ID is required instead of an ambiguous timestamp. The response contains `correction_logged`, `profile_updated`, `profile_update_reason` and the corrected attribution. Human corrections preserve the original model identity and score but clear its probability; they do not retroactively improve or recalibrate the model's original decision.

Clean corrected embeddings contribute to a rebuilt profile: normalize `0.8 * enrollment_anchor + 0.2 * mean(corrected_examples)`. No corrections means the original anchor. Overlap, silence, changes and windows not verified as one speaker do not train the profile. The label still enters the audit. Repeated identical corrections are idempotent. Relabeling moves one example and rebuilds both affected profiles transactionally.

`GET /health` is Java process liveness. It is not a model-readiness claim; worker `/health` is available only after both real models load.
