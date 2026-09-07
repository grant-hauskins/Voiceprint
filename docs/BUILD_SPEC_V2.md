# Voiceprint: what exists, what it proved, and the v2 build spec

Written 2026-09-06 after the first end-to-end live test with a hosted voice agent. This document is the working spec for the next build sessions. It supersedes the phasing section of the original brief; the original problem statement stands.

Later implementation checkpoint: [SESSION_HANDOFF_2026-09-06.md](SESSION_HANDOFF_2026-09-06.md) records the separate v2 branches and verified/unverified work. Historical evidence below does not verify the new runtime or privacy enforcement. [BIPA_V2.md](BIPA_V2.md) governs prior consent and retention; [V2_RESUME_CHECKLIST.md](V2_RESUME_CHECKLIST.md) gives the remaining integration and live-test steps.

## 1. The business problem, restated

AI agents are being put into rooms with more than one person: negotiations, family decisions, meetings, support calls with a relative on the line. Every hosted voice API (OpenAI Realtime, Gemini Live, xAI speech-to-speech) hears one microphone and treats everything on it as "the user". The agent cannot tell who said what, so it cannot represent one person, cannot weigh one voice over another, and cannot tell when to speak. Voice assistants dodged this with wake words. Agents that act on someone's behalf cannot.

Voiceprint is middleware that sits between a shared microphone and any agent. It answers three questions cheaply and honestly: who is talking, how sure are we, and is it a good moment for the agent to speak. It exposes those answers over MCP so any agent, local or hosted, can ask.

The commercial wedge is narrow and real: in-room, shared-microphone conversations where an agent acts for one participant. Platforms that already have per-participant audio (Zoom, Meet) do not need this. Diarization models are commodity; the product is the labeled, cursor-paged, agent-consumable transcript plus the turn-taking gate, delivered through a protocol agents already speak.

## 2. What was built

| Layer | What it is | Status |
|---|---|---|
| ML worker (Python, `worker/`) | SpeechBrain ECAPA embeddings, pyannote 3.0 segmentation (ONNX) for overlap and speaker change. Loopback HTTP. | Working since v1 |
| API (Java 21, `src/`) | Enrollment from 8 s statements, 250 ms chunk streaming with 1.5 s context, per-chunk attribution with similarity and margin, corrections that update profiles, SQLite persistence (schema v3). | Working |
| Utterances | Turn-level rows with text, similarity, margin, overlap ratio, abstention ratio, a label (high/medium/low/overlap/unknown), and candidates for overlap rows. `min_label` filter. | New this session |
| Client library (`scripts/voiceprint_client.py`) | Enrollment, mic capture, chunk streaming, turn grouping, overlap segment isolation, local ASR with faster-whisper, posting utterances. `#id` printed once a row is stored. | New this session |
| MCP, stdio (`java -jar … mcp`, `.mcp.json`) | Five tools: list_sessions, get_transcript, get_current_speaker, get_participant_statements, correct_attribution. Used by Claude Code. | Working |
| MCP, Streamable HTTP (`:8082/mcp`) | Same tools, stateless, token-protected, JSON-only, exposed with a cloudflared quick tunnel. Logs every call with caller IP, tool, arguments, size. | New this session |
| Voice agent (`scripts/realtime_openai.py`) | OpenAI Realtime session with the Voiceprint remote MCP tool, provider VAD segmenting only, application-level turn gate, half-duplex echo mute, device preference, keyboard override, tool-call continuation. | New this session, live-tested |
| Turn gate (`scripts/turn_gate.py`, `docs/TURN_TAKING.md`) | Decides speak / clarify / wait from direct address, silence, newest label, overlap, cooldown, manual keys. | New this session, unit-tested |
| Tests | 20 Java, 14 Python client. Replay fixtures for mic-free runs. | Passing |

Total on the branch: 17 commits since the v1 spike, PR #1 on grant-hauskins/Voiceprint.

## 3. What was proved, with evidence

Each item below has a log or a test behind it; none is an inference.

- **Two people on one microphone are told apart in real time.** Live session: every stored line matched the person who spoke, by the content of what they said. Latency per chunk about 200 ms.
- **Overlap is caught and isolated.** Live: two `OVERLAP Kyle+Grant` rows during simultaneous speech; the agent did not reply into them. Replay fixture: 11 of 11 mixed windows flagged, 0 false flags.
- **A local agent can use it.** Claude Code, over stdio MCP, answered "who said X" and "which lines were overlap" correctly, about $0.25 per question including session overhead.
- **A hosted agent can use it.** OpenAI Responses API listed the tools through the public tunnel, called get_transcript, and answered correctly. About 3,200 tokens.
- **A hosted voice agent can use it in the room.** OpenAI Realtime agent "Ava" called get_transcript before each reply (5 of 5), named the people present correctly, addressed whoever asked, and refused to guess when the newest line was uncertain. Audio out via the monitor, no self-echo in the transcript.
- **The agent's uncertainty is honest.** Labels are similarity-based and marked as such in every tool response. No probability is invented anywhere.

What is not proved: accuracy under noise, distance, similar voices, more than two people, or long sessions. The calibration set defined in `docs/CALIBRATION.md` has not been collected. Latency from speech onset to first attribution is about 1.5 s by design.

## 4. Lessons that shape v2

- Hosted providers each reject something different (Realtime refused `server_description`; `response.create` with `instructions` silently replaces the session prompt; a tool call ends the response and needs a continuation). Keep provider clients thin and log every event.
- The agent must be told who spoke last and what the roster is, in the nudge before each reply. Left to the tool alone, it over-applies old uncertainty and refuses.
- Timing is everything: the gate must wait for transcription to finish and for the tool result to land, or the agent answers from a stale transcript.
- Microphone level dominates label quality. Enrollment peaks under ~3000 of 32767 produced `low` lines all session.
- A JVM must not have its jar rebuilt underneath it. The launcher now runs from a copy.

## 5. v2 build spec

Ordered by value per hour. Each item states what to build, why, where, and how to verify. Keep the honesty rules: no invented probabilities, every claim backed by a test or a log.

### 5.1 Scoring harness (first, because everything after is measured with it)

**Why:** every run so far was scored by hand from the terminal. The brief's success metrics (attribution accuracy, overlap precision, correction rate) need numbers.

**Build:** `scripts/score_run.py EVENTS.jsonl --turns turns.txt`. Input: the events file and a hand-written turn order (`Grant, Kyle, Kyle, OVERLAP, Grant…`). Output: attribution accuracy per speaker, label distribution, overlap precision/recall against the marked overlaps, replies-with-tool-call ratio, replies started inside 3 s of an overlap, tool result sizes, latency percentiles, tokens if the provider reports them. Print a Markdown table and append a JSON row to `data/runs.jsonl`.

**Verify:** run it on the three existing agent runs in `data/realtime-events.jsonl`; numbers must match the hand scorecards in this document's section 3.

### 5.2 Agent voice enrollment (removes the half-duplex trade-off)

**Why:** the mute drops anything a human says while the agent talks. Enrolling the agent's voice makes its speech a labeled participant instead of noise, so the mic can stay open and interruptions become visible.

**Build:** at startup, request a short spoken sample from the agent (`response.create` with text "Say: I am Ava, the assistant in this room" before the humans enroll), capture the played audio through the shared mic, and enroll it as `participant_agent`. Set `GateState.agent_speaker_id`. Remove the mute when the agent is enrolled (`--no-agent-enroll` keeps the old path). Post the agent's own replies as utterances with `speaker_id = participant_agent` from `response.output_audio_transcript.done`, so the transcript is complete.

**Verify:** a human interrupts the agent mid-sentence; the transcript shows an OVERLAP row naming the agent and the human, and the human's words after the agent stops are attributed to the human.

### 5.3 Voice corrections through the agent

**Why:** the brief's P0 "user correction mechanism" exists in the API but nobody will type segment IDs. "Ava, that was Kyle, not me" should fix the record.

**Build:** new endpoint `POST /speaker/session/{id}/utterances/{utterance_id}/correct {actual_speaker}` that relabels the utterance and applies `correct_attribution` to every eligible chunk inside its span. MCP tool `correct_utterance(session_id, utterance_id, actual_speaker)`. Gate rule: a direct address containing "that was <name>" or "not me" maps to the newest human line and calls the tool; the agent confirms in one sentence.

**Verify:** Java test for span relabeling and profile update; live test where a deliberate misattribution is corrected by voice and the next utterance from that speaker gets a better label.

### 5.4 Pilot calibration set (4 pairs, not 10)

**Why:** labels are placeholders. The brief wants calibrated confidence with correlation above 0.8. The full VP-Live-En-v1 needs 20 people; a 4-pair pilot tells us whether the thresholds are even in the right place.

**Build:** record 4 pairs × 2 conditions with `live.py`, label independently with the `evaluation/observations.template.csv` protocol, run `worker/calibration.py`. Report Brier, ECE, Spearman. Do not ship the artifact unless it is release-eligible; do adjust the high/medium thresholds if the pilot shows they are wrong.

**Verify:** the calibration report file in `data/` and a paragraph in `docs/VALIDATION.md` with the numbers.

### 5.5 Provider transcript alignment (better text, lower CPU)

**Why:** local faster-whisper costs 1–2 s per turn and CPU. OpenAI already transcribes what it hears. Use both: Voiceprint for who and when, the provider for the words, aligned by time.

**Build:** in `realtime_openai.py`, collect `conversation.item.input_audio_transcription.completed` with `input_audio_buffer.speech_started/stopped` offsets; map each provider segment to the overlapping Voiceprint utterance span; post text with `source = "openai_transcription"`. Keep faster-whisper as fallback when the provider is silent for more than 3 s. Flag `--asr local|provider|both`.

**Verify:** same conversation transcribed both ways; word error against a hand transcript; turn-to-text latency.

### 5.6 Second and third providers

**Why:** the pitch is "any agent". One provider proves the plumbing; two prove the abstraction.

**Build:** `scripts/realtime_xai.py` (speech-to-speech with remote MCP, same tool config) and `scripts/gemini_live.py` (Live API function calling bridged to `POST /mcp`). Factor the shared parts of `realtime_openai.py` (mic, player, gate loop, logging) into `scripts/agent_runtime.py` first.

**Verify:** the 5.1 scoring harness on one scripted conversation per provider; a comparison table in `docs/PROVIDERS.md`.

### 5.7 Deployment beyond the laptop

**Why:** quick tunnels change name every run and have no SLA.

**Build:** a Cloudflare named tunnel with a stable hostname, or a small VM running worker + API + MCP behind TLS. Per-agent bearer tokens (a `tokens` table, token → allowed sessions). Session TTL and audio retention setting. Health endpoint that includes worker readiness.

**Verify:** the agent client works with the stable URL after a reboot; a token for session A cannot read session B.

### 5.8 Robustness

**Why:** every live run so far has been under 3 minutes.

**Build:** WebSocket reconnect with session resume; worker restart without losing the API session; API restart with `stale` handling already present; 30-minute soak test with replay audio looped; memory and latency plots.

**Verify:** soak run scored by 5.1 shows no drift in latency or attribution.

### 5.9 The negotiation demo (the brief's launch criterion)

**Why:** "at least one live test case where an AI agent successfully negotiates on behalf of a user without speaker confusion" is the stated bar.

**Build:** a scripted scenario: Grant hires Ava to negotiate a price with Kyle. Ava's instructions: represent Grant, never accept an offer Kyle states as if Grant said it, confirm every commitment by name. Run it with the 5.1 harness and record the audio.

**Verify:** transcript shows every commitment attributed to the correct party; Ava's replies name the counterparty on each offer; zero replies during overlap.

## 6. Non-goals, still

Batch processing of recordings, text-only disambiguation, source separation, multi-language, automatic pattern-recognition agents. Unchanged from the original brief.

## 7. Working agreements for the next sessions

- Run the worker, API and tunnel in your own terminals so the MCP call log is visible.
- Never paste API keys into chat; set `OPENAI_API_KEY` in the shell.
- Stop the API before `mvn package` if it was started any way other than `dev.ps1`.
- Score every live run with 5.1 before changing thresholds or prompts.
- Keep `docs/TURN_TAKING.md` and `turn_gate.py` in sync; change both or neither.
