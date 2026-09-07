# Voiceprint v3 scope: agents that represent someone

Design and scope notes from a product-strategy session on 2026-09-07, written for whoever builds this next — human or agent. **Nothing in this document is built or tested.** It is a decision record and an architecture proposal, not evidence. Keep the project's honesty rule: nothing here is a working feature until it has a test or a live run behind it, the same standard [BUILD_SPEC_V2.md](BUILD_SPEC_V2.md) §3 holds itself to.

Where this doc says "decided," that's Grant's call from the session, recorded so it doesn't need re-litigating. Where it says "open," it's a real gap — pick a reasonable answer, note what you chose and why, and move on rather than blocking on it. The goal is clear enough to build from, not so prescriptive that it removes your judgment.

## 0. Why this version, not more v2 hardening

Voiceprint is a solo technical proof of concept with no external user or target market yet. Nothing technical blocks putting it in front of demo users today. The v2 compliance/calibration gaps (uncalibrated labels, notice sufficiency, vendor DPAs — [CALIBRATION.md](CALIBRATION.md), [BIPA_V2.md](BIPA_V2.md)) stay open and tracked separately; they are not a prerequisite for this scope. What's missing for a demo to be *meaningful* isn't more hardening — it's giving the agents something to do besides transcribe: represent a person, pursue their objective, and deal with another agent doing the same for someone else.

Ranked priority from the scoping session, highest value first:

1. **Uniform system instructions + per-conversation objectives** — a shared instruction layer every agent gets, supplementing each agent's existing per-user custom instructions (`data/agents`), plus a structured, private objective per represented person.
2. **Agent-to-agent arbitration channel** — agents negotiate or mediate without leaking the numbers their principal doesn't want disclosed.
3. **Participation policy** — when an agent waits its turn vs. speaks up unprompted, generalized beyond today's turn gate.
4. **Conversation summarizer** — end-of-session agreements / open items / next steps. Surfaced mid-session as a natural companion to #2; not separately ranked.
5. *(Deferred)* Accuracy auditing and live-steering tooling. Real, and it was the first friction point named — but it doesn't unlock a new demo scenario by itself, so it's explicitly out of this version. Leave it on the backlog.

Explicit non-goal for this version: more than two represented principals in one arbitration. The base app already seats 2-4 people; generalizing advocate/arbitrator roles to N-way negotiation is a real design problem, not a corner case — deferred (§8).

## 1. Architecture: three agents, not two

The natural read of "agents negotiate for their principals" is two advocate agents talking to each other. That's the wrong shape for arbitration/mediation: two adversarial agents can only find a deal within what they're each willing to say aloud, which is exactly the information neither wants to give up. Real mediators work because they're the one party who knows both bottom lines.

**Decided:** a third, neutral role sits between the two advocates.

| Role | Knows | Job | Never does |
|---|---|---|---|
| **Advocate A** | User A's full private objective only | Speaks for A, to A's other party and to the Arbitrator | See User B's private objective |
| **Advocate B** | User B's full private objective only | Symmetric to A | See User A's private objective |
| **Arbitrator** | Both A's and B's private objectives, pulled from the structured record (§2), not from what the advocates choose to say | Judges whether a zone of agreement exists; writes the public notes board (§3); may prompt either advocate | Negotiate on behalf of either side; state either party's private figures to the other party |

This is the load-bearing decision for the whole feature set: it turns the leak-prevention problem from "trust two adversarial agents not to slip" into "one enforcement point on one neutral node." Everything in §3-4 assumes this topology.

**Decided:** the Arbitrator is always a text-only model — a plain Responses-style API call, never a live voice/realtime session. It has no independent voice presence in the room. It reads the human transcript through the same MCP tools (`get_transcript`, etc.) the Advocates already use, not a special access path — the Arbitrator is a distinguished consumer of the same tool surface, not a differently-privileged one. Advocates keep their existing voice presence (they're the ones actually speaking in the room, per §5); the Arbitrator's output is always text (notes board, prompts to an advocate, the closing summary in §6).

## 2. Objectives: structured, private, versioned

**Decided:** an objective is a structured record, not free text pasted into a prompt — because the redaction guard (§4) needs to check agent output against *known values*, and a value can only be checked if it was captured as data, not buried in prose.

Minimum shape (extend as needed, don't over-fit this now):

- `principal` — which participant this belongs to.
- `position` — the stated, shareable goal ("wants to sell the property").
- `constraints` — the non-disclosable figures/terms (ceiling, floor, must-haves, deal-breakers). This is the set the redaction guard watches.
- `source` — typed directly, or extracted from an uploaded document (a term sheet, an offer letter). **Decided:** file upload is a supported input path; the file is parsed into the structured fields above rather than handed to any agent verbatim.
- `history` — every edit is a new version, linked to what triggered it (a conversation event, a stated position change) — this is how "objectives can evolve mid-conversation" stays auditable rather than silently overwriting the record.

**Open:** what happens to the raw uploaded file after extraction — retained for re-parsing, or discarded once fields are captured? The project's existing posture (destroy session data on purpose completion, [BIPA_V2.md](BIPA_V2.md)) argues for discarding unless there's a concrete reason to keep it. Pick one and say so in the implementation.

**Open, and flag it, don't skip it:** a person's negotiation constraints (a reservation price, a walkaway term) are a new class of sensitive data this product didn't previously collect. The existing consent/notice flow governs audio and voice biometrics; it doesn't yet say anything about textual negotiation data. Before this ships to anyone outside Grant, the notice needs to cover it — same pattern as the rest of BIPA_V2, just a new data type to add to the map.

## 3. The channel: two tiers of visibility

**Decided:** don't make everything either fully hidden or fully visible. Two tiers, different trust models:

- **Public notes board** — the Arbitrator's curated output: agreed points, open points, suggested compromise ranges. Never contains either party's private constraint values by construction (it's written by the one role bound not to disclose them). **Default visible** to both users — it's safe by design, not by discipline.
- **Raw agent-to-agent stream** — whatever the advocates and Arbitrator actually say to each other, if that's exposed at all. **Default hidden.** A toggle reveals it, but only takes effect once *both* users consent — same mutual-consent shape as the rest of the app's release model.

### 3.1 Transport: an MCP tool, not a new protocol

**Decided:** build the agent-to-agent channel as a new tool on the same MCP server, the same way the human transcript is exposed — not Google's A2A protocol. A2A solves a problem this system doesn't have: independently-hosted agents, built by different parties, discovering and calling each other at runtime. Every agent here — both Advocates and the Arbitrator — already runs inside Voiceprint's own runtime behind the same auth boundary, so there's no discovery or cross-vendor handshake to do. Standing up a second protocol stack next to the MCP-everything architecture would duplicate the auth and proof-logging work `get_transcript` already has, for no interoperability benefit yet.

Build it the same shape as `get_transcript`: a new tool (e.g. `get_agent_channel(after_id)`) that returns new agent-to-agent lines since a cursor. Agents poll it on a loop exactly the way Advocates already poll `get_transcript` ([TURN_TAKING.md](TURN_TAKING.md) §5) — one tool call, only the new lines, no new transport paradigm to learn. Redaction (§4) happens before a line is ever stored or served through this tool, so there's no separate "redact on read" step to keep in sync.

**Open:** the exact tool name/schema, and whether Advocate-to-Advocate messages route directly or always pass through the Arbitrator — §1 already puts the Arbitrator between them for the negotiation logic; whether that also holds at the transport level is an implementation detail, not a new decision to make from scratch.

**Reconsider later, not now:** A2A becomes worth it the moment a real third-party or externally-hosted agent needs to join a room — a genuine interoperability problem. Nothing in this version's scope (§8) creates that case yet.

## 4. The redaction guard

Do not rely on instructions alone to keep an advocate or the Arbitrator from stating a value it's holding. A single leaked number in a visible channel is irreversible the instant it renders, and "don't reveal your principal's floor" is one adversarial phrasing away from failing under pressure from the other agent.

**Decided:** a server-side check runs on anything the Arbitrator emits (and on the raw stream, if/when visible) *before* it's committed or rendered, comparing the text against that sender's own registered `constraints` values (§2) and blocking or redacting a literal match. The agent still reasons over the real number internally — it has to, to evaluate offers — the guard only stops the number from crossing into a channel a human or the other party's agent can read.

**Open:** exact match, fuzzy/normalized match (commas, currency symbols, spelled-out numbers), or a lightweight classifier — pick the cheapest thing that catches the real cases and say what it misses.

**Open:** behavior on a catch — block and force a rephrase, or deliver with the value redacted (`"[amount withheld]"`)? Either is defensible; pick one and be consistent.

## 5. Participation policy

This extends the existing floor gate ([TURN_TAKING.md](TURN_TAKING.md), `scripts/turn_gate.py`) — it does not replace it. The live turn gate stays latency-critical (it's deciding whether to speak *now*, mid-conversation, and that constraint doesn't change). Everything below governs whether a request reaches that gate as a normal turn or as an override; the deciding step itself does not need to be real-time — it's fine for it to run a beat behind the live audio, since the humans' conversation isn't blocked on it.

**Decided:** default participation mode is a lookup, not a single global flag:

| Conversation type | Role | Default mode |
|---|---|---|
| Negotiation | Advocate | Raise-hand — wait for the floor |
| Arbitration | Arbitrator | May post to the notes board / trigger an announcement proactively |
| Casual/general | Any | Low-threshold, can speak more freely |

Direct address always overrides the default (existing rule, unchanged). The Arbitrator has no voice (§1), so "interject" for it never means taking the floor itself — it means writing to the notes board, or, for something that needs to be heard, prompting the relevant Advocate to say it.

**Decided:** separately, any agent's speak-request carries a self-declared category tag, most are ordinary and just enter the table above. One category is confirmed and should exist from the start: **`OBJECTIVE_ACHIEVED`** — a deal or conversation objective has been reached — which should let an agent (most naturally the Arbitrator) skip the queue and announce a summary of what was agreed. Do not build a whole taxonomy around a single illustrative example; add categories as real cases come up rather than speculatively.

**Decided:** the agent making the speak-request should not be the sole judge of whether its own override claim is valid — same self-policing risk as §4. A separate, lightweight check verifies an override claim before it's allowed to skip the queue. Because this check isn't on the live-audio critical path (see above), it can afford to be a real second pass rather than a keyword heuristic, if that's what gets it right.

## 6. Conversation summarizer

Natural extension of §1: an Arbitrator that's already tracking agreement/disagreement can produce a closing artifact — agreements reached, open items, recommended next steps — instead of that state disappearing when the room closes. Feed it the public notes board plus the transcript; the pattern (bounded input, structured output) is the same shape as the existing `scripts/score_run.py` evaluation harness, which is worth reading before designing this one.

**Decided:** the trigger is ending the conversation in the app (the existing "End conversation" action) — not a separate button. On that trigger, the app prompts the Arbitrator once for the summary/next-steps, using a plain text model call (a Responses-style API request), not the live realtime voice service — this is a one-shot text generation, not a spoken turn, so it doesn't need the voice session, the floor gate, or the turn-taking apparatus at all.

**Decided:** the app must save that response to a file, and it must do so *before the app finishes closing* — i.e., "End conversation" blocks on the summary call and its write completing, not a best-effort background task that can be dropped on shutdown. This is the same shape as the existing destruction/withdrawal sequencing ([TURN_TAKING.md](TURN_TAKING.md) §7, [BIPA_V2.md](BIPA_V2.md)): don't let the process exit while a required write is still in flight.

**Open, flag it like §2's:** a saved summary can itself contain sensitive negotiation content (even if it's careful not to restate a private ceiling/floor by name, "agreed to sell in the low range" is still meaningful). It needs the same retention/consent treatment as the rest of session data — decide where it's written, who can read it, and whether it's subject to the same destruction-on-withdrawal rule as everything else, rather than assuming it's exempt because it's "just a summary."

## 7. What "done" looks like for this version

A demo where: two people each brief their own agent privately (typed or uploaded objective); a neutral Arbitrator agent, seeded with both private objectives, identifies whether a deal is possible and posts agreed/open items to a visible notes board without ever stating either side's private number; the room's participation follows the role/type table above with at least the `OBJECTIVE_ACHIEVED` override wired up; and the session ends with a written summary. That is one coherent story, not four disconnected features — resist building any one piece to a polish level the others haven't reached.

## 8. Explicitly out of scope for this version

- N-way arbitration (more than two represented principals). The current model assumes exactly one Arbitrator mediating exactly two Advocates; extending this needs its own design pass (does every pair get its own Arbitrator? one Arbitrator holding every objective at once?).
- Accuracy calibration and live-steering/audit tooling (ranked #5 above). Still real, still wanted next — just not this version.
- Additional provider adapters (xAI, Gemini) — unrelated axis, unchanged from v2's backlog.
