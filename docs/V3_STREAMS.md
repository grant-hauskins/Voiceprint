# V3 build streams: agent-mediated arbitration with leak prevention

Build plan for `docs/BUILD_SPEC_V3.md`, grounded by the 2026-09-07 grounding version of `docs/HANDOFF_PROMPT.md` (its §C conflicts, §E questions, §G pseudocode and §K traceability matrix). That version lived uncommitted in the main checkout while this was built and is being rewritten by another session, so the committed `HANDOFF_PROMPT.md` may not carry the section letters cited here; the substance those sections carried is restated in this file and in the V3 section of `docs/API.md`.

**Status after the 2026-09-08 build (branch `worktree-v3-arbitration`):** everything below is built and unit-tested (Java 49, scripts 127, worker 13, web 4). Not done: the manual live smoke (two people, provider key, tunnel) and Grant's answers to the five questions, each of which has a reversible default in place. Contract: the "V3 arbitration contract" section of `docs/API.md`. Conventions carry over from `docs/V2_STREAMS.md`: contract first, one concern per commit, commit prefixes `[api] [agent] [gui] [eval] [contract] [docs]`, every stream green before merge, no invented probabilities, labels stay `similarity_based_uncalibrated`.

## Decisions made in Grant's absence (reversible; flagged for review)

`HANDOFF_PROMPT.md` §E lists five questions to ask Grant. The build ran unattended, so each got a default. Reverse any of them and the affected code is small and named.

| # | Question | Default taken | Where to reverse |
|---|---|---|---|
| 1 | Demo with real stakes despite uncalibrated labels? | No change to labels. README and the notes board say plainly that attribution is uncalibrated and the build is a demo, not for consequential negotiations (`CALIBRATION.md` still governs). | Documentation only. |
| 2 | Summary to disk vs protected store? | **Database record** (`summaries` table, 30-day retention, own deadline, swept, deletable) written before `/end`; that write is what "End conversation" blocks on. No file by default. Opt-in `--summary-file PATH` writes the same text after the record is confirmed, as the operator's explicit choice. | `scripts/agent_runtime.py` summarizer step; `SUMMARY_RETENTION_MS` in `PrivacyPolicy.java`. |
| 3 | Advocate voice leak: accepted residual risk or text-first rework? | **Advocates stay realtime voice (spec §1 Decided).** Mitigations: shared instruction layer that lists the values never to say; runtime guard on streamed `response.output_audio_transcript.delta` that cancels the response and flushes playback on a match; stored agent utterance text is redacted. **Residual risk: audio already rendered before the matching transcript delta arrives cannot be recalled.** This is the largest open risk in v3 and is the first thing to show Grant. | `Agent.event` in `scripts/agent_runtime.py`. |
| 4 | New consent purpose/scope for objectives, arbitrator calls, summary? | One new disclosure scope `negotiation_text` (same purpose `live_conversation_v1`), one notice sentence, effective only with every human's release plus OpenAI review. Changing the notice text invalidates every previously signed room (they must be re-signed). | `PrivacyPolicy.java`, `PrivacyGate.java`, `web/app.js` release form. |
| 5 | Second human pair available? | Unknown. No live run happened in this build; every "works" claim below points at a test. | §J of `HANDOFF_PROMPT.md` lists the manual smoke still owed. |

Safe-to-default items (§E) were decided as: tool names `get_agent_channel`/`post_agent_channel`; normalized numeric + whole-phrase text matching with in-place `[withheld]` redaction; uploaded objective files are parsed client-side and discarded (only fields are posted); override claims are verified by a second Responses call with the evidence lines.

Routing (§3.1 open): advocate-to-advocate messages are not direct. Advocates speak aloud (transcript) and may post short private notes to the raw tier; the arbitrator reads both and prompts an advocate through that advocate's pre-reply note. The runtime delivers arbitrator prompts to an advocate by queueing an `override` speak request (floor, idle transcription and the overlap/human-turn inhibitors still apply).

## Stream ownership

| Stream | Owns | Builds |
|---|---|---|
| `api` (Java, `src/`) | schema v6, REST, MCP tools, privacy scope, redaction guard | `objectives`, `agent_channel` (+reveal gate), `summaries`, `negotiation_text` scope + notice, `Redaction.java`, `get_agent_channel`/`post_agent_channel`, purge/verify additions, `RedactionTest` + `V3ContractTest` |
| `agent` (Python, `scripts/` except `score_run.py`/`test_score_run.py`) | runtime, providers, gate | `objectives.py` (records + guard mirror), `participation.py` (policy table + override), `arbitrator.py` (text-only sibling of `Agent`, MCP reads, tiered trigger, verifier), `providers/openai_responses.py`, persona/shared instruction layer, voice-leak cut, objective polling + `session.update`, summarizer sequencing, control/state additions, tests |
| `gui` (`web/`) | console | role select + conversation type in setup, `negotiation_text` checkbox, objectives entry per person (typed or file), notes board panel, raw-stream reveal (server flag), arbitrator card, summary view after end, `test_dom.js` updates |
| `eval` (`scripts/score_run.py`, `scripts/test_score_run.py`, `evaluation/`, `docs/EVENTS.md`) | scoring | arbitrator generation ratio, guard hits, override verification counts in `score()`/`aggregate()`/renderers; fixtures |

Shared: `evaluation/redaction_cases.json` (written with the contract; both guard implementations must pass every case). `docs/API.md` V3 section is the contract; change it only with a `[contract]` commit.

## Definition of done (unchanged commands)

```powershell
scripts\dev.ps1 build
.venv\Scripts\python.exe -m unittest discover -s scripts -p "test_*.py"
.venv\Scripts\python.exe -m unittest discover -s worker -p "test_*.py"
.venv\Scripts\python.exe -m unittest discover -s web -p "test_*.py"
.venv\Scripts\python.exe scripts\agent_runtime.py --check-config
```

Baseline before v3: Java 35, scripts 84, worker 13, web 4. Every row of `HANDOFF_PROMPT.md` §K gets a named test. The manual smoke (§J) still requires two humans, a provider key and a tunnel, and has not been run.
