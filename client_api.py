"""Persistent Day 3 API client. Preload inference and establish MQTT before requesting a scored image."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import io
import json
import os
from pathlib import Path
import ssl
import sys
import threading
import time
import uuid

import numpy as np
from PIL import Image, ImageOps
import paho.mqtt.client as mqtt
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
CLASSES = ("black", "broken", "good", "husk", "immature", "parchment", "shell")


def configuration(path):
    template = ROOT / "config.api.example.json"
    cfg = json.loads((path or template).read_text(encoding="utf-8-sig"))
    for variable, key in (("HTTP_URL", "http_url"), ("TEAM_ID", "team_id")):
        if os.environ.get(variable):
            cfg[key] = os.environ[variable]
    for variable, key in (("MQTT_HOST", "host"), ("MQTT_TRACK", "track"),
                          ("MQTT_EVENT", "event"), ("MQTT_WS_PATH", "ws_path")):
        if os.environ.get(variable):
            cfg["mqtt"][key] = os.environ[variable]
    if os.environ.get("MQTT_PORT"):
        cfg["mqtt"]["port"] = int(os.environ["MQTT_PORT"])
    return cfg


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


class Model:
    def __init__(self, project, weights, variant, threads):
        # Import the model project's own infer.py and sibling data/post modules.
        sys.path.insert(0, str(project))
        spec = importlib.util.spec_from_file_location("day3_model_infer", project / "infer.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.predictor = self.module.Predictor(weights, variant, threads)

    def predict(self, data, name):
        with Image.open(io.BytesIO(data)) as original:
            rgb = np.asarray(ImageOps.exif_transpose(original).convert("RGB"))
        labels, classes, regions = self.predictor.predict_instances(rgb)
        labels, objects, regions = self.module.submission_instances(labels, classes, regions, name)
        if any(obj["class"] not in CLASSES for obj in objects):
            raise ValueError("Model must return lowercase class names")
        return {"image_name": name, "objects": objects}, rgb, labels

    def artifacts(self, folder, prediction, rgb, labels):
        self.module.write_image(folder / "seg_mask" / (Path(prediction["image_name"]).stem + ".png"), labels)
        # Preview generation is deferred until both deliveries complete.
        import cv2
        image = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        h, w = labels.shape
        scale = min(1., 1280 / max(h, w))
        size = (max(1, round(w * scale)), max(1, round(h * scale)))
        image = cv2.resize(image, size, interpolation=cv2.INTER_AREA)
        ids = cv2.resize(labels, size, interpolation=cv2.INTER_NEAREST)
        for obj in prediction["objects"]:
            contours, _ = cv2.findContours((ids == obj["id"]).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            cv2.drawContours(image, contours, -1, (30, 220, 60), 1)
        self.module.write_image(folder / "vis" / (Path(prediction["image_name"]).stem + ".png"), image)


class Publisher:
    def __init__(self, config, team):
        self.ready = threading.Event()
        self.failure = None
        self.rejected = {}
        self.lock = threading.Lock()
        transport = config.get("transport", "websockets")
        protocol = mqtt.MQTTv5 if config.get("protocol", 5) == 5 else mqtt.MQTTv311
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                  client_id=f"{team}:python-{uuid.uuid4().hex[:12]}",
                                  transport=transport, protocol=protocol, reconnect_on_failure=False)
        key_env = config.get("key_env")
        key = os.environ[key_env] if key_env else ""
        if key_env:
            self.client.username_pw_set(team, key)
        if transport == "websockets":
            self.client.ws_set_options(path=config.get("ws_path", "/mqtt/"))
        if config.get("tls", True):
            self.client.tls_set_context(ssl.create_default_context())
        self.topic = config.get("topic") or f"/event/{key}/{config.get('track', 'EdgeAI')}/{config.get('event', '1')}"
        self.timeout = config.get("timeout", 15)
        self.client.connect_timeout = 10

        def connected(client, userdata, flags, reason, props):
            if reason.is_failure:
                self.failure = str(reason)
            self.ready.set()

        def published(client, userdata, mid, reason, props):
            if reason.is_failure:
                with self.lock:
                    self.rejected[mid] = str(reason)

        self.client.on_connect = connected
        self.client.on_publish = published
        self.client.connect(config["host"], config.get("port", 443), keepalive=30)
        self.client.loop_start()
        if not self.ready.wait(self.timeout) or self.failure:
            self.close()
            raise RuntimeError(f"MQTT connection failed: {self.failure or 'timeout'}")

    def publish(self, payload):
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if len(data) > 65536:
            raise ValueError("MQTT payload exceeds 64 KiB")
        info = self.client.publish(self.topic, data, qos=1, retain=False)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError("MQTT publish failed")
        info.wait_for_publish(timeout=self.timeout)
        if not info.is_published():
            raise RuntimeError("MQTT acknowledgement timeout")
        with self.lock:
            rejection = self.rejected.pop(info.mid, None)
        if rejection:
            raise RuntimeError("MQTT publish rejected: " + rejection)
        return time.perf_counter()

    def close(self):
        self.client.disconnect()
        self.client.loop_stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--config", type=Path, help="Optional JSON config override")
    parser.add_argument("--model-project", type=Path)
    parser.add_argument("--weights", type=Path)
    parser.add_argument("--variant", default="selected")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--count", type=int, default=1, help="Number of image GET requests; each can consume quota")
    parser.add_argument("--mode", choices=("train", "test"), default="train")
    parser.add_argument("--out", type=Path, default=ROOT / "outputs_api")
    parser.add_argument("--check-config", action="store_true", help="Check settings locally without requesting images")
    args = parser.parse_args()
    load_dotenv(args.env_file, override=False, encoding="utf-8-sig")
    if args.count < 1 or args.threads < 1 or args.warmup < 0:
        parser.error("Invalid count, threads or warmup")
    cfg = configuration(args.config)
    url = cfg["http_url"].rstrip("/")
    if not url:
        parser.error("Set HTTP_URL in .env")
    for section, field in ((cfg, "http_token_env"), (cfg["mqtt"], "key_env")):
        variable = section.get(field)
        if variable and not os.environ.get(variable):
            parser.error(f"Set {variable} in .env")
    project_setting = args.model_project or os.environ.get("MODEL_PROJECT") or ROOT
    project = Path(project_setting).resolve()
    weights_setting = args.weights or os.environ.get("MODEL_WEIGHTS")
    weights = Path(weights_setting).resolve() if weights_setting else project / "weights"
    if not (project / "infer.py").is_file() or not weights.is_dir():
        parser.error(f"Expected {project / 'infer.py'} and weights directory {weights}")
    if args.check_config:
        print("Configuration ready. Model paths and required credentials are present. No network request made.")
        return
    os.environ["YOLO_AUTOINSTALL"] = "false"
    os.environ["YOLO_OFFLINE"] = "true"
    os.environ["OMP_NUM_THREADS"] = str(args.threads)
    os.environ["MKL_NUM_THREADS"] = str(args.threads)
    import cv2
    cv2.setNumThreads(1)
    model = Model(project, weights, args.variant, args.threads)
    # Exercise inference before any quota-consuming GET.
    dummy = io.BytesIO()
    Image.new("RGB", (640, 640), (114, 114, 114)).save(dummy, format="JPEG")
    for _ in range(args.warmup):
        model.predict(dummy.getvalue(), "warmup.jpg")
    publisher = Publisher(cfg["mqtt"], cfg["team_id"])
    session = requests.Session()
    if cfg.get("http_token_env"):
        session.headers["Authorization"] = "Bearer " + os.environ[cfg["http_token_env"]]
    try:
        # Authenticate and warm HTTP connection using the non-quota stats endpoint.
        if cfg.get("preflight_stats", True):
            stats = session.get(url + "/api/v1/stats", timeout=(10, 15))
            stats.raise_for_status()
        with ThreadPoolExecutor(max_workers=2) as pool:
            for _ in range(args.count):
                if not publisher.client.is_connected():
                    raise RuntimeError("MQTT disconnected before requesting an image")
                started = time.perf_counter()
                response = session.get(url + "/api/v1/image", params={"mode": args.mode}, timeout=(10, 30))
                response.raise_for_status()
                received = time.perf_counter()
                image_id = response.headers["X-Image-ID"]
                name = response.headers["X-Image-Name"]
                if Path(name.replace("\\", "/")).name != name or name in ("", ".", ".."):
                    raise ValueError("Invalid X-Image-Name")
                # Use generated local folder names so server IDs cannot escape the output directory.
                folder = args.out / uuid.uuid4().hex
                try:
                    prediction, rgb, labels = model.predict(response.content, name)
                    inferred = time.perf_counter()
                    counts = dict.fromkeys(CLASSES, 0)
                    for obj in prediction["objects"]:
                        counts[obj["class"]] += 1
                    total = sum(counts.values())
                    payload = dict(image=name, latency_ms=round((inferred-received)*1000),
                                   counts=counts,
                                   defect_ratio=round((total-counts["good"])/total, 4) if total else 0.)
                    body = dict(img_id=image_id, predictions=prediction)

                    def submit():
                        result = session.post(url + "/api/v1/submit", json=body, timeout=(10, 30))
                        result.raise_for_status()
                        return result.json(), time.perf_counter()

                    http_future = pool.submit(submit)
                    mqtt_future = pool.submit(publisher.publish, payload)
                    errors = {}
                    try:
                        server, submitted = http_future.result()
                    except Exception as exc:
                        errors["http"] = str(exc)
                        server, submitted = None, None
                    try:
                        acknowledged = mqtt_future.result()
                    except Exception as exc:
                        errors["mqtt"] = str(exc)
                        acknowledged = None
                    report = dict(img_id=image_id, image=name, mode=response.headers.get("X-Mode"),
                                  download_ms=(received-started)*1000, inference_ms=(inferred-received)*1000,
                                  receive_to_mqtt_ack_ms=(acknowledged-received)*1000 if acknowledged else None,
                                  request_to_mqtt_ack_ms=(acknowledged-started)*1000 if acknowledged else None,
                                  receive_to_http_response_ms=(submitted-received)*1000 if submitted else None,
                                  server_http_response=server, errors=errors)
                    # Files and previews are generated after dispatch and broker acknowledgement.
                    folder.mkdir(parents=True, exist_ok=True)
                    for subdir in ("seg_mask", "vis", "mqtt"):
                        (folder/subdir).mkdir()
                    (folder/name).write_bytes(response.content)
                    save(folder/"predictions.json", [prediction])
                    save(folder/"results.json", body)
                    save(folder/"mqtt"/(Path(name).stem+".json"), payload)
                    save(folder/"timings.json", report)
                    model.artifacts(folder, prediction, rgb, labels)
                    print(json.dumps(report), flush=True)
                    if errors:
                        raise RuntimeError(f"Delivery incomplete. See {folder / 'timings.json'}")
                except BaseException:
                    folder.mkdir(parents=True, exist_ok=True)
                    (folder/name).write_bytes(response.content)
                    save(folder/"received.json", dict(img_id=image_id, image=name, mode=args.mode))
                    raise
    finally:
        session.close()
        publisher.close()


if __name__ == "__main__":
    main()
