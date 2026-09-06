# Voiceprint v2 plan: GUI, multiple voice agents, one shared loop, parallel build streams

## Context

Today's live test closed the loop the project exists for: a shared microphone, speaker identification with honest labels, and a hosted voice agent ("Ava", OpenAI Realtime) that reads who said what over MCP before every reply. The v2 build spec (`docs/BUILD_SPEC_V2.md`) lists the next technical steps. This plan expands it in three directions the user asked for, and organizes the work into parallel streams with merge rules:

1. **A GUI**: see the room, the transcript, the labels, the agents, and the MCP calls, without reading a terminal.
2. **More than one voice agent**: configure two or more agents (possibly different providers) in the same conversation.
3. **Everyone in the loop**: when one agent speaks, every other agent sees it as a transcript line, attributed, so agents and humans share one record.

Implementation is now in separate `ws/*` worktrees. See [the September 6 handoff](SESSION_HANDOFF_2026-09-06.md) for actual commits, tests and unfinished work, and [the resume checklist](V2_RESUME_CHECKLIST.md) for merge order. [BIPA_V2.md](BIPA_V2.md) supersedes this plan wherever prior consent, disclosure, logging or retention requirements differ; in particular, human replay is not permitted without documented prior permission.

## The one design decision that makes the rest easy

**The utterances table is the conversation bus.** Humans already land there via Voiceprint. Agents will land there too: each agent is a participant with an id, and every agent reply is posted as an utterance under that id (text from the provider's output transcript, source `agent`). Agents read each other exactly the way they read humans: `get_transcript(after_id)`. No agent-to-agent messaging, no second channel, no new protocol. The GUI reads the same table.

Consequences:
- A new participant kind: `human` (voice-enrolled) or `agent` (registered, no voice until the agent-voice enrollment step lands). Same id space (`participant_N`), same names in transcript lines.
- Agents need a **floor** so two of them do not speak at once. The API owns it: one holder per session, short lease, first-come.
- The gate gains agent awareness: it treats other agents' lines as speech in the room, obeys the floor, and can be addressed ("Ava, ask Ben what he thinks").

## Workstreams

Four streams, buildable in parallel by four sessions or four people. Each owns a directory and a contract section. Merge order A → (B, C, D).

### Stream A: API and storage (Java, `src/`)

Owns: schema, REST, MCP tools, the floor.

Build:
1. `participants.kind` column (`human|agent`), schema v4 migration in `Store.java` (same pattern as v3: ALTER when `version == 3`).
2. `POST /speaker/session/{id}/participants` to register an agent participant `{id, name, kind: "agent", provider, model}`; enrollment remains for humans. Enrollment atomicity unchanged.
3. Utterance `source` values standardized: `faster_whisper`, `openai_transcription`, `agent`. `GET utterances` unchanged; agent lines render as `#id t Name [agent]: words` (label `agent`, always trusted for attribution since the text came from the agent itself).
4. Floor: `POST /speaker/session/{id}/floor {participant_id, lease_ms}` returns `granted|held_by`. `DELETE .../floor` releases. Lease auto-expires. `GET .../floor` for the GUI. MCP tools `claim_floor`, `release_floor` (optional; the runtime calls REST directly).
5. `GET /speaker/session/{id}/events?after_id=N` returning utterances plus floor changes plus MCP call log entries in one ordered feed, for the GUI. Long-poll up to 10 s (no SSE, tunnels buffer it).
6. MCP call log persisted (`mcp_calls` table: time, caller ip, tool, arguments, bytes, session) so the GUI can show "Ava called get_transcript" and the proof survives restarts.
7. Static file serving at `/ui/*` from `web/` (Stream C's output). Loopback only.

Tests: participants kind, floor grant/deny/expiry, events feed ordering, migration from v3.

### Stream B: Agent runtime (Python, `scripts/`)

Owns: `agent_runtime.py` (new), provider adapters, the gate.

Build:
1. Extract from `realtime_openai.py`: mic capture, player, echo mute, gate loop, event logging into `agent_runtime.py`. One process hosts one microphone and N agents.
2. `agents.toml` config: per agent `name`, `provider` (`openai_realtime` now; `xai_speech`, `gemini_live` later), `model`, `voice`, `eagerness`, `output_device`, `instructions_extra`. Loaded by `--config`.
3. Each agent: registers as a participant (Stream A endpoint), gets its own `GateState`, posts every reply as an utterance under its id from `response.output_audio_transcript.done`, claims the floor before `response.create`, releases on `response.done`.
4. Gate changes in `turn_gate.py`: `others_speaking` from the floor; agent lines count as speech for silence timing; direct address by any agent name; per-agent cooldown; a room-level rule that no agent speaks twice in a row unless addressed (prevents two agents talking to each other forever).
5. Provider adapter interface: `connect()`, `send_audio(pcm24k)`, `request_reply(note)`, `cancel()`, events iterator. OpenAI adapter is the existing code; others are stubs with the same shape.
6. Keys: `--agent NAME` prefix on keyboard controls (`1`/`2` select agent, then Space/H/C), so the human can direct a specific agent.

Tests: gate with two agents (scripted), floor contention (fake REST), config parsing, adapter interface conformance test that the OpenAI adapter passes.

### Stream C: GUI (web, `web/`)

Owns: a single static page served by Stream A at `/ui`, plain HTML + JS, no build step.

Build:
1. Session picker (from `GET /speaker/sessions`).
2. Live transcript panel: one row per utterance, name, time, label chip (high/medium/low/overlap/agent), text; overlap rows highlighted; new rows appended from the events feed. Click a row to correct the speaker (calls the utterance correction endpoint from the v2 spec once Stream A ships it; until then, the existing chunk-level `correct` is hidden).
3. Room panel: participants with kind icon, current speaker with similarity bar, floor holder with lease countdown.
4. Agent panel per agent: provider/model, eagerness selector, Speak / Hold / Cancel buttons (these post to a small runtime control endpoint in Stream B: `http://127.0.0.1:8090/agents/{name}/control`), last MCP call with size and time.
5. MCP call log strip at the bottom: time, agent, tool, args, bytes. This is the proof view.
6. No frameworks, no bundler, dark and light via `prefers-color-scheme`. Everything from the API; no direct DB access.

Tests: a Python smoke test that serves the page and checks the events feed renders N rows (using the replay session).

### Stream D: Evaluation (Python, `scripts/score_run.py`, `data/runs.jsonl`)

Owns: scoring and the fixtures.

Build: the harness from v2 spec 5.1, extended for multiple agents: per-agent tool-call ratio, per-agent replies-during-overlap, floor violations (two agents' audio overlapping), agent-to-agent handoffs that named the right agent. Output Markdown plus a JSON row per run. A `--turns` file format: one line per turn, `Name` or `OVERLAP` or `AGENT:Name`.

Tests: scoring the three existing runs in `data/realtime-events.jsonl` reproduces today's hand scorecard.

## Conventions so the streams merge without friction

1. **Contracts before code.** Stream A writes the new endpoint and schema sections into `docs/API.md` first, in a single commit named `[contract] ...`, and the other streams build against that text. Any change to a contract is its own `[contract]` commit reviewed by whoever consumes it.
2. **Directory ownership.** A: `src/`, `docs/API.md`, `docs/CALIBRATION.md`. B: `scripts/agent_runtime.py`, `scripts/providers/`, `scripts/turn_gate.py`, `docs/TURN_TAKING.md`. C: `web/`. D: `scripts/score_run.py`, `scripts/test_score_run.py`, `evaluation/`. Shared and frozen except by agreement: `scripts/voiceprint_client.py` (its public names are the interface; additions are fine, signature changes are a contract change), `README.md` (each stream edits only its own section).
3. **Branches.** First merge PR #1 (`v1-spike`) into `main`. Then one branch per stream: `ws/api`, `ws/agent`, `ws/gui`, `ws/eval`, off `main`. Rebase on `main` at the start of every session. Merge order when ready: `ws/api` first, then the others in any order.
4. **Commit prefixes.** `[api]`, `[agent]`, `[gui]`, `[eval]`, `[contract]`, `[docs]`. One concern per commit. Body says what was verified.
5. **Green before merge.** `scripts\dev.ps1 build` (Java tests) and `python -m unittest discover -s scripts -p "test_*.py"` and the worker suite must pass on the branch. Anything touching audio paths must also pass the replay run (`live.py run --stream-wav data/replay/conversation_overlap.wav`).
6. **Schema changes only in Stream A**, always as a numbered `user_version` migration with a test that upgrades the previous version's database.
7. **Events log is a contract.** `data/*.jsonl` line shapes (`attribution`, `utterance`, `openai`, and new `floor`, `control`) are documented in `docs/EVENTS.md` (Stream D writes it from the current shapes; others add fields, never rename).
8. **No secrets in the repo, ever.** Keys in the shell environment; MCP token in ignored `data/`.
9. **Honesty rules carry over.** No invented probabilities. Labels are similarity-based until calibration lands. Every claim in a PR description points at a test or a log.
10. **Rebuild discipline.** Stop the API before `mvn package` unless it was launched by `dev.ps1` (which runs a jar copy).

## Suggested day plan

- Morning, one session: merge PR #1 to main; Stream A writes the contract commit (`docs/API.md`: participants kind, floor, events feed, mcp_calls). Half a day.
- Then fan out: A implements; B extracts the runtime and adds the second agent against the contract using a fake floor; C builds the page against the contract using the replay session; D writes the harness against today's logs.
- End of day: merge A, rebase B/C/D, run one live session with two agents (both OpenAI Realtime, different names and voices, one `quiet`, one `balanced`), score it with D, look at it in C.

## Verification for the whole plan

- Two agents, two humans, one mic: transcript shows all four names; no two agent audio streams overlap (floor); each agent reply appears as an utterance; the second agent's next reply references the first agent's line by name.
- The GUI shows the transcript growing live, the floor holder switching, and an MCP call row for every agent reply.
- `score_run.py` on that session prints per-agent tool-call ratio 100%, floor violations 0.
- All test suites green on `main` after the merges.

## Deferred, deliberately

Agent voice enrollment (v2 spec 5.2) stays queued behind this; the floor plus echo mute is enough for two agents. xAI and Gemini adapters are stubs until Stream B's interface is proven with OpenAI. Calibration pilot unchanged.
