# Handoff prompt for the next agent session

Paste everything below the line into a fresh Claude Code session opened in `C:\Users\Grant\git\Voiceprint`.

---

You are picking up Voiceprint from a previous Claude session that worked with Grant on 2026-09-05 and 06. Grant is the owner and product lead. He is pragmatic, moves fast, tests live with a second person (Kyle, sometimes a ChatGPT voice on a device), and values honest status over polish. He runs commands himself in PowerShell and pastes output back; treat pasted output as the ground truth about his machine.

## Read first, in this order

1. `docs/V2_STREAMS.md`: the approved plan for the next build. Four parallel streams (A API, B agent runtime, C GUI, D evaluation), the "utterances table is the conversation bus" decision, and the merge conventions. Follow those conventions exactly; they exist so parallel work merges cleanly.
2. `docs/BUILD_SPEC_V2.md`: what exists, what was proved with evidence, and the longer ordered roadmap.
3. `README.md`, `docs/API.md`, `docs/TURN_TAKING.md`: the contracts as they stand.
4. `scripts/realtime_openai.py` and `scripts/voiceprint_client.py`: the code Stream B will refactor. Read them before designing the runtime; the timing fixes in there (continuation after tool calls, waiting for idle transcription, half-duplex mute) were all earned from live failures and must survive the refactor.

## What the documents do not say

- **Grant tests live, early, and often.** Do not spend a whole session building before a live run. Get something runnable by mid-session, ask him to run it with Kyle, score the log, and adjust. Every real bug this project has found came from a live run, not from reasoning.
- **Score before you tune.** The events log at `data/realtime-events.jsonl` has three complete agent runs from 2026-09-06. Stream D's harness should reproduce the hand scorecard in `docs/BUILD_SPEC_V2.md` section 3 before anyone changes thresholds or prompts.
- **The API's MCP call log is the proof.** Grant cares that a hosted agent's tool use is verifiable from our side. Keep that log line intact and surface it in the GUI first.
- **Prompt engineering of the agent is fragile.** The nudge before each reply (session id, roster, who spoke last) is what made Ava behave. Small wording changes changed behavior in ways unit tests cannot catch. Change the nudge only with a live run to confirm.
- **Kyle's microphone level is the main source of `low` labels.** If attribution looks bad, check enrollment peak levels in the terminal output before suspecting the model.
- **The user pasted an OpenAI key into chat once.** It should be rotated. Never echo keys, never write them to files, and remind him once if he pastes one again.
- **`.mcp.json` has machine-specific absolute paths.** Fine for Grant's machine; do not generalize it unless asked.

## How to work across the streams

Use subagents, one per stream, each on its own `ws/*` branch, each with a clear contract to build against and its own test file to keep green. Suggested split for a single session:

- You (the orchestrator) write the `[contract]` commit for Stream A in `docs/API.md` first. Do not delegate this; everything else depends on its exact wording.
- Then launch subagents in parallel: `ws/api` (Java: participants kind, floor, events feed, mcp_calls table, `/ui` static serving), `ws/agent` (Python: extract `agent_runtime.py`, `agents.toml`, floor client against a fake, second agent), `ws/gui` (plain HTML/JS in `web/` against the contract and the replay session), `ws/eval` (`score_run.py` reproducing today's scorecard).
- Give each subagent: the contract text, its directory ownership, the commit prefix, the test command it must leave green, and the instruction to report what it verified and what it could not.
- Merge `ws/api` first, rebase the others, then run the two-agent live test with Grant. Do not merge anything whose tests are not green, and do not let a subagent edit outside its directory.

## Environment facts that will bite you

- Bare `java` on PATH is Java 8. Use `scripts\dev.ps1 build|api|mcp|tunnel|token`. The launcher runs the API from a jar copy so rebuilds are safe; if the API was started any other way, stop it before `mvn package`.
- Worker on 8091, API on 8080, HTTP MCP on 8082, cloudflared quick tunnel with a new hostname each run (`scripts\dev.ps1 tunnel` prints it and the token). Ask Grant to start these in his own terminals so he can see the logs.
- Audio devices: Seiren X microphone; output preference "HD 4.40,BenQ" resolved by name at startup; the BenQ output disappears when the monitor sleeps.
- Bash tool heredocs containing Python triple quotes fail to parse on this setup. Write patch scripts to the scratchpad with the Write tool and run them.
- Requests to OpenAI need `OPENAI_API_KEY` in Grant's shell; you never have it. Design tests so everything except the final provider call can be verified without it.

## Standards to keep

- No invented probabilities. Labels are similarity-based and say so in every tool response until the calibration set exists.
- Every claim of "works" points at a test, a log, or a live run. If something could not be verified, say so first.
- One concern per commit, prefixed by stream, body says what was verified. `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` on every commit.
- Keep `docs/TURN_TAKING.md` and `scripts/turn_gate.py` in sync.
- Update the memory notes in `C:\Users\Grant\.claude\projects\C--Users-Grant-git-Voiceprint\memory\` at the end of the session: what was verified, what was not, what surprised you.

## Ask Grant at the start

1. Will Kyle be available today for the two-agent live test, and roughly when? Plan the build so the runnable checkpoint lands before that.
2. Second agent's name and voice (Ava is the first). Suggest "Ben" with a different OpenAI voice so humans can tell them apart.
3. Whether the GUI should be usable by Kyle on his own device over the tunnel later, or stay loopback-only for now. The plan assumes loopback.

Start by confirming the environment (worker, API, tunnel health), then write the contract commit. Report progress in short status lines as you go; Grant reads the terminal while running things.
