# Client ตาม API ผู้จัดและการลด latency

อ่านตาม API_DOCS.md และ client_test.py ที่ผู้จัดแจก

client_api.py รองรับ GET /api/v1/image, POST /api/v1/submit แบบ JSON และ MQTT v5 ผ่าน WebSocket TLS port 443 path /mqtt/ ใช้ topic /event/<MQTT_KEY>/EdgeAI/1

## วิธีลดเวลารอบที่ถูกวัด

- โหลดโมเดลหนึ่งครั้งและ warm-up ก่อนขอภาพที่ใช้โควตา
- เชื่อม MQTT WSS/TLS ให้ได้รับ CONNACK ก่อน HTTP GET และรักษา connection เดิมตลอดรอบ
- ใช้ requests.Session กับ HTTP และเตรียม connection ด้วย /api/v1/stats ก่อนขอภาพ
- อ่าน image bytes เข้าโมเดลในหน่วยความจำ ประมวลผลและสร้าง polygon ที่ตรงกับ mask ก่อนส่ง
- ส่ง HTTP POST กับ MQTT QoS 1 พร้อมกัน เก็บ PUBACK และ response แยกกัน
- เขียน PNG, JSON, ภาพต้นฉบับและ overlay หลังส่งครบ ลด I/O บน SD card ในช่วงจับเวลา
- ขอภาพทีละภาพตามจำนวน --count ค่าเริ่มต้นเป็น 1 และหยุดเมื่อได้รับ HTTP error เพื่อควบคุมโควตา
- ไม่มีการส่งซ้ำอัตโนมัติหลัง timeout เพราะผู้จัดอาจรับผลแล้วและคืน 409 เมื่อส่งซ้ำ

## ตั้งค่าผ่าน .env บน Pi

ใช้ environment ที่รันโมเดลสำเร็จแล้ว จากโฟลเดอร์ team_41_day3

```bash
python -m pip install requests "paho-mqtt>=2,<3" "python-dotenv>=1,<2"
cp .env.example .env
nano .env
```

กรอก HTTP_URL, TEAM_HTTP_PASSWORD, TEAM_ID และ TEAM_MQTT_KEY ใน .env ตรวจ TEAM_ID ให้ตรง dashboard ใช้ HTTP password สำหรับล็อกอินเว็บไซต์ และ MQTT key ที่ Generate สำหรับ broker

ค่าเริ่มต้นโหลด infer.py และ weights/ ที่อยู่ข้าง client_api.py เว้น MODEL_PROJECT และ MODEL_WEIGHTS ว่างได้ ใช้สองค่านี้หรืออาร์กิวเมนต์ใน CLI เมื่อต้องการชี้ไปโมเดลในโฟลเดอร์อื่น กรณีรหัสผ่านมี # หรือช่องว่างให้ครอบค่าด้วย single quotes ตามรูปแบบ python-dotenv

ตรวจ config ในเครื่องก่อนขอภาพ โดยคำสั่งนี้ตรวจว่ามีค่าที่จำเป็นและพาธโมเดล โดยไม่แสดงค่ารหัสผ่าน

```bash
python client_api.py --check-config
```

จากนั้นซ้อม Train หนึ่งภาพ

```bash
python client_api.py --mode train --count 1 --warmup 2
```

client โหลด .env ในโฟลเดอร์โปรเจกต์โดยอัตโนมัติ ตัวแปรที่ export อยู่ใน shell มีลำดับความสำคัญสูงกว่า .env ถ้าต้องการไฟล์อื่นใช้ --env-file /path/to/file.env ส่วน --model-project และ --weights ในคำสั่งมีลำดับความสำคัญสูงกว่าค่าโมเดลใน .env

ตัวเลือก --config ยังคงรองรับ JSON สำหรับการเชื่อม mock หรือรูปแบบพิเศษ หากใช้ร่วมกัน ค่า HTTP_URL, TEAM_ID และ MQTT_* ที่มีค่าใน environment จะ override ค่าที่ตรงกันใน JSON

.env ถูก ignore โดย Git และชุด ZIP ส่วน .env.example เก็บใน Git ได้เพื่อใช้เป็นแบบกรอก คำสั่งนี้เรียกใช้งาน CLI ได้จาก directory อื่นเพราะโหลด .env ตามตำแหน่ง client_api.py

model-project ต้องมี infer.py ที่เปิด Predictor(weights, variant, threads), predict_instances และ submission_instances ตาม TesaModelTest ขณะนี้ client รองรับ interface ของแพ็กเกจนี้ หากใช้โมเดลใหม่ที่เปลี่ยน interface ให้ปรับคลาส Model ก่อนขอภาพจริง

เมื่อซ้อมผ่านให้เลือก --mode test และจำนวน --count ตามโควตาจริง การ GET ในโหมด Test ใช้สิทธิ์ของทีม แม้ inference หรือส่งผลภายหลังล้มเหลว

## ตัววัดเวลาที่บันทึก

แต่ละภาพเก็บผลใน outputs_api/<รหัสโฟลเดอร์> พร้อม timings.json

| ค่า | ความหมาย |
|---|---|
| inference_ms | จากรับ image bytes ครบจนสร้าง objects เสร็จ รวม decode, inference และ mask/polygon postprocessing |
| MQTT latency_ms | inference_ms ปัดเป็น integer ตาม API |
| receive_to_mqtt_ack_ms | รับภาพครบจน client ได้รับ MQTT PUBACK |
| request_to_mqtt_ack_ms | เริ่ม HTTP GET จนได้รับ PUBACK รวม network download |
| server_http_response.duration_seconds | เวลาที่ HTTP server ผู้จัดวัดจากส่งภาพเสร็จจนรับ JSON POST |

เวลา PUBACK เป็นเวลาที่ client ยืนยันว่าฝั่ง broker รับ publish สำเร็จ ค่า HTTP server duration วัดปลายทางที่ต่างออกไป ส่วนเวลาถึง subscriber ของผู้จัดต้องอ้างระบบผู้จัด ข้อมูลที่ผู้จัดแจ้งด้วยปากเปล่าคือ HTTP → MQTT ขณะที่ API ระบุ HTTP duration และ MQTT latency_ms แยกกัน จึงเก็บตัววัดเหล่านี้เพื่อเทียบกันได้

defect_ratio ใช้ (จำนวนทั้งหมด - good) / จำนวนทั้งหมด และปัด 4 ตำแหน่งตาม client_test.py ของผู้จัด ภาพว่างให้ 0.0 ชื่อ counts ใช้ตัวเล็กครบ 7 key และสร้างจาก objects หลังซ่อม mask

ผลลัพธ์ POST คือ {"img_id": "...", "predictions": {"image_name": "...", "objects": [...]}} ตัวอย่าง API ฉบับนี้ระบุการส่ง JSON รายภาพ ส่วนการอัปโหลด ZIP ให้ใช้ endpoint และวิธีที่ผู้จัดประกาศเพิ่มเติม
