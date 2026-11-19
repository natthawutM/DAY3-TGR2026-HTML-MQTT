# team_41_day3

API ผู้จัดประกาศแล้ว ใช้ [client_api.py](client_api.py) สำหรับรับภาพทีละภาพผ่าน /api/v1/image และส่ง /api/v1/submit พร้อม MQTT WebSocket TLS อ่านคำสั่งตั้งค่าและลด latency ใน [API_CLIENT.md](API_CLIENT.md) ตัว client.py ด้านล่างเป็น adapter สำหรับรอบจำลอง ZIP/multipart ที่เตรียมไว้ก่อนประกาศ

ระบบเชื่อมต่อสำหรับ TESA Day 3 รับภาพทาง HTTP เรียกโมเดลผ่าน CLI และส่งผลทาง HTTP กับ MQTT จาก client.py ชุดเดียว ชื่อคลาสใช้ภาษาอังกฤษตัวเล็กทั้ง 7 ได้แก่ black, broken, good, husk, immature, parchment และ shell

## สถานะที่เตรียมไว้

- รับภาพจาก HTTP GET ที่คืน ZIP และแยกเฉพาะ JPG/JPEG/PNG ไปที่ input/ รองรับ ZIP ที่มีโฟลเดอร์ซ้อนและป้องกันชื่อภาพซ้ำ
- เรียกคำสั่ง inference ที่ตั้งค่าใน config แล้วตรวจ predictions.json กับ PNG ทุกภาพก่อนส่งผล
- ตรวจจำนวน ID ชื่อคลาส ขนาด mask หลัง EXIF orientation และ polygon ที่วาดกลับมาได้ตรงกับ mask
- สร้าง results.json แบบเดียวกับ predictions.json และ MQTT JSON ภาพละไฟล์ มี counts ครบ 7 key
- เริ่ม HTTP POST กับ MQTT พร้อมกันหลังสร้างผลครบ HTTP รองรับ multipart หรือ JSON พร้อม endpoint ส่ง ZIP แยก
- MQTT รองรับ username/password, TLS และ QoS เก็บผล publish ที่เสร็จแล้วรายภาพ
- สร้าง team_41_Day3.zip ตามโครงสร้าง repo พร้อม outputs
- มี selftest.py ทดลองทั้ง HTTP และ MQTT บน localhost โดยใช้ผล inference จำลองสองเมล็ด

ผู้จัดยังต้องประกาศ URL, headers/token, รูปแบบรับภาพและส่งไฟล์, broker, topic, credentials และนิยามคลาสบกพร่อง รูปแบบ ZIP กับ multipart ที่เตรียมไว้เป็นตัวเลือกเริ่มต้นของ adapter หากสัญญา API ต่างออกไป ให้ปรับ download() และ post_http() ตามเอกสารจริง

## โมเดลสำหรับลองรันบน Pi

weights/ มี A0 พร้อม classifier ที่ปรับด้วยข้อมูล Day 3 ในรูป ONNX FP32 ขนาด 512 ใช้ watershed boundary 0.425 และ min_marker 20 ค่าเหล่านี้อยู่ใน weights/manifest.json ติดตั้งด้วย `python -m pip install -r requirements.txt` ใน environment บน Pi

`weights/best.pt` คือ checkpoint PyTorch ของตัวแยกเมล็ด A0 และ `weights/classifier.pt` คือ checkpoint ของ classifier ที่ฝึกเพิ่มด้วยข้อมูล Day 3 คำสั่ง inference โหลด `model.onnx` และ `classifier.onnx` ตาม manifest

คำสั่งที่ client จะเรียกต้องรองรับรูปแบบ

```bash
python infer.py --input input --weights weights --out outputs
```

สำหรับ client ตาม API ผู้จัด ให้ใช้ `--model-project . --weights weights` ตามวิธีตั้งค่าใน [API_CLIENT.md](API_CLIENT.md) เมื่อต้องการโหลดโมเดลจากโฟลเดอร์นี้

ตั้ง inference.command ใน config.json เป็นรายการ argument

```json
["{python}", "infer.py", "--input", "{input}", "--weights", "{weights}", "--out", "{out}"]
```

{python} ใช้ interpreter เดียวกับ client.py ส่วน {input}, {weights}, {out} จะถูกแทนด้วย absolute path การเรียกผ่าน subprocess ใช้รายการ argument

ผลจากโมเดลต้องมี outputs/seg_mask/<ชื่อภาพ>.png และ outputs/predictions.json ตามสไลด์ รวมถึง outputs/vis/ สำหรับแพ็กเกจส่ง โมเดลควรบันทึก outputs/latency.json ที่มีเวลา milliseconds รายภาพ เช่น

```json
{"test_01.jpg": 123.4, "test_02.jpg": 145.6}
```

กำหนด latency_mode เป็น per_image เพื่อใช้เวลานี้ หากกรรมการยืนยันการใช้เวลารวม CLI หารจำนวนภาพ ให้ตั้ง pipeline_average และ client จะจับเวลา subprocess พร้อมเก็บแหล่งที่มาของเวลาใน latency_source.json เมื่อใช้ --skip-infer ต้องมี latency.json ของผลชุดนั้น

## ติดตั้งบน Pi

```bash
cd team_41_day3
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp config.example.json config.json
```

requirements ชุดนี้ครอบคลุมตัวรับส่งและตรวจผล โมเดลที่นำมาเชื่อมต้องเพิ่มไลบรารีของ runtime นั้น

## ตั้งค่าก่อนส่งจริง

แก้ config.json ตามประกาศผู้จัด

| ส่วน | ค่าที่ต้องใส่ |
|---|---|
| download | url, format และ headers หรือ token_env ที่ต้องใช้รับภาพ |
| inference | command ของโมเดล |
| submit | url, mode, results_field, zip_field และ form_fields ตามชื่อจริงที่ API รับ |
| submit แบบ json | ตั้ง zip_url สำหรับอัปโหลด ZIP อีกคำขอ |
| mqtt | host, port, topic, username_env, password_env, tls และ ca_cert หากใช้ |
| defect_classes | รายชื่อคลาสที่ผู้จัดยืนยันว่าเป็นเมล็ดบกพร่อง |
| latency_mode | per_image หรือ pipeline_average ตามเกณฑ์ผู้จัด |

defect_classes เริ่มต้นเป็น null เพื่อให้ต้องระบุเกณฑ์จริง สูตรคือจำนวน objects ที่อยู่ในรายชื่อคลาสบกพร่องหารจำนวน objects ทั้งหมด ภาพที่หาเมล็ดได้ศูนย์ชิ้นให้ defect_ratio เป็น 0.0 โดยสมมติฐานนี้บันทึกใน latency_source.json

ตัวอย่างการอ้างอิง credentials ผ่าน environment variables ให้ตั้ง username_env เป็น MQTT_USERNAME และ password_env เป็น MQTT_PASSWORD ใน config ก่อน

```bash
export MQTT_USERNAME='ค่าจากผู้จัด'
export MQTT_PASSWORD='ค่าจากผู้จัด'
```

HTTP token_env ใช้ชื่อ environment variable พร้อม token_header และ token_prefix ถ้าผู้จัดกำหนด header ที่ต่างจาก Authorization: Bearer สามารถเพิ่มสองคีย์นี้ใน config ได้ topic รองรับ {image} หรือ {stem} เมื่อผู้จัดกำหนดให้ชื่อภาพอยู่ใน topic

## ซ้อมในเครื่องก่อนโมเดลพร้อม

มีเว็บอัปโหลดภาพและ MQTT broker จำลองใน mock_server/ ใช้ python mock_server/server.py เพื่อเปิดเว็บที่ http://127.0.0.1:8088 แล้วเรียก python mock_server/run_client.py กับ --model-project ของโมเดลที่พร้อมใช้งาน อ่านคำสั่งติดตั้งและรันระหว่าง Windows กับ Pi ใน [คู่มือเว็บจำลอง](mock_server/README.md)

```bash
python selftest.py
```

สร้าง HTTP server และ MQTT broker จำลองที่ 127.0.0.1 ใช้ port ว่างและโฟลเดอร์ชั่วคราว ทดสอบ ZIP download, inference adapter, multipart POST, MQTT QoS 1 PUBACK, การนับและ polygon รวมถึงปฏิเสธคลาสตัวใหญ่ ค่า defect_classes ในการทดสอบคือ broken สำหรับ fixture สองเมล็ด

ผลจากการทดสอบบน Windows สำเร็จแล้ว การเชื่อมต่อระบบกรรมการต้องทดสอบกับรายละเอียดจริงเมื่อประกาศ

## เตรียม payload จากผลโมเดลที่มีอยู่

นำภาพไว้ใน input/ นำผล PNG, predictions.json และ latency.json ของภาพชุดเดียวกันไว้ใน outputs/ แล้วตั้ง defect_classes ตามเกณฑ์ที่ยืนยัน

```bash
python client.py --skip-download --skip-infer --dry-run
```

จะได้ results.json, MQTT JSON และ ZIP สำหรับตรวจในเครื่อง ก่อนส่งให้ยืนยันผลทุกภาพและไฟล์ weights ในแพ็กเกจ

## รันรับภาพและส่งผลจริง

หลังใส่ config และเชื่อมโมเดลแล้ว

```bash
python client.py --config config.json --input input_round1 --out outputs_round1
```

ดาวน์โหลดแต่ละรอบลงโฟลเดอร์ input ใหม่เพื่อระบุขอบเขตชุดภาพชัดเจน ถ้ามีภาพและผลอยู่แล้ว ให้ส่งได้ด้วย

```bash
python client.py --config config.json --input input_round1 --out outputs_round1 --skip-download --skip-infer
```

outputs มีโครงสร้าง

```text
outputs_round1/
  seg_mask/
  predictions.json
  results.json
  mqtt/
  vis/
  team_41_Day3.zip
  latency_source.json
  delivery.json
  mqtt_receipts.json
```

delivery.json บันทึกผลของทั้ง HTTP และ MQTT ถ้า HTTP สำเร็จแต่ MQTT ล้มเหลว ผลของ HTTP ยังถูกเก็บไว้ mqtt_receipts.json เก็บรายชื่อภาพที่ publish เสร็จตาม QoS การยืนยันจาก broker เป็นผลระดับ protocol ส่วนระบบกรรมการต้องยืนยันการประมวลผล payload ตาม API จริง การเรียกส่งซ้ำอาจเกิดข้อมูลซ้ำได้ โดยเฉพาะหลังเครือข่ายขาดช่วงหลัง server รับข้อมูลแล้ว ต้องใช้กติกา request ID หรือ dedup ของผู้จัดเมื่อตกลงสัญญา API

ZIP มีซอร์สตามโครงสร้างส่งงาน weights และ outputs ที่จำเป็น config.example.json ใช้เป็นแม่แบบ ส่วน credentials จริงอ้างผ่าน environment variables ไฟล์ที่ควรเก็บบน GitHubคือซอร์ส README requirements และ weights หลังเลือกรุ่นแล้ว
