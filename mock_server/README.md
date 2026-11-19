# เว็บจำลอง Day 3

เว็บให้เลือกอัปโหลดภาพ แล้วแสดงภาพต้นฉบับ overlay จากโมเดล ผล HTTP รายเมล็ด และ counts จาก MQTT เปรียบเทียบจำนวนทั้งสองช่องทางอัตโนมัติ มี MQTT broker TCP จริงฝังใน server.py และ subscriber ของเว็บ

## ติดตั้ง

จากโฟลเดอร์ team_41_day3 ใช้ environment ที่มี dependency โมเดลซึ่งต้องการรัน แล้วติดตั้งส่วนเว็บเพิ่ม

```bash
python -m pip install -r requirements-mock.txt
```

requirements-mock.txt ครอบคลุม HTTP server, MQTT broker และตัวรับส่ง ส่วนไลบรารีโมเดลให้ใช้ชุดเดียวกับโปรเจกต์โมเดลที่ทดสอบไว้แล้ว

## รันในเครื่องเดียวกัน

Terminal หน้าต่างแรก

```bash
python mock_server/server.py
```

เปิด http://127.0.0.1:8088 เลือก JPG/PNG ได้หลายภาพแล้วกดอัปโหลด จากนั้น Terminal หน้าต่างที่สอง เรียก client บน environment ที่รันโมเดลได้ เช่น Windows

```powershell
python mock_server/run_client.py --server http://127.0.0.1:8088 --model-project C:/DAY3/TesaModelTest
```

run_client.py ดึง config และชุดภาพล่าสุดจากเว็บ แล้วเรียก client.py ซึ่งจะเรียก infer.py จาก model-project พร้อม weights ของโปรเจกต์นั้น ต้องใช้ CLI รูปแบบ --input --weights --out แบบ Day 2 ถ้าต้องการลองรุ่นอื่นใน TesaModelTest ให้เพิ่ม argument เช่น

```powershell
python mock_server/run_client.py --server http://127.0.0.1:8088 --model-project C:/DAY3/TesaModelTest --weights C:/DAY3/TesaModelTest/models/yolo_640_ncnn
```

หน้าเว็บ refresh ผลทุก 1.5 วินาที เมื่อส่งครบจะแสดง HTTP และ MQTT รับแล้ว พร้อมจำนวนตรงกันของแต่ละภาพ ผล class คือค่าจากโมเดลจริง เว็บไม่ได้ประเมินกับ GT

## ใช้ Pi รันโมเดลและ Windows เปิดเว็บ

เริ่มเว็บบน Windows ให้รับจาก LAN

```powershell
python mock_server/server.py --host 0.0.0.0
ipconfig
```

ดู IPv4 ของ Windows แล้วใช้เลขจริงแทน WINDOWS_IP เปิด http://WINDOWS_IP:8088 จาก browser เพื่ออัปโหลดภาพ บน Pi ให้เปิด environment ที่รันโมเดลได้ cd เข้า team_41_day3 แล้วใช้

```bash
python mock_server/run_client.py --server http://WINDOWS_IP:8088 --model-project /home/tgr25/Desktop/TESA2026/TGR-41_Day3/PepareModelDay3-Tesa2026
```

พาธ model-project ให้ใช้โฟลเดอร์ที่มี infer.py และ weights จริง Pi ต้องเข้าถึง TCP 8088 สำหรับ HTTP และ 18883 สำหรับ MQTT บนเครื่อง Windows ได้ เซิร์ฟเวอร์นี้ใช้ anonymous MQTT และ HTTP เพื่อซ้อมในเครื่องหรือเครือข่ายที่เราใช้งานร่วมกัน

## ข้อมูลจำลองและไฟล์ผล

- HTTP GET /api/config ให้ค่า URL กับ topic ของ batch ล่าสุด
- HTTP GET /api/batches/<batch>/images.zip ส่งชุดภาพ
- HTTP POST /api/batches/<batch>/results รับ multipart fields ชื่อ results และ file
- MQTT TCP port 18883 topic day3/mock/<batch>
- mock_server/runtime/<batch>/ เก็บภาพ ผล HTTP และ ZIP ที่เว็บได้รับ
- mock_server/runtime/client_<id>/ เก็บ input, outputs และ delivery receipts ของ client แต่ละรอบ
- สถานะ batch ของหน้าเว็บอยู่ในหน่วยความจำ เมื่อ restart server ให้อัปโหลดชุดภาพใหม่

เดโมกำหนด defect เป็นทุกคลาสยกเว้น good และใช้เวลารวม inference CLI หารจำนวนภาพเพื่อสร้าง latency_ms เกณฑ์จริงของผู้จัดต้องกำหนดตามประกาศ การเปิดเดโมไม่เปลี่ยน config สำหรับส่งกรรมการ

ทดสอบบน Windows แล้วกับ YOLO26n-seg ONNX 640 จำนวน 2 ภาพ รับและส่ง HTTP สำเร็จ MQTT รับ payload ทั้งสองภาพ counts ตรงกับ objects และหน้าเว็บส่งไฟล์ overlay ได้ครบ โปรเจกต์หลักยังเชื่อมโมเดลผ่าน adapter โดยใช้ weights ภายนอกในระหว่างซ้อม
