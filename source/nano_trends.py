"""Lightweight live sensor charts for the Nano desktop."""

import time
import tkinter as tk

from nano_environment import MAX_SAMPLE_AGE, TREND_SECONDS


SERIES = (
    ("温度", "℃", "#b3492d", 1.0, 1),
    ("湿度", "%RH", "#236f9a", 2.0, 1),
    ("照度", "lx", "#86611b", 10.0, 0),
)
BACKGROUND = "#f1f4f6"
INK = "#222f38"
SECONDARY = "#4d636f"


def draw_trends(canvas, samples, now):
    canvas.delete("plot")
    width = max(520, canvas.winfo_width())
    height = max(366, canvas.winfo_height())
    left, right = 58, width - 23
    row_height = (height - 33) / len(SERIES)
    start = now - TREND_SECONDS
    latest = samples[-1][1] if samples and now - samples[-1][0] <= MAX_SAMPLE_AGE else None

    def x_coordinate(stamp):
        return left + (stamp - start) * (right - left) / TREND_SECONDS

    for row, (name, unit, color, minimum_span, decimals) in enumerate(SERIES):
        top = 5 + row * row_height
        y_top, bottom = top + 26, top + row_height - 10
        canvas.create_rectangle(5, top, width - 5, bottom + 5, fill="white", outline="", tags="plot")
        canvas.create_text(16, top + 13, text="{}  {}".format(name, unit), anchor="w",
                           fill=INK, font=("sans-serif", 10, "bold"), tags="plot")
        current = "—" if latest is None else "{:.{}f} {}".format(latest[row], decimals, unit)
        canvas.create_text(right, top + 13, text=current, anchor="e",
                           fill=color, font=("sans-serif", 12, "bold"), tags="plot")
        values = [values[row] for _, values in samples if values is not None]
        for minute in range(6):
            x = x_coordinate(start + minute * 60)
            canvas.create_line(x, y_top, x, bottom, fill="#e2e9ed", tags="plot")
        if not values:
            canvas.create_text((left + right) / 2, (y_top + bottom) / 2, text="等待传感器数据",
                               fill=SECONDARY, tags="plot")
            continue

        middle = (min(values) + max(values)) / 2
        span = max(minimum_span, (max(values) - min(values)) * 1.25)
        if row == 1:
            span = min(span, 100)
        lower = middle - span / 2
        if row > 0:
            lower = max(0, lower)
        if row == 1:
            lower = min(lower, 100 - span)
        for fraction in (0, 0.5, 1):
            y = bottom - (bottom - y_top) * fraction
            canvas.create_line(left, y, right, y, fill="#e2e9ed", tags="plot")
            canvas.create_text(left - 7, y, text="{:.{}f}".format(lower + span * fraction, decimals),
                               fill=SECONDARY, anchor="e", tags="plot")

        previous = None
        for stamp, values in samples:
            if values is None:
                previous = None
                continue
            x = x_coordinate(stamp)
            y = bottom - (values[row] - lower) * (bottom - y_top) / span
            if previous and stamp - previous[0] <= MAX_SAMPLE_AGE:
                canvas.create_line(previous[1], previous[2], x, y, fill=color, width=2, tags="plot")
            else:
                canvas.create_oval(x - 2, y - 2, x + 2, y + 2, fill=color, outline="", tags="plot")
            previous = (stamp, x, y)

    for minute in range(6):
        label = "{} 分前".format(5 - minute) if minute < 5 else "现在"
        canvas.create_text(x_coordinate(start + minute * 60), height - 12, text=label, fill=SECONDARY, tags="plot")


def open_trends(root, environment):
    window = tk.Toplevel(root)
    window.title("温湿光实时曲线")
    window.minsize(520, 420)
    window.geometry("560x460")
    status = tk.Label(window, anchor="w", background=BACKGROUND, foreground=INK, padx=12, pady=6)
    status.pack(fill="x")
    canvas = tk.Canvas(window, background=BACKGROUND, highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    refresh_id = None

    def refresh():
        nonlocal refresh_id
        now = time.monotonic()
        status.configure(text="最近 5 分钟  ·  " + environment.status()["message"])
        draw_trends(canvas, environment.trend(now), now)
        refresh_id = window.after(1000, refresh)

    def close():
        if refresh_id is not None:
            window.after_cancel(refresh_id)
        window.destroy()

    window.protocol("WM_DELETE_WINDOW", close)
    refresh_id = window.after_idle(refresh)
    return window
