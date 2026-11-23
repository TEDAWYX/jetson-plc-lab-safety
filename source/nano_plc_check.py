#!/usr/bin/env python3
"""Camera-free connection test for the LabSafety TM12002 CODESYS project."""

import argparse
import socket
import struct
import sys
import time


VISION_READY = 0x04
PERSON_CLEAR = 0x08
VISION_ONLINE = 0x10
PHYSICAL_ALARMS = 0x03
VISION_ALARMS = 0x0C
LAMP_AND_BUZZER = 0x60
SCENARIOS = {"idle": 0, "person": 1, "bottle": 2, "both": 3}
STATUS_NAMES = ("light", "heat", "person", "bottle", "vision_online", "lamp", "buzzer")


class ModbusError(Exception):
    pass


class ModbusExceptionResponse(ModbusError):
    def __init__(self, function, code):
        super().__init__("Modbus function 0x{:02X} returned exception 0x{:02X}".format(function, code))
        self.code = code


class AlarmLink:
    def __init__(self, connection, unit, offset):
        self.connection = connection
        self.unit = unit
        self.offset = offset
        self.transaction = 0

    def _receive(self, size):
        data = bytearray()
        while len(data) < size:
            chunk = self.connection.recv(size - len(data))
            if not chunk:
                raise ModbusError("PLC closed the connection before the reply was complete")
            data.extend(chunk)
        return data

    def _exchange(self, request):
        self.transaction = (self.transaction + 1) & 0xFFFF
        header = struct.pack(">HHHB", self.transaction, 0, len(request) + 1, self.unit)
        self.connection.sendall(header + request)
        transaction, protocol, length, unit = struct.unpack(">HHHB", self._receive(7))
        if transaction != self.transaction or protocol != 0 or unit != self.unit:
            raise ModbusError("Unexpected Modbus reply header")
        if not 2 <= length <= 254:
            raise ModbusError("Invalid Modbus reply length")
        reply = self._receive(length - 1)
        function = request[0]
        if reply[0] == (function | 0x80) and len(reply) == 2:
            raise ModbusExceptionResponse(function, reply[1])
        if reply[0] != function:
            raise ModbusError("Unexpected Modbus reply function")
        return reply

    def read(self):
        return self.read_registers(self.offset, 2)

    def read_registers(self, start, count):
        reply = self._exchange(struct.pack(">BHH", 3, start, count))
        if len(reply) != 2 + 2 * count or reply[1] != 2 * count:
            raise ModbusError("Unexpected holding-register count")
        return struct.unpack_from(">{}H".format(count), reply, 2)

    def write_registers(self, start, values):
        count = len(values)
        payload = struct.pack(">{}H".format(count), *values)
        request = struct.pack(">BHHB", 16, start, count, len(payload)) + payload
        if self._exchange(request) != request[:5]:
            raise ModbusError("Unexpected multiple-register write acknowledgment")

    def write_register(self, offset, value):
        request = struct.pack(">BHH", 6, offset, value)
        if self._exchange(request) != request:
            raise ModbusError("PLC did not echo the written frame")

    def send_frame(self, frame):
        self.write_register(self.offset, frame)


class VisionPublisher:
    def __init__(self, link):
        self.link = link
        frame, status = link.read()
        if status & VISION_ONLINE:
            raise ModbusError("PLC already has an active vision sender. Stop it before starting this one.")
        self.sequence = frame >> 8
        self.next_send = 0
        self.last_status = None
        self.started = False

    def _send(self, bits):
        self.sequence = (self.sequence + 1) & 0xFF
        frame = (self.sequence << 8) | bits
        self.link.send_frame(frame)
        return frame

    def publish(self, ready, flags, now):
        if now < self.next_send:
            return
        self.next_send = now + 0.25
        self.started = True
        sent = self._send(VISION_READY | flags if ready else 0)
        echoed, status = self.link.read()
        if echoed != sent:
            raise ModbusError("PLC read-back differs from the frame just written")
        if status != self.last_status:
            print("PLC bottle={} lamp={} buzzer={} vision={} person={}".format(
                (status >> 3) & 1, (status >> 5) & 1, (status >> 6) & 1, (status >> 4) & 1,
                (status >> 2) & 1), flush=True)
            self.last_status = status

    def stop(self):
        if self.started:
            self._send(0)


def show_status(frame, status):
    print("Frame 0x{:04X}   Status 0x{:04X}".format(frame, status))
    print("  ".join("{}={}".format(name, (status >> bit) & 1)
                    for bit, name in enumerate(STATUS_NAMES)), flush=True)


def simulate(link, scenario, initial_frame, initial_status):
    events = SCENARIOS[scenario]
    if initial_status & VISION_ONLINE:
        raise ModbusError("Vision is already online. Stop the other sender first")
    if events and initial_status & (PHYSICAL_ALARMS | VISION_ALARMS):
        raise ModbusError("PLC already has latched alarms. Clear them before the event test")

    sequence = initial_frame >> 8
    print("Sending simulated {} frames. The PLC may activate sound and lights.".format(scenario),
          flush=True)
    deadline = time.monotonic() + (15 if scenario == "idle" else 3)
    try:
        while True:
            sequence = (sequence + 1) & 0xFF
            sent = (sequence << 8) | VISION_READY | events
            link.send_frame(sent)
            time.sleep(0.25)
            frame, status = link.read()
            if frame != sent:
                raise ModbusError("The PLC did not return the frame just written")
            if time.monotonic() >= deadline:
                break

        show_status(frame, status)
        expected = VISION_ONLINE
        if events:
            expected |= (events << 2) | LAMP_AND_BUZZER
        if (status & expected) != expected:
            raise ModbusError("The PLC application did not report the expected status flags")
    finally:
        test_failed = sys.exc_info()[0] is not None
        try:
            link.send_frame(((sequence + 1) & 0xFF) << 8)
        except (OSError, ModbusError) as error:
            print("Final idle frame failed. The PLC heartbeat timer will expire. {}".format(error),
                  file=sys.stderr)
            if not test_failed:
                raise

    print("PLC acknowledged the test. Any latched alarm still needs operator reset.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", help="PLC TCP-port IP address")
    parser.add_argument("--port", type=int, default=502)
    parser.add_argument("--unit", type=int, default=1)
    parser.add_argument("--offset", type=int, default=0, help="Zero-based address of Nano frame")
    parser.add_argument("--simulate", choices=tuple(SCENARIOS),
                        help="Send demo frames. Omit for a read-only connection check.")
    args = parser.parse_args()
    if not (1 <= args.port <= 65535 and 0 <= args.unit <= 255 and 0 <= args.offset <= 65534):
        parser.error("port, unit or offset is out of range")

    try:
        with socket.create_connection((args.host, args.port), timeout=2.0) as connection:
            link = AlarmLink(connection, args.unit, args.offset)
            frame, status = link.read()
            show_status(frame, status)
            if args.simulate:
                simulate(link, args.simulate, frame, status)
    except (OSError, ModbusError) as error:
        print("PLC check failed. {}".format(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
