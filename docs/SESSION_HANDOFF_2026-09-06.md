# Voiceprint v2 implementation handoff — 2026-09-06

**Continue working: yes. This is a development checkpoint, not a completed v2 or a compliance-certified release.** The API, runtime, GUI and evaluation work exist in separate local worktrees. No implementation stream has been merged into `main`. No two-agent human live test has been performed on this build.

This document records the stopping point requested by Grant. It supersedes the older documents' statements that v2 has not started or that PR #1 still needs merging. Existing v1 live evidence remains historical evidence; it does not verify the new consent gates or two-agent runtime.

## Integration checkpoint (later on 2026-09-06)

Branch `v2-integration` merges `ws/api` (`fb10324`), `ws/gui` (`a0b10c0`), `ws/agent` (`b70618a`) and `ws/eval` (`2f7272f`, the scorer/fixture/EVENTS commit) onto `main` `9ecdabb`. File ownership was disjoint, so every merge was conflict-free. Two follow-up commits: the launcher credential change (`b80ed34`) and a one-request runtime fix (`56a687b`) that sends the local `Origin` when `prepare_room` creates a pending room; without it the merged API answered `403 origin_rejected` and no live run could start. No consent gate was weakened.

Evidence obtained on the integration tree, all with synthetic people and no provider, tunnel or microphone:

- Java 35, scripts 61 (27 runtime, 15 live/turn-gate, 19 scorer), worker 13, web 4 including the opt-in headless Chrome smoke; `agent_runtime.py --check-config` passes. Zero skipped.
- A loopback HTTP driver against the built jar plus the real worker on isolated ports (18080/18082/18091, scratch database) passed 58 checks with vendor review flags off and 59 with them on: public notice; room creation only from the exact local Origin; challenge/release negatives (wrong name, not accepted, tampered hash, tampered or replayed nonce, participant outside roster); one unsigned human blocks the room and enrollment; effective scopes stay false without every disclosure and both review flags; enrollment requires the matching `X-Voiceprint-Session`; live chunks, sequence gaps, human and agent utterances, agent registration, floor contention and release, ordered events; hosted MCP requires the bearer token, rejects any Origin, hides undisclosed rooms, returns lines for a fully disclosed room, and persists a proof row for both allowed and denied calls; revoke stops audio, reads and re-signing; local-only destruction verifies `sqlite_session_graph`, while hosted egress leaves `vendor_deletion_evidence` pending with `verified:false` and sanitized failure messages. Enrollment audio was the public SpeechBrain fixtures under `data/fixtures`, not participant recordings.
- The real GUI served from that API in a browser created a room, signed both releases through the challenge flow, showed the room active with the enrollment prompt, and after Withdraw showed `destroyed` with verified local destruction.
- One transient `ConnectionResetError` occurred during the first hosted-mode run and did not recur in two reruns; not diagnosed.

Still not done: no runtime-against-API run with a provider (needs Grant's key and real releases); no two-agent live session; dedicated Java negative/fault tests for the privacy boundary remain unwritten beyond the HTTP driver above; direct OpenAI audio possible-egress accounting is present as a pending vendor item on hosted rooms but has not been reviewed against the runtime's actual send path; the runtime control port 8090 default is currently occupied on Grant's machine by an unrelated Wondershare notifier bound to `0.0.0.0`, so pass `--control-port` or stop that process before a live run.

## Decisions to preserve

- Grant owns the product. Second agent: **Ben**, configured with `cedar`; Ava uses `marin`. One shared microphone, one runtime, separate agent state and a server-owned floor. The utterances table remains the conversation bus.
- GUI/API stay on **127.0.0.1:8080**. Runtime controls use loopback 8090, worker 8091, hosted HTTP MCP 8082. Kyle is physically present and sees Grant's screen.
- The existing Cloudflare tunnel exists solely so OpenAI's servers can reach MCP on 8082. It is not the runtime's outbound channel to OpenAI. Never tunnel 8080 or `/ui`. Grant stopped the API and does not want the tunnel restarted unless necessary. This documentation checkpoint did not restart services or the tunnel.
- Consent is now a prerequisite for all v2 audio collection, processing and disclosure, including enrollment and replay of human recordings. Do not work around blocked consent by running the old entry point.
- Controller name, address and contact email are required configuration, never invented: `VOICEPRINT_CONTROLLER_NAME`, `VOICEPRINT_CONTROLLER_ADDRESS`, `VOICEPRINT_CONTROLLER_EMAIL`.
- No individual login this week. Operator enters full name and an email/phone, explicitly unverified. Each participant personally reads and submits the written release on the shared screen. The API token identifies the operator, not the signer.
- Commit the written release before opening the mic. Then record the eight-second enrollment statement beginning with the specified consent sentence. The server links the SHA-256 of exact enrollment PCM to the existing consent row. Spoken consent is corroboration, never a substitute for prior written release.
- Hosted audio and hosted MCP require each person's disclosure scopes and account-specific vendor review. Review flags are attestations, not proof that vendor retention is disabled.
- Preserve the earned reply nudge, continuation after tools, idle-transcription wait and half-duplex mute. Score before tuning. Similarity labels are not calibrated probabilities.

## What is saved, and where

The authoritative contract is [API.md](API.md), amended by [BIPA_V2.md](BIPA_V2.md). The latter contains the data map, consent-gate Mermaid diagram, database design, enforcement pseudocode, destruction-worker specification and vendor checklist requested by Grant.

`main` was at `7481829` before this documentation checkpoint. Contract commits, in order:

- `ae5afc3`: v2 participants, floor, events and local controls.
- `82a745a`: conversation paging follows stored utterance ID order.
- `0f92cfd`: prior-consent and lifecycle requirements.
- `bd541a9`: configured controller, participant release and enrollment linkage.
- `7481829`: effective consent scopes and destruction progress.

### A — API and storage

Branch `ws/api`; worktree `C:\Users\Grant\git\Voiceprint\data\streams\api`.

Commit `5209c82` contains v4 participants, agent utterances, persistent floor, ordered events and MCP proof, and loopback static serving. Its reported verification was **35 Java, 15 script and 10 worker tests passing** before privacy edits.

V5 privacy work was subsequently saved as **`fb10324`**, explicitly marked WIP, with a clean API worktree. It includes `PrivacyGate.java`, `PrivacyPolicy.java`, changes across store/service/REST/MCP/engine/startup, synthetic consent test fixtures, worker authorization and `worker/test_privacy.py`. It adds privacy rooms/releases/challenges, policy configuration, enrollment hashes, pre-body and service authorization, hosted disclosure checks and persistent destruction jobs with a sweeper.

**V5 is not merge-ready.** Final checkpoint verification reported **35 Java, 15 script and 13 worker tests passing, zero skipped**. Three new worker tests cover synthetic HTTP authentication/admission. Existing Java fixtures now obtain synthetic releases through the consent flow. The current worktree jar was packaged from the checkpoint source. Dedicated Java negative-path and fault tests remain unwritten; a green existing suite is insufficient evidence for the new privacy boundary.

**Direct OpenAI audio accounting remains an implementation gap:** current vendor tracking covers MCP, but the API cannot directly observe the runtime's outbound audio. Before merge, implement conservative tracking of a usable hosted-audio permit as a *possible vendor copy*, even if no actual send is observed. Such an item must not be presented as proof of transmission or verified deletion. Review destruction configuration and failure alerting as well.

### B — Shared runtime

Branch `ws/agent`; worktree `C:\Users\Grant\git\Voiceprint\data\streams\agent`; commit **`b70618a`**. Clean at inspection, based on the initial contract commit; it still needs rebase onto final API/contracts.

Contains `agent_runtime.py`, `agents.toml`, provider interface/OpenAI adapter, floor-controlled turns, local controls, per-agent state, consent checks in the client/capture/transcription/dispatch paths, and guarded legacy entry points. Turn-taking documentation and implementation were changed together. Runtime waits with the mic closed while the GUI collects releases.

Reported verification: **42 script tests** including 27 runtime tests, **10 worker tests**, and the no-key `--check-config` command. Tests use synthetic inputs and fake authority/provider responses. The commit explicitly says Java and rebased integration checks remain outstanding. The agent stopped after committing because its usage limit was reached.

Live event metadata is bounded in memory (20,000 events, eviction count); explicit disk `--events` is rejected. The runtime calls the evaluation stream's `summarize_events(rows)` hook. This cross-stream hook still needs integration verification. Eviction or missing playback evidence must make affected scoring incomplete, never imply zero violations.

Known limit: an already executing local faster-whisper inference cannot be interrupted cooperatively. Withdrawal must block its result persistence and clear queued work. Inspect and test this behavior across the API/runtime boundary.

### C — GUI

Branch `ws/gui`; worktree `C:\Users\Grant\git\Voiceprint\data\streams\gui`; commit **`a0b10c0`**. Clean at inspection and rebased onto `7481829`.

Only `web/` changed. Plain HTML/JS provides pending-room setup, controller notice, individual written release, optional disclosure scopes, withdrawal/destruction progress, transcript, roster, similarity labels, floor, Ava/Ben controls and prominent server MCP proof. It does not open a browser microphone or persist the API token.

Reported verification: **4 GUI tests**, including actual headless Chrome with synthetic release/replay, four transcript rows, two MCP rows, deduplication and text safety. Java 20/script 15/worker 10 baseline checks also passed on this branch. These are not tests of the new API privacy implementation. Real API/runtime integration and human use remain unverified.

### D — Evaluation

Branch `ws/eval`; worktree `C:\Users\Grant\git\Voiceprint\data\streams\eval`; base `ae5afc3`. The orchestrator took over after the evaluation agent stopped. Work is **uncommitted/untracked** at this checkpoint.

Files intended for review and eventual commit:

- `scripts/score_run.py` and `scripts/test_score_run.py`.
- `evaluation/make_synthetic_fixture.py` and `evaluation/synthetic_regression_runs.jsonl`.
- `docs/EVENTS.md`.

The scorer supports legacy runs, parallel-agent envelopes, fresh tool-before-audio ordering, numeric token usage, measured playback overlap, missing-evidence reporting, an allowlisted aggregate and the in-memory `summarize_events(rows)` entry point. **19 scorer tests passed during this documentation checkpoint.** Broader integration checks remain outstanding.

Do **not** commit `evaluation/legacy_last_three.jsonl`, `evaluation/make_legacy_fixture.py` or `evaluation/_inspect_log.py`: these are local working material derived from historical evidence or helpers for it. Preserve them for explicit legacy-data disposition. The replacement regression fixture is constructed, synthetic, and is not independent proof of live accuracy.

Historical log reconciliation found seven provider attempts, six configured sessions. For the last three attempts, reply/tool-call counts were 2/2, 6/4 and 5/5; strict fresh-result-before-audio counts were **2/2, 4/6 and 4/5**. Token totals including tool-only responses were **4,261; 17,298; 14,149**. The final run had a preamble before its fresh tool result, so equal call/reply counts alone do not establish correct ordering. There is no independent full three-run attribution scorecard or sufficient playback evidence to claim zero floor violations.

### Local launcher

`scripts/dev.ps1` on `main` has an **uncommitted** change provisioning separate operator API and worker credentials in ignored local data, restricted to the current Windows user and SYSTEM. Only the explicit `api-token` mode prints the operator credential; worker credentials are not printed. Tunnel behavior is unchanged.

Verified earlier in this session: PowerShell syntax, generation of a 64-character token, reuse on a second invocation and protected file ACLs using an isolated launcher copy. It has not yet been tested with the merged privacy API/worker. Same-user processes remain trusted; these ACLs do not create production service isolation.

## What remains before a v2 live run

Follow [V2_RESUME_CHECKLIST.md](V2_RESUME_CHECKLIST.md) in order. First finish and verify A's privacy enforcement, then merge A, rebase B/C/D, and test their combined behavior with synthetic inputs. No stream was merged while writing this checkpoint. Do not merge unverified code merely to obtain a single runnable tree.

After integration, Grant must supply the controller configuration, review the real notice/vendor settings, and obtain each person's real written release. Start services in Grant's visible terminals using the launcher; preserve the existing tunnel where viable. Then run a short Grant/Kyle/Ava/Ben session, inspect server MCP proof, score it and adjust from evidence. There has been no such live validation yet.

## Compliance and operational work still open

This implementation cannot establish absolute BIPA compliance. The legal entity, final notice/signature sufficiency, public retention policy, vendor contracts and account-specific retention settings need actual review. OpenAI's no-training default does not mean no retention. No executed vendor DPA or approved zero-retention account configuration was inspected.

SQLite record deletion/checkpoint/compaction does not prove erasure from SSD history, OS snapshots, swap, old exports or backups. Per-session encryption/key destruction and a verified artifact inventory are not completed. There is no S3 integration to certify. Vendor-copy destruction must remain pending without adequate evidence. These are material limitations, not a green test result.

Legacy data includes the existing SQLite database and sidecars, historical JSONL, human WAV fixtures, stream working copies, and `data/backups/voiceprint-pre-v4-20260906-023259.sqlite`. That backup was created and integrity-checked before migrations; it is also a retained biometric copy requiring disposition. No retrospective consent was fabricated and these artifacts were not silently destroyed.

Later v2 roadmap items remain: calibrated labels with an independent truth set, verified individual identity/accounts, stronger storage isolation/encryption, verified cross-destination deletion, additional provider adapters, agent voice enrollment and deferred utterance-correction UI. Keep these separate from the immediate merge-and-live-test milestone.

## Workspace preservation and environment cautions

- Preserve Grant's unrelated working changes: deleted `docs/HANDOFF_PROMPT.md`, untracked `docs/2101.09624v4.pdf`, and untracked `.codex/`. This handoff deliberately uses a new filename.
- Worktrees contain the implementation. Do not delete `data/streams`, run broad cleanup, reset branches or stage all untracked files.
- Bare Java on PATH is Java 8. Use `scripts\dev.ps1 build`; the prepared JDK 21/Maven dependencies are local. Do not package over a jar used by a manually started Java process. Launcher-started API uses a jar copy.
- No provider key is available to the agent. Keep provider keys in Grant's shell and never echo or write them to files. If a key is pasted again, remind Grant once to rotate it.
- `data/inspect_v2_environment.py` is an obsolete scratch probe that attempts authenticated access to an old public tunnel address. Do not rerun it. Automatic approval review previously rejected that credential-bearing public probe; a later unauthenticated 502 while the service was stopped did not establish that a tunnel restart was required.
- `data/verify_v2_integration.py` predates the consent amendment and uses real replay fixtures. It is not a valid privacy integration test as written.
- Audio preference: Seiren X input, output `HD 4.40,BenQ`; BenQ can disappear when the monitor sleeps. Check enrollment peak levels before blaming attribution when Kyle receives low labels.

Commits retain the agreed stream prefix, verification body and `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` trailer. The trailer follows Grant's requested convention; test claims must still identify the evidence actually obtained.

## Update 2026-09-07: launcher and the first two-agent live run

Commits `da8513e`..`6170333` on `v2-integration` add `Voiceprint.cmd` / `scripts\dev.ps1 up` (one window: worker, API, cloudflared, `agent_runtime.py --gui`, browser) and move every former console step into `/ui`: roster and provider key, per-person enrollment recording, Start, End. Occupied ports are refused unless `--replace-services` or `--reuse-services`; launcher children die with the launcher (Windows job object); the public MCP URL is verified with `tools/list` before the runtime starts; hosted rooms are refused before signing unless both vendor review flags are set.

Live evidence, reported by Grant from the console page on 2026-09-07 (session about four minutes): two humans (Grant and a second voice source) and two agents (Ava, Ben); both written releases with both disclosures; enrollment peaks 5622 and 5525; 11 utterances; **5 server-recorded `get_transcript` calls** attributed to both agent participant IDs with completed status; Ava and Ben each answered a "who said" question with the correct human name. Human labels were mostly `medium` (similarity 0.46-0.56). An earlier run the same night recorded zero server calls while the agent narrated fetching; it had reused a stale API from a previous launcher run, and the cause was not isolated before the fix that refuses stale services. This is one observed run, not calibration or compliance evidence; the limitations above still apply.

