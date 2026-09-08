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

`GET /speaker/session/{id}/utterances?after_id=0&limit=100&min_label=medium` returns rows ordered by `utterance_id` (v2; previously `start_ms`) with all numeric fields, `label_kind: similarity_based_uncalibrated`, and a `text` field holding compact lines: `#id m:ss.s-m:ss.s Name [high]: words`, or `#id ... OVERLAP Alice+Bob [overlap 100%]: words` for people talking at once, or `Name [overlap 40%]` for an attributed turn partly talked over. `min_label` (`high|medium|low`) drops rows below that rank; overlap and unknown rows rank lowest. `next_after_id` is the cursor. `GET /speaker/sessions?limit=20` lists sessions newest first.

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

Utterance times remain accepted-audio offsets, including silence inserted while playback mutes the microphone. The runtime posts output on `response.output_audio_transcript.done`, using the response's audio-timeline start and current stream offset at completion. Tool-only responses are not utterances. The runtime reads stored utterances with `after_id`, de-duplicates by `utterance_id`, and passes other agents' rows to its gate. Pages and compact transcript lines use increasing `utterance_id` (stored order); `next_after_id` is the last returned ID, or the incoming cursor for an empty page. This explicitly replaces v1's `start_ms` ordering because asynchronous transcription can store an earlier span later, and a time-ordered page with an ID cursor can skip rows. A presentation may sort accumulated rows by time without changing its cursor. Utterance-level correction is deferred; the GUI must not present an active correction control in this build.

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

## V2 privacy amendment (blocking contract)

The implementation requirements and endpoint shapes in `docs/BIPA_V2.md` supersede permissive collection, disclosure and retention behavior above. Schema v5 follows the in-progress v4 migration. An existing session or API token never implies a participant's consent. All streams must enforce the privacy amendment before their v2 live run. B additionally owns privacy enforcement changes in `scripts/voiceprint_client.py`, `scripts/realtime_openai.py`, `scripts/live.py` and `scripts/openai_responses_probe.py`; A additionally owns `worker/worker.py` and its privacy boundary test, to prevent direct worker calls from bypassing authorization. Root owns the privacy specification, retention verification and integration review. No threshold or agent-nudge wording change is authorized by this amendment.

## V3 arbitration contract (streams api, agent, gui, eval)

Implementation contract for v3 (`docs/BUILD_SPEC_V3.md`; stream plan in `docs/V3_STREAMS.md`). Existing endpoints above are unchanged. **This section explicitly supersedes `docs/V2_STREAMS.md`'s "no agent-to-agent messaging, no second channel" rule for one case only: the agent channel below, a new MCP tool on the same server with the same cursor shape as `get_transcript`.** The room-level gate that prevents automatic agent-to-agent voice loops is unchanged; the channel is text, stored, guarded and never claims the floor.

### Disclosure scope `negotiation_text` (schema v5 tables, notice text change)

A third optional disclosure scope, alongside `openai_audio` and `hosted_mcp`. A release may include `"negotiation_text"` in `disclosure_scopes`. `GET .../consent` reports `scopes.negotiation_text`, effective only when the room is valid, **every** human's release includes it, and `VOICEPRINT_OPENAI_REVIEWED=true`. It authorizes, for this room only: storing each participant's typed negotiation objective; injecting a participant's own objective into the prompt of the advocate agent that speaks for them; sending both objectives, the notes board and the named transcript to OpenAI text models (Responses API, `store:false`) for the arbitrator and the end-of-conversation summary; and retaining that written summary for 30 days after the room ends. `PrivacyPolicy.noticeText()` gains one sentence describing exactly that; the notice hash therefore changes and every previously signed room must be re-signed (expected). Without this scope from everyone, `POST .../objectives`, `POST .../summary` and the arbitrator's vendor calls are refused with 403 `prior_written_release_required`, and the room behaves as v2.

### Schema v6

`user_version=6` adds four tables. Objectives, channel rows and reveals reference `privacy_rooms(session_id)` (a room exists before enrollment creates `sessions`), so **destruction deletes them explicitly** in `PrivacyGate.purge()` and the zero-row verification list includes `objectives`, `agent_channel` and `channel_reveals`. Summaries are a separately retained artifact (see below): purpose completion (`POST .../end`) and room expiry keep them until their own 30-day deadline or `DELETE .../summary`, but a participant's withdrawal or an explicit `DELETE /speaker/session/{id}` destroys the summary in the same transaction that schedules the room's destruction. They are not part of the session graph.

```sql
CREATE TABLE objectives(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL REFERENCES privacy_rooms(session_id), principal_id TEXT NOT NULL, version INTEGER NOT NULL, position TEXT NOT NULL, constraints TEXT NOT NULL, source TEXT NOT NULL CHECK(source IN ('typed','uploaded')), trigger TEXT NOT NULL, created_ms INTEGER NOT NULL, UNIQUE(session_id,principal_id,version));
CREATE TABLE agent_channel(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL REFERENCES privacy_rooms(session_id), sender_participant_id TEXT NOT NULL, tier TEXT NOT NULL CHECK(tier IN ('board','raw')), tag TEXT, text TEXT NOT NULL, redactions INTEGER NOT NULL DEFAULT 0, timestamp_ms INTEGER NOT NULL);
CREATE TABLE channel_reveals(session_id TEXT NOT NULL REFERENCES privacy_rooms(session_id), participant_id TEXT NOT NULL, revealed_ms INTEGER NOT NULL, PRIMARY KEY(session_id,participant_id));
CREATE TABLE summaries(session_id TEXT PRIMARY KEY, text TEXT NOT NULL, model TEXT NOT NULL, board_rows INTEGER NOT NULL, transcript_rows INTEGER NOT NULL, created_ms INTEGER NOT NULL, retention_deadline_ms INTEGER NOT NULL);
```

`constraints` is a JSON array of `{"label":"...","value":"..."}`; `value` strings are what the redaction guard watches. The raw uploaded file is never stored: the GUI parses it client-side into these fields and posts only the fields (`source:"uploaded"`).

### Objectives (operator-local only; never an MCP tool)

`POST /speaker/session/{id}/objectives` with `{"principal_id":"participant_1","position":"wants to sell the property","constraints":[{"label":"floor","value":"300000"}],"source":"typed","trigger":"initial"}` returns 201 `{"session_id":"...","principal_id":"participant_1","version":1,"created_ms":...}`. Every POST creates a new version (`max+1`, never an in-place edit); `trigger` (≤200 chars) says what caused it. `principal_id` must be a human in the room's released roster. Limits: position ≤2000 chars, ≤20 constraints, label ≤80, value ≤200. Requires operator token, room admission and the `negotiation_text` scope.

`GET /speaker/session/{id}/objectives` returns `{"session_id":"...","objectives":[{principal_id,version,position,constraints,source,trigger,created_ms}]}`: the latest version per principal, ordered by principal ID; `?history=1` returns every version ordered by ID. Same requirements. The runtime is the only intended reader (it injects each objective into its own advocate's prompt and hands both to the arbitrator in-process). There is deliberately no per-agent read tool: `participant_id` on the MCP URL is unauthenticated (see the V2 proof section), so objective access is never keyed on it.

### Agent channel

`POST /speaker/session/{id}/agent_channel` with `{"sender_participant_id":"participant_3","tier":"board"|"raw","text":"...","tag":null|"OBJECTIVE_ACHIEVED"|"REFOCUS_NEEDED"}` returns 201 `{"session_id":"...","row_id":7,"tier":"board","redactions":0,"text":"<stored text>"}`. The sender must be a registered agent in an active session (400/404 as for the floor). **Before the row is stored the server runs the redaction guard over `text` against the union of every latest objective's constraint values in the session** (the sender's identity is a declared label, so the guard never depends on it); the stored text is the redacted text and `redactions` counts replacements. Text ≤4000 chars. A `board` row also commits an `agent_channel` event (feed type `agent_channel`, data = the row) atomically; `raw` rows produce no event. Hosted MCP writes arrive through `post_agent_channel` with the same body, sender taken from the MCP URL's `participant_id` tag.

`GET /speaker/session/{id}/agent_channel?after_id=0&limit=100&tier=board|raw|all` returns `{"session_id":"...","next_after_id":7,"revealed":false,"rows":[{row_id,sender_participant_id,sender_name,tier,tag,text,redactions,timestamp_ms}],"text":"#7 12:01:05 Mediator [board]: ...\n"}`. Tier defaults to `all`. **Reveal gate (API-enforced, not GUI-hidden):** a request carrying `X-Voiceprint-Hosted-MCP: true` (the MCP server's path, authenticated by the separate MCP bearer token, i.e. the agents themselves) receives every requested tier. Any other caller receives `board` rows only until **every** human in the released roster has a `channel_reveals` row; until then `raw` rows are omitted, `revealed` is false, and `next_after_id` still advances past omitted rows (highest ID examined, or the cursor when nothing new). Compact `text` lines use the server timestamp as `HH:MM:SS` local time, `Sender [tier]` and `(TAG)` when tagged. Trust boundary, stated plainly: the hosted header is honored on the loopback REST path under the operator token (the same trust the hosted sessions listing already extends), so a local process holding the operator token can read the raw tier by setting it; the gate protects the people in the room from the console's screen, not the operator from themselves. The cursor advances past withheld rows, so a console that sees `revealed` flip to true refetches once from `after_id=0`.

`POST /speaker/session/{id}/agent_channel/reveal` with `{"participant_id":"participant_1","revealed":true|false}` records or withdraws that human's consent to show the raw stream on the console; returns 200 `{"session_id":"...","revealed_by":["participant_1"],"revealed":false}`. Like consent writes it requires the exact local browser Origin (the person clicks it themselves) and a human roster member.

### Summary (retained artifact)

`POST /speaker/session/{id}/summary` with `{"text":"...","model":"gpt-5","board_rows":4,"transcript_rows":61}` returns 201 `{"session_id":"...","created_ms":...,"retention_deadline_ms":...}` and replaces any earlier summary for the session. Requires room admission (the session must not have ended: the runtime writes it **before** `POST .../end`) and the `negotiation_text` scope. `retention_deadline_ms = created_ms + 30 days`. Text ≤ 20000 chars. `GET /speaker/session/{id}/summary` (operator token; no room admission, because the room is destroyed by the time anyone reads it) returns the row or 404. `DELETE /speaker/session/{id}/summary` removes it. The 60-second privacy sweeper deletes summaries past their deadline. A withdrawal or session deletion request removes it immediately; only a room that ended by purpose completion retains it. The summary is stored in the database, not written to a file: `docs/TURN_TAKING.md` §7's disk-log ban stands; an operator who wants a file passes the runtime's explicit `--summary-file PATH` flag (off by default) after the database write is confirmed.

### MCP tools

- `get_agent_channel(session_id, after_id?, limit?)` (read-only): same cursor contract as `get_transcript`; returns compact lines in `content[0].text` and `{session_id,next_after_id,count,channel}` in `structuredContent`; served through the hosted REST path, so agents see both tiers.
- `post_agent_channel(session_id, text, tier?)` (not read-only): posts a row as the URL's `participant_id`; `tier` defaults to `raw`; refuses with a tool error when the URL carries no participant tag. Returns `{row_id,tier,redactions}`.
- `safeMcpArguments` redacts `text` like other identifying arguments (it is not persisted in `mcp_calls`). `McpServer.dispatch` receives the participant tag from `McpHttpServer` (null on stdio). Advocates' `allowed_tools` grows to `get_transcript, get_current_speaker, get_agent_channel, post_agent_channel`.

### Redaction guard (server-authoritative; runtime mirror for spoken output)

`Redaction.redact(text, values)` returns the redacted text and hit count. Shared vectors: `evaluation/redaction_cases.json`; both `src/test/java/.../RedactionTest.java` and `scripts/test_objectives.py` iterate every case and assert `expected` and `hits` exactly. Rules:

1. A registered value is **numeric** when, after removing `$`, `,`, `_`, spaces and `%`, and applying a trailing multiplier (`k`/`K` ×1000, `m`/`M` ×1,000,000, `thousand`, `million`), it parses as a number. Otherwise it is a **text** value; text values shorter than 3 characters after normalization are ignored.
2. Numeric mentions in the candidate text are spans of: optional `$` (with optional space), digits with optional thousands separators and decimals, optional space and multiplier or `%`; **or** spelled-out numbers (`zero`–`nineteen`, tens, hyphenated `twenty-five`, `hundred`, `thousand`, `million`, `billion`, with `and` allowed between parts but never consumed at the end of a span). A digit run immediately preceded or followed by a letter (other than a multiplier) is not a mention (`room42`). A mention matches a numeric value when the canonical numbers are equal (absolute difference < 0.005).
3. Text values match case-insensitively as a whole-word-bounded phrase with any run of whitespace/punctuation between words.
4. Every matching span is replaced with `[withheld]`; the count of replacements is returned.
5. Documented misses (deliberately not caught): adjacent numbers (`299,000` for `300000`), ranges and brackets (`between 280 and 320 thousand`), paraphrase (`the low three hundreds`), ordinal forms of dated text values (`June 30th` for `June 30`), and anything the model states as a *derived* figure. The guard is a literal-value backstop behind the instruction layer, not a classifier.

Behavior on a catch: **redact in place** for channel/board/summary text (the row is stored redacted and the count is visible); for an advocate's *spoken* output the runtime cancels the response and flushes playback the moment the streamed transcript matches (best effort: audio already played cannot be recalled; see `docs/TURN_TAKING.md` §9).

### Runtime control API additions (loopback 8090)

- `GET /agents`: `conversation_type` (`casual`|`negotiation`), each agent gains `role` (`voice`|`arbitrator`); an arbitrator agent additionally reports `arbitrator:{generations,ingested_rows,last_trigger,pending_tag,cooldown_until_ms,paused}` and never reports a voice. `agent_configs` rows gain `role`.
- `POST /setup`: accepts `conversation_type` and per-agent `role`. `negotiation` requires 2–4 humans, exactly two `voice` agents with distinct `speaks_for` values (the advocates) and exactly one `arbitrator`; an arbitrator is rejected in `casual`.
- `POST /agents/{name}/control` for an arbitrator: `speak` = generate a contribution now, `hold` = pause/resume posting, `cancel` = drop any pending override.
- Arbitrator reads use the **local MCP HTTP endpoint** (`http://127.0.0.1:8082/mcp?participant_id=<arbitrator>` with the MCP bearer token), not REST, so its `get_transcript`/`get_agent_channel` calls appear in `mcp_calls` and the proof panel, and its channel reads carry the agent credential.

## V3.1 contract: transcript review, retained voice profiles, provider-neutral notice (Grant's answers, 2026-09-08)

Grant's answers to `docs/V3_STREAMS.md`'s five questions: (2) writing data to disk is fine, (3) the advocate-voice leak is an accepted residual risk for v3, (4) the notice must make clear that data can be transmitted to providers other than OpenAI, (5) no second person is available for a demo yet. Plus one new requirement: better transcript accuracy through review, a way to improve the models, voice profiles that consenting people keep across sessions, and tolerant name matching (a person said "Ryan", the transcript wrote "Brian", Ryan's agent never knew it was addressed).

### Schema v7

```sql
ALTER TABLE utterances ADD COLUMN reviewed_text TEXT;
ALTER TABLE utterances ADD COLUMN reviewed_speaker_id TEXT;
ALTER TABLE utterances ADD COLUMN reviewed_ms INTEGER;
CREATE TABLE retained_profiles(subject_key TEXT PRIMARY KEY, subject_name TEXT NOT NULL, model TEXT NOT NULL, vector TEXT NOT NULL, sessions INTEGER NOT NULL DEFAULT 1, created_ms INTEGER NOT NULL, last_interaction_ms INTEGER NOT NULL, retention_deadline_ms INTEGER NOT NULL);
```

`subject_key = sha256(casefold(trimmed full name) + "\n" + casefold(trimmed contact))`, hex: the same operator-entered, unverified identity the release already records. `retained_profiles` has no session foreign key; it is a separate retention class with its own deadline and is never touched by session destruction.

### Transcript review (operator, during the room)

`POST /speaker/session/{id}/utterances/{utterance_id}/review` with `{"text":"...","speaker_id":"participant_2"}` (at least one field; `text` ≤ 4000; `speaker_id` an enrolled human of this session, refused for agent rows) returns 200 `{"utterance_id":42,"text":"...","speaker_id":"...","label":"reviewed","segments_corrected":3,"profile_updated":true}`. Effects, in one transaction: the review columns are set (`reviewed_ms` = now); when `speaker_id` is given and differs from the stored speaker, every segment of the session whose `[start_ms,end_ms)` lies inside the utterance's span receives the existing segment correction (a `corrections` row, a `correction_examples` row when the segment was verified single-speaker, `rebuildProfiles`), which is the in-session model improvement; an `utterance_reviewed` event carries the full updated utterance row. Requires operator token and room admission (`local_processing`); no browser Origin requirement (it is an operator edit, not a release).

Utterance reads (`GET .../utterances`, `get_transcript`, events) present the reviewed values: `text` is the reviewed text when present (the original is returned as `original_text`, null when unreviewed), `speaker_id`/`speaker_name` are the reviewed speaker when present (`original_speaker_id` likewise), and `label` becomes `reviewed` when a speaker was reviewed (a human label, rank 4 like `agent`, so it survives every `min_label` filter; compact lines read `Name [reviewed]: words`). A text-only review keeps the acoustic label. `label_kind` stays `similarity_based_uncalibrated`; `reviewed` is a human label, not a calibrated one.

### Retained voice profiles (per-person consent; improves across sessions)

New optional release scope `voice_profile_retention`. Unlike the disclosure scopes it is **per person**, not room-wide: `GET .../consent` lists `retain_profile: true|false` on each participant and does not add a room-wide scope. Notice sentence: "Optional voice_profile_retention keeps your voiceprint (the enrollment embedding and its corrected updates, never audio or transcript) after this room ends so that later rooms you join start from it and refine it; it is kept until you withdraw it in the console or three years after your last session, whichever comes first, and it is deleted when you withdraw from a room."

- **Seeding at enrollment** (`POST /speaker/session/init`): for each participant with `retain_profile` whose `subject_key` has a `retained_profiles` row with the same `model`, the stored profile anchor and vector become `normalize(0.5 × enrollment embedding + 0.5 × retained vector)`; the response's participants (add an array `participants:[{id,profile_seeded}]`) say which were seeded. A model mismatch ignores the retained row (it is not deleted).
- **Update at purpose completion** (`POST .../end`, before destruction is scheduled): for each human with `retain_profile`, upsert the retained row from the session's final `profiles.vector`: `normalize(0.5 × retained + 0.5 × final)` when a row exists, else `final`; `sessions += 1`; `last_interaction_ms = now`; `retention_deadline_ms = now + 3 calendar years` (February 29 falls back to February 28, per `BIPA_V2.md`). Withdrawal (`.../consents/{pid}/revoke`), deletion requests and deadline expiry never write a retained profile; **a withdrawal additionally deletes the withdrawing person's retained profile** (`BIPA_V2.md`: do not keep a withdrawing person's voice).
- `GET /privacy/profiles` (operator token) returns `{"profiles":[{subject_key,subject_name,model,sessions,created_ms,last_interaction_ms,retention_deadline_ms}]}`, never vectors. `DELETE /privacy/profiles/{subject_key}` (exact local browser Origin required, like consent writes: the person clicks it) returns `{"deleted":true}` or 404. The privacy sweeper deletes rows past their deadline. `PrivacyPolicy.retentionText()` gains one sentence describing this class.

### Provider-neutral notice (answer 4)

`noticeText()` no longer names OpenAI as the only recipient. The disclosure sentences say the audio, transcript and negotiation text go to "the AI provider(s) the operator has configured and reviewed (currently OpenAI Realtime and Responses through Cloudflare's tunnel; the operator may configure other providers such as xAI or Google Gemini under the same release)". Scope identifiers (`openai_audio`, `hosted_mcp`, `negotiation_text`) are unchanged for compatibility and the notice states that the `openai_` prefix is historical. `GET /privacy/notice` gains `"providers": [...]` from `VOICEPRINT_PROVIDERS` (comma-separated display names, default `OpenAI`), rendered into the notice text so the hash tracks the configured list. The console's release checkboxes use the same provider-neutral wording.

### Runtime and console (answers 2 and 3; fuzzy addressing)

- Answer 2: the closing summary is written **both** as the API record and as a file, by default `data/summaries/<session_id>.md` (directory created; `data/` is git-ignored); `--summary-file PATH` overrides the path and `--no-summary-file` disables the file. `docs/TURN_TAKING.md` §7's ban on raw event logs is unchanged: this answer covers the summary artifact only.
- Answer 3: accepted residual risk; `docs/V3_STREAMS.md` records it as Grant's decision, not a default.
- Fuzzy addressing: `turn_gate.addressed()` also matches a spoken token to an agent name when, after a light phonetic normalization (casefold; `y`→`i`; `ph`→`f`; `ck`→`k`; doubled letters collapsed), both forms are at least four characters and their `difflib` similarity ratio is ≥ 0.85 (so `brian` addresses `ryan` at 0.889; `been` never addresses `ben` because three-letter names match only by exact casefold; `great`/`grand` at 0.8 never address `grant`); the same rule applies to the "a line naming another agent is not an opening for me" check. When the match was inexact the pre-reply note appends one sentence naming the transcript's spelling, and the shared instruction layer tells every agent that the transcript may misspell or substitute a similar-sounding name. The console's transcript rows gain a Review control (edit text, pick the speaker) that calls the review endpoint, and the room step lists retained voiceprints with a per-person delete.
