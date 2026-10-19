# AI 辅助生成的学生项目代码；参与和整理说明见 README.md。
from flask import Flask, request, jsonify
from PIL import Image
from pydub import AudioSegment
import base64
import time
import os
import requests
import json
import copy
import edge_tts
import asyncio
import io
import datetime
import uuid

app = Flask(__name__)

# SmartMedBox 云端 API：配置、图像上传和语音服务。

def _env_int(name, default):
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except Exception:
        return default


def _env_str(name, default):
    val = os.getenv(name)
    if isinstance(val, str) and val.strip():
        return val
    return default


MAX_CONTENT_LENGTH = _env_int("MAX_CONTENT_LENGTH", 16 * 1024 * 1024)
TENCENT_API_KEY = _env_str("TENCENT_API_KEY", "")
SILICONFLOW_API_KEY = _env_str("SILICONFLOW_API_KEY", "")
TENCENT_CHAT_URL = _env_str("TENCENT_CHAT_URL", "")
SILICONFLOW_TRANSCRIPTION_URL = _env_str("SILICONFLOW_TRANSCRIPTION_URL", "")
UPLOAD_FOLDER = _env_str("UPLOAD_FOLDER", "uploads")
CONFIG_FILE = _env_str("CONFIG_FILE", "cloud_config.json")
STATUS_FILE = _env_str("STATUS_FILE", "med_status.json")  # legacy compatibility
RUNTIME_STATUS_FILE = _env_str("RUNTIME_STATUS_FILE", "runtime_status.json")
UPLOAD_RECORDS_FILE = _env_str("UPLOAD_RECORDS_FILE", "upload_records.jsonl")
SERVER_HOST = _env_str("SERVER_HOST", "0.0.0.0")
SERVER_PORT = _env_int("SERVER_PORT", 5000)
UPLOAD_AI_TIMEOUT_SEC = _env_int("UPLOAD_AI_TIMEOUT_SEC", 40)
UPLOAD_HTTP_TIMEOUT_SEC = _env_int("UPLOAD_HTTP_TIMEOUT_SEC", 60)

app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
SERVER_START_TS = time.time()

DEFAULT_ALARMS = {"早上": {"h": 8, "m": 0}, "中午": {"h": 12, "m": 30}, "晚上": {"h": 19, "m": 0}}
DEFAULT_CONFIG = {
    "alarms": DEFAULT_ALARMS,
    "last_local_edit_time": 0,
    "last_cloud_sync_time": 0,
}
DEFAULT_RUNTIME_STATUS = {
    "last_update_time": "",
    "period": "未知",
    "check_count": 0,
    "status": "暂无数据",
    "last_upload_time": "",
    "last_error": "",
    "last_image_filename": "",
    "cloud_ok": False,
}

os.makedirs(UPLOAD_FOLDER, exist_ok=True)


def _now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _load_json(path, fallback):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return copy.deepcopy(fallback)


def _save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def _append_jsonl(path, row):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _normalize_alarms(raw):
    if not isinstance(raw, dict):
        return None

    alias_map = {
        "早上": "早上",
        "morning": "早上",
        "中午": "中午",
        "noon": "中午",
        "晚上": "晚上",
        "night": "晚上",
    }
    normalized = {}
    for src, dst in alias_map.items():
        if src in raw and dst not in normalized and isinstance(raw[src], dict):
            h = raw[src].get("h")
            m = raw[src].get("m")
            if isinstance(h, int) and isinstance(m, int):
                normalized[dst] = {"h": h % 24, "m": m % 60}

    for k, v in DEFAULT_ALARMS.items():
        if k not in normalized:
            normalized[k] = {"h": v["h"], "m": v["m"]}
    return normalized


def normalize_config_payload(payload):
    if not isinstance(payload, dict):
        return None, "payload must be json object"

    if "alarms" in payload:
        raw_alarms = payload.get("alarms")
        last_local_edit_time = payload.get("last_local_edit_time", 0)
        last_cloud_sync_time = payload.get("last_cloud_sync_time", 0)
    else:
        raw_alarms = payload
        last_local_edit_time = 0
        last_cloud_sync_time = 0

    alarms = _normalize_alarms(raw_alarms)
    if not alarms:
        return None, "invalid alarms format"

    try:
        last_local_edit_time = float(last_local_edit_time)
    except Exception:
        last_local_edit_time = 0
    try:
        last_cloud_sync_time = float(last_cloud_sync_time)
    except Exception:
        last_cloud_sync_time = 0

    normalized = {
        "alarms": alarms,
        "last_local_edit_time": last_local_edit_time,
        "last_cloud_sync_time": last_cloud_sync_time,
    }
    return normalized, None


def get_saved_config():
    raw = _load_json(CONFIG_FILE, DEFAULT_CONFIG)
    normalized, _ = normalize_config_payload(raw)
    if normalized:
        return normalized
    return copy.deepcopy(DEFAULT_CONFIG)


def get_runtime_status():
    st = _load_json(RUNTIME_STATUS_FILE, DEFAULT_RUNTIME_STATUS)
    merged = copy.deepcopy(DEFAULT_RUNTIME_STATUS)
    if isinstance(st, dict):
        merged.update(st)
    return merged


def update_runtime_status(**kwargs):
    st = get_runtime_status()
    st.update(kwargs)
    st["last_update_time"] = _now_str()
    _save_json(RUNTIME_STATUS_FILE, st)
    return st


def append_upload_record(**kwargs):
    row = {"timestamp": _now_str()}
    row.update(kwargs)
    _append_jsonl(UPLOAD_RECORDS_FILE, row)


def _count_upload_records(limit=50000):
    if not os.path.exists(UPLOAD_RECORDS_FILE):
        return 0
    count = 0
    try:
        with open(UPLOAD_RECORDS_FILE, "r", encoding="utf-8") as f:
            for _ in f:
                count += 1
                if count >= limit:
                    break
    except Exception:
        return -1
    return count


def update_med_status_legacy(period, check_count, status):
    status_data = {
        "update_time": _now_str(),
        "period": period,
        "check_count": check_count,
        "status": status,
    }
    _save_json(STATUS_FILE, status_data)


def missing_key_response(key_name):
    return jsonify({"status": "error", "message": f"Missing {key_name}"}), 503


@app.route("/api/status", methods=["GET"])
def get_status():
    runtime = get_runtime_status()
    cfg = get_saved_config()
    uploads_count = _count_upload_records()
    now_ts = time.time()
    status_payload = {
        "server_time": _now_str(),
        "uptime_sec": int(max(0, now_ts - SERVER_START_TS)),
        "runtime": runtime,
        "config": cfg,
        "health": {
            "cloud_ai_keys_ready": bool(TENCENT_API_KEY and TENCENT_CHAT_URL),
            "stt_key_ready": bool(SILICONFLOW_API_KEY and SILICONFLOW_TRANSCRIPTION_URL),
            "upload_dir_ready": os.path.isdir(UPLOAD_FOLDER),
            "upload_records_count": uploads_count,
        },
        "paths": {
            "upload_folder": UPLOAD_FOLDER,
            "config_file": CONFIG_FILE,
            "runtime_status_file": RUNTIME_STATUS_FILE,
            "upload_records_file": UPLOAD_RECORDS_FILE,
        },
    }
    return jsonify(status_payload)


@app.route("/api/config", methods=["GET", "POST"])
def config_api():
    if request.method == "POST":
        payload = request.get_json(silent=True)
        normalized, err = normalize_config_payload(payload)
        if err:
            return jsonify({"status": "error", "message": err}), 400
        _save_json(CONFIG_FILE, normalized)
        # 重新读取落盘配置，返回实际保存的值。
        persisted = get_saved_config()
        return jsonify(persisted), 200

    return jsonify(get_saved_config()), 200


def audio_to_text(audio_path):
    if not SILICONFLOW_API_KEY or not SILICONFLOW_TRANSCRIPTION_URL:
        return ""
    url = SILICONFLOW_TRANSCRIPTION_URL
    headers = {"Authorization": f"Bearer {SILICONFLOW_API_KEY}"}
    try:
        with open(audio_path, "rb") as f:
            files = {"file": ("input.wav", f, "audio/wav"), "model": (None, "FunAudioLLM/SenseVoiceSmall")}
            res = requests.post(url, headers=headers, files=files, timeout=20)
        if res.status_code == 200:
            return res.json().get("text", "")
    except Exception:
        pass
    return ""


def chat_with_hunyuan(user_text):
    if not TENCENT_API_KEY or not TENCENT_CHAT_URL:
        return "云端对话服务未配置。"

    alarms = get_saved_config()["alarms"]
    now = datetime.datetime.now()
    current_minutes = now.hour * 60 + now.minute
    current_time_str = now.strftime("%H:%M")

    future_alarms = []
    for period in ["早上", "中午", "晚上"]:
        mins = alarms[period]["h"] * 60 + alarms[period]["m"]
        if mins > current_minutes:
            future_alarms.append((mins, period, alarms[period]["h"], alarms[period]["m"]))
    future_alarms.sort(key=lambda x: x[0])
    if future_alarms:
        next_med_str = f"今天{future_alarms[0][1]} {future_alarms[0][2]:02d}:{future_alarms[0][3]:02d}"
    else:
        next_med_str = f"明天早上 {alarms['早上']['h']:02d}:{alarms['早上']['m']:02d}"

    system_prompt = (
        "你叫'小药童'，是专门管理和提醒长者服药的AI助手。\n"
        f"系统事实：当前时间{current_time_str}；下一次吃药时间为{next_med_str}。\n"
        "若用户问下次服药时间，直接回答系统事实中的下一次时间，不要自行重算。\n"
        "回答简短，不超过20字。"
    )
    url = TENCENT_CHAT_URL
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {TENCENT_API_KEY}"}
    payload = {"model": "hunyuan-lite", "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_text}]}
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=20)
        if res.status_code == 200:
            return res.json()["choices"][0]["message"]["content"]
    except Exception:
        pass
    return "网络有点慢，请稍后再试。"


async def generate_edge_tts(text):
    communicate = edge_tts.Communicate(text, "zh-CN-XiaoxiaoNeural")
    audio_data = b""
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_data += chunk["data"]
    audio = AudioSegment.from_file(io.BytesIO(audio_data), format="mp3")
    return audio + 10


@app.route("/api/voice", methods=["POST"])
def handle_voice():
    if not SILICONFLOW_API_KEY:
        return missing_key_response("SILICONFLOW_API_KEY")
    if not SILICONFLOW_TRANSCRIPTION_URL:
        return missing_key_response("SILICONFLOW_TRANSCRIPTION_URL")
    if not TENCENT_API_KEY:
        return missing_key_response("TENCENT_API_KEY")
    if not TENCENT_CHAT_URL:
        return missing_key_response("TENCENT_CHAT_URL")
    try:
        payload = request.get_json(silent=True) or {}
        if "audio" not in payload:
            return jsonify({"status": "error", "message": "Missing audio"}), 400
        input_path = os.path.join(UPLOAD_FOLDER, "input.wav")
        with open(input_path, "wb") as f:
            f.write(base64.b64decode(payload["audio"]))
        user_text = audio_to_text(input_path)
        if not user_text:
            return jsonify({"status": "error", "message": "STT failed"}), 400
        ai_reply = chat_with_hunyuan(user_text)
        audio = asyncio.run(generate_edge_tts(ai_reply))
        audio = audio.set_frame_rate(16000).set_channels(1).set_sample_width(2)
        wav_io = io.BytesIO()
        audio.export(wav_io, format="wav")
        return jsonify({"status": "success", "reply_audio": base64.b64encode(wav_io.getvalue()).decode("utf-8")}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/tts_text", methods=["POST"])
def handle_tts_text():
    try:
        payload = request.get_json(silent=True) or {}
        text = payload.get("text", "")
        if not text:
            return jsonify({"status": "error", "message": "Missing text"}), 400
        audio = asyncio.run(generate_edge_tts(text))
        mp3_io = io.BytesIO()
        audio.export(mp3_io, format="mp3", bitrate="128k")
        return jsonify({"status": "success", "reply_audio": base64.b64encode(mp3_io.getvalue()).decode("utf-8")}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


def analyze_pill_with_ai(image_path):
    if not TENCENT_API_KEY:
        return None, "Missing TENCENT_API_KEY"
    if not TENCENT_CHAT_URL:
        return None, "Missing TENCENT_CHAT_URL"
    try:
        with open(image_path, "rb") as f:
            img_base64 = base64.b64encode(f.read()).decode("utf-8")
        url = TENCENT_CHAT_URL
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {TENCENT_API_KEY}"}
        prompt_text = (
            "你是医疗辅助视觉系统。请判断药盒区域内是否还有药丸。\n"
            "输出最后一行必须且只能是以下三选一：\n"
            "【视觉判定：有药丸】\n"
            "【视觉判定：无药丸】\n"
            "【视觉判定：判断不清】\n"
            "如果画面存在反光、遮挡、模糊、角度异常等情况，请输出【视觉判定：判断不清】。"
        )
        res = requests.post(
            url,
            headers=headers,
            json={
                "model": "hunyuan-vision",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt_text},
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_base64}"}},
                        ],
                    }
                ],
            },
            timeout=UPLOAD_AI_TIMEOUT_SEC,
        )
        if res.status_code != 200:
            return None, f"AI HTTP {res.status_code}"
        full_content = str(res.json()["choices"][0]["message"]["content"])
        normalized = full_content.replace(" ", "").replace("\n", "")
        if "视觉判定：无药丸" in normalized or "无药丸" in normalized:
            return "已服药", None
        if "视觉判定：有药丸" in normalized or "有药丸" in normalized:
            return "未服药", None
        if (
            "视觉判定：判断不清" in normalized
            or "判断不清" in normalized
            or "需复查" in normalized
            or "不确定" in normalized
            or "无法判断" in normalized
        ):
            return "需复查", None
        return "需复查", None
    except Exception as e:
        return None, str(e)


@app.route("/api/upload", methods=["POST"])
def upload_image():
    request_id = uuid.uuid4().hex[:12]
    started = time.time()

    def fail(stage, message, http_code, *, error="", period="", check_count=0, image=""):
        update_runtime_status(
            period=str(period),
            check_count=check_count if isinstance(check_count, int) else 0,
            status="失败",
            last_upload_time=_now_str(),
            last_error=error or message,
            last_image_filename=image,
            cloud_ok=False,
            request_id=request_id,
        )
        append_upload_record(
            request_id=request_id,
            success=False,
            stage=stage,
            error=error or message,
            period=str(period),
            check_count=check_count,
            image=image,
        )
        return jsonify({"status": "error", "message": message, "request_id": request_id}), http_code

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return fail("validate", "Invalid JSON payload", 400, error="invalid_json")

    image_b64 = payload.get("image")
    period = payload.get("period")
    raw_check_count = payload.get("check_count")

    if image_b64 is None or period is None or raw_check_count is None:
        return fail("validate", "Missing required fields", 400, error="missing_required_fields")
    if not isinstance(image_b64, str) or not image_b64.strip():
        return fail("validate", "image must be non-empty base64 string", 400, error="invalid_image_field")

    period = str(period).strip()
    if not period:
        return fail("validate", "period must be non-empty", 400, error="invalid_period")

    try:
        check_count = int(raw_check_count)
    except Exception:
        return fail("validate", "check_count must be integer", 400, error="invalid_check_count", period=period)
    if check_count < 1 or check_count > 4:
        return fail("validate", "check_count must be in [1,4]", 400, error="check_count_out_of_range", period=period, check_count=check_count)

    update_runtime_status(
        period=period,
        check_count=check_count,
        status="处理中",
        last_upload_time=_now_str(),
        last_error="",
        cloud_ok=False,
        request_id=request_id,
    )

    # Stage 1: base64 decode
    try:
        raw_b64 = image_b64.strip()
        missing_padding = len(raw_b64) % 4
        if missing_padding:
            raw_b64 += "=" * (4 - missing_padding)
        img_bytes = base64.b64decode(raw_b64)
    except Exception as e:
        return fail("decode", "BASE64 decode failed", 400, error=str(e), period=period, check_count=check_count)

    if len(img_bytes) < 1000:
        return fail("decode", "decoded image too small", 400, error="decoded_image_too_small", period=period, check_count=check_count)

    # Stage 2: image restore
    try:
        filename = os.path.join(UPLOAD_FOLDER, f"med_{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time()*1000)%1000:03d}.jpg")
        ba = bytearray(img_bytes)
        ba[0::2], ba[1::2] = ba[1::2], ba[0::2]
        img = Image.frombytes("RGB", (240, 320), bytes(ba), "raw", "BGR;16")
        img = img.rotate(90, expand=True)
        img.save(filename)
    except Exception as e:
        return fail("image_restore", "Image restore failed", 500, error=str(e), period=period, check_count=check_count)

    image_name = os.path.basename(filename)

    # Stage 3: AI analysis
    ai_reply, ai_error = analyze_pill_with_ai(filename)
    if ai_error:
        return fail("ai", "AI analysis failed", 502, error=ai_error, period=period, check_count=check_count, image=image_name)

    # Stage 4: persist result
    elapsed_ms = int((time.time() - started) * 1000)
    update_runtime_status(
        period=period,
        check_count=check_count,
        status=ai_reply,
        last_upload_time=_now_str(),
        last_error="",
        last_image_filename=image_name,
        cloud_ok=True,
        request_id=request_id,
        last_latency_ms=elapsed_ms,
    )
    update_med_status_legacy(period, check_count, ai_reply)
    append_upload_record(
        request_id=request_id,
        success=True,
        stage="done",
        period=period,
        check_count=check_count,
        ai_result=ai_reply,
        image=image_name,
        latency_ms=elapsed_ms,
    )

    return jsonify({"status": "success", "ai_result": ai_reply, "request_id": request_id, "latency_ms": elapsed_ms}), 200


def parse_schedule_with_hunyuan(user_text, context=""):
    if not TENCENT_API_KEY or not TENCENT_CHAT_URL:
        return {"status": "complete", "schedules": [], "ask_user": ""}

    system_prompt = """你是一个医嘱信息提取器。请从用户文本提取服药安排，输出严格JSON。"""
    full_input = f"[上下文]{context}\n[用户]{user_text}" if context else user_text
    url = TENCENT_CHAT_URL
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {TENCENT_API_KEY}"}
    payload = {"model": "hunyuan-lite", "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": full_input}]}
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=20)
        if res.status_code == 200:
            content = res.json()["choices"][0]["message"]["content"].strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[-1]
            if content.endswith("```"):
                content = content[:-3]
            return json.loads(content.strip())
    except Exception:
        pass
    return {"status": "complete", "schedules": [], "ask_user": ""}


@app.route("/api/stt", methods=["POST"])
def stt_api():
    if not SILICONFLOW_API_KEY:
        return missing_key_response("SILICONFLOW_API_KEY")
    if not SILICONFLOW_TRANSCRIPTION_URL:
        return missing_key_response("SILICONFLOW_TRANSCRIPTION_URL")
    try:
        payload = request.get_json(silent=True) or {}
        if "audio" not in payload:
            return jsonify({"status": "error", "message": "Missing audio data"}), 400
        audio_data = base64.b64decode(payload["audio"])
        temp_path = os.path.join(UPLOAD_FOLDER, "stt_input.wav")
        with open(temp_path, "wb") as f:
            f.write(audio_data)
        text = audio_to_text(temp_path)
        if not text:
            return jsonify({"status": "error", "text": ""})
        return jsonify({"status": "success", "text": text})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/parse_med_schedule", methods=["POST"])
def parse_med_schedule():
    if not TENCENT_API_KEY:
        return missing_key_response("TENCENT_API_KEY")
    if not TENCENT_CHAT_URL:
        return missing_key_response("TENCENT_CHAT_URL")
    try:
        payload = request.get_json(silent=True) or {}
        text = payload.get("text", "")
        context = payload.get("context", "")
        if not text:
            return jsonify({"status": "error", "message": "缺少文本"}), 400
        result = parse_schedule_with_hunyuan(text, context)
        return jsonify(
            {
                "status": result.get("status", "complete"),
                "schedules": result.get("schedules", []),
                "ask_user": result.get("ask_user", ""),
            }
        ), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    if not TENCENT_API_KEY:
        print("Missing TENCENT_API_KEY")
    if not SILICONFLOW_API_KEY:
        print("Missing SILICONFLOW_API_KEY")
    print("SmartMedBox cloud server started")
    app.run(host=SERVER_HOST, port=SERVER_PORT)
