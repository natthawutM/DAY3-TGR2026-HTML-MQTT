"""Day 3 HTTP/MQTT integration. Model inference remains a separate CLI."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import zipfile

import requests
from PIL import Image

CLASSES = ("black", "broken", "good", "husk", "immature", "parchment", "shell")
ROOT = Path(__file__).resolve().parent
IMAGE_TYPES = {".jpg", ".jpeg", ".png"}


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def headers(config):
    result = dict(config.get("headers", {}))
    token_env = config.get("token_env")
    if token_env:
        token = os.environ.get(token_env)
        if not token:
            raise ValueError(f"Missing environment variable {token_env}")
        result[config.get("token_header", "Authorization")] = config.get("token_prefix", "Bearer ") + token
    return result


def download(config, folder):
    if config.get("format") != "zip":
        raise ValueError("This download adapter requires a ZIP response. Adapt it to the announced API contract.")
    url = config.get("url")
    if not url:
        raise ValueError("Set download.url after the organizer announces the HTTP contract")
    folder.mkdir(parents=True, exist_ok=True)
    if any(folder.iterdir()):
        raise ValueError(f"Use an empty input folder for a fresh download: {folder}")
    with requests.get(url, headers=headers(config), timeout=(10, config.get("timeout", 120)),
                      stream=True) as response:
        response.raise_for_status()
        archive = folder / "received.zip"
        with archive.open("wb") as handle:
            for chunk in response.iter_content(1024 * 1024):
                handle.write(chunk)
    names = set()
    with zipfile.ZipFile(archive) as bundle:
        for entry in bundle.infolist():
            name = Path(entry.filename.replace("\\", "/")).name
            if entry.is_dir() or Path(name).suffix.lower() not in IMAGE_TYPES:
                continue
            if Path(name).stem.casefold() in names:
                raise ValueError(f"Duplicate image stem in download: {name}")
            names.add(Path(name).stem.casefold())
            # Copy image bytes only, flattening nested directories without extracting paths.
            with bundle.open(entry) as source, (folder / name).open("wb") as dest:
                while chunk := source.read(1024 * 1024):
                    dest.write(chunk)
    if not names:
        raise ValueError("ZIP contains no supported images")


def infer(config, input_dir, weights, output):
    command = config.get("command")
    if not command:
        raise ValueError("Set inference.command when the model is ready, or use --skip-infer with existing outputs")
    replacements = {"python": sys.executable, "input": str(input_dir),
                    "weights": str(weights), "out": str(output)}
    args = [part.format(**replacements) for part in command]
    start = time.perf_counter()
    subprocess.run(args, check=True, cwd=ROOT)
    return (time.perf_counter() - start) * 1000


def validate(input_dir, output):
    import numpy as np
    import cv2

    rows = json.loads((output / "predictions.json").read_text(encoding="utf-8"))
    images = {p.name: p for p in input_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_TYPES}
    if not images or len({Path(n).stem.casefold() for n in images}) != len(images):
        raise ValueError("Input must contain images with unique stems")
    if not isinstance(rows, list) or len(rows) != len(images):
        raise ValueError("One predictions.json entry is required per input image")
    seen = set()
    for row in rows:
        name = row["image_name"]
        if name not in images or name in seen:
            raise ValueError(f"Unexpected or duplicate image: {name}")
        seen.add(name)
        objects = row["objects"]
        with Image.open(images[name]) as original:
            from PIL import ImageOps
            width, height = ImageOps.exif_transpose(original).size
        path = output / "seg_mask" / (Path(name).stem + ".png")
        labels = cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_UNCHANGED) if path.exists() else None
        if labels is None or labels.ndim != 2 or labels.shape != (height, width) or labels.dtype.kind != "u":
            raise ValueError(f"Invalid integer ID PNG: {path}")
        ids = [obj["id"] for obj in objects]
        if any(type(i) is not int for i in ids) or ids != list(range(1, len(ids) + 1)):
            raise ValueError(f"Object IDs must be 1..N in {name}")
        if np.unique(labels[labels > 0]).tolist() != ids:
            raise ValueError(f"PNG IDs and JSON IDs disagree in {name}")
        for obj in objects:
            if obj["class"] not in CLASSES:
                raise ValueError(f"Unknown lowercase class in {name}: {obj['class']}")
            points = np.asarray(obj["polygon"])
            if points.ndim != 2 or points.shape[1] != 2 or len(points) < 3:
                raise ValueError(f"Invalid polygon in {name}")
            if not np.isfinite(points).all() or not (points == np.floor(points)).all():
                raise ValueError("Polygon coordinates must be finite integer pixels")
            if (points < 0).any() or (points[:, 0] >= width).any() or (points[:, 1] >= height).any():
                raise ValueError(f"Out-of-bounds polygon in {name}")
            x, y, w, h = cv2.boundingRect(points.astype(np.int32))
            filled = np.zeros((h, w), np.uint8)
            cv2.fillPoly(filled, [points.astype(np.int32) - [x, y]], 1)
            region = labels[y:y+h, x:x+w] == obj["id"]
            if not np.array_equal(region, filled.astype(bool)) or int(filled.sum()) != int((labels == obj["id"]).sum()):
                raise ValueError(f"Polygon does not reproduce ID {obj['id']} in {name}")
    return rows


def messages(rows, config, output, measured_ms):
    defects = config.get("defect_classes")
    if not isinstance(defects, list) or not set(defects) <= set(CLASSES):
        raise ValueError("Set defect_classes to the organizer-confirmed list of defective classes")
    latency_path = output / "latency.json"
    if latency_path.exists():
        latencies = json.loads(latency_path.read_text(encoding="utf-8"))
        source = "latency.json supplied by inference"
    elif measured_ms is not None and config.get("latency_mode") == "pipeline_average":
        latencies = {row["image_name"]: measured_ms / len(rows) for row in rows}
        source = "whole inference CLI elapsed / image count"
    else:
        raise ValueError("Provide outputs/latency.json with {image_name: milliseconds}, or choose pipeline_average")
    result = []
    for row in rows:
        name = row["image_name"]
        latency = latencies[name]
        if isinstance(latency, bool) or not isinstance(latency, (int, float)) or not 0 <= latency < float("inf"):
            raise ValueError(f"Invalid latency for {name}")
        counts = dict.fromkeys(CLASSES, 0)
        for obj in row["objects"]:
            counts[obj["class"]] += 1
        count = sum(counts.values())
        payload = dict(image=name, latency_ms=latency, counts=counts,
                       defect_ratio=sum(counts[c] for c in set(defects)) / count if count else 0.)
        save_json(output / "mqtt" / (Path(name).stem + ".json"), payload)
        result.append(payload)
    save_json(output / "latency_source.json", {"source": source, "empty_image_defect_ratio": 0.0})
    return result


def package(output, team):
    if not team.isdigit():
        raise ValueError("team_id must contain digits")
    archive = output / f"team_{team}_Day3.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        prefix = f"team_{team}_day3/"
        for name in ("README.md", "requirements.txt", "client.py", "train.py", "infer.py",
                     "data.py", "post.py", "config.example.json"):
            path = ROOT / name
            if path.exists():
                bundle.write(path, prefix + name)
        for path in sorted((ROOT / "weights").rglob("*")):
            if path.is_file() and path.name != ".gitkeep":
                bundle.write(path, prefix + path.relative_to(ROOT).as_posix())
        for folder in ("seg_mask", "mqtt", "vis"):
            for path in sorted((output / folder).rglob("*")):
                if path.is_file():
                    bundle.write(path, prefix + "outputs/" + path.relative_to(output).as_posix())
        for name in ("predictions.json", "results.json", "latency_source.json"):
            bundle.write(output / name, prefix + "outputs/" + name)
    return archive


def post_http(config, results, archive):
    url = config.get("url")
    if not url:
        raise ValueError("Set submit.url to the announced endpoint")
    timeout = (10, config.get("timeout", 120))
    if config.get("mode") == "json":
        response = requests.post(url, json=json.loads(results.read_text(encoding="utf-8")),
                                 headers=headers(config), timeout=timeout)
        response.raise_for_status()
        zip_url = config.get("zip_url")
        if not zip_url:
            raise ValueError("JSON submit mode also requires submit.zip_url")
        with archive.open("rb") as handle:
            response = requests.post(zip_url, files={config["zip_field"]: (archive.name, handle, "application/zip")},
                                     data=config.get("form_fields", {}), headers=headers(config), timeout=timeout)
            response.raise_for_status()
    else:
        with results.open("rb") as result_file, archive.open("rb") as zip_file:
            response = requests.post(url, files={
                config["results_field"]: ("results.json", result_file, "application/json"),
                config["zip_field"]: (archive.name, zip_file, "application/zip")},
                data=config.get("form_fields", {}), headers=headers(config), timeout=timeout)
            response.raise_for_status()
    return {"status": "http_accepted", "http_status": response.status_code}


def publish_mqtt(config, payloads, output):
    import paho.mqtt.client as mqtt
    if not config.get("host") or not config.get("topic"):
        raise ValueError("Set mqtt.host and mqtt.topic after announcement")
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=config.get("client_id", ""),
                         protocol=mqtt.MQTTv311)
    if config.get("username_env"):
        client.username_pw_set(os.environ[config["username_env"]],
                               os.environ[config["password_env"]] if config.get("password_env") else None)
    if config.get("tls"):
        client.tls_set(ca_certs=config.get("ca_cert") or None)
    client.connect(config["host"], config.get("port", 1883), keepalive=30)
    client.loop_start()
    acknowledged = []
    save_json(output / "mqtt_receipts.json", acknowledged)
    try:
        for payload in payloads:
            topic = config["topic"].format(image=payload["image"], stem=Path(payload["image"]).stem)
            info = client.publish(topic, json.dumps(payload), qos=config.get("qos", 1), retain=False)
            info.wait_for_publish(timeout=config.get("timeout", 30))
            if info.rc != mqtt.MQTT_ERR_SUCCESS or not info.is_published():
                raise RuntimeError(f"MQTT publish failed: {payload['image']}")
            acknowledged.append({"image": payload["image"], "topic": topic, "qos": config.get("qos", 1),
                                 "status": "publish_complete"})
            save_json(output / "mqtt_receipts.json", acknowledged)
    finally:
        client.disconnect()
        client.loop_stop()
    return {"status": "publish_complete", "images": len(acknowledged)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--input", type=Path, default=ROOT / "input")
    parser.add_argument("--weights", type=Path, default=ROOT / "weights")
    parser.add_argument("--out", type=Path, default=ROOT / "outputs")
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--skip-infer", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Prepare and validate payloads locally")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    if not args.dry_run:
        if not config.get("submit", {}).get("url") or not config.get("mqtt", {}).get("host") or not config.get("mqtt", {}).get("topic"):
            raise ValueError("Set the organizer HTTP submission URL, MQTT host and MQTT topic before delivery")
        if config["submit"].get("mode") == "json" and not config["submit"].get("zip_url"):
            raise ValueError("JSON submission mode requires zip_url before delivery")
        if config["mqtt"].get("qos", 1) not in (0, 1, 2):
            raise ValueError("MQTT QoS must be 0, 1 or 2")
    args.input, args.weights, args.out = args.input.resolve(), args.weights.resolve(), args.out.resolve()
    if args.input == args.out or args.input in args.out.parents or args.out in args.input.parents:
        raise ValueError("Input and output directories must be separate")
    args.out.mkdir(parents=True, exist_ok=True)
    if not args.skip_download:
        download(config["download"], args.input)
    measured = None if args.skip_infer else infer(config["inference"], args.input, args.weights, args.out)
    rows = validate(args.input, args.out)
    payloads = messages(rows, config, args.out, measured)
    save_json(args.out / "results.json", rows)
    archive = package(args.out, str(config["team_id"]))
    if args.dry_run:
        print(f"Validated {len(rows)} images. Payloads and ZIP prepared at {args.out}")
        return
    receipt = {"images": len(rows), "results_sha256": hashlib.sha256((args.out / "results.json").read_bytes()).hexdigest()}
    save_json(args.out / "delivery.json", receipt)
    # Both channels start independently after inference and validation finish.
    with ThreadPoolExecutor(max_workers=2) as executor:
        tasks = {
            "http": executor.submit(post_http, config["submit"], args.out / "results.json", archive),
            "mqtt": executor.submit(publish_mqtt, config["mqtt"], payloads, args.out)}
        for name, task in tasks.items():
            try:
                receipt[name] = task.result()
            except Exception as error:
                receipt[name] = {"status": "error", "error": str(error)}
            save_json(args.out / "delivery.json", receipt)
    if any(receipt[name]["status"] == "error" for name in tasks):
        raise SystemExit("Delivery incomplete. See delivery.json and mqtt_receipts.json")
    print(f"Delivered {len(rows)} images via HTTP and MQTT")


if __name__ == "__main__":
    main()
