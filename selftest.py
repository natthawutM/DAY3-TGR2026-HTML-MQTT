"""Local rehearsal using a ZIP HTTP server, HTTP upload and a minimal MQTT QoS 1 broker."""
import argparse
import io
import json
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import zipfile

from PIL import Image
import numpy as np

ROOT = Path(__file__).resolve().parent


def fixture_infer(output):
    output.mkdir(parents=True, exist_ok=True)
    (output / "seg_mask").mkdir(exist_ok=True)
    labels = np.zeros((16, 16), np.uint16)
    labels[2:6, 2:6] = 1
    labels[9:13, 9:13] = 2
    Image.fromarray(labels).save(output / "seg_mask/sample.png")
    objects = [
        {"id": 1, "class": "good", "polygon": [[2, 2], [5, 2], [5, 5], [2, 5]]},
        {"id": 2, "class": "broken", "polygon": [[9, 9], [12, 9], [12, 12], [9, 12]]}]
    (output / "predictions.json").write_text(json.dumps([{"image_name": "sample.png", "objects": objects}]))
    (output / "latency.json").write_text(json.dumps({"sample.png": 12.5}))


def read_exact(connection, size):
    data = b""
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise EOFError("MQTT socket closed")
        data += chunk
    return data


def mqtt_broker(server, received, errors):
    try:
        server.settimeout(30)
        with server.accept()[0] as connection:
            connection.settimeout(30)
            while True:
                first = read_exact(connection, 1)[0]
                length = shift = 0
                while True:
                    value = read_exact(connection, 1)[0]
                    length += (value & 127) << shift
                    shift += 7
                    if not value & 128:
                        break
                body = read_exact(connection, length)
                packet = first >> 4
                if packet == 1:
                    connection.sendall(b"\x20\x02\x00\x00")
                elif packet == 3:
                    size = struct.unpack("!H", body[:2])[0]
                    topic = body[2:2+size].decode()
                    qos = (first >> 1) & 3
                    offset = 2 + size
                    identifier = body[offset:offset+2] if qos else b""
                    payload = json.loads(body[offset+(2 if qos else 0):])
                    received.append((topic, payload))
                    if qos == 1:
                        connection.sendall(b"\x40\x02" + identifier)
                elif packet == 12:
                    connection.sendall(b"\xd0\x00")
                elif packet == 14:
                    break
    except Exception as error:
        errors.append(str(error))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-infer", type=Path)
    args = parser.parse_args()
    if args.fixture_infer:
        fixture_infer(args.fixture_infer)
        return
    image = io.BytesIO()
    Image.new("RGB", (16, 16), "beige").save(image, format="PNG")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("nested/sample.png", image.getvalue())
    uploads = []
    received = []
    errors = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(archive.getvalue())

        def do_POST(self):
            uploads.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"accepted":true}')

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    mqtt = socket.socket()
    mqtt.bind(("127.0.0.1", 0))
    mqtt.listen()
    thread = threading.Thread(target=mqtt_broker, args=(mqtt, received, errors), daemon=True)
    thread.start()
    threading.Thread(target=http.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="day3_integration_") as temp:
        folder = Path(temp)
        cfg = json.loads((ROOT / "config.example.json").read_text())
        # Fixture definition only. The organizer's defect class policy still needs confirmation.
        cfg["defect_classes"] = ["broken"]
        cfg["download"]["url"] = f"http://127.0.0.1:{http.server_port}/images.zip"
        cfg["submit"]["url"] = f"http://127.0.0.1:{http.server_port}/results"
        cfg["mqtt"].update(host="127.0.0.1", port=mqtt.getsockname()[1], topic="test/day3")
        cfg["inference"]["command"] = ["{python}", str(Path(__file__).resolve()),
                                       "--fixture-infer", "{out}"]
        config = folder / "config.json"
        config.write_text(json.dumps(cfg))
        output = folder / "outputs"
        command = [sys.executable, str(ROOT / "client.py"), "--config", str(config),
                   "--input", str(folder / "input"), "--out", str(output)]
        try:
            subprocess.run(command, check=True, timeout=60)
            thread.join(timeout=5)
            assert not errors, errors
            assert len(uploads) == 1 and b"team_41_Day3.zip" in uploads[0] and b"results.json" in uploads[0]
            assert len(received) == 1
            topic, payload = received[0]
            assert topic == "test/day3" and payload["counts"]["broken"] == 1 and payload["counts"]["good"] == 1
            assert sum(payload["counts"].values()) == 2 and len(payload["counts"]) == 7
            assert payload["latency_ms"] == 12.5 and payload["defect_ratio"] == .5
            receipt = json.loads((output / "delivery.json").read_text())
            assert receipt["http"]["status"] == "http_accepted" and receipt["mqtt"]["images"] == 1
            with zipfile.ZipFile(output / "team_41_Day3.zip") as bundle:
                names = bundle.namelist()
                assert "team_41_day3/client.py" in names
                assert "team_41_day3/outputs/seg_mask/sample.png" in names
                assert not any(n.endswith("/config.json") for n in names)
            # Wrong count/ID/polygon correspondence must stop before sending.
            predictions = output / "predictions.json"
            rows = json.loads(predictions.read_text())
            rows[0]["objects"][0]["class"] = "Parchment"
            predictions.write_text(json.dumps(rows))
            result = subprocess.run(command + ["--skip-download", "--skip-infer", "--dry-run"],
                                    capture_output=True, text=True, timeout=20)
            assert result.returncode != 0 and "Unknown lowercase class" in result.stderr
            assert len(uploads) == 1
            print("PASS HTTP ZIP download, inference CLI adapter, PNG/JSON validation, multipart upload,")
            print("MQTT QoS 1 acknowledgement, seven-class counts, defect ratio, ZIP packaging and invalid class rejection")
        finally:
            http.shutdown()
            mqtt.close()


if __name__ == "__main__":
    main()
