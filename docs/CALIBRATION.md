# Calibration deliverable: VP-Live-En-v1

**Status: mapping, fitting tool, validation contract and rejection tests implemented; the live validation set has not been collected. No release calibration artifact exists.** The two-speaker public fixture test is integration evidence only and is explicitly ineligible for release calibration.

## What the probability means

For a verified single-speaker window with enrolled candidates, the target is:

`P(the top-ranked participant is the true speaker | best cosine score, top-two score margin)`.

Python performs embedding extraction, cosine matching, calibration fitting and probability inference. Java passes profiles to the worker, coordinates the session, stores its outputs and applies the uncertainty policy. The processes communicate over REST.

Similarity itself is not a probability. Softmax over enrolled speakers is also insufficient: it can confidently choose someone even when the real speaker is not enrolled. The fitter uses binary logistic calibration:

```text
x1 = (best_similarity - fit_mean_similarity) / fit_std_similarity
x2 = (best_similarity - second_similarity - fit_mean_margin) / fit_std_margin
p_correct = sigmoid(bias + weight_similarity*x1 + weight_margin*x2)
```

`worker/calibration.py` fits regularized binary log loss on the **fit split only**. The regularization coefficient is fixed at 0.01. Means, standard deviations and coefficients are frozen before evaluation on the validation split. A later tuning pass requires a fresh untouched test split. Do not repeatedly tune against the same validation set.

The artifact records its dataset hash, model fingerprint, exact participant count, feature normalization, coefficients, observed validation feature range and validation metrics. The worker abstains when the model, participant count or score range does not match. It fails startup for an ineligible artifact. Current rejection thresholds and the 1.5 second context are part of the model-policy version and must change the version if modified.

## The named validation set

Collect **VP-Live-En-v1**, consented English conversations captured through the actual shared-microphone streaming client:

1. Recruit ten disjoint pairs (20 people). Assign five whole pairs to fitting and five different pairs to validation before recording. No participant may occur in both groups. A `speaker_group` identifies a disjoint connected group of participants, not an arbitrary meeting ID.
2. Capture two sessions per pair in different conditions: normal conversational distance, then quieter/farther or moderate background noise. Enroll with new opening statements for each session. At least five independent sessions and five independent speaker groups are required in each split; the planned twenty sessions exceed that minimum.
3. Use structured turn-taking plus natural conversation. Include pauses, short turns, interruptions, similar voices, and a non-enrolled guest in selected periods. Overlap and changes are evaluated separately; probability fitting concerns single-speaker candidate decisions. Do not derive true speaker identity from the model under evaluation.
4. A moderator independently labels sampled segments with the true participant or `unknown`, even when the system appears correct. Sample non-overlapping contexts at least 1.5 seconds apart, using a schedule fixed before inspecting scores. Record the original model prediction, not a later corrected label. Corrections-only logs are biased and cannot be the validation set.
5. Include baseline and post-correction sessions with the same fixed adaptation rule used in production. Measure correction benefit on later utterances that were not used to update the profile. Scoring the correction example against its updated profile is a plumbing check, not evidence of future improvement.
6. Collect at least 200 independent labeled observations per split, with at least 20 correct and 20 incorrect top-candidate outcomes in each. Preserve the natural error prevalence: if there are too few errors, collect more representative data rather than oversampling mistakes. The minimum is a guardrail, not a power analysis.

Maintain participant membership privately so speaker-disjointness can be verified. The fitting tool checks session/group identifiers but cannot prove that a human used them correctly or that audio was live. The dataset manifest's provenance must be reviewed.

The repository includes `evaluation/observations.template.csv` with the required columns. It deliberately contains **no invented measurements**. Use one row per independently reviewed, eligible window:

- `session_id`, `segment_id`: unique observation identity.
- `speaker_group`: connected participant group, disjoint across splits.
- `split`: `fit` or `validation`, assigned before capture.
- `evaluation_kind`: `live_independent`, set only after independent review.
- `model_id`: exact fingerprint returned by the worker.
- `participant_count`: number of enrolled profiles (two for the first artifact).
- `similarity`, `margin`: original worker scores.
- `correct`: 1 if the original top candidate was the true speaker, otherwise 0, including non-enrolled speakers. Keep labels independent of later corrections.

## Fit, evaluate and enable

```powershell
.venv\Scripts\python.exe worker\calibration.py data\reviewed-observations.csv data\calibration.json
.venv\Scripts\python.exe worker\worker.py --calibration data\calibration.json
```

The fitter rejects empty sets, non-live provenance, duplicate observations, insufficient independent groups, mixed models/counts, missing positive/negative outcomes and leakage between fitting and validation.

It reports:

- **Brier score:** mean squared probability error; target <=0.15 and better than predicting the fit-set correctness rate for every observation.
- **Expected calibration error:** ten fixed-width confidence bins; target <=0.08.
- **Reliability bins:** sample count, average probability and actual correctness for inspection.
- **Spearman correlation:** reported for comparison with the brief's >0.80 target, not used as a substitute for calibration. A ranking statistic does not establish that a reported 90% means 90% correctness.
- Number of observations, validation session/group IDs, observed score range and dataset SHA-256.

Only artifacts meeting the data checks, Brier gate and calibration-error gate receive `release_eligible: true`. These initial gates require product review and larger validation before deployment in consequential negotiations. Report uncertainty intervals by resampling whole speaker groups when the live data exists; 200 correlated audio windows are not 200 independent people.

After loading an eligible artifact, clear single-speaker attributions expose `confidence` in `[0,1]`. Values below 0.60 remain uncertain. Overlap, changing speakers, silence, stale context, inference errors, unmatched versions and out-of-range scores retain an unavailable probability and cannot be trusted automatically. Human correction labels also do not receive an invented probability.

## Required evidence before claiming completion

- Reviewed VP-Live-En-v1 provenance and speaker membership manifest.
- Fitting observations and the untouched labeled validation split.
- Saved calibration artifact and reliability/coverage results.
- Known-speaker accuracy including abstentions, plus accuracy among accepted decisions.
- Overlap precision **and recall**, using independently labeled overlap/non-overlap windows.
- Capture-onset, turn-change and steady-state latency percentiles on target hardware.
- Before/after correction results on held-out future utterances.

The real-model smoke test and unit tests do not satisfy these live-validation deliverables.
