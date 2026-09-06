"""Local ML worker: real speaker embeddings, overlap inference, and calibrated matching.

No simulated inference mode. Models must load successfully before the port opens.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import numpy as np

from calibration import Calibrator

SAMPLE_RATE = 16000
MODEL_ID = "speechbrain-ecapa-voxceleb+pyannote3-onnx:context1500-v1"


def decode_audio(request):
    if request.get("sample_rate") != SAMPLE_RATE:
        raise ValueError("sample_rate must be 16000")
    pcm = base64.b64decode(request["audio_base64"], validate=True)
    enrollment = request.get("enrollment", False)
    if type(enrollment) is not bool:
        raise ValueError("enrollment must be boolean")
    if len(pcm) % 2 or not (160000 <= len(pcm) <= 480000 if enrollment else len(pcm) == 48000):
        raise ValueError("Invalid audio duration")
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0, enrollment


def segmentation_summary(logits, sample_count, frame_start, frame_step, frame_duration, enrollment=False):
    """Pyannote powerset: 0=silence, 1..3=single speaker, 4..6=overlap.

    Derive times from the model's convolutional receptive field, not the padded
    waveform length. Only actual input frames count toward overlap or changes.
    """
    labels = np.argmax(logits, axis=-1)
    times = frame_start + np.arange(len(labels)) * frame_step + frame_duration / 2
    actual = labels[times < sample_count / SAMPLE_RATE]
    recent = labels[(times >= max(0, sample_count / SAMPLE_RATE - .25)) & (times < sample_count / SAMPLE_RATE)]
    if enrollment:
        recent = actual
    active = actual[(actual >= 1) & (actual <= 3)]
    # Ignore isolated local-slot flicker; at least 100 ms per slot is a change.
    slots = [slot for slot in (1, 2, 3) if np.count_nonzero(active == slot) * frame_step >= .1]
    overlap = bool(len(recent) and np.mean(recent >= 4) >= .20)
    context_overlap = bool(len(actual) and np.any(actual >= 4))
    speech = bool(len(recent) and np.mean(recent != 0) >= .20)
    single_fraction = float(np.mean((actual >= 1) & (actual <= 3))) if len(actual) else 0.0
    return {
        "speech": speech,
        "overlap": "detected" if overlap else "clear",
        "speaker_change": len(slots) > 1,
        "profile_eligible": speech and not context_overlap and len(slots) == 1 and single_fraction >= .7,
    }


class Models:
    def __init__(self, root: Path, calibration_path=None):
        manifest = json.loads((root / "manifest.json").read_text())
        for relative, expected in manifest["sha256"].items():
            actual = hashlib.sha256((root / relative).read_bytes()).hexdigest()
            if actual != expected:
                raise ValueError("Model checksum mismatch: " + relative)
        import torch
        import onnxruntime as ort
        from speechbrain.inference.speaker import EncoderClassifier
        from speechbrain.utils.fetching import LocalStrategy

        torch.set_num_threads(int(os.environ.get("VOICEPRINT_ML_THREADS", "2")))
        self.torch = torch
        # Local-only model loading: setup_models.py performs the explicit downloads.
        self.encoder = EncoderClassifier.from_hparams(
            source=str(root / "ecapa"), savedir=str(root / "ecapa"),
            overrides={"pretrained_path": (root / "ecapa").as_posix()},
            run_opts={"device": "cpu"}, local_strategy=LocalStrategy.COPY,
        )
        options = ort.SessionOptions()
        options.intra_op_num_threads = int(os.environ.get("VOICEPRINT_ML_THREADS", "2"))
        options.inter_op_num_threads = 1
        self.segmentation = ort.InferenceSession(str(root / "segmentation.onnx"), options, providers=["CPUExecutionProvider"])
        # SincNet kernels [251,5,5], strides [1,1,1], max pools [3,3,3],
        # initial sinc stride 10: receptive field 991 samples, hop 270 samples.
        self.frame_duration = 991 / SAMPLE_RATE
        self.frame_step = 270 / SAMPLE_RATE
        self.calibrator = Calibrator.load(calibration_path) if calibration_path else None
        self.model_id = MODEL_ID + ":" + manifest["fingerprint"]

    def analyze(self, request):
        samples, enrollment = decode_audio(request)
        # Enrollment may be 15 s; segment in <=10 s chunks using the trained length.
        summaries = []
        for start in range(0, len(samples), 160000):
            excerpt = samples[start:start + 160000]
            padded = np.pad(excerpt, (0, 160000 - len(excerpt)))[None, None, :].astype(np.float32)
            logits = self.segmentation.run(None, {"input_values": padded})[0][0]
            summaries.append(segmentation_summary(logits, len(excerpt), 0, self.frame_step, self.frame_duration, enrollment))
        summary = summaries[-1]
        if enrollment:
            summary = {
                "speech": all(s["speech"] for s in summaries),
                "overlap": "detected" if any(s["overlap"] == "detected" for s in summaries) else "clear",
                "speaker_change": any(s["speaker_change"] for s in summaries),
                "profile_eligible": all(s["profile_eligible"] for s in summaries),
            }
        embedding = None
        if summary["speech"]:
            with self.torch.inference_mode():
                vector = self.encoder.encode_batch(self.torch.from_numpy(samples.copy()).unsqueeze(0)).flatten().cpu().numpy()
            norm = float(np.linalg.norm(vector))
            if not np.isfinite(vector).all() or norm < 1e-8:
                raise ValueError("Model returned an invalid embedding")
            embedding = (vector / norm).tolist()
        return {"model_id": self.model_id, "embedding": embedding, **summary}

    def match(self, request):
        if request.get("model_id") != self.model_id:
            raise ValueError("Model identity mismatch")
        vector = np.asarray(request["embedding"], dtype=np.float64)
        profiles = request["profiles"]
        if vector.shape != (192,) or not np.isfinite(vector).all() or np.linalg.norm(vector) < 1e-8 or not 2 <= len(profiles) <= 4:
            raise ValueError("Invalid matching request")
        vector /= np.linalg.norm(vector)
        ranked = []
        for profile in profiles:
            other = np.asarray(profile["embedding"], dtype=np.float64)
            if other.shape != vector.shape or not np.isfinite(other).all() or np.linalg.norm(other) < 1e-8:
                raise ValueError("Invalid profile embedding")
            similarity = float(np.clip(np.dot(vector, other / np.linalg.norm(other)), -1, 1))
            ranked.append({"speaker_id": profile["id"], "similarity": similarity})
        ranked.sort(key=lambda item: item["similarity"], reverse=True)
        best, margin = ranked[0]["similarity"], ranked[0]["similarity"] - ranked[1]["similarity"]
        prediction = self.calibrator.predict(best, margin, self.model_id, len(profiles)) if self.calibrator else None
        return {"candidates": ranked, "confidence": prediction,
                "calibration_id": self.calibrator.id if prediction is not None else None}


def serve(models, port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Never log audio, profiles, or participant identities.

        def reply(self, status, body):
            encoded = json.dumps(body, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            self.reply(200 if self.path == "/health" else 404,
                       {"status": "ready", "model_id": models.model_id} if self.path == "/health" else {"error": "not_found"})

        def do_POST(self):
            if self.headers.get("Origin") or self.headers.get("Host", "").split(":")[0] not in ("localhost", "127.0.0.1"):
                self.reply(403, {"error": "origin_rejected"})
                return
            try:
                self.connection.settimeout(10)
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 700000:
                    raise ValueError("Invalid body length")
                request = json.loads(self.rfile.read(length))
                if self.path == "/analyze":
                    result = models.analyze(request)
                elif self.path == "/match":
                    result = models.match(request)
                else:
                    self.reply(404, {"error": "not_found"})
                    return
                self.reply(200, result)
            except (ValueError, KeyError, TypeError):
                self.reply(422, {"error": "invalid_inference_request"})
            except Exception as error:
                print("Inference failed:", type(error).__name__, flush=True)
                self.reply(503, {"error": "inference_failed"})

    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", type=Path, default=Path("models"))
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--calibration", type=Path)
    args = parser.parse_args()
    models = Models(args.models.resolve(), args.calibration)
    print("Speech worker ready at http://127.0.0.1:" + str(args.port), flush=True)
    serve(models, args.port)
