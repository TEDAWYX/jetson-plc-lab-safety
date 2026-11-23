#!/usr/bin/env python3
"""Draw a virtual floor zone in the live Nano preview and link it to the PLC."""

import argparse
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import threading
import time
import zlib

from nano_bottle_check import HEIGHT, WIDTH, UNKNOWN, monitor, monitor_with_plc
from nano_environment import EnvironmentMonitor
from nano_zone_check import cross, foreground_size, is_tape, sample_floor


CONFIG = Path.home() / ".lab_safety_virtual_zone.json"
PORT = 8765
HOST = "127.0.0.1:{}".format(PORT)
URL = "http://" + HOST
CLEAR_CONFIRM_SECONDS = 0.6

PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>实验室 · 虚拟警戒区</title>
<style>
:root{color-scheme:dark;font:16px system-ui,'Microsoft YaHei',sans-serif;background:#101820;color:#e8eff5}
body{margin:0}main{max-width:1040px;margin:auto;padding:26px 20px 32px}
header{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}
h1{font-size:clamp(22px,4vw,32px);margin:0;line-height:1.3}
#state{padding:7px 12px;border-radius:6px;background:#213348;color:#d1e4fa;font-weight:650;white-space:nowrap}
#state[data-mode="CLEAR"]{background:#1a3c33;color:#a6efcc}#state[data-mode="INTRUSION"]{background:#52232b;color:#ffd5d5}
#view{position:relative;aspect-ratio:16/9;background:#0b1119;border:1px solid #41546a;border-radius:8px;overflow:hidden}
canvas{display:block;width:100%;height:auto;cursor:crosshair}canvas.locked{cursor:default}
#instructions{color:#b9c9d9;min-height:1.3em;margin:14px 0 12px}
.actions{display:flex;gap:9px;flex-wrap:wrap}button{font:inherit;border:1px solid #50657b;border-radius:6px;background:#233347;color:#edf3fa;padding:8px 14px;cursor:pointer}
button:disabled{opacity:.45;cursor:default}button:focus-visible{outline:2px solid #fff;outline-offset:2px}
#save{background:#5de1c2;border-color:#5de1c2;color:#102b26;font-weight:750}
#environment{margin:14px 0;border-block:1px solid #41546a;padding:10px 0;color:#c8d6e4}
#environment dl{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));margin:0;gap:12px}
#environment dd{font-size:clamp(17px,3vw,23px);color:#edf3fa;margin:2px 0 0;font-variant-numeric:tabular-nums}
#environment p{font-size:14px;margin:7px 0 0;color:#a6c3d4}
#zone-storage{color:#ffd497;margin:8px 0;font-size:14px}
</style></head><body><main>
<header><h1>虚拟警戒区</h1><span id="state" role="status">等待画面</span></header>
<div id="view"><canvas id="frame" width="480" height="270" aria-label="摄像头画面和警戒区"></canvas></div>
<section id="environment" aria-label="实验台环境" hidden>
<dl><div><dt>温度</dt><dd id="temperature">—</dd></div><div><dt>湿度</dt><dd id="humidity">—</dd></div>
<div><dt>照度</dt><dd id="illuminance">—</dd></div></dl><p id="environment-status" role="status"></p>
</section>
<p id="instructions" aria-live="polite">清空地面警戒区，沿边界顺序点击至少三个顶点。</p>
<p id="zone-storage" role="status" hidden></p>
<div class="actions"><button id="undo" type="button" disabled>撤销一点</button><button id="clear" type="button" disabled>重置顶点</button><button id="save" type="button" disabled>保存并开始检测</button></div>
</main><script>
const canvas = document.querySelector('#frame');
const ctx = canvas.getContext('2d');
const state = document.querySelector('#state');
const instructions = document.querySelector('#instructions');
const zoneStorage = document.querySelector('#zone-storage');
const undo = document.querySelector('#undo');
const clear = document.querySelector('#clear');
const save = document.querySelector('#save');
const environment = document.querySelector('#environment');
const environmentStatus = document.querySelector('#environment-status');
function showEnvironment(data) {
  environment.hidden = !data;
  if (!data) return;
  for (const [key, digits, unit] of [['temperature',1,'℃'],['humidity',1,'%RH'],['illuminance',0,'lx']]) {
    document.getElementById(key).textContent = Number.isFinite(data[key]) ? data[key].toFixed(digits) + ' ' + unit : '—';
  }
  if (environmentStatus.textContent !== data.message) environmentStatus.textContent = data.message;
}
const labels = {SETUP: '待划区', ARMING: '正在读取空场', CLEAR: '区域清空', INTRUSION: '检测到侵入'};
let points = [], locked = false, saving = false, hasFrame = false, mode = 'SETUP', revision = 0;
let currentImage = null;
function render() {
  if (currentImage) ctx.drawImage(currentImage,0,0,canvas.width,canvas.height);
  if (points.length) {
    ctx.beginPath();
    points.forEach(([x,y],i) => i ? ctx.lineTo(x,y) : ctx.moveTo(x,y));
    if (points.length >= 3) {ctx.closePath();ctx.fillStyle = mode === 'INTRUSION' ? '#ff606848' : '#53dfbf38';ctx.fill();}
    ctx.strokeStyle = mode === 'INTRUSION' ? '#ff7881' : '#68f2d4';ctx.lineWidth = 2;ctx.stroke();
    points.forEach(([x,y],i) => {ctx.beginPath();ctx.arc(x,y,4,0,2*Math.PI);ctx.fillStyle='#f0f8ff';ctx.fill();
      ctx.font='bold 12px sans-serif';ctx.lineWidth=3;ctx.strokeStyle='#162430';ctx.strokeText(String(i+1),x+6,y-5);ctx.fillText(String(i+1),x+6,y-5);});
  }
  canvas.classList.toggle('locked',locked);
  undo.disabled = locked || saving || !points.length;
  clear.disabled = saving || (!locked && !points.length);
  save.disabled = locked || saving || points.length < 3;
}
canvas.addEventListener('click', event => {
  if (locked || saving || !hasFrame || points.length >= 12) return;
  const box = canvas.getBoundingClientRect();
  const x = Math.min(canvas.width-1,Math.max(0,Math.round((event.clientX-box.left)*canvas.width/box.width)));
  const y = Math.min(canvas.height-1,Math.max(0,Math.round((event.clientY-box.top)*canvas.height/box.height)));
  points.push([x,y]);render();
});
undo.addEventListener('click',() => {points.pop();render();});
function startEditing() {
  points=[];locked=false;mode='SETUP';
  state.textContent=labels[mode];state.dataset.mode=mode;
  instructions.textContent='清空地面警戒区，沿边界顺序点击至少三个顶点。';
}
async function editZone(path, data, onSuccess) {
  revision++;saving=true;render();
  try {
    const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    const result=await response.json();
    if (!response.ok) throw new Error(result.error);
    onSuccess();
  } catch (error) {instructions.textContent=error.message || '操作失败，请重试。';}
  finally {saving=false;render();}
}
clear.addEventListener('click',() => {
  editZone('/zone/reset',{},startEditing);
});
save.addEventListener('click',() => {
  editZone('/zone',points,() => {
    locked=true;instructions.textContent='请保持区域为空，等状态变为「区域清空」。';
  });
});
async function refresh() {
  const requestRevision=revision;
  try {
    const result=await (await fetch('/status',{cache:'no-store'})).json();
    showEnvironment(result.environment);
    if (!saving && requestRevision === revision) {
      if (!result.points && locked) startEditing();
      mode=result.state;
      state.textContent=result.detectorError ? '检测已停止' : labels[mode] || '等待';
      state.dataset.mode=result.detectorError ? 'STOPPED' : mode;
      if (result.points) {
        points=result.points;locked=true;
        if (mode === 'CLEAR' || mode === 'INTRUSION') instructions.textContent='画面中的有色区域就是 PLC 人员报警区。';
      }
      if (result.detectorError) {
        hasFrame=false;
        instructions.textContent=result.detectorError;
      } else if (result.setupError) {
        instructions.textContent=result.setupError;
      }
      const storageMessage=result.saveWarning || '';
      if (zoneStorage.textContent !== storageMessage) zoneStorage.textContent=storageMessage;
      zoneStorage.hidden=!storageMessage;
    }
    if (result.frame) {
      const view=new Image();
      view.onload=() => {currentImage=view;hasFrame=!result.detectorError;render();};
      view.src='/frame?'+result.frame;
    }
    render();
  } catch (error) {
    state.textContent='连接中断';
    if (!environment.hidden) showEnvironment({message:'数据连接中断'});
  }
  setTimeout(refresh,500);
}
refresh();
</script></body></html>""".encode("utf-8")


def polygon_edges(points):
    return list(zip(points, points[1:] + points[:1]))


def intersects(a, b, c, d):
    first, second = cross(a, b, c), cross(a, b, d)
    third, fourth = cross(c, d, a), cross(c, d, b)
    if first * second < 0 and third * fourth < 0:
        return True
    for value, p, q, r in ((first, a, b, c), (second, a, b, d), (third, c, d, a), (fourth, c, d, b)):
        if value == 0 and all(min(p[i], q[i]) <= r[i] <= max(p[i], q[i]) for i in (0, 1)):
            return True
    return False


def validate_polygon(points):
    if not isinstance(points, list) or not 3 <= len(points) <= 12:
        raise ValueError("请沿边界顺序点选 3 到 12 个顶点。")
    for point in points:
        if (not isinstance(point, list) or len(point) != 2 or any(type(v) != int for v in point)
                or not (0 <= point[0] < WIDTH and 0 <= point[1] < HEIGHT)):
            raise ValueError("顶点必须在相机画面内。")
    points = tuple(tuple(point) for point in points)
    if len(set(points)) != len(points):
        raise ValueError("顶点重复，请撤销重复的点。")
    edges = polygon_edges(points)
    for i, (a, b) in enumerate(edges):
        for j in range(i + 2, len(edges)):
            if (j + 1) % len(edges) != i and intersects(a, b, *edges[j]):
                raise ValueError("边界交叉，请按顺时针或逆时针依次点选。")
    area = abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in edges)) / 2
    if area < 2000:
        raise ValueError("区域太小，无法稳定检测。请画大一点或调整镜头。")
    return points


def contains(edges, x, y):
    inside = False
    for (ax, ay), (bx, by) in edges:
        if (ay > y) != (by > y) and x < ax + (bx - ax) * (y - ay) / (by - ay):
            inside = not inside
    return inside


def background_tape(frame):
    # Mask the tape seen at startup, with a small margin for camera jitter.
    # New red objects elsewhere are still part of the foreground test.
    channels = zip(frame[0::3], frame[1::3], frame[2::3])
    tape = ((pixel % WIDTH, pixel // WIDTH) for pixel, rgb in enumerate(channels) if is_tape(*rgb))
    return {(x + dx, y + dy) for x, y in tape for dy in range(-2, 3) for dx in range(-2, 3)}


def outside_light_reference(frame, edges):
    # Estimate global exposure and white balance changes from outside the zone.
    reference = []
    for y in range(6, HEIGHT, 12):
        for x in range(6, WIDTH, 12):
            if contains(edges, x, y):
                continue
            offset = 3 * (y * WIDTH + x)
            r, g, b = frame[offset:offset + 3]
            if 20 < min(r, g, b) and max(r, g, b) < 225:
                reference.append((offset, (r + 2 * g + b) // 4, r - g, b - g))
    return reference if len(reference) >= 80 else ()


def png(frame):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    stride = WIDTH * 3
    rows = b"".join(b"\x00" + frame[i:i + stride] for i in range(0, len(frame), stride))
    header = struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows, 1)) + chunk(b"IEND", b"")


class VirtualZone:
    def __init__(self, config, setup, desktop=False):
        self.config = config
        self.desktop = desktop
        self.lock = threading.Lock()
        self.detector_error = None
        self.reset()
        self.preview = b""
        self.frame_id = 0
        self.preview_at = 0
        if config.is_file() and not setup:
            try:
                self.points = validate_polygon(json.loads(config.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                self.setup_error = "无法读取上次的警戒区，请重新划区。"
            else:
                self.state = "ARMING"

    def reset(self):
        with self.lock:
            self.clear_zone()

    def clear_zone(self, error=None):
        self.points = None
        self.floor = None
        self.light_reference = ()
        self.clear_since = None
        self.save_pending = False
        self.state = "SETUP"
        self.setup_error = error
        self.save_warning = None

    def choose(self, data):
        with self.lock:
            if self.detector_error:
                raise ValueError(self.detector_error)
            if self.points is not None:
                raise ValueError("区域已保存。请点「重置顶点」后再画。")
            if not self.preview:
                raise ValueError("请等相机画面出现后再保存。")
            self.points = validate_polygon(data)
            self.setup_error = None
            self.state = "ARMING"
            self.save_pending = True

    def __call__(self, frame):
        with self.lock:
            return self.update(frame)

    def update(self, frame):
        now = time.monotonic()
        if now >= self.preview_at:
            self.preview = frame if self.desktop else png(frame)
            self.frame_id += 1
            self.preview_at = now + (0.25 if self.desktop else 0.5)
        points = self.points
        if points is None:
            return UNKNOWN, "draw the virtual zone at " + URL
        if self.floor is None:
            edges = polygon_edges(points)
            tape = background_tape(frame)

            def usable_floor(x, y):
                return (x, y) not in tape and contains(edges, x, y)

            try:
                self.floor = sample_floor(frame, points, usable_floor)
            except ValueError:
                self.clear_zone("区域内可用的地面太少。请避开强反光，圈一块更大的空地再保存。")
                return UNKNOWN, "too little clear floor inside the virtual zone"
            self.light_reference = outside_light_reference(frame, edges)
            if self.save_pending:
                pending = self.config.with_suffix(".tmp")
                try:
                    pending.write_text(json.dumps(points), encoding="utf-8")
                    pending.replace(self.config)
                except OSError as error:
                    self.save_warning = "警戒区已启用，仅本次运行有效。重启程序后需重新划区。"
                    print("Could not save the zone: {}".format(error), file=sys.stderr, flush=True)
                self.save_pending = False
            print("Virtual zone calibrated. The empty floor is the reference.", flush=True)

        largest = foreground_size(frame, self.floor, self.light_reference)
        if largest:
            self.clear_since = None
            self.state = "INTRUSION"
            return self.state, "foreground inside virtual zone, {} samples".format(largest)
        if self.state == "INTRUSION":
            if self.clear_since is None:
                self.clear_since = now
            if now - self.clear_since < CLEAR_CONFIRM_SECONDS:
                return self.state, "checking that the virtual zone stays empty"
        self.state = "CLEAR"
        return self.state, "virtual zone clear"


class SetupHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(2)

    def send_data(self, status, content_type, data):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, status, data):
        self.send_data(status, "application/json", json.dumps(data).encode())

    def do_GET(self):
        if self.headers.get("Host") != HOST:
            self.send_error(403)
            return
        zone = self.server.zone
        path = self.path.split("?", 1)[0]
        if path == "/":
            self.send_data(200, "text/html; charset=utf-8", PAGE)
        elif path == "/status":
            with zone.lock:
                status = {"points": zone.points, "state": zone.state, "frame": zone.frame_id,
                          "detectorError": zone.detector_error, "setupError": zone.setup_error,
                          "saveWarning": zone.save_warning}
            if self.server.environment is not None:
                status["environment"] = self.server.environment.status()
            self.send_json(200, status)
        elif path == "/frame" and zone.preview:
            self.send_data(200, "image/png", zone.preview)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.headers.get("Host") != HOST or self.headers.get("Origin", URL) != URL:
            self.send_error(403)
            return
        if self.path not in ("/zone", "/zone/reset") or self.headers.get_content_type() != "application/json":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 1024:
                raise ValueError("请在画面上重新点选边界。")
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            if self.path == "/zone/reset":
                if data != {}:
                    raise ValueError("重置请求格式错误。")
                self.server.zone.reset()
            else:
                self.server.zone.choose(data)
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
            return
        self.send_json(200, {"saved": True} if self.path == "/zone" else {"ok": True})

    def log_message(self, *args):
        pass


def detect(args, zone, environment, stop):
    try:
        if args.plc:
            monitor_with_plc(args, zone, zone=True, stop_event=stop,
                             on_plc_sample=environment.sync if environment else None)
        else:
            monitor(args.device, detector=zone, zone=True, stop_event=stop)
    except (OSError, RuntimeError) as error:
        if environment is not None and args.plc:
            environment.plc_disconnected()
        with zone.lock:
            zone.detector_error = "检测已停止：{}".format(error)
        print(zone.detector_error, file=sys.stderr, flush=True)


def run_desktop(args, zone, environment):
    try:
        import tkinter as tk
    except ImportError as error:
        raise RuntimeError("Desktop preview needs python3-tk. Run: sudo apt install python3-tk") from error
    try:
        root = tk.Tk()
    except tk.TclError as error:
        raise RuntimeError("Open the program from a terminal on the Nano desktop: {}".format(error)) from error

    stop = threading.Event()

    root.title("虚拟警戒区")
    root.resizable(False, False)
    status = tk.Label(root, text="等待摄像头画面", font=("TkDefaultFont", 14, "bold"))
    status.pack(fill="x", pady=5)
    canvas = tk.Canvas(root, width=WIDTH, height=HEIGHT, background="#20252a", highlightthickness=0)
    canvas.pack(padx=8)
    environment_label = None
    if environment is not None:
        environment_label = tk.Label(root, text="正在读取环境数据", font=("TkDefaultFont", 11))
        environment_label.pack(pady=(6, 1))
    photo = tk.PhotoImage(master=root, width=WIDTH, height=HEIGHT)
    canvas.create_image(0, 0, anchor="nw", image=photo)
    instructions = tk.Label(root, text="空场时沿边界顺序点击至少三个顶点。", wraplength=WIDTH)
    instructions.pack(pady=5)
    storage = tk.Label(root, text="", wraplength=WIDTH)
    controls = tk.Frame(root)
    controls.pack(pady=(0, 7))
    points = []
    displayed_id = 0
    labels = {"SETUP": ("待划区", "#243b55"), "ARMING": ("正在读取空场", "#705615"),
              "CLEAR": ("区域清空", "#155941"), "INTRUSION": ("检测到侵入", "#972938")}

    def draw():
        locked = zone.points is not None
        vertices = zone.points if locked else points
        canvas.delete("zone")
        color = "#ff6877" if zone.state == "INTRUSION" else "#58dfbf"
        if len(vertices) >= 2:
            path = list(vertices)
            if len(vertices) >= 3:
                path.append(vertices[0])
            canvas.create_line(*[value for p in path for value in p], fill=color, width=2, tags="zone")
        for number, (x, y) in enumerate(vertices, 1):
            canvas.create_oval(x-3, y-3, x+3, y+3, fill=color, outline=color, tags="zone")
            canvas.create_text(x+10, y-10, text=str(number), fill="white", tags="zone")
        undo.configure(state="normal" if points and not locked else "disabled")
        reset.configure(state="normal" if vertices else "disabled")
        save.configure(state="normal" if not locked and len(points) >= 3 else "disabled")

    def add_point(event):
        if zone.points is None and displayed_id and len(points) < 12:
            points.append((event.x, event.y))
            draw()

    def undo_point():
        points.pop()
        draw()

    def reset_points():
        zone.reset()
        points.clear()
        instructions.configure(text="空场时沿边界顺序点击至少三个顶点。")
        draw()

    def save_points():
        try:
            zone.choose([list(point) for point in points])
        except ValueError as error:
            instructions.configure(text=str(error))
            return
        points.clear()
        instructions.configure(text="如要重画，可点击「重置顶点」。")
        draw()

    undo = tk.Button(controls, text="撤销一点", command=undo_point, state="disabled")
    reset = tk.Button(controls, text="重置顶点", command=reset_points, state="disabled")
    save = tk.Button(controls, text="保存并开始检测", command=save_points, state="disabled")
    for button in (undo, reset, save):
        button.pack(side="left", padx=4)
    canvas.bind("<Button-1>", add_point)
    ppm_header = "P6\n{} {}\n255\n".format(WIDTH, HEIGHT).encode("ascii")

    def refresh():
        nonlocal displayed_id
        if environment_label is not None:
            data = environment.status()
            readings = data["message"]
            if "temperature" in data:
                readings = "{temperature:.1f} ℃    {humidity:.1f} %RH    {illuminance} lx    {message}".format(**data)
            environment_label.configure(text=readings)
        with zone.lock:
            frame_id, frame, phase = zone.frame_id, zone.preview, zone.state
            detector_error, setup_error, save_warning = zone.detector_error, zone.setup_error, zone.save_warning
        if save_warning:
            storage.configure(text=save_warning)
            if not storage.winfo_ismapped():
                storage.pack(before=controls, pady=(0, 5))
        else:
            storage.pack_forget()
        if detector_error:
            status.configure(text="检测已停止", background="#972938", foreground="white")
            instructions.configure(text=detector_error)
            for button in (undo, save):
                button.configure(state="disabled")
            root.after(200, refresh)
            return
        if setup_error:
            instructions.configure(text=setup_error)
            reset.configure(state="normal")
        if frame_id != displayed_id:
            photo.configure(data=ppm_header + frame, format="PPM")
            displayed_id = frame_id
            draw()
        if displayed_id:
            label, background = labels[phase]
            status.configure(text=label, background=background, foreground="white")
        root.after(200, refresh)

    worker = threading.Thread(target=detect, args=(args, zone, environment, stop), daemon=True)
    worker.start()
    root.after(0, refresh)
    try:
        root.mainloop()
    finally:
        stop.set()
        worker.join(timeout=3)
    return int(zone.detector_error is not None)


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plc", help="enable person alarms on this PLC TCP address")
    parser.add_argument("--device", default="/dev/video0")
    parser.add_argument("--port", type=int, default=502)
    parser.add_argument("--unit", type=int, default=1)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--setup", action="store_true", help="draw a new zone")
    parser.add_argument("--config", type=Path, default=CONFIG, help="path for the saved zone polygon")
    parser.add_argument("--show", action="store_true", help="open the preview on the Nano display")
    parser.add_argument("--desktop", action="store_true", help="show a lightweight local window without a web server")
    parser.add_argument("--sensor", help="IP address of the RS485-to-Ethernet sensor converter")
    parser.add_argument("--light-multiplier", type=int, choices=(1, 100), default=1,
                        help="set 100 for the 0–200000 lux sensor model")
    args = parser.parse_args()
    if not (1 <= args.port <= 65535 and 0 <= args.unit <= 255 and 0 <= args.offset <= 65534):
        parser.error("port, unit or offset is out of range")
    if args.sensor and args.plc and args.offset > 65531:
        parser.error("the environment registers exceed the Modbus address range")
    try:
        zone = VirtualZone(args.config, args.setup, desktop=args.desktop)
        with ExitStack() as resources:
            environment = None
            if args.sensor:
                environment = resources.enter_context(EnvironmentMonitor(args.sensor, args.light_multiplier, bool(args.plc)))
            if args.desktop:
                return run_desktop(args, zone, environment)
            server = resources.enter_context(HTTPServer(("127.0.0.1", PORT), SetupHandler))
            server.zone = zone
            server.environment = environment
            stop = threading.Event()
            worker = threading.Thread(target=detect, args=(args, zone, environment, stop), daemon=True)
            worker.start()
            try:
                print("Nano display preview: " + URL, flush=True)
                if args.show:
                    env = os.environ.copy()
                    env.setdefault("DISPLAY", ":0")
                    try:
                        subprocess.Popen(["xdg-open", URL], env=env)
                    except OSError:
                        print("Open the preview address in the Nano display's browser.", flush=True)
                server.serve_forever(poll_interval=0.2)
            finally:
                stop.set()
                worker.join(timeout=3)
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run())
