# Voiceprint

A working two-speaker middleware spike: **Python owns ML; Java owns the API, session coordination and SQLite persistence.** The two processes communicate over local REST. A thin Java stdio MCP adapter queries the same API.

The real-model integration path runs. **This is not a launch-qualified MVP:** calibrated confidence and live accuracy still require the explicitly defined validation set in [CALIBRATION.md](docs/CALIBRATION.md). No fixture-derived probabilities are presented to agents.

## What works

- Enroll 2–4 participants from separate 5–15 second opening statements.
- Send one mixed, mono 16 kHz PCM16LE stream in ordered 250 ms chunks. Matching uses a 1.5 second rolling context.
- SpeechBrain ECAPA speaker embeddings and Python cosine matching against session profiles.
- Actual pyannote powerset overlap inference using its public ONNX export. Overlap and speaker changes suppress a single-speaker decision.
- Corrections update SQLite profiles for verified single-speaker windows. Reassigning a correction moves its training example; repeated requests do not double count it.
- Current-speaker queries expire after 1.5 seconds without fresh input. Worker errors return 503 and make current context unavailable.
- Attributions, profiles and the correction audit survive restart. Raw audio is held only in memory.
- MCP tools for current speaker, participant statements and exact-segment corrections.

The API returns `confidence: null`, `confidence_kind: "uncalibrated"`, and `trusted: false` until a validated calibration artifact is loaded. `similarity` is a cosine score, **not a probability**. Text is optional, supplied by an external ASR client; this spike does not transcribe audio.

## Run locally

Requires **JDK 21**, **Maven 3.9+**, and **Python 3.12**. Java 8 is insufficient. In PowerShell, from the repository:

On the prepared Windows checkout, `scripts\dev.ps1 build`, `scripts\dev.ps1 worker`, and `scripts\dev.ps1 api` use the project-local Maven and an installed/editor-bundled JDK. The helper downloads nothing. Start worker and API in separate terminals. The manual setup below is for a fresh checkout.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install torch==2.6.0 torchaudio==2.6.0 --index-url https://download.pytorch.org/whl/cpu
.venv\Scripts\python.exe -m pip install -r worker\requirements.txt
.venv\Scripts\python.exe worker\setup_models.py
mvn package
```

The model setup downloads public weights into ignored `models/`, records resolved repository revisions and SHA-256 hashes, and reuses those revisions on subsequent runs. Inference then loads from local disk.

Start the worker in one terminal:

```powershell
.venv\Scripts\python.exe worker\worker.py
```

Start the Java API in another:

```powershell
java -jar target\voiceprint-0.1.0.jar
```

Run a shared-microphone conversation in a third terminal:

```powershell
.venv\Scripts\python.exe scripts\live.py run --names Grant Kyle --seconds 60 --events data\live-events.jsonl
```

Each participant presses Enter and speaks alone for eight seconds. The client then starts the live stream. It prints speaker, confidence availability, uncertainty, latency and segment ID. It stops on audio overflow or sustained inference backlog instead of silently losing samples. Use `--device N` to choose a microphone device. All participants use the same microphone; separate participant streams are not implemented.

Apply a correction in another terminal using IDs printed by the client:

```powershell
.venv\Scripts\python.exe scripts\live.py correct SESSION_ID SEGMENT_ID participant_2
```

On macOS/Linux, use `.venv/bin/python` for the same commands.

## Verify

```powershell
mvn test
.venv\Scripts\python.exe -m unittest discover -s worker -p "test_*.py" -v
.venv\Scripts\python.exe scripts\fetch_test_audio.py
```

For the real-model smoke test, start the Java API with a dedicated database:

```powershell
$env:VOICEPRINT_DB = "data/integration.sqlite"
java -jar target\voiceprint-0.1.0.jar
```

With both services running:

```powershell
.venv\Scripts\python.exe scripts\two_speaker_smoke.py
```

This integration-only client replays public fixtures at capture cadence. Enrollment uses utterances 1–3 and evaluation uses utterances 4–6 for each speaker. It verifies identification of both speakers, actual overlap inference on mixed speech, persisted profile adaptation, history retrieval and session termination. It writes observations to ignored `data/two-speaker-smoke.json`. This does **not** add a batch-processing endpoint or constitute live validation.

See [VALIDATION.md](docs/VALIDATION.md) for measured results and their limits, [API.md](docs/API.md) for contracts, and [CALIBRATION.md](docs/CALIBRATION.md) for the probability mapping and validation deliverable.

## MCP

Keep the worker and Java API running. Configure your MCP client to launch:

```json
{
  "mcpServers": {
    "voiceprint": {
      "command": "java",
      "args": ["-jar", "/absolute/path/to/Voiceprint/target/voiceprint-0.1.0.jar", "mcp"],
      "env": {"VOICEPRINT_API_URL": "http://127.0.0.1:8080"}
    }
  }
}
```

The adapter implements the MCP `2025-11-25` initialization/stdio protocol. It does not expose REST routes as if they were MCP; `initialize`, `notifications/initialized`, `tools/list` and `tools/call` are implemented and tested. It does not implement Streamable HTTP or claim support for newer protocol revisions.

## Runtime settings and retention

- `VOICEPRINT_PORT`: Java API port, default `8080`.
- `VOICEPRINT_WORKER_URL`: default `http://127.0.0.1:8091`.
- `VOICEPRINT_DB`: default `data/voiceprint.sqlite`.
- `VOICEPRINT_API_TOKEN`: optional bearer token; set the same value in the API, microphone client and MCP adapter environments.
- `VOICEPRINT_ML_THREADS`: Python CPU inference threads, default `2`.
- Worker `--calibration PATH`: load an eligible calibration artifact; fails startup if invalid.

Both servers bind to loopback. They are intended for a single trusted local user. Network deployment requires a separate authenticated TLS boundary and tenant isolation; it is not part of this spike. Browser-origin API requests are rejected.

Enrollment anchors, corrected embeddings, participant names, supplied text, attributions and correction logs remain in SQLite until explicitly deleted with `DELETE /speaker/session/{session_id}`. Ending a session clears its audio buffer but retains history. Deletion removes logical records; SQLite pages/WAL, backups and operating-system snapshots are not secure erasure. There is no automatic retention policy yet. Obtain participant agreement before enrolling real voices. Avoid putting audio or identifiers in source control.

## Next gate

Run the shared-microphone path with two consenting people; collect and independently label the planned calibration/validation set. Evaluate cold-start latency, turn-change latency, overlap precision/recall, abstention coverage, probability calibration and correction benefit on future utterances before expanding scope. A 250 ms chunk plus about 250 ms processing does not remove the initial 1.5 second context requirement.
