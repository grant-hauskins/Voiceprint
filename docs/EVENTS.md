# Event log and scoring contract

V2 privacy amendment: `BIPA_V2.md` controls retention and takes precedence over historical JSONL defaults. No human/agent prompt changes or label threshold tuning is part of this stream.

## Historical shapes (read compatibility only)

- `{"attribution":{...},"capture_end_to_response_ms":number|null}`: per-chunk output; offsets use the accepted 16 kHz audio timeline. `inference_ms` is model/matching duration. No wall-clock timestamp was consistently attached to provider events.
- `{"utterance":{utterance_id,speaker_id,start_ms,end_ms,label,text,source,...}}`: stored ASR turn. Text and identity are protected; never copy these events into new committed fixtures.
- `{"openai":{type,...}}`: provider events. Earlier writers copied session configuration and transcript/tool content; an old log may contain credentials. The scorer never prints session configuration or raw tool outputs.
- Legacy `session.created` starts a provider attempt; explicit v2 `run_id` groups multiple providers in one room. An abandoned/config-rejected attempt must not be called a complete conversation solely because it has a response event.

## V2 operational envelopes

Fields are additive. Existing field names are not renamed. A runtime may omit protected payloads entirely; omissions are missing evidence, never zero counts or correct attribution.

An event can carry `run_id`, `session_id`, `agent`, `participant_id`, `timestamp_ms` (Unix milliseconds) and `monotonic_ms` (within-runtime monotonic clock), plus one of:

- `openai`: safe event type/response/item IDs and numeric usage only. A completed MCP output item can be `{id,type:"mcp_call",name:"get_transcript",succeeded:true,output_bytes:123}`. This proves the runtime observed a completed provider result without retaining its body. Missing `output` is not a failure when `succeeded` is explicitly Boolean. `succeeded:false` does not earn a successful-tool credit.
- `floor`: claim/renew/release/expiry/denial metadata, never a substitute for observed playback intervals.
- `control`: action and result metadata. No credential or entered signature text.
- `playback`: `{action:"started|drained|cancelled",response_id,audio_timeline_ms,timing_basis:"played",includes_mute_tail?:true}`. `started` must come from actual output callback activity; queue submission must use another timing basis. `drained` may conservatively include mute tail; documented overlap can therefore exceed physical audio overlap.
- `mcp_call`: independent server proof metadata `{call_id,tool,bytes,failed}`. Do not copy arguments, participant details or result bodies into the scoring log. Provider events are not independent API proof.

## V3 arbitration envelopes

Same row shape (`run_id`, `session_id`, `agent`, `participant_id`, `timestamp_ms`, `monotonic_ms`, plus one kind) and the same positive allowlist discipline as V2. No event of these kinds ever contains constraint values, prompt text, channel text, summary text or names beyond the existing fields. Contract: `docs/API.md` "V3 arbitration contract"; the plan is `docs/V3_STREAMS.md`.

- `arbitrator`: `{action:"ingested"|"generated"|"posted"|"override_claimed"|"override_confirmed"|"override_rejected"|"skipped", trigger:"contribution"|"manual"|"OBJECTIVE_ACHIEVED"|"REFOCUS_NEEDED"|null, tag:null|"OBJECTIVE_ACHIEVED"|"REFOCUS_NEEDED", confirmed:true|false|null, ingested_rows:int (cumulative rows ingested so far, on "ingested"), generations:int (cumulative, on "generated"), tier:"board"|"raw"|null (on "posted"), redactions:int|null (on "posted")}`. `ingested_rows` and `generations` are running totals, so the scorer takes the largest value seen in the run.
- `guard`: `{action:"redacted"|"cut", stage:"spoken_delta"|"stored_utterance"|"board"|"raw"|"summary", redactions:int}`. A `cut` at `spoken_delta` is the runtime cancelling an advocate's spoken response after its streamed transcript matched a constraint value; audio rendered before the cut cannot be recalled.
- `summary`: `{action:"saved"|"failed"|"skipped", attempts:int, board_rows:int, transcript_rows:int, reason:"not_negotiation"|"consent_failed"|"not_operator_end"|"scope_missing" (only on skipped)}`.

`score()` adds three blocks to its result, and `aggregate()` (now `schema_version` 3) copies them through unchanged because they are operational counts plus one enum string:

- `arbitrator`: `{generations, ingested_rows, generation_ratio: fraction(generations, ingested_rows), posts:{board, raw}, post_redactions, overrides:{claimed, confirmed, rejected}}`. `generation_ratio` is the `BUILD_SPEC_V3.md` §5.1 instrumentation ("Arbitrator doesn't generate on every line"): its `ratio` is `None` when no rows were ingested, and a run with arbitrator rows whose ratio reaches 1.0 gets the warning "Arbitrator generated on every ingested row". Override counts come from the `action` field; a confirmation with no unconsumed earlier claim in the same run (each verdict consumes one claim) adds the warning "Override confirmed without a recorded claim".
- `guard`: `{spoken_cuts (action "cut" at stage "spoken_delta"), stored_utterance_redactions, board_redactions, raw_redactions, summary_redactions (sum of redactions per stage), events (all guard rows)}`. Any spoken cut adds the warning "Advocate speech was cut by the leak guard N times: audio before the cut may have been heard."
- `summary`: `{saved:bool, attempts:int|None, board_rows, transcript_rows, skipped_reason:str|None, failed:bool}`. `saved` and `failed` are independent (a failed attempt followed by a save reports both, with `attempts` the largest seen); any failed row adds the warning "A summary attempt failed".

`render_aggregate()` prints one line per block (`Arbitrator: 4 generations over 61 ingested rows (ratio 0.066); posts board 3 raw 1; overrides 1 claimed, 1 confirmed, 0 rejected.` / `Guard: 0 spoken cuts; redactions stored 0, board 1, raw 0, summary 0.` / `Summary: saved after 1 attempt (3 board rows, 61 transcript rows).` or `Summary: skipped (not_negotiation).` or `Summary: not recorded.`); `--details` adds a small Arbitration table. Runs without v3 rows print the zero/`NA`/`not recorded` forms and are otherwise unchanged. `evaluation/v3_events_sample.jsonl` is a constructed run (`synthetic-v3`, room `synthetic_room`, agents Ava/Ben/Mediator) exercising every action, stage and summary outcome above; it contains no conversation text, constraint values or real identifiers.

Runtime event files are disabled, including an explicit disk-events option, until artifact tracking and protected storage are implemented. This milestone keeps bounded metadata in RAM and calls `score_run.summarize_events(rows)` before clearing it on purpose completion or withdrawal. Eviction makes the record incomplete and must not imply zero floor violations. `summarize_ephemeral(path)` remains a read-only helper for already-authorized offline evidence, not permission to create live disk logs. An operator-held legacy copy remains subject to its original purpose and deletion policy.

## Scoring behavior

Run an authorized offline score with:

```powershell
.venv\Scripts\python.exe scripts\score_run.py PATH_TO_EVENTS --no-append
```

By default output is a concise operational summary; an appended `data/runs.jsonl` row is an allowlisted aggregate without human names, session IDs, response IDs, voice scores or transcripts. Agent labels become `Agent 1`, `Agent 2`. This reduces linkage; it is not a claim that every small aggregate is legally anonymous in every context. Restrict access and approve its use/retention separately. `--details` prints detailed Markdown for an authorized offline audit; the persisted row remains aggregate-only. `--output` chooses the aggregate file. `--run` selects an explicit run ID, legacy attempt number or recorded session ID.

`--turns FILE` requires exactly one selected run and one line per observed conversation turn: a participant name, `OVERLAP`, or `AGENT:Name`. Blank/comment lines are ignored. `--name ID=Name` supplies a roster when absent. Misaligned counts fail rather than silently truncating or matching by content. Attribution accuracy, overlap precision/recall and named handoffs are unavailable without independent truth and sufficient authorized text. Privacy-safe live metadata deliberately cannot establish those content metrics.

- Replies are deduplicated by agent/response ID. Audio-start evidence, not transcript completion time, establishes tool-before-speech order.
- A successful get_transcript result is credited only once, to that agent's next spoken response. Results arriving after audio starts cannot be credited retroactively. The scorer reports the denominator with usable timing separately from missing timing.
- All unique `response.done.usage` values are summed, including tool-only responses. Missing usage is unknown, not zero.
- Result bytes use UTF-8 length when a legacy output is present, or the v2 allowlisted `output_bytes`; they differ from the server's full JSON-RPC response bytes.
- Floor overlap needs complete, same-clock played intervals for all replies. Queued-only timestamps, missing drain, duplicate starts and incomplete coverage yield `NA`; observed overlaps can still be reported as partial evidence.
- An overlap/reply timing calculation requires both offsets on the same accepted-audio timeline. No wall time is invented from line order or a provider response ID.

## Baseline reconciliation, 2026-09-06

The original log contained seven provider attempts, six configured sessions, rather than an unambiguous set of three complete conversations. The last three attempts were independently examined: spoken responses/transcript calls were 2/2, 6/4 and 5/5. Strict successful-result-before-audio counts were 2/2, 4/6 and 4/5. All reported response token totals (including tool-only responses) were 4,261, 17,298 and 14,149, independently checked by summing response.done usage in the source log.

The final run reproduces the handoff's five calls and five replies by count, but one preamble began before its fresh tool result. BUILD_SPEC_V2 section 3 does not contain independent turn labels or a full three-run scorecard. No attribution accuracy, precise overlap timing or zero floor-violation claim can be reconstructed from missing evidence.

`evaluation/synthetic_regression_runs.jsonl` is a constructed sequence testing these aggregate count/order cases; it contains no actual conversation text, original session/response IDs or audio. It is not a recording or an independent replication of the live test. The actual legacy evidence stays in ignored local data pending its privacy disposition.
