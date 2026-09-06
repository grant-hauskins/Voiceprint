"""Fit P(top-ranked speaker is correct | cosine similarity, top-two margin).

Consumes reviewed score observations, never recorded audio. Production artifacts
require independent live conversations with disjoint speaker groups and sessions.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

import numpy as np


def sigmoid(value):
    return 1 / (1 + np.exp(-np.clip(value, -40, 40)))


def metrics(labels, probabilities):
    from scipy.stats import spearmanr
    labels, probabilities = np.asarray(labels), np.asarray(probabilities)
    ece, bins = 0.0, []
    for low in np.arange(0, 1, .1):
        selected = (probabilities >= low) & (probabilities < low + .1 if low < .9 else probabilities <= 1)
        if np.any(selected):
            accuracy, confidence = float(labels[selected].mean()), float(probabilities[selected].mean())
            ece += float(selected.mean()) * abs(accuracy - confidence)
            bins.append({"low": round(float(low), 1), "count": int(selected.sum()), "accuracy": accuracy, "confidence": confidence})
    rho = float(spearmanr(labels, probabilities).statistic) if len(np.unique(probabilities)) > 1 and len(np.unique(labels)) > 1 else None
    return {"count": len(labels), "correct_rate": float(labels.mean()),
            "brier": float(np.mean((probabilities - labels) ** 2)), "ece": ece,
            "spearman": rho, "reliability_bins": bins}


class Calibrator:
    def __init__(self, artifact):
        if artifact.get("schema_version") != 1 or artifact.get("release_eligible") is not True:
            raise ValueError("Calibration is not eligible for live confidence reporting")
        self.artifact = artifact
        self.id = artifact["calibration_id"]
        self.mean = np.asarray(artifact["feature_mean"], dtype=float)
        self.scale = np.asarray(artifact["feature_scale"], dtype=float)
        self.weights = np.asarray(artifact["weights"], dtype=float)
        if self.mean.shape != (2,) or self.scale.shape != (2,) or self.weights.shape != (3,) or not all(np.isfinite(a).all() for a in (self.mean, self.scale, self.weights)) or np.any(self.scale <= 0):
            raise ValueError("Invalid calibration coefficients")

    @classmethod
    def load(cls, path):
        return cls(json.loads(Path(path).read_text()))

    def predict(self, similarity, margin, model_id, participant_count):
        if model_id != self.artifact["model_id"] or participant_count != self.artifact["participant_count"]:
            return None
        features = np.array([similarity, margin])
        if not np.isfinite(features).all():
            return None
        # Abstain outside the validated score range rather than extrapolating.
        if np.any(features < self.artifact["feature_min"]) or np.any(features > self.artifact["feature_max"]):
            return None
        x = (features - self.mean) / self.scale
        return float(sigmoid(self.weights[0] + x @ self.weights[1:]))


def fit(input_path, output_path):
    from scipy.optimize import minimize
    with Path(input_path).open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    if not rows:
        raise ValueError("The validation set is empty")
    keys = [(r["session_id"], r["segment_id"]) for r in rows]
    if len(set(keys)) != len(keys):
        raise ValueError("Duplicate segments would bias calibration")
    if any(r["evaluation_kind"] != "live_independent" for r in rows):
        raise ValueError("Release calibration requires independently reviewed live speech, not fixtures or corrections-only sampling")
    if any(r["split"] not in ("fit", "validation") for r in rows):
        raise ValueError("Use predefined fit and validation splits")
    if len({r["model_id"] for r in rows}) != 1 or len({r["participant_count"] for r in rows}) != 1:
        raise ValueError("Use one model version and participant count per calibration")
    train = [r for r in rows if r["split"] == "fit"]
    validation = [r for r in rows if r["split"] == "validation"]
    for name, split in (("fit", train), ("validation", validation)):
        if len(split) < 200 or len({r["session_id"] for r in split}) < 5 or len({r["speaker_group"] for r in split}) < 5:
            raise ValueError(name + " requires >=200 observations from >=5 sessions and >=5 disjoint speaker groups")
        labels = [int(r["correct"]) for r in split]
        if any(y not in (0, 1) for y in labels) or labels.count(0) < 20 or labels.count(1) < 20:
            raise ValueError(name + " requires >=20 correct and >=20 incorrect independent labels")
    for key in ("session_id", "speaker_group"):
        if {r[key] for r in train} & {r[key] for r in validation}:
            raise ValueError("Fit and validation must be disjoint by " + key)
    def arrays(split):
        x = np.array([[float(r["similarity"]), float(r["margin"])] for r in split])
        y = np.array([int(r["correct"]) for r in split])
        if not np.isfinite(x).all() or np.any(x[:, 0] < -1) or np.any(x[:, 0] > 1) or np.any(x[:, 1] < 0) or np.any(x[:, 1] > 2):
            raise ValueError("Invalid similarities or margins")
        return x, y
    x, y = arrays(train)
    vx, vy = arrays(validation)
    mean, scale = x.mean(axis=0), np.maximum(x.std(axis=0), 1e-6)
    design = np.column_stack([np.ones(len(x)), (x - mean) / scale])
    def loss(w):
        z = design @ w
        return np.mean(np.logaddexp(0, z) - y * z) + .01 * np.sum(w[1:] ** 2)
    fitted = minimize(loss, np.zeros(3), method="BFGS")
    if not fitted.success:
        raise ValueError("Calibration optimizer did not converge")
    probabilities = sigmoid(fitted.x[0] + ((vx - mean) / scale) @ fitted.x[1:])
    report = metrics(vy, probabilities)
    baseline_brier = float(np.mean((vy - y.mean()) ** 2))
    dataset_hash = hashlib.sha256(Path(input_path).read_bytes()).hexdigest()
    artifact = {
        "schema_version": 1, "calibration_id": dataset_hash[:16], "dataset_sha256": dataset_hash,
        "model_id": rows[0]["model_id"], "participant_count": int(rows[0]["participant_count"]),
        "mapping": "sigmoid(bias + w_similarity*z_similarity + w_margin*z_margin)",
        "feature_mean": mean.tolist(), "feature_scale": scale.tolist(), "weights": fitted.x.tolist(),
        "feature_min": vx.min(axis=0).tolist(), "feature_max": vx.max(axis=0).tolist(),
        "fit_count": len(train), "validation_sessions": sorted({r["session_id"] for r in validation}),
        "validation_speaker_groups": sorted({r["speaker_group"] for r in validation}),
        "validation": report, "baseline_brier": baseline_brier,
        "release_eligible": report["ece"] <= .08 and report["brier"] <= .15 and report["brier"] < baseline_brier,
    }
    Path(output_path).write_text(json.dumps(artifact, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"release_eligible": artifact["release_eligible"], "validation": report}, indent=2))
    return artifact


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observations", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    fit(args.observations, args.output)
