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

## Utterances (external transcript)

The API never transcribes audio. A client that groups chunks into turns and runs its own ASR (the shipped `scripts/live.py` uses faster-whisper locally) stores each finished turn:

```json
{"speaker_id": "grant", "start_ms": 1000, "end_ms": 3500, "text": "words spoken", "source": "faster_whisper"}
```

`POST /speaker/session/{id}/utterances` returns `utterance_id` and `label`. Optional numeric fields `similarity` (-1..1), `margin`, `overlap_ratio` (0..1) and `abstain_ratio` (0..1) describe the chunks behind the utterance; `candidates` (list of enrolled IDs) marks an overlap row. `speaker_id` may be null; a non-enrolled ID or candidate is 404.

The server assigns `label` from engineering thresholds (see CALIBRATION.md; these are **not** calibrated probabilities): `overlap` when `overlap_ratio >= 0.3` or candidates are given; `high` when similarity >= 0.55, margin >= 0.25, overlap < 0.1 and abstention < 0.2; `medium` when similarity >= 0.40 and margin >= 0.12; `low` otherwise or when abstention >= 0.5; `unknown` when no stats were supplied.

`GET /speaker/session/{id}/utterances?after_id=0&limit=100&min_label=medium` returns rows ordered by `start_ms` with all numeric fields, `label_kind: similarity_based_uncalibrated`, and a `text` field holding compact lines: `#id m:ss.s-m:ss.s Name [high]: words`, or `#id ... OVERLAP Alice+Bob [overlap 100%]: words` for people talking at once, or `Name [overlap 40%]` for an attributed turn partly talked over. `min_label` (`high|medium|low`) drops rows below that rank; overlap and unknown rows rank lowest. `next_after_id` is the cursor. `GET /speaker/sessions?limit=20` lists sessions newest first.

## MCP over HTTP

`POST http://127.0.0.1:{VOICEPRINT_MCP_PORT:-8082}/mcp` is a stateless MCP Streamable HTTP endpoint (spec 2025-11-25) exposing the same tools as the stdio adapter. Rules: POST only (GET/DELETE 405); any `Origin` header is 403; `VOICEPRINT_MCP_TOKEN`, when set, requires `Authorization: Bearer`; unsupported `MCP-Protocol-Version` is 400; JSON-RPC batches are 400; notifications return 202 with no body; every response is `application/json` (never SSE, which tunnels buffer). `initialize` may be repeated and `tools/list` needs no prior `initialize`, because hosted clients send each request from a different worker. Expose it with `scripts\dev.ps1 tunnel` (cloudflared quick tunnel) and give the printed URL plus token to the hosted agent.

`GET /health` is Java process liveness. It is not a model-readiness claim; worker `/health` is available only after both real models load.

## V2 shared-room contract (streams A, B, C and D)

This section is the implementation contract for the next build. Existing endpoints above remain compatible except for the explicit browser-origin and agent-label additions below. The utterances table is the conversation bus. The GUI and runtime consume the same stored rows; provider output becomes visible to other agents only after it is stored successfully. No thresholds or existing pre-reply nudge wording change in this build.

### Participants and schema v4

Schema `user_version=4` adds a `participants` registry keyed by `(session_id, id)` with `name`, `kind` (`human|agent`), nullable `provider` and nullable `model`. Existing v3 `profiles` remain the human embedding store; the migration backfills a human participant for every profile. New enrollment writes both atomically. Agent registration never creates an embedding and agents never enter acoustic matching or profile adaptation. Session deletion cascades to the registry, floor, event history and session-scoped MCP calls. Migration from v3 preserves all existing utterances, segments and corrections and is tested; older supported schemas upgrade through v3 first.

`POST /speaker/session/{id}/participants` registers an agent in an active session:

```json
{"id":"participant_3","name":"Ava","kind":"agent","provider":"openai_realtime","model":"gpt-realtime-2.1"}
```

Returns 201 with `{"session_id":"...","participant":{...the five fields above...}}`. An identical retry returns 200 and the same participant; reuse of an ID with different fields returns 409. Human registration uses enrollment only (400 here); ended sessions return 409 and missing sessions 404. IDs share the existing participant ID namespace. Required name/provider/model are nonempty strings, maximum 200 characters each.

`GET /speaker/session/{id}/participants` returns `{"session_id":"...","participants":[{"id":"participant_1","name":"Grant","kind":"human","provider":null,"model":"<embedding model>"}, ...]}` ordered by ID. Existing `/profiles` remains human-only. The existing `/speaker/sessions` shape is unchanged; each session's `participants` display string includes agents.

### Agent utterances

The existing utterance POST accepts registered IDs. New clients use `source` values `faster_whisper`, `openai_transcription` or `agent`; older human source strings remain readable and accepted for compatibility. `source=agent` requires an agent speaker (400 otherwise); an agent speaker requires `source=agent`. These rows receive `label=agent`, with null similarity/margin/ratios and no candidates. Attribution is declared by the registered producer, not an acoustic probability. Human label thresholds are unchanged. Agent rows survive all existing `min_label` filters. Read rows and compact transcript text include the agent's registered name, for example `#42 0:12.0-0:14.0 Ava [agent]: Ben, what do you think?` The response's `label_kind: similarity_based_uncalibrated` and honesty notice remain intact for human labels.

Utterance times remain accepted-audio offsets, including silence inserted while playback mutes the microphone. The runtime posts output on `response.output_audio_transcript.done`, using the response's audio-timeline start and current stream offset at completion. Tool-only responses are not utterances. The runtime reads stored utterances with `after_id`, de-duplicates by `utterance_id`, and passes other agents' rows to its gate. Utterance-level correction is deferred; the GUI must not present an active correction control in this build.

### Floor lease

`POST /speaker/session/{id}/floor` takes `{"participant_id":"participant_3","lease_ms":15000}`. Only registered agents can claim (400 for a human, 404 for an unknown participant). `lease_ms` is an integer from 1000 to 30000; default 15000. A claim or renewal by the current holder returns HTTP 200:

```json
{"session_id":"room","granted":true,"held_by":"participant_3","expires_at_ms":1780000015000,"server_time_ms":1780000000000}
```

A competing claim also returns 200, with `granted:false` and the existing holder/expiry. Grant, renewal and release are atomic. Expiry uses server time; a lease is free when `now >= expires_at_ms`. `GET .../floor` returns the same shape without `granted`; a free floor has `held_by:null` and `expires_at_ms:null`. `DELETE .../floor?participant_id=participant_3` releases only that holder, returns the GET shape plus `released:true|false`, and is harmless if already free or owned by another agent. Ended sessions reject claims (409); ending a session releases its floor. Reads materialize expired leases, including an `expired` event, before returning. Leases survive restart and still expire by time.

The runtime claims before the initial `response.create`, renews while a turn or playback is active, and retains the lease across tool-only responses and their continuations. It releases after the final `response.done` AND queued playback/tail have drained, or after cancellation flushes playback. A lost lease cancels output and flushes audio immediately. Manual speak never bypasses the floor. Waiting for idle transcription, completed MCP calls, and half-duplex mute are required. A room-level gate blocks unaddressed agent replies to agent speech, preventing automatic agent-to-agent loops; explicit named handoffs allow one reply, with per-agent cooldown preserved.

### Persistent event feed and MCP proof

Schema v4 adds `events` with a globally increasing persistent integer ID, session ID, Unix `timestamp_ms`, `type` and JSON `data`, plus `mcp_calls` with call ID, nullable session ID, timestamp, `caller_ip`, nullable `participant_id`, `tool`, JSON `arguments`, UTF-8 response `bytes` and `failed`. An event and the record it describes commit atomically. The migration backfills existing utterances as events once, ordered by utterance ID. Floor storage is keyed by session ID with participant ID and expiry. MCP calls without a valid session are retained with null session and do not appear in a session feed.

`GET /speaker/session/{id}/events?after_id=0&limit=100&wait_ms=0` returns:

```json
{"session_id":"room","events":[{"event_id":71,"timestamp_ms":1780000000000,"type":"utterance","data":{"utterance_id":42,"speaker_id":"participant_3","speaker_name":"Ava","start_ms":12000,"end_ms":14000,"text":"Hello","source":"agent","label":"agent","similarity":null,"margin":null,"overlap_ratio":null,"abstain_ratio":null,"candidates":[]}}],"next_after_id":71}
```

`after_id` is a nonnegative event cursor (not an utterance ID), `limit` is 1–200, and `wait_ms` is 0–10000. Events are ordered by event ID; `next_after_id` equals the last returned ID, or the supplied cursor when empty. If no rows exist, wait up to `wait_ms`, returning earlier when an event appears. Long polling must not hold the database/service lock or block audio ingestion. Clients tolerate empty pages, unknown event types and added fields. Catch up with `wait_ms=0` before long-polling.

Event types: `utterance` carries the complete GET utterance row; `floor` carries `action` (`granted|renewed|released|expired`) plus `held_by`, `expires_at_ms`, `server_time_ms`; `mcp_call` carries `call_id`, `session_id`, `timestamp_ms`, `caller_ip`, `participant_id`, `tool`, `arguments`, `bytes`, `failed`. Participant lists/current attribution are polled separately; they are not feed event types in this build.

Keep the existing HTTP MCP terminal log line intact. Persist every authenticated `tools/call` response (including tool failures), with size measured from the exact serialized JSON-RPC response. GUI evidence comes from this server log, never inferred from provider audio or a model statement. The MCP URL may carry `?participant_id=participant_3`; the runtime sets this per adapter. It is a caller-declared routing label, not independently authenticated identity. Only a matching registered agent in the tool's `session_id` is stored; otherwise use null. Show unattributed calls as such. No bearer tokens, authorization headers or provider keys enter the call record. Internal persistence wiring is not a new public write endpoint for the GUI.

### Local GUI and runtime controls

The API serves `web/index.html` at `/ui` and `/ui/`, and files below `web/` at `/ui/*`, with correct MIME types. Reject traversal (including encoded traversal), directories and symlink escapes. Static assets need no bearer token; JSON API requests still require `VOICEPRINT_API_TOKEN` when configured. The GUI may ask for that token and hold it in memory only. API Host validation remains restricted to `127.0.0.1|localhost` and its listening port. Requests without Origin remain supported; browser requests with Origin are accepted only when Origin exactly equals the API's local scheme/Host/port. No wildcard CORS or tunnel access to the GUI. MCP continues rejecting every Origin header.

Stream B serves a loopback HTTP control server at `127.0.0.1:8090`. It accepts only local Host and allows CORS solely for `http://127.0.0.1:8080` and `http://localhost:8080` (or an explicitly configured local API origin); reject all other Origin values. OPTIONS allows GET/POST, Content-Type and Authorization. If `VOICEPRINT_API_TOKEN` is configured, controls require the same bearer token. `GET /agents` returns `{"session_id":"room","agents":[{"name":"Ava","participant_id":"participant_3","provider":"openai_realtime","model":"gpt-realtime-2.1","voice":"marin","eagerness":"balanced","held":false,"responding":false}]}`. `POST /agents/{url-encoded-name}/control` takes `{"action":"speak|hold|cancel"}` or `{"action":"eagerness","value":"quiet|balanced|eager"}`; `hold` toggles. Returns 200 `{"ok":true,"agent":{...same agent state...}}` after enqueueing, unknown agent 404, invalid action/value 400. Speak is a one-shot request; Hold cancels and stays held until toggled or Speak. Cancel only cancels the current reply. GUI disables controls when the runtime is unavailable or its session does not match the displayed session.

Stream B also owns `scripts/agents.toml` and `scripts/test_agent_runtime.py` in addition to its listed files. Stream D owns `docs/EVENTS.md` in addition to its scoring files and `evaluation/`. Stream C keeps all tests and fixtures under `web/`. These explicit ownership additions resolve the plan's omitted config/test/event-document paths; shared `voiceprint_client.py`, `realtime_openai.py` and README stay frozen unless agreed separately.
