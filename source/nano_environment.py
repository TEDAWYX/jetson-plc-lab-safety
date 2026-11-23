"""Read the Renkeer three-in-one sensor and verify the PLC register copy."""

from collections import deque
import socket
import threading
import time

from nano_plc_check import AlarmLink, ModbusError, ModbusExceptionResponse


POLL_INTERVAL = 2.0
MAX_SAMPLE_AGE = 6.0
TREND_SECONDS = 300


class EnvironmentMonitor:
    def __init__(self, host, light_multiplier, plc_enabled):
        self.host = host
        self.light_multiplier = light_multiplier
        self.plc_enabled = plc_enabled
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.worker = threading.Thread(target=self.poll, daemon=True)
        self.sample = None
        self.history = deque(maxlen=2 * (TREND_SECONDS // int(POLL_INTERVAL) + 1))
        self.confirmed = None
        self.last_attempt = None
        self.plc_failed = False
        self.single_register_writes = False

    def __enter__(self):
        self.worker.start()
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self.stop.set()
        self.worker.join(timeout=3)

    def poll(self):
        while not self.stop.is_set():
            try:
                with socket.create_connection((self.host, 502), timeout=2.0) as connection:
                    sensor = AlarmLink(connection, unit=1, offset=0)
                    while not self.stop.is_set():
                        values = sensor.read_registers(0, 7)
                        with self.lock:
                            self.sample = (time.monotonic(), (values[1], values[0], values[6]))
                            self.history.append(self.sample)
                        self.stop.wait(POLL_INTERVAL)
            except (OSError, ModbusError):
                with self.lock:
                    if self.sample is not None:
                        self.history.append((time.monotonic(), None))
                    self.sample = None
                    self.confirmed = None
                self.stop.wait(POLL_INTERVAL)

    def sync(self, link):
        with self.lock:
            sample = self.sample
            if (sample is None or sample[0] == self.last_attempt
                    or time.monotonic() - sample[0] > MAX_SAMPLE_AGE):
                return
            self.last_attempt = sample[0]

        offset = link.offset + 2
        values = sample[1]
        try:
            if not self.single_register_writes:
                try:
                    link.write_registers(offset, values)
                except ModbusExceptionResponse as error:
                    if error.code != 1:
                        raise
                    self.single_register_writes = True
            if self.single_register_writes:
                for index, value in enumerate(values):
                    link.write_register(offset + index, value)
            matched = link.read_registers(offset, len(values)) == values
        except ModbusExceptionResponse:
            matched = False

        with self.lock:
            self.plc_failed = not matched
            self.confirmed = sample if matched else None

    def status(self):
        with self.lock:
            sample, confirmed, plc_failed = self.sample, self.confirmed, self.plc_failed
        fresh = sample is not None and time.monotonic() - sample[0] <= MAX_SAMPLE_AGE
        if not fresh:
            return {"message": "传感器暂无数据"}

        message = "传感器直读"
        if self.plc_enabled:
            if confirmed is not None and time.monotonic() - confirmed[0] <= MAX_SAMPLE_AGE:
                sample = confirmed
                message = "PLC 已确认"
            else:
                message = "PLC 同步失败" if plc_failed else "等待 PLC 确认"
        temperature, humidity, illuminance = self.decode(sample[1])
        return {"message": message, "temperature": temperature,
                "humidity": humidity, "illuminance": illuminance}

    def decode(self, values):
        temperature, humidity, illuminance = values
        if temperature >= 0x8000:
            temperature -= 0x10000
        return temperature / 10.0, humidity / 10.0, illuminance * self.light_multiplier

    def trend(self, now):
        with self.lock:
            while self.history and self.history[0][0] < now - TREND_SECONDS:
                self.history.popleft()
            samples = tuple(self.history)
        return [(stamp, self.decode(raw) if raw is not None else None) for stamp, raw in samples]

    def plc_disconnected(self):
        with self.lock:
            self.confirmed = None
            self.plc_failed = True
