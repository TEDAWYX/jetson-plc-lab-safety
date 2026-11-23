#!/usr/bin/env python3
"""Detect the demo bottle's pose from its cap and label in the full camera frame."""

import argparse
import math
import socket
import subprocess
import sys
import threading
import time


WIDTH, HEIGHT = 480, 270
UNKNOWN = "UNKNOWN"
NEIGHBORS = ((-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0),
             (-1, 1), (0, 1), (1, 1))


def color_masks(frame):
    label = set()
    cap = set()
    channels = zip(frame[0::3], frame[1::3], frame[2::3])
    for pixel, (red, green, blue) in enumerate(channels):
        spread = red - blue
        if red < 110 or spread < 32:
            continue
        green_offset = green - blue
        if 100 * green_offset > 22 * spread and green <= red + 8:
            label.add((pixel % WIDTH, pixel // WIDTH))
        if red >= 125 and spread >= 42 and red - green >= 28 and 100 * green_offset < 57 * spread:
            cap.add((pixel % WIDTH, pixel // WIDTH))
    return label, cap


def components(mask, minimum, maximum):
    while mask:
        points = [mask.pop()]
        for x, y in points:
            for dx, dy in NEIGHBORS:
                neighbor = (x + dx, y + dy)
                if neighbor in mask:
                    mask.remove(neighbor)
                    points.append(neighbor)
        if minimum <= len(points) <= maximum:
            yield points


def centroid(points):
    size = len(points)
    return sum(x for x, _ in points) / size, sum(y for _, y in points) / size


def label_axis(points, center):
    cx, cy = center
    xx = yy = xy = 0
    for x, y in points:
        dx, dy = x - cx, y - cy
        xx += dx * dx
        yy += dy * dy
        xy += dx * dy
    spread = math.hypot(xx - yy, 2 * xy)
    if xx + yy + spread < 2 * (xx + yy - spread):
        return None
    axis = math.atan2(2 * xy, xx - yy) / 2
    ux, uy = math.cos(axis), math.sin(axis)
    positions = [x * ux + y * uy for x, y in points]
    return ux, uy, max(positions) - min(positions)


def bottle_state(frame):
    label_mask, cap_mask = color_masks(frame)
    caps = [(centroid(points), len(points)) for points in components(cap_mask, 22, 500)]
    matches = []
    for points in components(label_mask, 190, 4200):
        center = centroid(points)
        axis = label_axis(points, center)
        if not axis:
            continue
        ux, uy, length = axis
        best_match = None
        for (cap_x, cap_y), cap_size in caps:
            if not 0.035 <= cap_size / len(points) <= 0.29:
                continue
            dx, dy = cap_x - center[0], cap_y - center[1]
            along = abs(dx * ux + dy * uy) / length
            across = abs(dx * uy - dy * ux) / length
            if 0.64 <= along <= 1.65 and across <= 0.29:
                error = abs(along - 1.05) + across
                if best_match is None or error < best_match[0]:
                    best_match = (error, math.degrees(math.atan2(abs(dx), abs(dy))))
        if best_match is not None:
            matches.append(best_match[1])

    if len(matches) != 1:
        return UNKNOWN, "bottle features not found" if not matches else "multiple possible bottles"
    angle = matches[0]
    detail = "{:.0f} degrees from vertical".format(angle)
    if angle <= 30:
        return "UPRIGHT", detail
    if angle >= 60:
        return "TIPPED", detail
    return UNKNOWN, detail


def monitor(device, report_pose=None, detector=bottle_state, zone=False, stop_event=None):
    pipeline = [
        "gst-launch-1.0", "-q", "v4l2src", "device=" + device,
        "!", "decodebin", "!", "video/x-raw,width=1920,height=1080",
    ]
    if stop_event is not None:
        pipeline += ["!", "videorate", "drop-only=true", "max-rate=10"]
    pipeline += [
        "!", "videoconvert", "!", "videoscale", "!",
        "video/x-raw,format=RGB,width={},height={},pixel-aspect-ratio=1/1".format(WIDTH, HEIGHT),
        "!", "fdsink", "fd=1", "sync=false",
    ]
    frame_size = WIDTH * HEIGHT * 3
    pending = announced = None
    title = "Floor zone. Keep the area empty" if zone else "Full-frame bottle detection (cap + label)"
    print(title + ". Warming up camera...", flush=True)
    confirm_seconds = 0.12 if zone else 0.6
    with subprocess.Popen(pipeline, stdout=subprocess.PIPE) as camera:
        def stop_camera():
            if camera.poll() is None:
                camera.terminate()

        if stop_event is not None:
            def wait_for_close():
                stop_event.wait()
                stop_camera()

            watcher = threading.Thread(target=wait_for_close, daemon=True)
            watcher.start()
        start = time.monotonic()
        last_sample = last_report = start
        try:
            while True:
                frame = camera.stdout.read(frame_size)
                if stop_event is not None and stop_event.is_set():
                    break
                if len(frame) != frame_size:
                    raise RuntimeError("Camera stream ended. Check the GStreamer messages above.")
                now = time.monotonic()
                if now - start < 3 or now - last_sample < 0.15:
                    continue
                last_sample = now
                state, detail = detector(frame)
                if state != pending:
                    pending, since = state, now
                confirmed = now - since >= confirm_seconds
                if (confirmed and state != announced) or now - last_report >= 2:
                    display = state if confirmed else "CHECKING " + state
                    print(time.strftime("%H:%M:%S"), display, "(" + detail + ")", flush=True)
                    last_report = now
                if confirmed:
                    announced = state
                if report_pose is not None:
                    report_pose(state if confirmed else UNKNOWN, now)
        finally:
            if stop_event is None:
                stop_camera()
            else:
                stop_event.set()
                watcher.join()


def monitor_with_plc(args, detector=bottle_state, zone=False, stop_event=None, on_plc_sample=None):
    try:
        from nano_plc_check import PERSON_CLEAR, SCENARIOS, AlarmLink, ModbusError, VisionPublisher
    except ImportError as error:
        raise RuntimeError("Copy nano_plc_check.py next to nano_bottle_check.py") from error

    try:
        with socket.create_connection((args.plc, args.port), timeout=1.0) as connection:
            link = AlarmLink(connection, args.unit, args.offset)
            publisher = VisionPublisher(link)
            event_state, scenario = ("INTRUSION", "person") if zone else ("TIPPED", "bottle")

            def report_pose(pose, now):
                flags = SCENARIOS[scenario] if pose == event_state else 0
                if zone and pose == "CLEAR":
                    flags |= PERSON_CLEAR
                publisher.publish(pose != UNKNOWN, flags, now)
                if on_plc_sample is not None:
                    on_plc_sample(link)

            print("PLC connected. A confirmed {} event triggers the PLC alarm.".format(event_state), flush=True)
            try:
                monitor(args.device, report_pose, detector, zone, stop_event)
            finally:
                try:
                    publisher.stop()
                except (OSError, ModbusError) as error:
                    print("Could not send the final inactive frame. The PLC heartbeat will time out. {}".format(error),
                          file=sys.stderr)
    except ModbusError as error:
        raise RuntimeError("PLC: {}".format(error)) from error


def main(detector=bottle_state, zone=False):
    description = "Detect an intrusion into the red floor zone." if zone else __doc__
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--device", default="/dev/video0")
    parser.add_argument("--plc", help="enable live alarms on this PLC TCP address")
    parser.add_argument("--port", type=int, default=502)
    parser.add_argument("--unit", type=int, default=1)
    parser.add_argument("--offset", type=int, default=0, help="zero-based address of the Nano frame")
    args = parser.parse_args()
    if not (1 <= args.port <= 65535 and 0 <= args.unit <= 255 and 0 <= args.offset <= 65534):
        parser.error("port, unit or offset is out of range")
    try:
        if args.plc:
            monitor_with_plc(args, detector, zone)
        else:
            monitor(args.device, detector=detector, zone=zone)
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError) as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
