"""Local rehearsal server with HTTP task exchange and an embedded MQTT broker."""
import argparse
import asyncio
from email import policy
from email.parser import BytesParser
import io
import json
from pathlib import Path
import threading
import time
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from PIL import Image
import paho.mqtt.client as mqtt
from amqtt.broker import Broker

ROOT = Path(__file__).resolve().parent
CLASSES = ("black", "broken", "good", "husk", "immature", "parchment", "shell")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--mqtt-port", type=int, default=18883)
    args = parser.parse_args()
    lock = threading.RLock()
    batches = {}
    tasks = {}
    latest = None
    ready = threading.Event()
    broker_errors = []
    shutdown = threading.Event()
    storage = ROOT / "runtime"
    storage.mkdir(exist_ok=True)

    def broker_thread():
        async def serve():
            broker = Broker({
                "listeners": {"default": {"type": "tcp", "bind": f"{args.host}:{args.mqtt_port}"}},
                "plugins": {"amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}}})
            try:
                await broker.start()
                ready.set()
                while not shutdown.is_set():
                    await asyncio.sleep(.25)
            finally:
                await broker.shutdown()
        try:
            asyncio.run(serve())
        except Exception as exc:
            broker_errors.append(str(exc))
            ready.set()

    threading.Thread(target=broker_thread, daemon=True).start()
    if not ready.wait(15) or broker_errors:
        raise RuntimeError(f"Broker start failed: {broker_errors}")

    subscriber = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    def on_connect(client, userdata, flags, reason, properties):
        client.subscribe("day3/mock/+", qos=1)

    def on_message(client, userdata, message):
        try:
            batch_id = message.topic.rsplit("/", 1)[-1]
            payload = json.loads(message.payload)
            if set(payload["counts"]) != set(CLASSES):
                raise ValueError("Expected seven lowercase class keys")
            if any(type(v) is not int or v < 0 for v in payload["counts"].values()):
                raise ValueError("Counts must be nonnegative integers")
            with lock:
                batch = batches[batch_id]
                if payload["image"] not in batch["images"]:
                    raise ValueError("Unexpected image")
                batch["mqtt"][payload["image"]] = payload
                matching = [task for task in tasks.values() if task["batch"] == batch_id
                            and task["name"] == payload["image"] and "sent" in task]
                if matching:
                    task = matching[-1]
                    task["mqtt_received_ms"] = (time.perf_counter() - task["sent"]) * 1000
                    payload["mock_server_roundtrip_ms"] = task["mqtt_received_ms"]
                batch["events"].append(f"MQTT received · {payload['image']}")
        except Exception as error:
            print(f"Rejected MQTT message: {error}", flush=True)

    subscriber.on_connect = on_connect
    subscriber.on_message = on_message
    subscriber.connect("127.0.0.1" if args.host == "0.0.0.0" else args.host, args.mqtt_port)
    subscriber.loop_start()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, pattern, *values):
            print(pattern % values, flush=True)

        def reply(self, value, content_type="application/json", status=200):
            if not isinstance(value, bytes):
                value = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(value)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(value)

        def parts(self):
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size <= 100 * 1024 * 1024:
                raise ValueError("Request must be between 1 byte and 100 MB")
            raw = self.rfile.read(size)
            message = BytesParser(policy=policy.default).parsebytes(
                ("Content-Type: " + self.headers["Content-Type"] + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + raw)
            if not message.is_multipart():
                raise ValueError("Expected multipart/form-data")
            return [(part.get_param("name", header="content-disposition"), part.get_filename(),
                     part.get_payload(decode=True)) for part in message.iter_parts()]

        def do_GET(self):
            try:
                path = urlparse(self.path).path
                if path == "/":
                    return self.reply((ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
                with lock:
                    if path == "/api/v1/stats":
                        return self.reply({"requests": len(tasks)})
                    if path == "/api/v1/image":
                        if latest is None:
                            return self.reply({"error": "Upload images first"}, status=503)
                        batch = batches[latest]
                        used = {t["name"] for t in tasks.values() if t["batch"] == latest}
                        available = [n for n in batch["images"] if n not in used]
                        if not available:
                            return self.reply({"error": "Batch exhausted"}, status=403)
                        name = available[0]
                        image_id = "img_" + uuid.uuid4().hex
                        data = (storage / latest / "images" / name).read_bytes()
                        self.send_response(200)
                        self.send_header("Content-Type", "image/png" if name.lower().endswith(".png") else "image/jpeg")
                        self.send_header("Content-Length", str(len(data)))
                        self.send_header("X-Image-ID", image_id)
                        self.send_header("X-Image-Name", name)
                        self.send_header("X-Mode", "train")
                        self.end_headers()
                        self.wfile.write(data)
                        self.wfile.flush()
                        tasks[image_id] = {"batch": latest, "name": name, "sent": time.perf_counter()}
                        batch["events"].append("Official API GET · " + name)
                        return
                    if path == "/api/state":
                        batch = batches.get(latest)
                        if batch is None:
                            return self.reply({"batch": None})
                        records = []
                        for name in batch["images"]:
                            objects = batch["results"].get(name)
                            payload = batch["mqtt"].get(name)
                            counts = dict.fromkeys(CLASSES, 0)
                            if objects is not None:
                                for obj in objects:
                                    counts[obj["class"]] += 1
                            records.append({
                                "name": name, "url": f"/media/{latest}/images/{name}",
                                "overlay": f"/media/{latest}/vis/{Path(name).stem}.png" if name in batch["overlays"] else None,
                                "objects": objects, "mqtt": payload,
                                "consistent": counts == payload["counts"] if objects is not None and payload else None})
                        return self.reply({"batch": latest, "images": records, "events": batch["events"],
                                           "zip_received": batch["zip_received"]})
                    if path == "/api/config":
                        if latest is None:
                            return self.reply({"error": "Upload images first"}, status=409)
                        host = self.headers["Host"]
                        hostname = host.rsplit(":", 1)[0]
                        base = "http://" + host
                        config = json.loads((ROOT.parent / "config.example.json").read_text())
                        config.update(latency_mode="pipeline_average",
                                      defect_classes=["black", "broken", "husk", "immature", "parchment", "shell"])
                        config["download"]["url"] = base + f"/api/batches/{latest}/images.zip"
                        config["submit"]["url"] = base + f"/api/batches/{latest}/results"
                        config["mqtt"].update(host=hostname, port=args.mqtt_port, topic=f"day3/mock/{latest}")
                        return self.reply(config)
                    elements = path.strip("/").split("/")
                    if len(elements) == 4 and elements[:2] == ["api", "batches"] and elements[3] == "images.zip":
                        batch = batches[elements[2]]
                        data = io.BytesIO()
                        with zipfile.ZipFile(data, "w") as archive:
                            for name in batch["images"]:
                                archive.write(storage / elements[2] / "images" / name, name)
                        batch["events"].append("HTTP GET · downloaded images")
                        return self.reply(data.getvalue(), "application/zip")
                    if len(elements) == 4 and elements[0] == "media" and elements[2] in ("images", "vis"):
                        batch_id, kind, name = elements[1:]
                        if batch_id not in batches or Path(name).name != name:
                            raise ValueError("Invalid image path")
                        file = storage / batch_id / kind / name
                        return self.reply(file.read_bytes(), "image/png" if file.suffix.lower() == ".png" else "image/jpeg")
                self.reply({"error": "Not found"}, status=404)
            except (OSError, ValueError, KeyError) as error:
                self.reply({"error": str(error)}, status=400)

        def do_POST(self):
            nonlocal latest
            try:
                path = urlparse(self.path).path
                if path == "/api/v1/submit":
                    size = int(self.headers.get("Content-Length", 0))
                    if not 0 < size <= 100 * 1024 * 1024:
                        raise ValueError("Invalid request size")
                    body = json.loads(self.rfile.read(size))
                    with lock:
                        task = tasks[body["img_id"]]
                        if task.get("submitted"):
                            return self.reply({"error": "Already submitted"}, status=409)
                        row = body["predictions"]
                        if row["image_name"] != task["name"] or any(o["class"] not in CLASSES for o in row["objects"]):
                            raise ValueError("Invalid prediction image or class")
                        task["submitted"] = True
                        duration = time.perf_counter() - task["sent"]
                        batch = batches[task["batch"]]
                        batch["results"][task["name"]] = row["objects"]
                        batch["events"].append("Official API POST · " + task["name"])
                    return self.reply({"status": "success", "img_id": body["img_id"],
                                       "image_name": task["name"], "duration_seconds": duration})
                parts = self.parts()
                if path == "/api/upload":
                    prepared = {}
                    for field, filename, data in parts:
                        if field != "images":
                            continue
                        name = Path((filename or "").replace("\\", "/")).name
                        if Path(name).suffix.lower() not in (".png", ".jpg", ".jpeg"):
                            raise ValueError("Use JPG or PNG images")
                        if Path(name).stem.casefold() in {Path(n).stem.casefold() for n in prepared}:
                            raise ValueError("Image stems must be unique")
                        with Image.open(io.BytesIO(data)) as image:
                            image.verify()
                        prepared[name] = data
                    if not prepared:
                        raise ValueError("Choose at least one image")
                    batch_id = uuid.uuid4().hex
                    folder = storage / batch_id / "images"
                    folder.mkdir(parents=True)
                    for name, data in prepared.items():
                        (folder / name).write_bytes(data)
                    with lock:
                        batches[batch_id] = {"images": list(prepared), "results": {}, "mqtt": {},
                                             "overlays": set(), "zip_received": False,
                                             "events": [f"Uploaded {len(prepared)} images"]}
                        latest = batch_id
                    return self.reply({"batch": batch_id})
                elements = path.strip("/").split("/")
                if len(elements) != 4 or elements[:2] != ["api", "batches"] or elements[3] != "results":
                    return self.reply({"error": "Not found"}, status=404)
                batch_id = elements[2]
                received = {field: data for field, filename, data in parts}
                rows = json.loads(received["results"])
                with lock:
                    batch = batches[batch_id]
                    if len(rows) != len(batch["images"]) or {r["image_name"] for r in rows} != set(batch["images"]):
                        raise ValueError("Results must cover every image")
                    for row in rows:
                        if any(obj["class"] not in CLASSES for obj in row["objects"]):
                            raise ValueError("Unknown class")
                    overlays = {}
                    with zipfile.ZipFile(io.BytesIO(received["file"])) as archive:
                        for item in archive.infolist():
                            parts = item.filename.split("/")
                            if len(parts) >= 2 and parts[-2] == "vis" and parts[-1].lower().endswith(".png"):
                                overlays[parts[-1]] = archive.read(item)
                    folder = storage / batch_id
                    (folder / "received.zip").write_bytes(received["file"])
                    (folder / "results.json").write_bytes(received["results"])
                    (folder / "vis").mkdir(exist_ok=True)
                    for name, data in overlays.items():
                        (folder / "vis" / name).write_bytes(data)
                    batch["results"] = {r["image_name"]: r["objects"] for r in rows}
                    batch["overlays"] = {n for n in batch["images"] if Path(n).stem + ".png" in overlays}
                    batch["zip_received"] = True
                    batch["events"].append(f"HTTP POST · results and ZIP received for {len(rows)} images")
                self.reply({"accepted": True, "images": len(rows)})
            except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
                self.reply({"error": str(error)}, status=400)

    http = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Web http://{args.host}:{args.port} | MQTT {args.host}:{args.mqtt_port}", flush=True)
    print("Demo defect policy: all classes except good. Confirm the organizer policy for actual delivery.", flush=True)
    try:
        http.serve_forever()
    finally:
        http.server_close()
        subscriber.disconnect()
        subscriber.loop_stop()
        shutdown.set()


if __name__ == "__main__":
    main()
