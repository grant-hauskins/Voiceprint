# Voiceprint

Speaker-attributed conversation middleware for voice agents: it tells an agent **who said what** in a group conversation on one shared microphone, and gives the agent that transcript over MCP. **Python owns ML; Java owns the API, session coordination, consent enforcement and SQLite persistence.** The two processes talk over local REST. A browser console on loopback runs the session, and a Python runtime hosts the voice agents.

**Status (2026-09-07):** the consent-gated v2 build is integrated on `main` ([PR #2](https://github.com/grant-hauskins/Voiceprint/pull/2)) with a one-window launcher. One two-human, two-agent live run has been observed on it: both agents fetched the transcript through the server-recorded MCP endpoint and named the right speaker. Details and limits are in the [September 6 handoff](docs/SESSION_HANDOFF_2026-09-06.md) (with its 2026-09-07 update), the [consent and destruction contract](docs/BIPA_V2.md) and the [API contract](docs/API.md). **This is not a launch-qualified product:** speaker labels are uncalibrated similarity, one live run is not validation, and the legal sufficiency of the notice and vendor settings is the operator's responsibility ([CALIBRATION.md](docs/CALIBRATION.md), [BIPA_V2.md](docs/BIPA_V2.md)).

## Start (one window)

Double-click `Voiceprint.cmd` in the repository root, or run `scripts\dev.ps1 up`. It provisions the local credentials, asks once for the controller name, address and email (saved to ignored `data\launcher.env`; they appear verbatim in every written release), builds the jar if missing, then starts the worker, the API, the cloudflared MCP tunnel and the agent runtime in that one window, verifies the public MCP URL, and opens `http://127.0.0.1:8080/ui`. The page connects to the runtime by itself; nothing is pasted. The rest of a session happens in the browser:

1. **Conversation runtime** panel: paste the OpenAI API key (held only in the runtime process, never written to disk) and enter each person's full name and email/phone; two to four people. Under **Agents**, give each agent its flavor: standing instructions typed in or loaded from a text file ("always answer in haiku", "only words that start with A", a summary of what your agent should know), and optionally the roster name it **speaks for**. Instructions are saved on this computer under `data\agents\NAME.md` and prefilled next time; the room rules about who spoke and when to speak still apply on top. *Create room* posts the roster; the microphone stays closed.
2. **Participant releases**: each person reads the notice on the shared screen, types their name, checks the release and both optional disclosures (OpenAI audio, hosted MCP), and signs. Without both disclosures from everyone the room cannot use the agents.
3. **Enrollment**: press *Record NAME now*; that person speaks the eight-second statement. Peak levels and rejections show inline; a rejected set is recorded again.
4. Press **Start conversation**. Speak/Hold/Cancel/eagerness per agent, the transcript, the floor, provider-declared tool activity and the server's own MCP proof are on the same page. *End conversation* closes the microphone and ends the room; *Withdraw* on any release stops everything and starts destruction.

When a conversation ends the launcher offers another one in the same services; Ctrl+C in the window stops everything, and services started by the launcher die with it even if the window is closed.

Before hosted agents can run, review the OpenAI and Cloudflare account settings, then add to `data\launcher.env`:

```
VOICEPRINT_OPENAI_REVIEWED=true
VOICEPRINT_CLOUDFLARE_REVIEWED=true
```

Until then the launcher says so at start and the page disables *Create room*. Optional settings in the same file: `VOICEPRINT_DEVICE` (microphone preference), `VOICEPRINT_MCP_URL` (skip the tunnel), `VOICEPRINT_CONTROL_PORT`. If 8090 is busy the launcher picks the next free port and opens `/ui?control=PORT`. If something already listens on the worker/API/MCP ports (a leftover from an earlier run keeps its old settings and credentials), the launcher stops and names the PIDs: rerun with `--replace-services` to stop them, or `--reuse-services` to keep them after it checks their notice and credential against yours. `--no-browser`, `--once` and `--skip-tunnel-check` exist for scripted runs.

Agents are configured in `scripts/agents.toml`: Ava (voice `marin`, balanced) and Ben (voice `cedar`, quiet), both on OpenAI Realtime `gpt-realtime-2.1`, with a preferred output device per agent; `instructions_extra` and `speaks_for` there are the defaults the page shows, and a saved `data\agents\NAME.md` wins over the file. xAI and Gemini adapters are stubs. Extra MCP servers per agent (an agent reaching its person's own data) are not wired yet: another server is another recipient of what is said in the room, so the signed notice has to name it first.

## What works

- Consent first: a pending room, a controller notice with a hash, a one-use challenge per signer, a typed-name written release with optional disclosure scopes, effective scopes computed by the server (local processing; OpenAI audio and hosted MCP only when everyone disclosed and both vendor flags are set), withdrawal that stops the whole room, purpose completion, a 30-minute inactivity expiry and a destruction sweeper with per-destination evidence. No microphone opens before every release is committed; the enrollment audio hash is linked to the existing release.
- Enrollment of 2–4 people from separate eight-second statements; one mixed, mono 16 kHz PCM16LE stream in ordered 250 ms chunks; SpeechBrain ECAPA embeddings with cosine matching over a 1.5 second rolling context; pyannote powerset overlap inference from its public ONNX export. Overlap and speaker changes suppress a single-speaker decision.
- Turn-level transcript: the runtime groups chunks into speaker turns, transcribes each turn locally with faster-whisper (CPU, `base.en`) and stores it with similarity, margin, overlap and abstention stats. Labels `high/medium/low` are similarity-based, not calibrated; overlap rows name both candidates. Agents can ask for `min_label=high` only.
- Two voice agents on one microphone with a server-owned speaking floor (leased, renewed, released), an application-level turn-taking gate ([TURN_TAKING.md](docs/TURN_TAKING.md)): the provider's VAD only segments audio; the runtime decides when an agent may speak from speaker labels, overlap, silence and direct address. Agent turns are stored on the same conversation bus so agents hear each other.
- MCP tools: `list_sessions`, `get_transcript` (compact `#id time Name [label]: words` lines with an `after_id` cursor), `get_current_speaker`, participant statements and exact-segment corrections. Over stdio for local clients (`.mcp.json`) and over Streamable HTTP (`:8082/mcp`, bearer token, no browser origins) for hosted agents. Every hosted call, allowed or denied, is written by the server as proof and shown in the console next to what the provider claims it did.
- Corrections update SQLite profiles for verified single-speaker windows; attributions, profiles, releases, proof rows and the correction audit survive restart. Raw audio is held only in memory. Disk event logs are disabled; scoring works from bounded in-memory metadata (`scripts/score_run.py`, [EVENTS.md](docs/EVENTS.md)).

The API returns `confidence: null`, `confidence_kind: "uncalibrated"` and `trusted: false` until a validated calibration artifact is loaded. `similarity` is a cosine score, **not a probability**.

## Manual run

The launcher is the supported path. The pieces can also be started by hand, which is what the launcher does:

```powershell
scripts\dev.ps1 build            # jar, using the project-local Maven and an installed or editor-bundled JDK 21
scripts\dev.ps1 worker           # speech worker on 8091
scripts\dev.ps1 api              # API on 8080 (+ /ui), hosted MCP on 8082; needs the VOICEPRINT_CONTROLLER_* variables
scripts\dev.ps1 tunnel           # cloudflared quick tunnel for 8082 only; prints the URL and the MCP bearer token
.venv\Scripts\python.exe scripts\agent_runtime.py --gui --mcp-url https://<name>.trycloudflare.com/mcp
```

`scripts\dev.ps1 api-token` prints the operator token for the console when the runtime is not started with `--gui`. Without `--gui`, `agent_runtime.py --names A B --contacts a@x b@y` takes the roster on the command line, prompts in the console for each enrollment recording and for Start, and keeps the keyboard controls (1/2 select an agent, Space speak, H hold, C cancel, Q quit); the releases are still signed in the console page. `scripts\live.py` is the local-only path without agents and is subject to the same releases; replaying human recordings through it is blocked.

Fresh checkout, once (Windows PowerShell; JDK 21, Maven 3.9+ and Python 3.12; Java 8 is insufficient):

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install torch==2.6.0 torchaudio==2.6.0 --index-url https://download.pytorch.org/whl/cpu
.venv\Scripts\python.exe -m pip install -r worker\requirements.txt
.venv\Scripts\python.exe worker\setup_models.py
winget install --id Cloudflare.cloudflared --scope user
```

The model setup downloads public weights into ignored `models/`, records resolved repository revisions and SHA-256 hashes, and reuses those revisions on subsequent runs. Inference then loads from local disk.

## Verify

```powershell
scripts\dev.ps1 build                                                     # runs the Java suite
.venv\Scripts\python.exe -m unittest discover -s scripts -p "test_*.py"    # runtime, lifecycle, launcher, turn gate, scorer
.venv\Scripts\python.exe -m unittest discover -s worker -p "test_*.py"
.venv\Scripts\python.exe -m unittest discover -s web -p "test_*.py"        # set VOICEPRINT_BROWSER_SMOKE=1 for the headless Chrome check
.venv\Scripts\python.exe scripts\agent_runtime.py --check-config
```

All suites use synthetic inputs: no provider key, microphone or human recordings. `scripts\two_speaker_smoke.py` and `data\verify_v2_integration.py` predate the consent gate and replay fixtures directly; they are not valid against the v2 API as written. See [VALIDATION.md](docs/VALIDATION.md) for measured results and their limits and [CALIBRATION.md](docs/CALIBRATION.md) for the probability mapping and the validation set still required.

## MCP clients

With the worker and API running, `.mcp.json` in the repository root configures Claude Code for this checkout (project scope; approve it when Claude Code asks). The v2 API requires the operator bearer on every session endpoint, so the environment Claude Code runs in needs `VOICEPRINT_API_TOKEN` set to the value from `scripts\dev.ps1 api-token`; do not commit it. For other stdio clients:

```json
{
  "mcpServers": {
    "voiceprint": {
      "command": "java",
      "args": ["-jar", "/absolute/path/to/Voiceprint/target/voiceprint-0.1.0.jar", "mcp"],
      "env": {"VOICEPRINT_API_URL": "http://127.0.0.1:8080", "VOICEPRINT_API_TOKEN": "<operator token>"}
    }
  }
}
```

The adapter implements the MCP `2025-11-25` stdio protocol: `initialize`, `notifications/initialized`, `tools/list` and `tools/call`.

Hosted providers use the Streamable HTTP endpoint on `:8082/mcp` through the cloudflared tunnel, with the bearer token from `scripts\dev.ps1 token`. Only port 8082 is ever tunnelled; the API and the console stay on loopback. OpenAI Realtime is the tested provider (`scripts/providers/openai_realtime.py`); xAI Responses and Speech-to-Speech accept the same remote MCP server directly, Gemini Live would need a function-call bridge; neither is wired up. A text-only check of the endpoint with the OpenAI Responses API is `scripts\openai_responses_probe.py <url>`.

## Settings and retention

- `VOICEPRINT_CONTROLLER_NAME`, `VOICEPRINT_CONTROLLER_ADDRESS`, `VOICEPRINT_CONTROLLER_EMAIL`: required before any release can be collected; the launcher asks once and stores them in `data\launcher.env`.
- `VOICEPRINT_OPENAI_REVIEWED`, `VOICEPRINT_CLOUDFLARE_REVIEWED`: the operator's attestation that the vendor agreements and account retention settings were reviewed; hosted scopes are false without both.
- `VOICEPRINT_API_TOKEN` (operator), `VOICEPRINT_WORKER_TOKEN` (API to worker), `VOICEPRINT_MCP_TOKEN` (hosted MCP bearer): generated once by `scripts\dev.ps1` into ignored files under `data\`, restricted to the current Windows user.
- `VOICEPRINT_PORT` (8080), `VOICEPRINT_MCP_PORT` (8082), `VOICEPRINT_WORKER_URL` (`http://127.0.0.1:8091`), `VOICEPRINT_DB` (`data/voiceprint.sqlite`), `VOICEPRINT_ML_THREADS` (1; more can slow these small windows).
- Worker `--calibration PATH`: load an eligible calibration artifact; fails startup if invalid.

Both servers bind to loopback and serve a single trusted local operator; the API accepts only its exact local origin and rejects browser origins on MCP. Network deployment would need a separate authenticated TLS boundary and tenant isolation. Retention is governed by the notice each person signs ([BIPA_V2.md](docs/BIPA_V2.md)): data lives for the current conversation, withdrawal or purpose completion starts destruction of the whole room, and a job stays incomplete while any destination lacks verified deletion. SQLite deletion is logical: pages, WAL, backups and OS snapshots are not secure erasure, and a provider closing a connection does not prove deletion of its logs. Never put audio or identifiers in source control.

## Next

Collect and independently label the calibration/validation set; write the dedicated Java negative and fault tests for the privacy boundary; review direct OpenAI audio possible-egress accounting against the runtime's actual send path; run more two-agent sessions and score them before tuning any threshold or prompt.
