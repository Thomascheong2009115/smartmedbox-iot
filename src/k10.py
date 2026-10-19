# AI 辅助生成的学生项目代码；参与和整理说明见 README.md。
# SmartMedBox：K10 感知节点，负责采集、显示和本地提醒。

import time
import gc
import sys
import ubinascii
import select
import os


try:
    from unihiker_k10 import screen, button, speaker, temp_humi, rgb, servo
    HAS_SERVO = True
except Exception:
    from unihiker_k10 import screen, button, speaker, temp_humi, rgb
    servo = None
    HAS_SERVO = False

try:
    from k10_base import Camera
except Exception:
    Camera = None

C_BLACK = 0x0000
C_WHITE = 0xFFFF
C_RED = 0xF800
C_YELLOW = 0xFFE0
C_BLUE = 0x001F
C_ORANGE = 0xFC00
C_CYAN = 0x07FF
C_GRAY = 0x8410
C_GREEN = 0x07E0

bt_a = button(button.a)
bt_b = button(button.b)

MAX_TEMP = 35.0
MAX_HUMI = 75.0
CHECK_INTERVAL = 5000
TEMP_OFFSET = 9.9

HEARTBEAT_TIMEOUT = 8000
AUDIO_RECV_TIMEOUT = 15000
ENABLE_RAW_SERIAL_LOG = False

SERVO_OPEN_ANGLE = 160
SERVO_CLOSE_ANGLE = 10

last_check_time = 0
current_temp = 0.0
current_humi = 0.0
sensor_error_count = 0

m10_connected = False
m10_last_seen = 0

camera = None
poll_obj = None

servos = [None, None, None]
servo_status = [False, False, False]
servo_positions = ["未知", "未知", "未知"]


def flush_stdout():
    try:
        sys.stdout.flush()
    except Exception:
        pass


def log_error(msg):
    if not ENABLE_RAW_SERIAL_LOG:
        return
    try:
        safe_msg = str(msg).replace("\n", " ")[:160]
        sys.stdout.write("<<<LOG:ERROR:{}>>>\n".format(safe_msg))
        flush_stdout()
    except Exception:
        pass


def _remove_file_if_exists(path):
    try:
        os.remove(path)
    except OSError as e:
        err_no = getattr(e, "errno", None)
        if err_no == 2:
            return
        if len(getattr(e, "args", ())) > 0 and e.args[0] == 2:
            return
        raise


def send_ack(cmd_type):
    try:
        sys.stdout.write("<<<ACK:{}>>>\n".format(cmd_type))
        flush_stdout()
    except Exception:
        pass


def send_err(err_type):
    try:
        sys.stdout.write("<<<ERR:{}>>>\n".format(err_type))
        flush_stdout()
    except Exception:
        pass


def update_connection_status():
    global m10_connected, m10_last_seen
    m10_connected = True
    m10_last_seen = time.ticks_ms()


def clamp_angle(angle):
    if angle < 0:
        return 0
    if angle > 180:
        return 180
    return angle


def set_servo_angle(servo_id, angle):
    if not HAS_SERVO:
        return False
    idx = servo_id - 1
    if idx < 0 or idx >= 3:
        return False
    if (not servo_status[idx]) or (servos[idx] is None):
        return False

    try:
        safe_angle = clamp_angle(angle)
        servos[idx].angle(value=safe_angle)
        return True
    except Exception as e:
        log_error("Servo {} move failed: {}".format(servo_id, e))
        return False


def open_servo_slot(servo_id):
    if set_servo_angle(servo_id, SERVO_OPEN_ANGLE):
        servo_positions[servo_id - 1] = "开"
        return True
    return False


def close_servo_slot(servo_id):
    if set_servo_angle(servo_id, SERVO_CLOSE_ANGLE):
        servo_positions[servo_id - 1] = "关"
        return True
    return False


def test_all_servos():
    try:
        for sid in range(1, 4):
            open_servo_slot(sid)
            time.sleep(0.2)
            close_servo_slot(sid)
            time.sleep(0.1)
        return True
    except Exception as e:
        log_error("Servo test failed: {}".format(e))
        return False


def draw_idle_ui():
    screen.draw_rect(0, 0, 240, 320, C_BLACK, C_BLACK)
    screen.draw_text(text="系 统 监 控 中", x=75, y=5, font_size=16, color=C_GRAY)
    screen.draw_text(
        text="温度: {}℃ | 湿度: {}%".format(current_temp, current_humi),
        x=10,
        y=30,
        font_size=16,
        color=C_CYAN,
    )

    screen.draw_rect(x=65, y=90, w=110, h=10, bcolor=C_GRAY, fcolor=C_WHITE)
    screen.draw_rect(x=70, y=100, w=100, h=70, bcolor=C_WHITE, fcolor=C_WHITE)
    screen.draw_rect(x=115, y=120, w=10, h=30, bcolor=C_RED, fcolor=C_RED)
    screen.draw_rect(x=105, y=130, w=30, h=10, bcolor=C_RED, fcolor=C_RED)

    screen.draw_text(text="智 能 药 箱", x=60, y=190, font_size=24, color=C_WHITE)
    screen.draw_text(text="SmartMedBox", x=75, y=225, font_size=23, color=C_GRAY)

    screen.draw_rect(x=10, y=270, w=220, h=40, bcolor=C_WHITE, fcolor=C_BLACK)
    screen.draw_text(text="[A] 或 [B] 停 止 警 报", x=30, y=280, font_size=16, color=C_GREEN)
    screen.show_draw()


def update_env_ui():
    screen.draw_rect(0, 28, 240, 30, C_BLACK, C_BLACK)
    screen.draw_text(
        text="温度: {}℃ | 湿度: {}%".format(current_temp, current_humi),
        x=10,
        y=30,
        font_size=16,
        color=C_CYAN,
    )
    screen.show_draw()


def draw_progress(current, total):
    pct = int(current / total * 100) if total > 0 else 0
    if pct > 100:
        pct = 100
    screen.draw_rect(20, 260, 200, 20, C_GRAY, C_GRAY)
    fill_w = int(200 * pct / 100)
    if fill_w > 0:
        screen.draw_rect(20, 260, fill_w, 20, C_GREEN, C_GREEN)
    screen.draw_rect(0, 290, 240, 30, C_BLACK, C_BLACK)
    screen.draw_text(
        text="{}KB / {}KB".format(int(current / 1024), int(total / 1024)),
        x=50,
        y=290,
        font_size=16,
        color=C_WHITE,
    )
    screen.show_draw()


def draw_capture_ui():
    screen.draw_rect(0, 0, 240, 320, C_BLACK, C_BLACK)
    screen.draw_text(text="静 默 抓 拍 中...", x=50, y=140, font_size=20, color=C_GRAY)
    screen.show_draw()


def _read_serial_line_nonblocking():
    if poll_obj is None:
        return ""
    try:
        if poll_obj.poll(0):
            return sys.stdin.readline().strip()
    except Exception as e:
        log_error("Read serial failed: {}".format(e))
    return ""


def _stop_alarm_and_back_to_idle():
    rgb.clear()
    try:
        _remove_file_if_exists("alarm.wav")
    except Exception as e:
        log_error("Del alarm failed: {}".format(e))
        send_err("FILE_OP_FAIL")
    draw_idle_ui()


def _handle_alarm_serial_command(msg):
    if not msg:
        return False

    if msg == "<<<CMD:STOP_ALARM>>>":
        update_connection_status()
        send_ack("STOP_ALARM")
        return True

    if msg == "<<<ALARM_AUDIO_START>>>":
        update_connection_status()
        receive_audio("alarm.wav", play_now=False)
        return False

    if msg == "<<<PING>>>":
        update_connection_status()
        send_ack("PING")
        return False

    if msg == "<<<ACK:K10>>>":
        update_connection_status()
        return False

    if msg == "<<<CMD:ALARM_MED>>>":
        update_connection_status()
        send_ack("ALARM_MED")
        return False

    if (
        msg == "<<<CMD:TAKE_PIC>>>"
        or msg == "<<<REPLY_START>>>"
        or msg.startswith("<<<CMD:SERVO_")
    ):
        update_connection_status()
        send_err("BUSY")
        return False

    return False


def _alarm_should_stop():
    if bt_a.status() == 1 or bt_b.status() == 1:
        return True

    msg = _read_serial_line_nonblocking()
    if msg:
        return _handle_alarm_serial_command(msg)
    return False


def freeze_and_send():
    send_ack("TAKE_PIC")
    gc.collect()
    draw_capture_ui()

    if camera is None:
        send_err("NO_CAMERA")
        draw_idle_ui()
        return

    try:
        for _ in range(5):
            camera.camera_capture()
            time.sleep(0.2)

        raw_ref = camera.camera_capture()
        if not raw_ref:
            send_err("CAPTURE_FAIL")
            draw_idle_ui()
            return

        img_base64 = ubinascii.b2a_base64(bytes(raw_ref))
        total_len = len(img_base64)
        raw_ref = None
        gc.collect()

        sys.stdout.write("<<<START:{}>>>\n".format(total_len))
        flush_stdout()

        chunk_size = 1024
        offset = 0
        draw_progress(0, total_len)

        while offset < total_len:
            end = min(offset + chunk_size, total_len)
            sys.stdout.write(img_base64[offset:end].decode("utf-8"))
            flush_stdout()
            time.sleep(0.015)
            offset = end
            if offset % 10240 == 0 or offset == total_len:
                draw_progress(offset, total_len)

        time.sleep(0.1)
        sys.stdout.write("\n<<<END>>>\n")
        flush_stdout()
    except Exception as e:
        log_error("Send image failed: {}".format(e))
        send_err("SEND_FAIL")
    finally:
        gc.collect()
        draw_idle_ui()


def trigger_env_alarm():
    screen.draw_rect(0, 0, 240, 320, C_YELLOW, C_YELLOW)
    screen.draw_text(text="环 境 警 报", x=55, y=80, font_size=24, color=C_BLACK)
    screen.draw_text(text="药 箱 温 度 过 高", x=35, y=140, font_size=20, color=C_RED)
    screen.draw_text(text="超 过 35.0 ℃", x=45, y=200, font_size=24, color=C_BLACK)
    screen.show_draw()

    while True:
        try:
            speaker.play_tone_music("BA_DING")
        except Exception:
            pass

        rgb.write(num=-1, R=255, G=255, B=0)
        for _ in range(5):
            if _alarm_should_stop():
                _stop_alarm_and_back_to_idle()
                return
            time.sleep(0.1)

        rgb.clear()
        for _ in range(3):
            if _alarm_should_stop():
                _stop_alarm_and_back_to_idle()
                return
            time.sleep(0.1)


def trigger_med_alarm():
    screen.draw_rect(0, 0, 240, 320, C_RED, C_RED)
    screen.draw_text(text="用 药 警 报", x=55, y=80, font_size=24, color=C_WHITE)
    screen.draw_text(text="请 立 即 服 药 !", x=35, y=200, font_size=24, color=C_WHITE)
    screen.show_draw()

    rgb.write(num=-1, R=255, G=0, B=0)

    # 先播放三声提示，再循环播放语音；无语音时使用蜂鸣器。
    for _ in range(3):
        if _alarm_should_stop():
            _stop_alarm_and_back_to_idle()
            return
        try:
            speaker.play_tone(2500, 400)
        except Exception:
            pass
        time.sleep(0.1)

    while True:
        rgb.write(num=-1, R=255, G=0, B=0)

        has_voice = False
        try:
            if "alarm.wav" in os.listdir() and os.stat("alarm.wav")[6] > 100:
                has_voice = True
        except Exception:
            pass

        if has_voice:
            try:
                speaker.play_sys_music("alarm.wav")
            except Exception:
                pass
        else:
            try:
                speaker.play_tone(2500, 400)
            except Exception:
                pass

        for _ in range(25):
            if _alarm_should_stop():
                _stop_alarm_and_back_to_idle()
                return
            time.sleep(0.1)

        rgb.clear()
        for _ in range(10):
            if _alarm_should_stop():
                _stop_alarm_and_back_to_idle()
                return
            time.sleep(0.1)


def receive_audio(filename, play_now=False):
    try:
        try:
            _remove_file_if_exists(filename)
        except Exception as e:
            log_error("Del file failed: {}".format(e))
            send_err("FILE_OP_FAIL")

        start_wait = time.ticks_ms()
        got_data = False

        with open(filename, "wb") as f:
            while True:
                if time.ticks_diff(time.ticks_ms(), start_wait) > AUDIO_RECV_TIMEOUT:
                    send_err("AUDIO_RECV_TIMEOUT")
                    break

                if poll_obj and poll_obj.poll(100):
                    line = sys.stdin.readline().strip()

                    if line.endswith("END>>>"):
                        send_ack("AUDIO_RECV")
                        break
                    elif line == "<<<REPLY_FAIL>>>":
                        break
                    elif line:
                        try:
                            chunk = ubinascii.a2b_base64(line)
                            if chunk:
                                f.write(chunk)
                                got_data = True
                                start_wait = time.ticks_ms()
                        except Exception as e:
                            log_error("Audio chunk decode failed: {}".format(e))
                            send_err("AUDIO_CHUNK_DECODE_FAIL")
                            break

        if play_now:
            try:
                if got_data and filename in os.listdir() and os.stat(filename)[6] > 100:
                    speaker.play_sys_music(filename)
            except Exception as e:
                log_error("Play audio failed: {}".format(e))
    except Exception as e:
        log_error("Receive audio failed: {}".format(e))
        send_err("AUDIO_RECV_ERROR")


def _parse_servo_id(msg, prefix):
    if not msg.startswith(prefix):
        return None
    if not msg.endswith(">>>"):
        return None
    sid_text = msg[len(prefix):-3]
    try:
        return int(sid_text)
    except Exception:
        return None


def _handle_servo_cmd(msg):
    if msg.startswith("<<<CMD:SERVO_OPEN:"):
        sid = _parse_servo_id(msg, "<<<CMD:SERVO_OPEN:")
        if sid is None:
            send_err("SERVO_CMD_PARSE_FAIL")
            return
        if open_servo_slot(sid):
            send_ack("SERVO_OPEN_{}".format(sid))
        else:
            send_err("SERVO_OPEN_FAIL_{}".format(sid))
        return

    if msg.startswith("<<<CMD:SERVO_CLOSE:"):
        sid = _parse_servo_id(msg, "<<<CMD:SERVO_CLOSE:")
        if sid is None:
            send_err("SERVO_CMD_PARSE_FAIL")
            return
        if close_servo_slot(sid):
            send_ack("SERVO_CLOSE_{}".format(sid))
        else:
            send_err("SERVO_CLOSE_FAIL_{}".format(sid))
        return

    if msg == "<<<CMD:SERVO_TEST_ALL>>>":
        if test_all_servos():
            send_ack("SERVO_TEST_ALL")
        else:
            send_err("SERVO_TEST_FAIL")
        return


def listen_to_m10():
    if poll_obj is None:
        return

    # 一次循环最多处理几条，避免占满主循环
    handled = 0
    while poll_obj.poll(0):
        handled += 1
        if handled > 6:
            break

        try:
            msg = sys.stdin.readline().strip()
        except Exception as e:
            log_error("Read cmd failed: {}".format(e))
            break

        if not msg:
            continue

        if msg == "<<<CMD:ALARM_MED>>>":
            update_connection_status()
            send_ack("ALARM_MED")
            trigger_med_alarm()

        elif msg == "<<<CMD:TAKE_PIC>>>":
            update_connection_status()
            freeze_and_send()

        elif msg == "<<<REPLY_START>>>":
            update_connection_status()
            receive_audio("reply.wav", play_now=True)

        elif msg == "<<<ALARM_AUDIO_START>>>":
            update_connection_status()
            receive_audio("alarm.wav", play_now=False)

        elif msg == "<<<CMD:STOP_ALARM>>>":
            update_connection_status()
            send_ack("STOP_ALARM")

        elif msg.startswith("<<<CMD:SERVO_"):
            update_connection_status()
            _handle_servo_cmd(msg)

        elif msg == "<<<PING>>>":
            update_connection_status()
            send_ack("PING")

        elif msg == "<<<ACK:K10>>>":
            update_connection_status()


def read_env_values():
    global current_temp, current_humi, sensor_error_count

    try:
        current_temp = round(temp_humi.read_temp() - TEMP_OFFSET, 1)
        current_humi = round(temp_humi.read_humi(), 1)
        sensor_error_count = 0
        return True
    except Exception as e:
        sensor_error_count += 1
        log_error("Sensor read failed {}: {}".format(sensor_error_count, e))
        if sensor_error_count >= 3:
            send_err("SENSOR_FAIL")
            sensor_error_count = 0
        return False


def init_hardware():
    global camera, poll_obj

    try:
        screen.init(dir=2)
        screen.clear()
    except Exception as e:
        log_error("Screen init failed: {}".format(e))

    try:
        speaker.reinit()
    except Exception as e:
        log_error("Speaker init failed: {}".format(e))
        send_err("SPEAKER_INIT_FAIL")

    if Camera is not None:
        try:
            camera = Camera()
            camera.init()
        except Exception as e:
            camera = None
            log_error("Camera init failed: {}".format(e))
            send_err("CAMERA_INIT_FAIL")
    else:
        camera = None
        send_err("NO_CAMERA_LIB")

    try:
        poll_obj = select.poll()
        poll_obj.register(sys.stdin, select.POLLIN)
    except Exception as e:
        poll_obj = None
        log_error("Serial init failed: {}".format(e))
        send_err("SERIAL_INIT_FAIL")

    if HAS_SERVO:
        for sid in range(1, 4):
            try:
                s = servo(sid)
                servos[sid - 1] = s
                servo_status[sid - 1] = True
                set_servo_angle(sid, SERVO_CLOSE_ANGLE)
                servo_positions[sid - 1] = "关"
            except Exception as e:
                servo_status[sid - 1] = False
                log_error("Servo {} init failed: {}".format(sid, e))

    read_env_values()


def keep_connection_fresh():
    global m10_connected
    if not m10_connected:
        return
    if time.ticks_diff(time.ticks_ms(), m10_last_seen) > HEARTBEAT_TIMEOUT:
        m10_connected = False


def main():
    global last_check_time

    init_hardware()
    draw_idle_ui()

    sys.stdout.write("<<<HELLO:K10>>>\n")
    flush_stdout()

    last_check_time = time.ticks_ms()

    while True:
        try:
            now = time.ticks_ms()
            if time.ticks_diff(now, last_check_time) > CHECK_INTERVAL:
                last_check_time = now
                if read_env_values():
                    update_env_ui()
                    if current_temp > MAX_TEMP or current_humi > MAX_HUMI:
                        trigger_env_alarm()

            listen_to_m10()
            keep_connection_fresh()
            time.sleep(0.1)
        except Exception as e:
            log_error("Main loop failed: {}".format(e))
            time.sleep(0.2)


if __name__ == "__main__":
    main()
