"""Fetch the mock batch configuration and run the Day 3 client with an existing model."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import uuid

import requests

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="http://127.0.0.1:8088")
    parser.add_argument("--model-project", type=Path, default=ROOT.parent / "TesaModelTest")
    parser.add_argument("--weights", type=Path, help="Override model-project/weights")
    args = parser.parse_args()
    project = args.model_project.resolve()
    if not (project / "infer.py").is_file():
        parser.error(f"infer.py not found in {project}")
    weights = (args.weights or project / "weights").resolve()
    if not weights.is_dir():
        parser.error(f"Weights folder not found: {weights}")
    response = requests.get(args.server.rstrip("/") + "/api/config", timeout=15)
    response.raise_for_status()
    config = response.json()
    config["inference"]["command"] = ["{python}", str(project / "infer.py"), "--input", "{input}",
                                      "--weights", "{weights}", "--out", "{out}"]
    folder = ROOT / "mock_server" / "runtime" / ("client_" + uuid.uuid4().hex)
    folder.mkdir(parents=True)
    path = folder / "config.json"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    print("Demo defect policy: all classes except good. Latency: inference CLI average.", flush=True)
    subprocess.run([sys.executable, str(ROOT / "client.py"), "--config", str(path),
                    "--weights", str(weights), "--input", str(folder / "input"),
                    "--out", str(folder / "outputs")], check=True)
    print(f"Saved client artifacts at {folder}")


if __name__ == "__main__":
    main()
