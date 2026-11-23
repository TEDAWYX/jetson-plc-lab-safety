#!/usr/bin/env python3
"""Run the contest display on the Nano desktop."""

import argparse
from ipaddress import IPv4Address
from pathlib import Path
import socket
import threading
import time
import tkinter as tk
from tkinter import messagebox

from nano_bottle_check import HEIGHT, UNKNOWN, WIDTH, bottle_state, monitor
from nano_environment import EnvironmentMonitor
from nano_plc_check import AlarmLink, ModbusError, PERSON_CLEAR, SCENARIOS, VISION_ONLINE, VisionPublisher
from nano_virtual_zone import CONFIG, VirtualZone


PERSON, BOTTLE = "person", "bottle"
BOTTLE_UPRIGHT = 0x10
VISION_FLAGS = {
    (PERSON, "CLEAR"): PERSON_CLEAR,
    (PERSON, "INTRUSION"): SCENARIOS["person"],
    (BOTTLE, "UPRIGHT"): BOTTLE_UPRIGHT,
    (BOTTLE, "TIPPED"): SCENARIOS["bottle"],
}
LABELS = {
    "SETUP": ("请划定警戒区", "#314c65"),
    "ARMING": ("正在读取空场", "#6d541b"),
    "CLEAR": ("警戒区正常", "#155941"),
    "INTRUSION": ("检测到人员进入", "#9c2c37"),
    "UPRIGHT": ("样品瓶直立", "#155941"),
    "TIPPED": ("检测到样品瓶倾倒", "#9c2c37"),
    UNKNOWN: ("等待识别", "#314c65"),
}
ALARM_REASONS = ("光输入报警", "热输入报警", "危险区有人进入", "样品瓶倾倒")
POSE_ALARMS = {"INTRUSION": 0x04, "TIPPED": 0x08}
SENSOR_CONFIG = Path(__file__).resolve().with_name("sensor.conf")
SOFTWARE_RESET_SUPPORTED = 0x80
RESET_REQUEST_OFFSET = 5


class EnvironmentDisplay:
    def __init__(self, config):
        self.config = config
        self.monitor = None
        self.error = ""
        try:
            address, multiplier = self.config.read_text(encoding="utf-8").splitlines()
            self.configure(address, multiplier, save=False)
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            self.error = "传感器设置无法读取，请重新设置"

    def configure(self, address, multiplier, save=True):
        try:
            address = IPv4Address(address.strip())
        except ValueError as error:
            raise ValueError("请输入塔石转换器的 IPv4 地址") from error
        if address.is_unspecified or address.is_loopback or address.is_multicast or address.is_reserved:
            raise ValueError("请输入塔石转换器的局域网地址")
        if multiplier not in ("1", "100"):
            raise ValueError("照度倍率只能选择 ×1 或 ×100")
        address = str(address)
        if save:
            pending = self.config.with_suffix(".tmp")
            pending.write_text(address + "\n" + multiplier + "\n", encoding="utf-8")
            pending.replace(self.config)
        previous = self.monitor
        if previous is not None:
            previous.stop.set()
        new_monitor = EnvironmentMonitor(address, int(multiplier), plc_enabled=True)
        new_monitor.worker.start()
        self.monitor = new_monitor
        self.error = ""

    def status(self):
        monitor = self.monitor
        if monitor is None:
            return {"message": self.error or "请点击「传感器设置」填写转换器 IP"}
        data = monitor.status()
        if "temperature" not in data:
            return {"message": "等待传感器数据 · " + monitor.host}
        return data

    def sync(self, link):
        monitor = self.monitor
        if monitor is not None:
            monitor.sync(link)

    def trend(self, now):
        monitor = self.monitor
        return monitor.trend(now) if monitor is not None else []

    def plc_disconnected(self):
        monitor = self.monitor
        if monitor is not None:
            monitor.plc_disconnected()

    def close(self):
        if self.monitor is not None:
            self.monitor.close()


def warning_text(pose, status):
    active = (status or 0) | POSE_ALARMS.get(pose, 0)
    reasons = [reason for bit, reason in enumerate(ALARM_REASONS) if active & (1 << bit)]
    return "WARNING  ·  " + "、".join(reasons) if reasons else ""


def sensor_settings(root, environment):
    dialog = tk.Toplevel(root)
    dialog.title("传感器设置")
    dialog.transient(root)
    dialog.resizable(False, False)
    dialog.grab_set()
    monitor = environment.monitor
    address = tk.StringVar(value=monitor.host if monitor else "")
    multiplier = tk.StringVar(value=str(monitor.light_multiplier) if monitor else "1")
    feedback = tk.StringVar()
    form = tk.Frame(dialog, padx=14, pady=10)
    form.pack()
    tk.Label(form, text="塔石转换器 IP").grid(row=0, column=0, sticky="w", pady=4)
    entry = tk.Entry(form, textvariable=address, width=22)
    entry.grid(row=0, column=1, padx=8)
    tk.Label(form, text="照度倍率").grid(row=1, column=0, sticky="w", pady=4)
    tk.OptionMenu(form, multiplier, "1", "100").grid(row=1, column=1, sticky="w", padx=8)
    tk.Label(form, textvariable=feedback, foreground="#9c2c37", wraplength=300).grid(row=2, columnspan=2)

    def save(event=None):
        try:
            environment.configure(address.get(), multiplier.get())
        except ValueError as error:
            feedback.set(str(error))
        except OSError:
            feedback.set("无法保存传感器设置，请检查安装目录权限")
        else:
            dialog.destroy()

    tk.Button(form, text="保存并连接", command=save).grid(row=3, columnspan=2, pady=(5, 0))
    dialog.bind("<Return>", save)
    dialog.bind("<Escape>", lambda event: dialog.destroy())
    entry.focus_set()


class ContestState:
    def __init__(self):
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.camera_stop = threading.Event()
        self.mode = None
        self.bottle_armed = False
        self.last_bottle_seen = 0
        self.zone = VirtualZone(CONFIG, setup=False, desktop=True)
        self.pose = self.pending = UNKNOWN
        self.since = self.last_sample = 0
        self.frame = None
        self.frame_time = 0
        self.camera_error = ""
        self.plc_message = "正在连接 PLC"
        self.plc_status = None
        self.reset_pending = False
        self.reset_message = ""

    def select(self, mode, redraw=False):
        with self.lock:
            if self.reset_pending:
                return
            if mode == PERSON:
                self.zone = VirtualZone(CONFIG, setup=redraw, desktop=True)
            self.mode = mode
            self.bottle_armed = False
            self.last_bottle_seen = 0
            self.pose = self.pending = UNKNOWN
            self.last_sample = 0

    def request_reset(self):
        with self.lock:
            if self.reset_pending:
                return
            if self.plc_status is None:
                self.reset_message = "PLC 未连接，无法发送复位"
                return
            if not self.plc_status & SOFTWARE_RESET_SUPPORTED:
                self.reset_message = "PLC 需要更新为支持一键复位的版本"
                return
            self.mode = None
            self.bottle_armed = False
            self.pose = self.pending = UNKNOWN
            self.last_sample = 0
            self.reset_pending = True
            self.reset_message = "正在等待 PLC 复位确认…"

    def finish_reset(self, message, status=None):
        with self.lock:
            self.reset_pending = False
            self.reset_message = message
            if status is not None:
                self.plc_status = status

    def reset_plc(self, link):
        _, status = link.read()
        if not status & SOFTWARE_RESET_SUPPORTED:
            self.finish_reset("PLC 需要更新为支持一键复位的版本", status)
            return
        offset = link.offset + RESET_REQUEST_OFFSET
        request, acknowledged = link.read_registers(offset, 2)
        token = acknowledged % 65535 + 1
        if token == request:
            token = token % 65535 + 1
        link.write_register(offset, token)
        for _ in range(8):
            if self.stop.wait(0.075):
                return
            registers = link.read_registers(link.offset + 1, RESET_REQUEST_OFFSET + 1)
            status = registers[0]
            if registers[-1] == token:
                if status & 0x40:
                    message = "PLC 已复位，蜂鸣器仍报警，请检查当前输入"
                elif status & 0x20:
                    message = "蜂鸣器已停，有输入仍处于报警状态"
                else:
                    message = "复位成功 · 红灯灭、蜂鸣器停"
                self.finish_reset(message, status)
                return
        self.finish_reset("未收到 PLC 复位确认，请检查后重试", status)

    def arm_bottle(self):
        with self.lock:
            if (self.mode == BOTTLE and self.pose == "UPRIGHT"
                    and time.monotonic() - self.last_sample < 1.5):
                self.bottle_armed = True

    def detect(self, frame):
        now = time.monotonic()
        with self.lock:
            self.frame = frame
            self.frame_time = now
            if self.mode == PERSON:
                pose, detail = self.zone(frame)
                confirmation = 0.12
            elif self.mode == BOTTLE:
                if now - self.last_bottle_seen > 1.5:
                    self.bottle_armed = False
                pose, detail = bottle_state(frame)
                if pose != UNKNOWN:
                    self.last_bottle_seen = now
                if pose == "TIPPED" and not self.bottle_armed:
                    pose = UNKNOWN
                confirmation = 0.6
            else:
                return UNKNOWN, "waiting for mode selection"
            if pose != self.pending:
                self.pending, self.since = pose, now
            self.pose = pose if now - self.since >= confirmation else UNKNOWN
            self.last_sample = now
            return self.pose, detail

    def flags(self, now):
        with self.lock:
            if now - self.last_sample < 1.5:
                return VISION_FLAGS.get((self.mode, self.pose))
        return None

    def capture(self, device):
        while not self.stop.is_set():
            self.camera_stop.clear()
            if self.stop.is_set():
                return
            try:
                monitor(device, detector=self.detect, stop_event=self.camera_stop)
            except (OSError, RuntimeError):
                with self.lock:
                    self.frame = None
                    self.pose = self.pending = UNKNOWN
                    self.bottle_armed = False
                    self.last_sample = 0
                    self.camera_error = "摄像头未就绪，请检查 USB 或关闭其他摄像头程序"
            self.stop.wait(2)

    def update_plc(self, message, status=None):
        with self.lock:
            self.plc_message, self.plc_status = message, status

    def connect(self, host, environment):
        while not self.stop.is_set():
            try:
                with socket.create_connection((host, 502), timeout=1.0) as connection:
                    link = AlarmLink(connection, unit=1, offset=0)
                    self.exchange(link, environment)
            except (OSError, ModbusError):
                self.update_plc("PLC 未连接，正在重试 · 检查网线和 Nano 有线 IP")
                if self.reset_pending:
                    self.finish_reset("PLC 连接中断，复位结果待确认")
                if environment:
                    environment.plc_disconnected()
                self.stop.wait(1)

    def exchange(self, link, environment):
        publisher = None
        try:
            while not self.stop.wait(0.25):
                with self.lock:
                    reset_requested = self.reset_pending
                if reset_requested:
                    self.reset_plc(link)
                    continue
                now = time.monotonic()
                flags = self.flags(now)
                if publisher is None:
                    _, status = link.read()
                    if status & VISION_ONLINE:
                        self.update_plc("PLC 已连接 · 请关闭旧的检测程序", status)
                        continue
                    if flags is not None:
                        publisher = VisionPublisher(link)
                if publisher is not None:
                    publisher.publish(flags is not None, flags or 0, now)
                    status = publisher.last_status
                self.update_plc("PLC 已连接", status)
                if environment:
                    environment.sync(link)
        finally:
            if publisher is not None:
                try:
                    publisher.stop()
                except (OSError, ModbusError):
                    pass


def relay_status(status):
    if status is None:
        return ""
    sound = "蜂鸣器响" if status & 0x40 else "蜂鸣器停"
    lamp = "红灯亮" if status & 0x20 else "红灯灭"
    return "PLC 回读  ·  " + lamp + "  ·  " + sound


def run_window(root, state, environment):
    root.title("实验台安全演示")
    root.resizable(False, False)
    modes = tk.Frame(root)
    modes.pack(pady=(9, 4))
    banner = tk.Label(root, font=("sans-serif", 15, "bold"), foreground="white", wraplength=WIDTH-16)
    banner.pack(fill="x", padx=8, pady=4)
    canvas = tk.Canvas(root, width=WIDTH, height=HEIGHT, background="#20252a", highlightthickness=0)
    canvas.pack(padx=8)
    photo = tk.PhotoImage(master=root, width=WIDTH, height=HEIGHT)
    picture = canvas.create_image(0, 0, anchor="nw", image=photo)
    hint = tk.Label(root, wraplength=WIDTH)
    hint.pack(pady=3)
    region = tk.Frame(root)
    region.pack(pady=(0, 4))
    environment_panel = tk.Frame(root)
    environment_panel.pack(fill="x", padx=8, pady=(2, 0))
    readings = {}
    for column, (key, name) in enumerate((("temperature", "温度"), ("humidity", "湿度"), ("illuminance", "照度"))):
        environment_panel.grid_columnconfigure(column, weight=1, uniform="readings")
        tk.Label(environment_panel, text=name).grid(row=0, column=column)
        readings[key] = tk.Label(environment_panel, text="—", font=("sans-serif", 14, "bold"))
        readings[key].grid(row=1, column=column, pady=(0, 2))
    sensor_row = tk.Frame(root)
    sensor_row.pack(fill="x", padx=12, pady=(0, 4))
    sensor_status = tk.Label(sensor_row, wraplength=WIDTH-126, anchor="w")
    sensor_status.pack(side="left", fill="x", expand=True)
    trend_window = None

    def show_trends():
        nonlocal trend_window
        if trend_window is not None and trend_window.winfo_exists():
            trend_window.lift()
            return
        try:
            from nano_trends import open_trends
        except (ImportError, SyntaxError) as error:
            print("Cannot load the trend window: {}".format(error), flush=True)
            messagebox.showerror("实时曲线", "实时曲线模块加载失败，请重新安装演示程序。", parent=root)
            return
        trend_window = open_trends(root, environment)

    tk.Button(sensor_row, text="实时曲线", command=show_trends).pack(side="right")
    connection = tk.Label(root, wraplength=WIDTH)
    connection.pack()
    alarm = tk.Label(root, font=("sans-serif", 10, "bold"))
    alarm.pack(pady=(2, 9))
    reset_result = tk.Label(root, wraplength=WIDTH)
    reset_result.pack(pady=(0, 8))

    points = []
    shown = None
    setup_message = ""
    header = "P6\n{} {}\n255\n".format(WIDTH, HEIGHT).encode("ascii")

    def select(mode, redraw=False):
        nonlocal setup_message
        if mode == PERSON and state.mode == PERSON and not redraw:
            return
        state.select(mode, redraw)
        points.clear()
        setup_message = ""

    def add_point(event):
        if state.mode == PERSON and state.zone.points is None and shown is not None and len(points) < 12:
            points.append([event.x, event.y])

    def undo_point():
        if points:
            points.pop()

    def save_points():
        nonlocal setup_message
        try:
            state.zone.choose(points)
        except ValueError as error:
            setup_message = str(error)
        else:
            setup_message = ""
            points.clear()

    def draw_points(vertices, red):
        canvas.delete("zone")
        color = "#ff6877" if red else "#58dfbf"
        if len(vertices) >= 2:
            path = list(vertices) + ([vertices[0]] if len(vertices) >= 3 else [])
            canvas.create_line(*[v for point in path for v in point], fill=color, width=2, tags="zone")
        for x, y in vertices:
            canvas.create_oval(x-3, y-3, x+3, y+3, fill=color, outline=color, tags="zone")

    def refresh():
        nonlocal shown
        with state.lock:
            frame, mode, pose = state.frame, state.mode, state.pose
            bottle_armed = state.bottle_armed
            camera_error = state.camera_error
            if frame is not None and time.monotonic() - state.frame_time > 1.5:
                frame = None
                camera_error = "摄像头画面中断，请检查 USB"
            message, status = state.plc_message, state.plc_status
            reset_pending, reset_message = state.reset_pending, state.reset_message
            zone = state.zone
            with zone.lock:
                phase, vertices = zone.state, zone.points
                error = zone.setup_error or zone.save_warning

        if frame is not shown:
            if frame is not None:
                photo.configure(data=header + frame, format="PPM")
            canvas.itemconfigure(picture, state="normal" if frame is not None else "hidden")
            shown = frame
        if mode == PERSON:
            show_phase = phase if phase in ("SETUP", "ARMING") else pose
            label, color = LABELS[show_phase]
            action = "清空区域，沿边界点击至少三个点，然后保存。" if vertices is None else "机位改变后，清空区域并重新划区。"
        elif mode == BOTTLE:
            label, color = LABELS[pose]
            action = ("报警后可点「一键复位」结束本轮演示。" if bottle_armed
                      else "对准直立的样品瓶，识别成功后点「开始瓶倒测试」。")
        else:
            label, color = "请选择演示项目", "#314c65"
            action = "先对准地面或样品瓶，再选择对应检测。"
        if frame is None:
            label, color = "等待摄像头", "#314c65"
            action = camera_error or "摄像头正在启动"
        warning = warning_text(pose if frame is not None else UNKNOWN, status)
        banner.configure(text=warning or label, background="#9c2c37" if warning else color)
        action = (setup_message or error or action) if mode == PERSON and frame else action
        hint.configure(text=(label + " · " if warning else "") + action)
        draw_points((vertices or points) if mode == PERSON and frame else [], pose == "INTRUSION")
        for button in (undo, redraw, save):
            button.configure(state="normal" if mode == PERSON and frame else "disabled")
        undo.configure(state="normal" if mode == PERSON and frame and points else "disabled")
        save.configure(state="normal" if mode == PERSON and frame and len(points) >= 3 else "disabled")
        if mode == BOTTLE:
            if not arm.winfo_manager():
                arm.pack(side="left", padx=3)
            arm.configure(text="瓶倒测试已开始" if bottle_armed else "开始瓶倒测试",
                          state="normal" if frame and pose == "UPRIGHT" and not bottle_armed else "disabled")
        else:
            arm.pack_forget()
        connection.configure(text=message)
        alarm.configure(text=relay_status(status))
        reset_button.configure(state="disabled" if reset_pending else "normal")
        reset_result.configure(text=reset_message)
        data = environment.status()
        for key, template in (("temperature", "{:.1f} ℃"), ("humidity", "{:.1f} %RH"), ("illuminance", "{} lx")):
            value = template.format(data[key]) if key in data else "—"
            readings[key].configure(text=value)
        sensor_status.configure(text=data["message"])
        root.after(250, refresh)

    for label, mode in (("人员警戒区", PERSON), ("样品瓶倾倒", BOTTLE)):
        tk.Button(modes, text=label, command=lambda selected=mode: select(selected), width=13).pack(side="left", padx=3)
    tk.Button(modes, text="传感器设置", command=lambda: sensor_settings(root, environment), width=11).pack(side="left", padx=3)
    tk.Button(modes, text="退出", command=root.quit, width=6).pack(side="left", padx=3)
    undo = tk.Button(region, text="撤销一点", command=undo_point)
    redraw = tk.Button(region, text="重新划区", command=lambda: select(PERSON, redraw=True))
    save = tk.Button(region, text="保存警戒区", command=save_points)
    arm = tk.Button(region, text="开始瓶倒测试", command=state.arm_bottle)
    for button in (undo, redraw, save):
        button.pack(side="left", padx=3)
    reset_button = tk.Button(root, text="一键复位", command=state.request_reset, width=14)
    reset_button.pack(before=reset_result, pady=(0, 4))
    canvas.bind("<Button-1>", add_point)
    root.protocol("WM_DELETE_WINDOW", root.quit)
    root.after(0, refresh)
    root.mainloop()
    root.destroy()


def main():
    import fcntl

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plc", default="192.168.3.130")
    parser.add_argument("--device", default="/dev/video0")
    parser.add_argument("--sensor")
    parser.add_argument("--light-multiplier", choices=("1", "100"), default="1")
    args = parser.parse_args()
    root = tk.Tk()
    root.withdraw()
    with (Path.home() / ".lab-safety-contest.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            messagebox.showinfo("实验台安全演示", "演示窗口已打开。", parent=root)
            root.destroy()
            return
        root.deiconify()
        run(root, args)


def run(root, args):
    state = ContestState()
    environment = EnvironmentDisplay(SENSOR_CONFIG)
    try:
        if args.sensor:
            environment.configure(args.sensor, args.light_multiplier, save=False)
        workers = [threading.Thread(target=state.capture, args=(args.device,), daemon=True),
                   threading.Thread(target=state.connect, args=(args.plc, environment), daemon=True)]
        for worker in workers:
            worker.start()
        try:
            run_window(root, state, environment)
        finally:
            state.stop.set()
            state.camera_stop.set()
            for worker in workers:
                worker.join(timeout=4)
    finally:
        environment.close()


if __name__ == "__main__":
    main()
