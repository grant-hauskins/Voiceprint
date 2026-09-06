# Resume v2 from the September 6 checkpoint

Read [SESSION_HANDOFF_2026-09-06.md](SESSION_HANDOFF_2026-09-06.md) first for branch locations and limitations, then [API.md](API.md), [BIPA_V2.md](BIPA_V2.md), [V2_STREAMS.md](V2_STREAMS.md), and the runtime/client code. Do not redesign the runtime without reading the timing fixes it preserves.

## 1. Recover the exact state

- Inspect status/logs in `main` and all four `data/streams/*` worktrees. Preserve A's WIP commit `fb10324`, uncommitted D and launcher work, plus Grant's unrelated files.
- Read the API checkpoint in the handoff. Its existing suite is green, but dedicated privacy negative/fault tests remain missing.
- Check local service health without recording audio. Grant stopped the API; leave the existing tunnel alone. Never send credentials to a hostname inferred only from an old log.

## 2. Finish API privacy enforcement and tests

- Add meaningful negative tests for unsigned/missing/revoked/stale/wrong-room releases, one unsigned room member, nonce replay/tampering/expiry, roster mismatch, missing controller configuration, pre-body denial and service-level bypass attempts.
- Test worker auth with missing/wrong/service credentials and reject non-loopback worker destinations. Use synthetic inputs; no human replay before proven permission.
- Test effective scopes independently: local authorization must not imply OpenAI audio or hosted MCP authorization. Exercise every hosted tool and both MCP transports.
- Implement and test conservative possible-egress accounting for direct OpenAI audio; the WIP checkpoint tracks MCP only. A vendor copy must not escape destruction accounting merely because the provider never called MCP.
- Test enrollment PCM hash linkage only after an existing written release and successful enrollment.
- Verify numbered migrations, including legacy upgrade paths, without creating consent for old sessions. Test failure/retry and atomic migration behavior.
- Test withdrawal and purpose completion during queued/concurrent processing, deadline recovery after restart, whole-record-graph deletion, WAL/checkpoint outcomes, and failure retries. Assert that pending vendor evidence prevents a verified-destruction claim.
- Run Java, script and worker suites on the final API branch. Commit only its owned changes with exact verification and remaining limitations. Merge **A first** when green.

## 3. Integrate the remaining streams

- Rebase B, C and D onto merged A, keeping directory ownership. Resolve contract differences explicitly; do not weaken the gate to satisfy old tests.
- In D, commit only the synthetic fixture/generator, scorer/tests and event documentation. Keep historical derived working files out of commits.
- Review and commit the separate launcher credential change. Verify API, worker, MCP and client agree about distinct credentials without logging them.
- Verify the runtime's in-memory score hook with D, metadata eviction handling and incomplete timing. Disk event logging stays disabled until artifact tracking and protected storage are implemented.
- Exercise GUI room creation, exact notice/hash/nonce signing, effective scopes, controls and withdrawal against the real local API with synthetic data. Verify the GUI clears cached protected content and accurately displays pending destruction.
- Test server-side MCP proof alongside provider metadata. The server log is the independent evidence; provider output alone cannot prove our endpoint was called.
- Run all applicable suites after rebase and before each merge. Commit bodies must separate isolated tests from combined integration evidence.

Representative test commands, executed in the relevant worktree using its prepared dependencies:

```powershell
.\scripts\dev.ps1 build
.\.venv\Scripts\python.exe -m unittest discover -s scripts -p "test_*.py"
.\.venv\Scripts\python.exe -m unittest discover -s worker -p "test_*.py"
.\.venv\Scripts\python.exe -m unittest discover -s web -p "test_*.py"
.\.venv\Scripts\python.exe scripts\agent_runtime.py --check-config
```

The GUI's actual-browser test is opt-in; read `web/README.md` and enable `VOICEPRINT_BROWSER_SMOKE=1` with its documented browser dependency. Record skipped tests explicitly. Scorer-only validation uses `-s scripts -p "test_score_run.py"`. None of these commands authorizes replaying real voices.

## 4. Prepare the controlled live test

- Grant supplies controller name/address/contact and the approved policy. Publish the retention policy through an appropriate public channel separately; loopback `/ui` is not a public policy publication.
- Obtain and verify account-specific vendor terms/settings before setting the OpenAI/Cloudflare review flags. Do not turn them on merely to make a demo run.
- Agree on disposition of historical recordings/logs/backups. Do not silently erase them or treat a new release as permission for earlier collection.
- Have Grant start worker/API/MCP in his own visible terminals. Check the existing MCP tunnel without restarting it unless there is evidence it is necessary. Do not tunnel the UI.
- Enter full participant names and unverified contact details, present each person's notice and collect the written release while the microphone is closed. Record enrollment only afterward, with the fixed opening sentence.
- Run a short two-human/two-agent session early. Observe all four names in the shared transcript, floor exclusion during actual playback, Ben responding to Ava through the conversation bus, and server MCP evidence for replies.
- Score the RAM metadata before it is cleared. Report missing metrics as unknown. Test withdrawal and end-of-purpose cleanup, including the distinction between local deletion and pending vendor evidence.

## 5. Decide readiness from evidence

The immediate milestone is an integrated, tested consent-gated v2 with one observed two-agent live run. It is still not a legal certification. Document any unresolved timing, identity, vendor, encryption, backup or deletion limitations before declaring readiness for wider use.

Update the handoff, build spec and memory notes with exact commits, tests, live evidence and unresolved items. Do not carry forward an old "works" claim when the path has changed.
