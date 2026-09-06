"""Download public, licensed model weights; pin resolved revisions and checksums."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

from huggingface_hub import HfApi, hf_hub_download


def setup(root):
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "manifest.json"
    existing = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    repos = {
        "speechbrain/spkrec-ecapa-voxceleb": {
            "hyperparams.yaml": "ecapa/hyperparams.yaml", "embedding_model.ckpt": "ecapa/embedding_model.ckpt",
            "mean_var_norm_emb.ckpt": "ecapa/mean_var_norm_emb.ckpt", "classifier.ckpt": "ecapa/classifier.ckpt",
            "label_encoder.txt": "ecapa/label_encoder.txt",
        },
        "onnx-community/pyannote-segmentation-3.0": {"onnx/model.onnx": "segmentation.onnx"},
    }
    revisions, hashes = {}, {}
    for repo, files in repos.items():
        revision = existing.get("revisions", {}).get(repo) or HfApi().model_info(repo).sha
        revisions[repo] = revision
        for remote, local in files.items():
            cached = hf_hub_download(repo, remote, revision=revision, cache_dir=root / "cache")
            destination = root / local
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(cached, destination)
            hashes[local] = hashlib.sha256(destination.read_bytes()).hexdigest()
    fingerprint = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()[:16]
    manifest_path.write_text(json.dumps({"revisions": revisions, "sha256": hashes, "fingerprint": fingerprint}, indent=2) + "\n")
    print("Models downloaded and pinned:", manifest_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", type=Path, default=Path("models"))
    setup(parser.parse_args().models)
