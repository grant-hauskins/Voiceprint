# Verification record

## Real-model two-speaker integration run

Executed locally on 2026-09-05 with Java 21, Python 3.12, Torch 2.6 CPU, SpeechBrain 1.0.3 and ONNX Runtime 1.22.1. Models were downloaded from the public repositories linked below, pinned by revision and hashed in local `models/manifest.json`.

The fixture client exercised Python inference → Java attribution and coordination → SQLite → REST reads and corrections. It sent 76 chunks of 250 ms at simulated capture cadence. Two actual speech voices were used, not tones, synthetic embeddings or canned worker responses. Enrollment used SpeechBrain utterances 1–3; test speech used disjoint utterances 4–6. The mixed section combined the two test voices at comparable RMS levels.

Observed in the final packaged-build run:

- Both fixture speakers were identified through the full HTTP path.
- All 11 evaluated overlap windows whose full context lay in the mixed section were flagged by the real segmentation model.
- No overlap flags occurred in the evaluated single-speaker portions.
- HTTP round-trip latency: median about 234 ms, 95th percentile about 265 ms.
- Capture-end-to-response latency: 95th percentile about 265 ms, including replay backlog. An earlier run measured 254 ms; timing varies with machine load.
- Initial context requirement: 1500 ms; this run does **not** meet <500 ms from speech onset.
- A correction changed the persisted profile; similarity to that corrected example rose from 0.680 to 0.785. This proves acoustic adaptation was applied. Future-utterance benefit remains to be measured with live data.
- Speaker histories, corrections and end-of-session state were retrievable.

The full local observation log is generated at `data/two-speaker-smoke.json`; it is ignored by version control. The fixtures are only two people, brief clean utterances and an artificial overlap mix. Overlapping rolling windows are correlated. Silence inside utterances lacks independent frame labels. These observations are **not** production accuracy, overlap precision or confidence-calibration claims. Report results from VP-Live-En-v1 separately.

## Automated behavior checks

Latest verification: **16 Java tests and 10 Python tests passed**. The pinned Python environment also passed `pip check`.

Java tests exercise atomic enrollment, distinct participants, buffering, idempotent retries, sequence gaps, worker failures and recovery, stale context, end-of-session behavior, silence, overlap, correction-driven profile changes, future match changes, relabeling without double counting, persistence, scoped queries, invalid input, the confidence threshold and human correction probability handling.

Transport tests exercise bearer-token enforcement, malformed HTTP input, browser-origin rejection, MCP initialization, tool listing, successful HTTP-backed MCP reads, missing sessions and protocol error recovery.

Python tests cover powerset overlap interpretation, padded-frame exclusion, speaker-change safeguards, silence, input format, the logistic mapping, model/count/range mismatch, calibration metrics, fixture provenance rejection and speaker-group leakage rejection. Model weights themselves are exercised by the separate real-model smoke test, not by unit fixtures.

## Remaining blockers

No independently labeled live calibration set has been collected and no production confidence artifact is enabled. A shared-microphone run with two people has not been performed in this development session. The <500 ms onset latency, >85% live identification accuracy, >90% overlap precision, calibration correlation and negotiation user story remain unverified.

The 10-second-trained segmentation model is evaluated causally on right-padded short windows. That avoids waiting for future speech but changes the model's operating conditions. The tail lacks future conversational context, and the 1.5 second embedding can straddle turns. Both effects require live validation. The implementation abstains on detected changes; it cannot guarantee detection of every turn boundary or overlap. The encoder and segmentation model are single-language MVP candidates, not a validated system for every acoustic domain.

## Sources and reproducibility

- [SpeechBrain ECAPA model and input contract](https://huggingface.co/speechbrain/spkrec-ecapa-voxceleb).
- [Original pyannote segmentation model and powerset classes](https://huggingface.co/pyannote/segmentation-3.0).
- [Published ONNX export used here](https://huggingface.co/onnx-community/pyannote-segmentation-3.0).
- [Pyannote SincNet receptive field implementation](https://github.com/pyannote/pyannote-audio/blob/3.3.2/pyannote/audio/models/blocks/sincnet.py), used to map model frames to actual sample times.
- [Versioned SpeechBrain speech fixtures](https://github.com/speechbrain/speechbrain/tree/v1.0.3/tests/samples/ASR).
- [MCP 2025-11-25 lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle).

SpeechBrain ECAPA weights are published under Apache 2.0; the pyannote ONNX export is published under MIT. Model and fixture provenance should remain attached to any redistributed evaluation bundle. This repository does not commit third-party weights or audio.
