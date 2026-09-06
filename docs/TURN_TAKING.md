# Voice agent turn-taking in group conversations

Brief for the Voiceprint agent client (`scripts/realtime_openai.py`, gate in `scripts/turn_gate.py`).

## 1. Problem restatement

Hosted voice APIs (OpenAI Realtime, Gemini Live, xAI speech-to-speech) are tuned for one person and one agent. Their voice-activity detection answers a simple question: did the human stop talking? In a room with three people that question has the wrong shape. The agent either answers every pause and talks over people, or it waits for an explicit prompt every time and the conversation loses its rhythm.

The missing piece is application-level: the agent needs a notion of *whose turn it is* and *whether it is being addressed*. That requires knowing who just spoke, whether two people are talking at once, and how confident that knowledge is. Voiceprint supplies exactly those signals, so the gate lives next to Voiceprint, not inside the provider.

## 2. Signal framework

Signals tiered by reliability, each mapped to what the middleware already emits.

**Hard signals (sufficient on their own)**

| Signal | Source | Rule |
|---|---|---|
| Direct address ("Ava, what do you think?") | `get_transcript` line text | Agent may respond once the speaker finishes |
| Push-to-talk / "you may speak" key | client keyboard | Immediate `response.create` |
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

1. **Manual override always wins.** Space = speak now, H = hold. Cheap insurance for demos and for people who dislike surprises.
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
