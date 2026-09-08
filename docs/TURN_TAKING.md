# Voice agent turn-taking in group conversations

Brief for the shared runtime (`scripts/agent_runtime.py`, provider adapters in `scripts/providers/`, gate in `scripts/turn_gate.py`). `realtime_openai.py` remains a single-agent compatibility entry point into the same guarded runtime and supplies the unchanged device/player helpers and instructions.

## 1. Problem restatement

Hosted voice APIs (OpenAI Realtime, Gemini Live, xAI speech-to-speech) are tuned for one person and one agent. Their voice-activity detection answers a simple question: did the human stop talking? In a room with three people that question has the wrong shape. The agent either answers every pause and talks over people, or it waits for an explicit prompt every time and the conversation loses its rhythm.

The missing piece is application-level: the agent needs a notion of *whose turn it is* and *whether it is being addressed*. That requires knowing who just spoke, whether two people are talking at once, and how confident that knowledge is. Voiceprint supplies exactly those signals, so the gate lives next to Voiceprint, not inside the provider.

## 2. Signal framework

Signals tiered by reliability, each mapped to what the middleware already emits.

**Hard signals (sufficient on their own)**

| Signal | Source | Rule |
|---|---|---|
| Direct address ("Ava, what do you think?") | `get_transcript` line text | Agent may respond once the speaker finishes |
| Push-to-talk / "you may speak" key | client keyboard | One response after idle transcription and a floor grant |
| Hold key | client keyboard | Suppress the next opportunity |

**Soft signals (need to co-occur)**

| Signal | Source | Rule |
|---|---|---|
| Silence after a finished utterance | no attributed chunk for ≥ 1.2 s (balanced) | Opportunity window opens |
| Last line is question-like | transcript text (`?`, wh-word) | Raises eagerness |
| Last speaker label `high` | utterance `label` | Agent knows whom to answer |
| No overlap in the last 3 s | overlap rows / `overlap: detected` | Room is not contested |

**Inhibitors (any one blocks)**

| Inhibitor | Source |
|---|---|
| A human turn is open | chunk `speaker_id` present or status `unknown` |
| Overlap in progress or within 3 s | chunk `overlap: detected`, overlap row |
| Last attribution `low`/`overlap`/`unknown` while addressed | utterance label → answer becomes a clarification ("was that Kyle?") |
| Agent spoke within cooldown (6 s balanced) and is not addressed | agent's own `response.done` |
| Agent's own voice echoed through the shared mic | enrolled agent voice id, ignored by the gate |

**Conversation stage** sets the default eagerness: `quiet` during introductions and enrollment (address-only), `balanced` during discussion, `eager` during decision rounds where the agent is expected to summarize or vote.

## 3. Demo strategy

Same three-person scenario, run twice, side by side.

- **Chaos run:** provider VAD with `create_response: true`. The agent answers every pause, including pauses inside other people's sentences, and answers into an overlap.
- **Gated run:** `create_response: false`, decisions from `turn_gate.decide`. The agent stays silent through the overlap, waits for the room to clear, answers when addressed by name, and asks "who said that?" when the last label is low.

Script (about 90 seconds): two humans discuss a plan; one asks the agent a direct question; both talk at once for a moment; one asks a rhetorical question not aimed at the agent; one addresses the agent while talking quietly from across the room (low label).

Scoreboard printed from `data/realtime-events.jsonl`:

- interruptions per minute (agent audio while a human chunk is attributed)
- replies that name the right person
- replies that started inside 3 s of an overlap row
- clarification asked when the label was low (yes/no)

## 4. UX patterns

Hybrid, in this order of precedence:

1. **Manual controls select an agent.** 1/2 selects Ava/Ben, Space requests one reply, H toggles sticky hold and cancels playback, C cancels the current reply, Q stops the room. `--agent NAME` selects the initial keyboard target. Manual speak still requires the floor, valid consent and idle transcription.
2. **Address-by-name is the default trigger.** Same mental model as Alexa, but the name is used naturally in a sentence rather than as a wake word, and it works because the gate can see the transcript.
3. **Context-aware defaults fill the gaps.** A clean, high-label question followed by silence is a fair opportunity in `balanced` mode; `quiet` mode disables this entirely.
4. **Uncertainty is spoken, not hidden.** When the gate returns `clarify`, the agent asks who spoke instead of guessing. This is the product promise of the confidence labels.
5. **Eagerness is a session setting**, not a per-turn command: `quiet | balanced | eager`.

## 5. Technical recommendation

Hybrid orchestration with the gate in the client, not the provider.

- The provider owns speech I/O, transcription of what the model itself heard, and MCP tool execution. Its VAD is used only to segment audio (`turn_detection.create_response: false`).
- The Voiceprint client owns the gate because it is the only component that sees speaker identity, overlap and silence together, in real time, without paying model tokens.
- The model reads the transcript on demand through `get_transcript(after_id)`. Cost per reply is one tool call and only the new lines.
- Server-side-only gates cannot see who is talking. Client-SDK-only gates cannot see the transcript cheaply. Putting the gate beside the speaker-ID service avoids both problems and is provider-agnostic: the same `/mcp` endpoint serves OpenAI Responses/Realtime and xAI directly; Gemini Live needs a thin function-calling bridge to the same endpoint.

Known limits: the 1.5 s attribution context means the gate learns who started speaking about a second late; that is fine for deciding *not* to speak, less fine for very short interjections. Labels are similarity-based until the calibration set exists.

## 6. Next steps

1. Run the two-run demo with two people plus the agent; keep the events file.
2. Compute the scoreboard; adjust `SILENCE_AFTER_TURN_S` and `AGENT_COOLDOWN_S` if the agent feels late or pushy.
3. Enroll the agent's own voice at startup so its echo is labeled and ignored (the `agent_speaker_id` path in the gate).
4. Add xAI (same endpoint) and a Gemini Live bridge once the OpenAI path is stable.
5. Move the gate into a small service beside the API when two providers share it.

## 7. Shared room runtime and evidence

The runtime captures one microphone and sends the same accepted chunks to each configured provider. It imports the original resampling and name-based device selection: Seiren input and `HD 4.40,BenQ` output preferences. While any output queue or configured hardware tail is busy, microphone chunks become zeros for both Voiceprint and provider inputs; the accepted-audio clock still advances. This remains half duplex: human speech during playback is lost. No silence, overlap, cooldown, similarity threshold or pre-reply nudge wording changed.

`scripts/agents.toml` starts Ava with `marin`/`balanced` and Ben with `cedar`/`quiet`. OpenAI Realtime supports both voices. `xai_speech` and `gemini_live` implement the adapter interface as explicit unavailable stubs. `--check-config` validates and prints safe settings without opening a device or contacting a provider.

All agents register without embeddings. The runtime polls stored utterances by increasing ID, de-duplicates them and delivers the same rows to each gate. Own agent output suppresses replying again to an older human question. Another agent's line only allows a reply when it names this agent; a human line naming another configured agent is also not a soft opening. Existing direct-address window and per-agent cooldown remain. Agent rows are published only after their transcript POST succeeds; failed storage stops the runtime.

The API floor is mandatory before any initial response, including manual speak. A 15-second lease renews after five seconds and remains held through tool-only responses, completed MCP calls, their continuation, final `response.done`, and actual queued playback plus mute tail. Renewal failure, expired lease or a changed holder cancels generation and aborts audio. Cancellation discards queued audio and continues muting through the hardware tail. A response cannot continue until its in-progress MCP calls finish. The runtime never starts a fresh reply while local transcription is outstanding.

Controls bind only `127.0.0.1:8090`, validate Host and exact local GUI origins, and use the API bearer token. GET `/agents` exposes the agreed state; POST controls enqueue the same operations as the keyboard. The tunnel remains solely the hosted MCP endpoint on 8082.

Disk logs are disabled; an explicit `--events` fails before collection until encrypted artifact registration and verified destruction exist. A bounded in-memory buffer holds allowlisted metadata, prints an aggregate score if the scorer is installed, and clears on shutdown or withdrawal. It reports evicted records and incomplete evidence if the 20,000-record bound is reached. No audio, human names, transcript text, tool arguments/results, session configuration, authorization or keys enter this buffer. Safe provider events preserve tool success/UTF-8 size and response item types. Actual playback start comes from the output callback; drained timing includes the conservative mute tail. The software cannot certify physical erasure of OS memory, swap or snapshots; those remain deployment safeguards.

## 8. Prior written consent is a launch gate

The current `docs/BIPA_V2.md` contract applies before every capture, WAV read, enrollment, transcription, stream dispatch and hosted send. The runtime first creates a pending room from each person's full typed name and contact, then waits with the microphone closed while each person personally signs in the local GUI. The API must have configured controller details and an operator token. Local consent alone cannot enable the provider: effective `openai_audio` and `hosted_mcp` scopes must both be true, incorporating vendor review flags. No API key or loopback address substitutes for a release.

After the written release, each eight-second enrollment begins with the specified spoken corroboration. The API links the exact PCM hash to the earlier written record. A shared ConsentGuard rechecks authoritative status before protected actions, rejects changed policy/roster, and latches failure on revocation, expiry or API outage. Capture callbacks stop once authorization freshness exceeds one second. Withdrawal flushes pending audio and ASR work and cancels providers. Session completion finishes pending permitted transcription before `/end` destroys the session purpose data. The legacy realtime command delegates into this runtime; unconsented legacy replay is blocked pending a provenance workflow. Automated tests use synthetic PCM and fake authority/provider services; a human live test remains required after genuine individual releases.

## 9. Participation policy, overrides and the spoken-leak guard (v3)

The live gate in `decide()` is unchanged in shape; v3 layers a lookup on top of it (`scripts/participation.py`, per `BUILD_SPEC_V3.md` §5). The default mode is a function of (conversation type × role): a negotiation's advocates are **raise-hand** (`GateState.raise_hand`: only a direct address or an override opens a turn; the soft "clean question followed by silence" opportunities are disabled), the arbitrator is **proactive** (it posts to the notes board on its own schedule and never touches the floor), and a casual room is **low-threshold** (today's behavior). Direct address still overrides the default.

An **override** is a new one-shot manual value on the gate (`manual = "override"`). It differs from the operator's Space key in one way: it is evaluated after the inhibitors (open human turn, overlap, overlap hold, hold key, another agent holding the floor), so a queue-skip never talks over a person, but it ignores the cooldown, the address window and eagerness. Only the runtime sets it, only for the advocate the arbitrator named, and only after a separate verifier call confirmed the arbitrator's `OBJECTIVE_ACHIEVED` or `REFOCUS_NEEDED` claim from the evidence lines; the arbitrator's own claim is never sufficient. The advocate's pre-reply note then carries the arbitrator's prompt; the earned nudge wording is otherwise untouched.

The arbitrator has its own tiered check, run every couple of seconds off the audio path (`scripts/arbitrator.py`): inhibitor (a 20-second cooldown after any generation), hard (an operator "post now"), soft (three or more new lines, a line carrying a number or offer word, or ninety seconds with anything new). Everything else is passive intake. `score_run.py` reports generations against ingested rows so the ratio can be checked after a run.

**Spoken-leak guard.** Advocates keep their realtime voice, so their words reach the room before the runtime sees them. The runtime mirrors the server's redaction guard over the streamed `response.output_audio_transcript.delta` text and, on a match with the advocate's own principal's constraint values, cancels the response and flushes playback (the existing cancel path, which also keeps the microphone muted through the hardware tail); the stored utterance text is redacted. This is best effort: **audio already rendered before the matching transcript delta arrives cannot be recalled.** The shared instruction layer that lists the values never to say is the first line of defense; this guard is the backstop; `docs/V3_STREAMS.md` records this as the largest open risk in v3.
