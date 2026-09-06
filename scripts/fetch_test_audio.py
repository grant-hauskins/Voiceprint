"""Explicitly download public SpeechBrain test fixtures for integration testing."""
import argparse
import urllib.request
from pathlib import Path

BASE = "https://raw.githubusercontent.com/speechbrain/speechbrain/v1.0.3/tests/samples/ASR/"

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/fixtures"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for speaker in (1, 2):
        for utterance in range(1, 7):
            name = f"spk{speaker}_snt{utterance}.wav"
            with urllib.request.urlopen(BASE + name, timeout=30) as response:
                (args.output / name).write_bytes(response.read())
    print("Test fixtures saved to", args.output)
