# AI 辅助生成的学生项目代码；参与和整理说明见 README.md。
# SmartMedBox：M10 网关，负责调度、串口通信和云端同步。

from unihiker import GUI
import datetime
import time
import threading
import serial
import requests
import json
import os
import subprocess
import base64

DEFAULT_CLOUD_BASE_URL = "http://127.0.0.1:5000"
CLOUD_BASE_URL = (os.getenv("SMARTMEDBOX_CLOUD_BASE_URL", DEFAULT_CLOUD_BASE_URL) or DEFAULT_CLOUD_BASE_URL).rstrip("/")
CLOUD_API_URL = f"{CLOUD_BASE_URL}/api/upload"
CLOUD_CONFIG_URL = f"{CLOUD_BASE_URL}/api/config"
CLOUD_VOICE_URL = f"{CLOUD_BASE_URL}/api/voice"
CLOUD_TTS_TEXT_URL = f"{CLOUD_BASE_URL}/api/tts_text"

SERIAL_PORT = '/dev/ttyACM0'
BAUD_RATE = 115200
ser = None

CONFIG_FILE = "alarms_config.json"
DEFAULT_ALARMS = {"早上": {"h": 8, "m": 0}, "中午": {"h": 12, "m": 30}, "晚上": {"h": 19, "m": 0}}
config_state = {
    "alarms": {"早上": {"h": 8, "m": 0}, "中午": {"h": 12, "m": 30}, "晚上": {"h": 19, "m": 0}},
    "last_local_edit_time": 0,
    "last_cloud_sync_time": 0,
    "sync_status": "未同步",  # 已同步, 待同步, 同步失败
    "config_source": "默认"  # 本地, 云端, 默认
}
alarms = config_state["alarms"]  # 兼容引用
current_page = "HOME"
edit_tab = "早上"

active_period_name = "未知"
is_med_active = False
check_count = 0
last_action_time = 0
last_recheck_attempt_time = 0
last_k10_offline_log_time = 0
last_serial_error_log_time = 0
last_serial_send_error_log_time = 0

HEARTBEAT_INTERVAL_SEC = 2.5
K10_OFFLINE_RETRY_SEC = 10
K10_HEARTBEAT_TIMEOUT_SEC = 8
IMAGE_RX_TIMEOUT_SEC = 6
RECHECK_INTERVAL_SEC = 300  # 每 5 分钟复查拍照
MAX_RECHECK_COUNT = 4       # 第 1/2/3/4 次分别对应 0/5/10/15 分钟
ALARM_AFTER_MINUTES = 15
last_k10_seen_ts = 0

# --- 系统状态变量 ---
MODE_IDLE = "IDLE"
MODE_REMINDING = "REMINDING"
MODE_CAPTURING = "CAPTURING"
MODE_UPLOADING = "UPLOADING"
MODE_ANALYZING = "ANALYZING"
MODE_WAITING_RECHECK = "WAITING_RECHECK"
MODE_OFFLINE = "OFFLINE"
MODE_ERROR = "ERROR"

MODE_MAP = {
    MODE_IDLE: "待机中",
    MODE_REMINDING: "提醒中",
    MODE_CAPTURING: "正在拍照",
    MODE_UPLOADING: "接收图像中",
    MODE_ANALYZING: "云端分析中",
    MODE_WAITING_RECHECK: "等待复查",
    MODE_OFFLINE: "离线模式",
    MODE_ERROR: "异常告警"
}

ICON_MAP = {
    MODE_IDLE: "○",
    MODE_REMINDING: "铃",
    MODE_CAPTURING: "P",
    MODE_UPLOADING: "U",
    MODE_ANALYZING: "C",
    MODE_WAITING_RECHECK: "W",
    MODE_OFFLINE: "⚠",
    MODE_ERROR: "❗"
}

sys_state = {
    "mode": MODE_IDLE,
    "last_result": "暂无检测记录",
    "k10_status": "OFFLINE",  # CONNECTED, OFFLINE
    "cloud_status": "OFFLINE" # CONNECTED, FAILED, OFFLINE
}

last_logs = ["系统启动..."]
throttled_log_ts = {}

gui = GUI()
page_elements = []
dynamic_ui = {}

def clear_page():
    gui.clear()
    page_elements.clear()
    dynamic_ui.clear()

def add_ui(element):
    page_elements.append(element)
    return element

def add_log(msg):
    global last_logs
    now_str = datetime.datetime.now().strftime("%H:%M:%S")
    last_logs.insert(0, f"[{now_str}] {msg}")
    if len(last_logs) > 5:
        last_logs.pop()

def add_log_throttled(key, msg, cooldown_sec=5):
    now_ts = time.time()
    last_ts = throttled_log_ts.get(key, 0)
    if now_ts - last_ts >= cooldown_sec:
        add_log(msg)
        throttled_log_ts[key] = now_ts

def set_sys_mode(mode):
    sys_state["mode"] = mode
    add_log(f"模式切换: {MODE_MAP.get(mode, mode)}")

def mark_k10_seen():
    global last_k10_seen_ts
    last_k10_seen_ts = time.time()
    sys_state["k10_status"] = "CONNECTED"

def is_k10_online():
    if not (ser and ser.is_open):
        return False
    if last_k10_seen_ts <= 0:
        # 首次握手前允许发送一次，避免刚上电阶段被误判离线
        return True
    return (time.time() - last_k10_seen_ts) <= K10_HEARTBEAT_TIMEOUT_SEC

def send_k10_command(cmd_bytes, log_if_offline=False):
    global last_k10_offline_log_time, last_serial_send_error_log_time
    try:
        if is_k10_online():
            ser.write(cmd_bytes)
            return True
    except Exception as e:
        now_ts = time.time()
        if now_ts - last_serial_send_error_log_time > 5:
            cmd_text = cmd_bytes.decode('utf-8', errors='ignore').strip()
            add_log(f"串口发送失败: {e}")
            if cmd_text:
                sys_state["last_result"] = f"串口发送失败: {cmd_text}"
            last_serial_send_error_log_time = now_ts

    sys_state["k10_status"] = "OFFLINE"
    if log_if_offline:
        now_ts = time.time()
        if now_ts - last_k10_offline_log_time > 5:
            add_log("K10离线，未触发本轮检测")
            last_k10_offline_log_time = now_ts
    return False

def heartbeat_thread():
    while True:
        try:
            if ser and ser.is_open:
                ser.write(b"<<<PING>>>\n")
            if last_k10_seen_ts > 0 and (time.time() - last_k10_seen_ts) > K10_HEARTBEAT_TIMEOUT_SEC:
                sys_state["k10_status"] = "OFFLINE"
        except Exception as e:
            sys_state["k10_status"] = "OFFLINE"
            add_log_throttled("heartbeat_write_fail", f"心跳发送失败: {e}")
        time.sleep(HEARTBEAT_INTERVAL_SEC)

def _sanitize_alarms(raw):
    if not isinstance(raw, dict):
        return None

    # 兼容云端/本地历史字段，统一为 早上/中午/晚上
    alias_map = {
        "早上": "早上", "morning": "早上",
        "中午": "中午", "noon": "中午",
        "晚上": "晚上", "night": "晚上"
    }
    normalized = {}
    for raw_key, normalized_key in alias_map.items():
        if raw_key in raw and normalized_key not in normalized:
            slot = raw.get(raw_key, {})
            if not isinstance(slot, dict):
                continue
            h = slot.get("h")
            m = slot.get("m")
            if isinstance(h, int) and isinstance(m, int):
                normalized[normalized_key] = {"h": h % 24, "m": m % 60}

    # 缺失项回落默认值，避免“下次服药”计算异常
    for key, value in DEFAULT_ALARMS.items():
        if key not in normalized:
            normalized[key] = {"h": value["h"], "m": value["m"]}

    return normalized

def get_next_alarm_text(now):
    valid = []
    for name, item in alarms.items():
        if isinstance(item, dict) and isinstance(item.get("h"), int) and isinstance(item.get("m"), int):
            total = (item["h"] % 24) * 60 + (item["m"] % 60)
            valid.append((total, name, item))

    if not valid:
        return "下次: 未配置"

    valid.sort(key=lambda x: x[0])
    cur_mins = now.hour * 60 + now.minute
    future = [row for row in valid if row[0] > cur_mins]
    if future:
        _, name, item = future[0]
        return f"下次: {name} {item['h']:02d}:{item['m']:02d}"

    _, name, item = valid[0]
    return f"下次: 明天{name} {item['h']:02d}:{item['m']:02d}"

def get_terminal_status_display():
    is_online = (sys_state.get("k10_status") == "CONNECTED")
    return ("[T] 终端: 在线", "#43A047") if is_online else ("[T] 终端: 离线", "#E53935")

def get_cloud_status_display():
    cloud_state = sys_state.get("cloud_status")
    if cloud_state == "CONNECTED":
        return "[C] 云端: 在线", "#43A047"
    if cloud_state == "FAILED":
        return "[C] 云端: 同步失败", "#E53935"
    return "[C] 云端: 离线", "#E53935"

def get_config_status_display():
    source = config_state.get("config_source", "默认")
    sync_status = config_state.get("sync_status", "")
    local_file_exists = os.path.exists(CONFIG_FILE)

    if source == "云端" and sys_state.get("cloud_status") == "CONNECTED":
        return "[S] 配置: 云端", "#43A047"

    if source == "本地" or local_file_exists:
        if sync_status == "同步失败" or sys_state.get("cloud_status") == "FAILED":
            return "[S] 配置: 本地·同步失败", "#E53935"
        if sys_state.get("cloud_status") == "OFFLINE":
            return "[S] 配置: 本地·离线运行", "#FB8C00"
        if sync_status in ("待同步", "未同步", "已加载"):
            return "[S] 配置: 本地·待同步", "#FB8C00"
        if sync_status == "同步中...":
            return "[S] 配置: 本地·同步中", "#1E88E5"
        return "[S] 配置: 本地", "#43A047"

    return "[S] 配置: 默认", "#9E9E9E"

def load_alarms():
    global config_state, alarms
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                # 兼容两种格式:
                # 1) {"alarms": {...}, ...}
                # 2) {"早上": {...}, "中午": {...}, "晚上": {...}}
                raw_alarms = saved.get("alarms") if isinstance(saved, dict) and "alarms" in saved else saved
                normalized = _sanitize_alarms(raw_alarms)
                if normalized:
                    config_state["alarms"] = normalized
                    alarms = config_state["alarms"]
                    if isinstance(saved, dict):
                        config_state["last_local_edit_time"] = saved.get("last_local_edit_time", 0)
                        config_state["last_cloud_sync_time"] = saved.get("last_cloud_sync_time", 0)
                    config_state["config_source"] = "本地"
                    config_state["sync_status"] = "已加载"
                    add_log("已加载本地配置")
        except Exception as e:
            add_log_throttled("load_alarms_error", f"配置读取失败: {e}")

def push_to_cloud():
    global config_state
    config_state["sync_status"] = "同步中..."
    try:
        # 优先兼容旧接口：直接上传 alarms
        payload = alarms
        res = requests.post(CLOUD_CONFIG_URL, json=payload, timeout=5)
        # 如果云端已切到新结构，则回退到包含 metadata 的结构
        if res.status_code != 200:
            payload = {
                "alarms": alarms,
                "last_local_edit_time": config_state.get("last_local_edit_time", 0),
                "last_cloud_sync_time": config_state.get("last_cloud_sync_time", 0),
            }
            res = requests.post(CLOUD_CONFIG_URL, json=payload, timeout=5)
        if res.status_code == 200:
            sys_state["cloud_status"] = "CONNECTED"
            config_state["last_cloud_sync_time"] = time.time()
            config_state["sync_status"] = "已同步"
            add_log("配置已同步云端")
            return True
    except Exception as e:
        sys_state["cloud_status"] = "FAILED"
        config_state["sync_status"] = "同步失败"
        add_log_throttled("push_cloud_error", f"配置同步云端失败: {e}")
    return False

def sync_cloud_config():
    global config_state, alarms
    config_state["sync_status"] = "同步中..."
    try:
        res = requests.get(CLOUD_CONFIG_URL, timeout=5)
        if res.status_code == 200:
            sys_state["cloud_status"] = "CONNECTED"
            cloud_data = res.json()

            raw_alarms = cloud_data.get("alarms") if isinstance(cloud_data, dict) and "alarms" in cloud_data else cloud_data
            normalized = _sanitize_alarms(raw_alarms)
            if normalized:
                config_state["alarms"] = normalized
                alarms = config_state["alarms"]
                config_state["last_cloud_sync_time"] = time.time()
                config_state["config_source"] = "云端"
                config_state["sync_status"] = "已同步"
                add_log("应用云端配置")
                _save_to_disk()
                return True

            # 云端无有效配置时回推本地，避免状态卡死
            return push_to_cloud()
    except Exception as e:
        sys_state["cloud_status"] = "FAILED"
        config_state["sync_status"] = "同步失败"
        add_log_throttled("sync_cloud_error", f"获取云端配置失败: {e}")
    return False

def _save_to_disk():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f: json.dump(config_state, f)
    except Exception as e:
        add_log_throttled("save_disk_error", f"保存配置失败: {e}")

def save_alarms(sync=True):
    config_state["alarms"] = alarms
    config_state["last_local_edit_time"] = time.time()
    config_state["config_source"] = "本地"
    config_state["sync_status"] = "待同步"
    _save_to_disk()
    if sync:
        threading.Thread(target=push_to_cloud).start()

def restore_default_alarms():
    global config_state, alarms
    config_state["alarms"] = json.loads(json.dumps(DEFAULT_ALARMS))
    alarms = config_state["alarms"]
    config_state["config_source"] = "默认"
    save_alarms(sync=True)
    add_log("已恢复默认时间")


def init_popup():
    dynamic_ui['pop_bg'] = add_ui(gui.fill_round_rect(x=-500, y=135, w=220, h=50, r=10, color="#263238"))
    dynamic_ui['pop_text'] = add_ui(gui.draw_text(x=-500, y=160, text="等待中...", origin="center", font_size=14, color="#81C784"))

def show_popup(msg):
    if 'pop_bg' in dynamic_ui:
        dynamic_ui['pop_bg'].config(x=10)
        dynamic_ui['pop_text'].config(x=120, text=msg)

def hide_popup():
    if 'pop_bg' in dynamic_ui:
        dynamic_ui['pop_bg'].config(x=-500)
        dynamic_ui['pop_text'].config(x=-500)

def render_home():
    global current_page
    current_page = "HOME"
    clear_page()

    # 背景
    add_ui(gui.fill_rect(x=0, y=0, w=240, h=320, color="#F7F7FB"))

    # 顶部时间与核心模式
    dynamic_ui['clock'] = add_ui(gui.draw_text(x=120, y=35, text="00:00", origin="center", font_size=46, color="#212121"))

    # 当前运行模式
    add_ui(gui.fill_round_rect(x=20, y=85, w=200, h=35, r=6, color="#FFFFFF"))
    add_ui(gui.fill_round_rect(x=20, y=89, w=2, h=27, r=1, color="#2F66D9"))  # 细竖线
    dynamic_ui['sys_mode_text'] = add_ui(gui.draw_text(x=28, y=102, text=MODE_MAP.get(sys_state["mode"], "未知"), origin="left", font_size=13, color="#2F66D9"))
    dynamic_ui['next_alarm'] = add_ui(gui.draw_text(x=210, y=102, text=get_next_alarm_text(datetime.datetime.now()), origin="right", font_size=10, color="#7E57C2"))

    # 最近结果与设备状态
    dynamic_ui['result_card_bg'] = add_ui(gui.fill_round_rect(x=20, y=135, w=200, h=105, r=8, color="#FFFFFF"))
    add_ui(gui.draw_text(x=30, y=150, text="最新结果", origin="left", font_size=10, color="#7A7A7A"))

    dynamic_ui['last_result'] = add_ui(gui.draw_text(x=30, y=170, text=sys_state["last_result"], origin="left", font_size=12, color="#333333"))

    add_ui(gui.draw_line(x0=30, y0=190, x1=210, y1=190, width=1, color="#EAECEF"))

    k10_text, k10_color = get_terminal_status_display()
    cloud_text, cloud_color = get_cloud_status_display()
    config_text, config_color = get_config_status_display()
    dynamic_ui['k10_status'] = add_ui(gui.draw_text(x=30, y=205, text=k10_text, origin="left", font_size=9, color=k10_color))
    dynamic_ui['cloud_status'] = add_ui(gui.draw_text(x=128, y=205, text=cloud_text, origin="left", font_size=9, color=cloud_color))
    dynamic_ui['config_status'] = add_ui(gui.draw_text(x=30, y=224, text=config_text, origin="left", font_size=9, color=config_color))

    # 设置、测试与录音入口
    add_ui(gui.fill_round_rect(x=15, y=260, w=85, h=35, r=6, color="#E7F0FF", onclick=lambda: open_settings("早上")))
    add_ui(gui.draw_text(x=57, y=277, text="时间设置", origin="center", font_size=11, color="#1E66D0", onclick=lambda: open_settings("早上")))

    add_ui(gui.fill_round_rect(x=110, y=260, w=85, h=35, r=6, color="#EFE7FF", onclick=render_test_page))
    add_ui(gui.draw_text(x=152, y=277, text="更多功能", origin="center", font_size=11, color="#6A4FB3", onclick=render_test_page))

    dynamic_ui['btn_bg'] = add_ui(gui.fill_round_rect(x=205, y=262, w=30, h=30, r=15, color="#E6F7EE", onclick=toggle_recording))
    # 纯图形麦克风图标（避免 Tcl 对高位 emoji 的字符限制）
    dynamic_ui['btn_icon_head'] = add_ui(gui.fill_round_rect(x=216, y=268, w=8, h=10, r=4, color="#1B5E20", onclick=toggle_recording))
    dynamic_ui['btn_icon_stem'] = add_ui(gui.fill_rect(x=219, y=278, w=2, h=4, color="#1B5E20", onclick=toggle_recording))
    dynamic_ui['btn_icon_base'] = add_ui(gui.fill_round_rect(x=215, y=283, w=10, h=2, r=1, color="#1B5E20", onclick=toggle_recording))

    init_popup()

def render_settings():
    global current_page, temp_edit_h, temp_edit_m
    current_page = "SETTINGS"
    clear_page()

    add_ui(gui.fill_rect(x=0, y=0, w=240, h=320, color="#F8F9FA"))
    add_ui(gui.fill_rect(x=0, y=0, w=240, h=35, color="#FFFFFF"))
    add_ui(gui.draw_text(x=120, y=17, text="时间设置", origin="center", font_size=14, color="#333333"))
    add_ui(gui.fill_round_rect(x=5, y=5, w=40, h=25, r=4, color="#F5F5F5", onclick=render_home))
    add_ui(gui.draw_text(x=25, y=17, text="返回", origin="center", font_size=11, color="#616161", onclick=render_home))

    # 时段切换
    tab_y = 50
    for i, (t, icon) in enumerate([("早上", "☀"), ("中午", "☼"), ("晚上", "☾")]):
        is_active = (edit_tab == t)
        color = "#1976D2" if is_active else "#9E9E9E"
        bg_color = "#E3F2FD" if is_active else "#FFFFFF"
        add_ui(gui.fill_round_rect(x=25 + i*65, y=tab_y, w=55, h=28, r=14, color=bg_color, onclick=lambda t=t: open_settings(t)))
        add_ui(gui.draw_text(x=52 + i*65, y=tab_y+14, text=f"{icon} {t}", origin="center", font_size=11, color=color, onclick=lambda t=t: open_settings(t)))

    add_ui(gui.fill_round_rect(x=35, y=95, w=70, h=65, r=8, color="#FFFFFF"))
    add_ui(gui.draw_text(x=70, y=127, text=f"{temp_edit_h:02d}", origin="center", font_size=36, color="#212121"))
    add_ui(gui.draw_text(x=120, y=125, text=":", origin="center", font_size=32, color="#BDBDBD"))
    add_ui(gui.fill_round_rect(x=135, y=95, w=70, h=65, r=8, color="#FFFFFF"))
    add_ui(gui.draw_text(x=170, y=127, text=f"{temp_edit_m:02d}", origin="center", font_size=36, color="#212121"))

    add_ui(gui.fill_round_rect(x=35, y=170, w=70, h=30, r=6, color="#F5F5F5", onclick=lambda: handle_click('adj_h', 1)))
    add_ui(gui.draw_text(x=70, y=185, text="小时 +", origin="center", font_size=12, color="#616161", onclick=lambda: handle_click('adj_h', 1)))
    add_ui(gui.fill_round_rect(x=35, y=205, w=70, h=30, r=6, color="#F5F5F5", onclick=lambda: handle_click('adj_h', -1)))
    add_ui(gui.draw_text(x=70, y=220, text="小时 -", origin="center", font_size=12, color="#616161", onclick=lambda: handle_click('adj_h', -1)))

    add_ui(gui.fill_round_rect(x=135, y=170, w=70, h=30, r=6, color="#F5F5F5", onclick=lambda: handle_click('adj_m', 1)))
    add_ui(gui.draw_text(x=170, y=185, text="分钟 +", origin="center", font_size=12, color="#616161", onclick=lambda: handle_click('adj_m', 1)))
    add_ui(gui.fill_round_rect(x=135, y=205, w=70, h=30, r=6, color="#F5F5F5", onclick=lambda: handle_click('adj_m', -1)))
    add_ui(gui.draw_text(x=170, y=220, text="分钟 -", origin="center", font_size=12, color="#616161", onclick=lambda: handle_click('adj_m', -1)))

    add_ui(gui.fill_round_rect(x=20, y=250, w=60, h=35, r=6, color="#F5F5F5", onclick=lambda: handle_click('reset', None)))
    add_ui(gui.draw_text(x=50, y=267, text="恢复默认", origin="center", font_size=10, color="#9E9E9E", onclick=lambda: handle_click('reset', None)))

    add_ui(gui.fill_round_rect(x=90, y=250, w=130, h=35, r=6, color="#1976D2", onclick=lambda: handle_click('save_sync', None)))
    add_ui(gui.draw_text(x=155, y=267, text="保存并同步", origin="center", font_size=12, color="#FFFFFF", onclick=lambda: handle_click('save_sync', None)))

    add_ui(gui.draw_text(x=120, y=295, text=f"当前配置来源: {config_state['config_source']} · {config_state['sync_status']}", origin="center", font_size=9, color="#BDBDBD"))

def render_test_page():
    global current_page
    current_page = "TEST"
    clear_page()

    add_ui(gui.fill_rect(x=0, y=0, w=240, h=320, color="#FAFAFA"))
    add_ui(gui.fill_rect(x=0, y=0, w=240, h=35, color="#FFFFFF"))
    add_ui(gui.draw_text(x=120, y=17, text="更多功能", origin="center", font_size=14, color="#333333"))
    add_ui(gui.fill_round_rect(x=5, y=5, w=40, h=25, r=4, color="#F5F5F5", onclick=render_home))
    add_ui(gui.draw_text(x=25, y=17, text="返回", origin="center", font_size=11, color="#616161", onclick=render_home))

    add_ui(gui.fill_round_rect(x=15, y=45, w=100, h=32, r=4, color="#F0F4F8", onclick=lambda: test_action('TAKE_PIC')))
    add_ui(gui.draw_text(x=65, y=61, text="[P] 立即拍照", origin="center", font_size=11, color="#37474F", onclick=lambda: test_action('TAKE_PIC')))

    add_ui(gui.fill_round_rect(x=125, y=45, w=100, h=32, r=4, color="#F0F4F8", onclick=lambda: test_action('ALARM_MED')))
    add_ui(gui.draw_text(x=175, y=61, text="[A] 测试提醒", origin="center", font_size=11, color="#37474F", onclick=lambda: test_action('ALARM_MED')))

    add_ui(gui.fill_round_rect(x=15, y=85, w=100, h=32, r=4, color="#F0F4F8", onclick=lambda: test_action('SYNC')))
    add_ui(gui.draw_text(x=65, y=101, text="[S] 同步配置", origin="center", font_size=11, color="#37474F", onclick=lambda: test_action('SYNC')))

    add_ui(gui.fill_round_rect(x=125, y=85, w=100, h=32, r=4, color="#F0F4F8", onclick=lambda: test_action('TEST_CLOUD')))
    add_ui(gui.draw_text(x=175, y=101, text="[C] 测试云端", origin="center", font_size=11, color="#37474F", onclick=lambda: test_action('TEST_CLOUD')))

    add_ui(gui.fill_round_rect(x=15, y=125, w=100, h=32, r=4, color="#FFEBEE", onclick=lambda: test_action('STOP_ALARM')))
    add_ui(gui.draw_text(x=65, y=141, text="[X] 停止提醒", origin="center", font_size=11, color="#C62828", onclick=lambda: test_action('STOP_ALARM')))

    add_ui(gui.fill_round_rect(x=125, y=125, w=100, h=32, r=4, color="#F5F5F5", onclick=lambda: test_action('CLEAR_LOGS')))
    add_ui(gui.draw_text(x=175, y=141, text="[L] 清空日志", origin="center", font_size=11, color="#757575", onclick=lambda: test_action('CLEAR_LOGS')))

    add_ui(gui.draw_text(x=20, y=175, text="执行日志", origin="left", font_size=10, color="#9E9E9E"))
    add_ui(gui.draw_line(x0=20, y0=190, x1=220, y1=190, width=1, color="#EEEEEE"))

    dynamic_ui['log_texts'] = []
    for i in range(4):
        txt = add_ui(gui.draw_text(x=20, y=200 + i*18, text="", origin="left", font_size=9, color="#616161"))
        dynamic_ui['log_texts'].append(txt)

    update_test_logs()
    init_popup()

def update_test_logs():
    if current_page == "TEST" and 'log_texts' in dynamic_ui:
        for i in range(4):
            if i < len(last_logs):
                dynamic_ui['log_texts'][i].config(text=last_logs[i])
            else:
                dynamic_ui['log_texts'][i].config(text="")

def test_action(action):
    if action == 'TAKE_PIC':
        if send_k10_command(b"<<<CMD:TAKE_PIC>>>\n"):
            add_log("-> 发送: 立即拍照")
        else:
            add_log("K10 未连接")
    elif action == 'ALARM_MED':
        if send_k10_command(b"<<<CMD:ALARM_MED>>>\n"):
            add_log("-> 发送: 测试报警")
        else:
            add_log("K10 未连接")
    elif action == 'STOP_ALARM':
        if send_k10_command(b"<<<CMD:STOP_ALARM>>>\n"):
            add_log("-> 发送: 停止报警")
        else:
            add_log("K10 未连接")
    elif action == 'SYNC':
        add_log("尝试同步云端配置...")
        threading.Thread(target=sync_cloud_config).start()
    elif action == 'TEST_CLOUD':
        add_log("测试云端连接...")
        def _test_cloud():
            try:
                res = requests.get(CLOUD_CONFIG_URL, timeout=5)
                if res.status_code == 200:
                    add_log("云端连接: 成功")
                    sys_state["cloud_status"] = "CONNECTED"
                else:
                    add_log(f"云端连接: 异常 {res.status_code}")
                    sys_state["cloud_status"] = "FAILED"
            except Exception as e:
                add_log("云端连接: 失败/超时")
                sys_state["cloud_status"] = "OFFLINE"
        threading.Thread(target=_test_cloud).start()
    elif action == 'CLEAR_LOGS':
        global last_logs
        last_logs = ["已清空日志"]

    update_test_logs()

def handle_click(action, val):
    global temp_edit_h, temp_edit_m, alarms
    if action == 'adj_h':
        temp_edit_h = (temp_edit_h + val) % 24
    elif action == 'adj_m':
        temp_edit_m = (temp_edit_m + val * 5) % 60
    elif action == 'save_local':
        alarms[edit_tab]['h'] = temp_edit_h
        alarms[edit_tab]['m'] = temp_edit_m
        save_alarms(sync=False)
        add_log(f"已保存 {edit_tab} 时间")
        render_home()
        return
    elif action == 'save_sync':
        alarms[edit_tab]['h'] = temp_edit_h
        alarms[edit_tab]['m'] = temp_edit_m
        save_alarms(sync=True)
        add_log(f"已保存 {edit_tab} 时间")
        render_home()
        return
    elif action == 'reset':
        restore_default_alarms()
        open_settings(edit_tab)
        return
    clear_page()
    render_settings()

def open_settings(tab):
    global edit_tab, temp_edit_h, temp_edit_m
    edit_tab = tab
    temp_edit_h = alarms[tab]['h']
    temp_edit_m = alarms[tab]['m']
    render_settings()

record_process = None
is_recording = False

def upload_voice_to_cloud():
    try:
        if not os.path.exists("temp_record.wav") or os.path.getsize("temp_record.wav") < 5000:
            show_popup("上传失败")
            add_log("语音上传失败: 录音太短")
            time.sleep(1.2)
            hide_popup()
            return

        show_popup("上传中")
        add_log("上传语音至云端...")
        with open("temp_record.wav", "rb") as f:
            audio_b64 = base64.b64encode(f.read()).decode('utf-8')
        res = requests.post(CLOUD_VOICE_URL, json={"audio": audio_b64}, timeout=20)

        if res.status_code == 200:
            reply_b64 = res.json().get("reply_audio")
            if reply_b64 and ser and ser.is_open:
                add_log("下发回复语音至 K10")
                ser.write(b"<<<REPLY_START>>>\n")
                for i in range(0, len(reply_b64), 1024):
                    ser.write(reply_b64[i:i+1024].encode('utf-8') + b"\n")
                    time.sleep(0.02)
                ser.write(b"<<<REPLY_END>>>\n")
            else:
                add_log("无回复语音或 K10 离线")
            show_popup("上传成功")
            time.sleep(1.2)
            hide_popup()
        else:
            show_popup("上传失败")
            time.sleep(1.2)
            hide_popup()
            add_log("语音处理失败")
    except Exception as e:
        show_popup("上传失败")
        time.sleep(1.2)
        hide_popup()
        add_log(f"网络异常: {e}")

def toggle_recording():
    global is_recording, record_process
    def _set_mic_icon_color(color):
        for key in ("btn_icon_head", "btn_icon_stem", "btn_icon_base"):
            if key in dynamic_ui:
                dynamic_ui[key].config(color=color)

    if not is_recording:
        is_recording = True
        if 'btn_bg' in dynamic_ui:
            dynamic_ui['btn_bg'].config(color="#F44336")
        _set_mic_icon_color("#FFFFFF")
        show_popup("录音中")
        subprocess.run(['killall', 'arecord'], stderr=subprocess.DEVNULL)
        record_process = subprocess.Popen(['arecord', '-f', 'S16_LE', '-r', '16000', 'temp_record.wav'])
    else:
        is_recording = False
        hide_popup()
        if 'btn_bg' in dynamic_ui:
            dynamic_ui['btn_bg'].config(color="#E6F7EE")
        _set_mic_icon_color("#1B5E20")
        if record_process:
            record_process.terminate()
            record_process.wait()
            record_process = None
        threading.Thread(target=upload_voice_to_cloud, daemon=True).start()

def trigger_smart_alarm(period_name=None):
    global active_period_name
    if period_name:
        active_period_name = period_name
    set_sys_mode(MODE_REMINDING)
    try:
        alarm_text = f"警告！系统发现您{active_period_name}的药过了{ALARM_AFTER_MINUTES}分钟还没吃，请赶快服药！"
        add_log("请求终极警报语音...")
        res = requests.post(CLOUD_TTS_TEXT_URL, json={"text": alarm_text}, timeout=10)
        if res.status_code == 200:
            reply_b64 = res.json().get("reply_audio")
            if reply_b64 and ser and ser.is_open:
                ser.write(b"<<<ALARM_AUDIO_START>>>\n")
                for i in range(0, len(reply_b64), 1024):
                    ser.write(reply_b64[i:i+1024].encode('utf-8') + b"\n")
                    time.sleep(0.02)
                ser.write(b"<<<ALARM_AUDIO_END>>>\n")
    except Exception as e:
        add_log(f"警报语音失败: {e}")

    if not send_k10_command(b"<<<CMD:ALARM_MED>>>\n", log_if_offline=True):
        set_sys_mode(MODE_OFFLINE)

def handle_recheck_or_alarm(period_name, check_idx, wait_log_msg="等待下一次复查"):
    global is_med_active
    if check_idx >= MAX_RECHECK_COUNT:
        is_med_active = False
        add_log(f"{ALARM_AFTER_MINUTES}分钟后仍无法确认，触发警报")
        trigger_smart_alarm(period_name)
        return "ALARMED"
    add_log(wait_log_msg)
    set_sys_mode(MODE_WAITING_RECHECK)
    return "WAITING"

def timekeeper_thread():
    global active_period_name, is_med_active, check_count, last_action_time, last_recheck_attempt_time
    current_hm_record = ""
    while True:
        now = datetime.datetime.now()
        current_hm = f"{now.hour:02d}:{now.minute:02d}"

        # 1. 触发点 (第 0 分钟，第一次检测)
        for period, t in alarms.items():
            if current_hm == f"{t['h']:02d}:{t['m']:02d}" and current_hm_record != current_hm:
                current_hm_record = current_hm
                if send_k10_command(b"<<<CMD:TAKE_PIC>>>\n", log_if_offline=True):
                    active_period_name = period
                    is_med_active = True
                    check_count = 1
                    set_sys_mode(MODE_CAPTURING)
                    last_action_time = time.time()
                    last_recheck_attempt_time = last_action_time
                else:
                    set_sys_mode(MODE_OFFLINE)
                break

        # 2. 轮询检测 (第 5/10/15 分钟)
        if is_med_active and (time.time() - last_action_time >= RECHECK_INTERVAL_SEC):
            if check_count < MAX_RECHECK_COUNT:
                now_ts = time.time()
                if now_ts - last_recheck_attempt_time >= K10_OFFLINE_RETRY_SEC:
                    last_recheck_attempt_time = now_ts
                    if send_k10_command(b"<<<CMD:TAKE_PIC>>>\n", log_if_offline=True):
                        check_count += 1
                        set_sys_mode(MODE_CAPTURING)
                        last_action_time = now_ts
                    else:
                        set_sys_mode(MODE_OFFLINE)

        time.sleep(1)

def serial_listener():
    global ser, active_period_name, is_med_active, check_count, last_serial_error_log_time, last_k10_seen_ts
    buffer = bytearray()
    receiving_pic = False
    expected_total = 35840
    last_update_time = 0

    while True:
        try:
            if ser is None or not ser.is_open:
                try:
                    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.01)
                except Exception as e:
                    sys_state["k10_status"] = "OFFLINE"
                    last_k10_seen_ts = 0
                    add_log_throttled("serial_open_fail", f"串口打开失败({SERIAL_PORT}): {e}")
                    time.sleep(1)
                    continue

            if ser.in_waiting > 0:
                chunk = ser.read(ser.in_waiting)
                buffer.extend(chunk)
                if chunk:
                    mark_k10_seen()
                    if receiving_pic:
                        last_update_time = time.time()

                if not receiving_pic:
                    # 1) 先解析 ERR 报文，避免硬件错误被吞掉
                    while True:
                        err_start = buffer.find(b'<<<ERR:')
                        if err_start == -1:
                            break
                        err_end = buffer.find(b'>>>', err_start)
                        if err_end == -1:
                            break
                        err_code = buffer[err_start+7:err_end].decode('utf-8', 'ignore').strip()
                        add_log(f"K10错误: {err_code}")
                        set_sys_mode(MODE_ERROR)
                        buffer = bytearray(buffer[:err_start] + buffer[err_end+3:])

                    # 2) 再解析 ACK 报文，只移除 ACK 片段，不能清空整个缓冲
                    while True:
                        ack_start = buffer.find(b'<<<ACK:')
                        if ack_start == -1:
                            break
                        ack_end = buffer.find(b'>>>', ack_start)
                        if ack_end == -1:
                            break
                        ack_msg = buffer[ack_start+7:ack_end].decode('utf-8', 'ignore').strip()
                        if ack_msg and ack_msg != "PING":
                            add_log(f"K10应答: {ack_msg}")
                        buffer = bytearray(buffer[:ack_start] + buffer[ack_end+3:])

                    # 3) HELLO 握手报文也只裁掉本段
                    hello_token = b'<<<HELLO:K10>>>'
                    while True:
                        hello_idx = buffer.find(hello_token)
                        if hello_idx == -1:
                            break
                        add_log("收到 K10 握手")
                        buffer = bytearray(buffer[:hello_idx] + buffer[hello_idx+len(hello_token):])

                    # 4) 处理图片传输头
                    if b'<<<START:' in buffer and b'>>>' in buffer[buffer.find(b'<<<START:'):]:
                        start_idx = buffer.find(b'<<<START:')
                        end_idx = buffer.find(b'>>>', start_idx)
                        len_str = buffer[start_idx+9 : end_idx].decode('utf-8', 'ignore')
                        try: expected_total = int(len_str)
                        except: expected_total = 35840
                        buffer = bytearray(buffer[end_idx+3:])
                        receiving_pic, last_update_time = True, time.time()
                        set_sys_mode(MODE_UPLOADING)

                    elif b'<<<START>>>' in buffer:
                        start_idx = buffer.find(b'<<<START>>>')
                        buffer = bytearray(buffer[start_idx+11:])
                        expected_total = 35840
                        receiving_pic, last_update_time = True, time.time()
                        set_sys_mode(MODE_UPLOADING)

                else:
                    if b'<<<END>>>' in buffer:
                        end_idx = buffer.find(b'<<<END>>>')
                        final_data = buffer[:end_idx]
                        tail = buffer[end_idx+9:]
                        receiving_pic = False
                        buffer = bytearray(tail)
                        set_sys_mode(MODE_ANALYZING)
                        task_snapshot = {
                            "period": active_period_name,
                            "check_count": check_count,
                            "timestamp": time.time()
                        }

                        def upload_to_cloud(img_bytes, snapshot):
                            global is_med_active
                            try:
                                clean_b64 = img_bytes.decode('utf-8', errors='ignore').replace('\n', '').replace('\r', '').strip()
                                payload = {
                                    "image": clean_b64,
                                    "period": snapshot["period"],
                                    "check_count": snapshot["check_count"]
                                }
                                res = requests.post(CLOUD_API_URL, json=payload, timeout=60)
                                if res.status_code == 200:
                                    ai_reply = res.json().get("ai_result", "未知")
                                    sys_state["last_result"] = f"{snapshot['period']} 第{snapshot['check_count']}次: {ai_reply}"
                                    add_log(f"AI: {ai_reply}")

                                    if "已服药" in ai_reply:
                                        is_med_active = False
                                        set_sys_mode(MODE_IDLE)
                                        # 可选：如果已服药，可以发指令关闭对应药格，需知道是哪个药格
                                    elif "需复查" in ai_reply or "判断不清" in ai_reply:
                                        handle_recheck_or_alarm(
                                            snapshot["period"],
                                            snapshot["check_count"],
                                            "视觉不确定，等待下一次复查",
                                        )
                                    else:
                                        if snapshot["check_count"] >= MAX_RECHECK_COUNT:
                                            add_log(f"{ALARM_AFTER_MINUTES}分钟未服药！触发警报")
                                            is_med_active = False
                                            trigger_smart_alarm(snapshot["period"])
                                        else:
                                            set_sys_mode(MODE_WAITING_RECHECK)
                                    sys_state["cloud_status"] = "CONNECTED"
                                else:
                                    add_log(f"云端分析失败: {res.status_code}")
                                    sys_state["cloud_status"] = "FAILED"
                                    sys_state["last_result"] = f"{snapshot['period']} 第{snapshot['check_count']}次: 云端失败，等待复查"
                                    handle_recheck_or_alarm(
                                        snapshot["period"],
                                        snapshot["check_count"],
                                        "云端失败，等待下一次复查",
                                    )
                            except Exception as e:
                                add_log(f"云端分析请求失败: {e}")
                                sys_state["cloud_status"] = "FAILED"
                                sys_state["last_result"] = f"{snapshot['period']} 第{snapshot['check_count']}次: 云端失败，等待复查"
                                handle_recheck_or_alarm(
                                    snapshot["period"],
                                    snapshot["check_count"],
                                    "云端请求异常，等待下一次复查",
                                )

                        threading.Thread(target=upload_to_cloud, args=(final_data, task_snapshot), daemon=True).start()

            if receiving_pic and last_update_time > 0 and (time.time() - last_update_time) >= IMAGE_RX_TIMEOUT_SEC:
                receiving_pic = False
                buffer.clear()
                sys_state["cloud_status"] = "FAILED"
                sys_state["last_result"] = "图像传输中断，等待下次复查"
                handle_recheck_or_alarm(
                    active_period_name,
                    check_count,
                    f"图像传输超时({IMAGE_RX_TIMEOUT_SEC}s)，等待下次复查",
                )
        except OSError:
            sys_state["k10_status"], ser = "OFFLINE", None
            last_k10_seen_ts = 0
        except Exception as e:
            now_ts = time.time()
            if now_ts - last_serial_error_log_time > 5:
                add_log(f"串口监听异常: {e}")
                last_serial_error_log_time = now_ts
        time.sleep(0.001)

load_alarms()
threading.Thread(target=sync_cloud_config).start()
threading.Thread(target=timekeeper_thread, daemon=True).start()
threading.Thread(target=serial_listener, daemon=True).start()
threading.Thread(target=heartbeat_thread, daemon=True).start()
render_home()

while True:
    try:
        if current_page == "HOME" and 'clock' in dynamic_ui:
            now = datetime.datetime.now()
            dynamic_ui['clock'].config(text=now.strftime("%H:%M"))

            # 更新模式
            mode = sys_state["mode"]
            if 'sys_mode_text' in dynamic_ui:
                dynamic_ui['sys_mode_text'].config(text=MODE_MAP.get(mode, "未知"))

            # 更新最近结果图标
            res_text = sys_state["last_result"]
            result_bg_color = "#FFFFFF"
            if "未服药" in res_text or "需复查" in res_text or "异常" in res_text or "失败" in res_text or "告警" in res_text:
                result_bg_color = "#FFF3F3"
            if 'result_card_bg' in dynamic_ui:
                dynamic_ui['result_card_bg'].config(color=result_bg_color)
            if 'last_result' in dynamic_ui:
                dynamic_ui['last_result'].config(text=res_text)

            if 'next_alarm' in dynamic_ui:
                dynamic_ui['next_alarm'].config(text=get_next_alarm_text(now))

            if 'k10_status' in dynamic_ui:
                k10_text, k10_color = get_terminal_status_display()
                dynamic_ui['k10_status'].config(text=k10_text, color=k10_color)
            if 'cloud_status' in dynamic_ui:
                cloud_text, cloud_color = get_cloud_status_display()
                dynamic_ui['cloud_status'].config(text=cloud_text, color=cloud_color)
            if 'config_status' in dynamic_ui:
                config_text, config_color = get_config_status_display()
                dynamic_ui['config_status'].config(text=config_text, color=config_color)
    except Exception as e:
        add_log_throttled("home_ui_refresh_error", f"界面刷新异常: {e}")
    time.sleep(1)
