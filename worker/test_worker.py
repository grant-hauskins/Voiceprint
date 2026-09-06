import csv
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from calibration import Calibrator, fit, metrics
from worker import decode_audio, segmentation_summary


class ModelBoundaryTests(unittest.TestCase):
    def scores(self, labels):
        return np.eye(7)[labels]

    def test_overlap_uses_powerset_classes_and_ignores_padding(self):
        labels = np.ones(100, dtype=int)
        labels[88:] = 4  # Beyond the actual 1.5 seconds: padding must not count.
        summary = segmentation_summary(self.scores(labels), 24000, 0, 270 / 16000, 991 / 16000)
        self.assertEqual(summary["overlap"], "clear")
        labels[75:86] = 4
        summary = segmentation_summary(self.scores(labels), 24000, 0, 270 / 16000, 991 / 16000)
        self.assertEqual(summary["overlap"], "detected")
        self.assertFalse(summary["profile_eligible"])

    def test_two_local_speaker_slots_are_not_one_clean_embedding(self):
        labels = np.r_[np.ones(40, dtype=int), np.full(60, 2)]
        summary = segmentation_summary(self.scores(labels), 24000, 0, 270 / 16000, 991 / 16000)
        self.assertTrue(summary["speaker_change"])
        self.assertFalse(summary["profile_eligible"])

    def test_silence_is_not_a_speaker(self):
        summary = segmentation_summary(self.scores(np.zeros(100, dtype=int)), 24000, 0, 270 / 16000, 991 / 16000)
        self.assertFalse(summary["speech"])
        self.assertFalse(summary["profile_eligible"])

    def test_decoder_rejects_wrong_format(self):
        with self.assertRaises(ValueError):
            decode_audio({"sample_rate": 8000, "audio_base64": ""})


class CalibrationTests(unittest.TestCase):
    def artifact(self):
        # Pure unit fixture; never persisted as a release calibration.
        return {"schema_version": 1, "release_eligible": True, "calibration_id": "unit-test", "model_id": "unit-model",
                "participant_count": 2, "feature_mean": [0, 0], "feature_scale": [1, 1], "weights": [-1, 2, 1],
                "feature_min": [0, 0], "feature_max": [1, 1]}

    def test_probability_is_a_defined_logistic_mapping(self):
        calibration = Calibrator(self.artifact())
        self.assertAlmostEqual(calibration.predict(.5, 0, "unit-model", 2), .5)
        self.assertGreater(calibration.predict(.8, .3, "unit-model", 2), .5)

    def test_wrong_model_count_or_unvalidated_range_abstains(self):
        calibration = Calibrator(self.artifact())
        self.assertIsNone(calibration.predict(.8, .3, "other-model", 2))
        self.assertIsNone(calibration.predict(.8, .3, "unit-model", 4))
        self.assertIsNone(calibration.predict(-.5, .3, "unit-model", 2))
        artifact = self.artifact(); artifact["release_eligible"] = False
        with self.assertRaises(ValueError):
            Calibrator(artifact)

    def test_metrics_penalize_overconfident_wrong_answers(self):
        good = metrics([1, 1, 0, 0], [.9, .9, .1, .1])
        bad = metrics([1, 1, 0, 0], [.99, .99, .99, .99])
        self.assertLess(good["brier"], bad["brier"])
        self.assertLess(good["ece"], bad["ece"])

    def test_fixture_audio_cannot_become_release_calibration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.csv"
            path.write_text("session_id,segment_id,evaluation_kind\na,b,fixture\n")
            with self.assertRaisesRegex(ValueError, "independently reviewed"):
                fit(path, Path(directory) / "calibration.json")

    def test_training_and_validation_speaker_leakage_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.csv"
            fields = ["session_id", "segment_id", "speaker_group", "split", "evaluation_kind", "model_id", "participant_count", "correct", "similarity", "margin"]
            with path.open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader()
                for split in ("fit", "validation"):
                    for i in range(200):
                        writer.writerow(dict(session_id=split + str(i % 5), segment_id=i, speaker_group=str(i % 5), split=split,
                                             evaluation_kind="live_independent", model_id="unit-model", participant_count=2, correct=i % 2, similarity=.7, margin=.1))
            with self.assertRaisesRegex(ValueError, "speaker_group"):
                fit(path, Path(directory) / "calibration.json")

    def test_fitter_runs_on_disjoint_schema_fixture(self):
        # Numeric optimizer test only; these invented observations never leave the temp directory.
        import contextlib
        import io
        rng = np.random.default_rng(42)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.csv"
            fields = ["session_id", "segment_id", "speaker_group", "split", "evaluation_kind", "model_id", "participant_count", "correct", "similarity", "margin"]
            with path.open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader()
                for split in ("fit", "validation"):
                    for i in range(200):
                        correct = i % 2
                        writer.writerow(dict(session_id=split + str(i % 5), segment_id=i, speaker_group=split + str(i % 5), split=split,
                                             evaluation_kind="live_independent", model_id="unit-model", participant_count=2, correct=correct,
                                             similarity=float(np.clip(.2 + .6 * correct + rng.normal(0, .1), 0, 1)), margin=.1 + .2 * correct))
            with contextlib.redirect_stdout(io.StringIO()):
                artifact = fit(path, Path(directory) / "calibration.json")
            self.assertEqual(artifact["validation"]["count"], 200)
            self.assertEqual(len(artifact["weights"]), 3)
            self.assertLess(artifact["validation"]["brier"], artifact["baseline_brier"])


if __name__ == "__main__":
    unittest.main()
