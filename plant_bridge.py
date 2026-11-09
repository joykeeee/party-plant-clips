#!/usr/bin/env python3
"""Plant Band -> Sonic Pi bridge.

Reads the sensor stream from Alex's PlantBandUSB Arduino sketch (newline-delimited
JSON at 115200 baud), turns each reading into a 0-1 value, and sends it to Sonic Pi
as an OSC message. Sonic Pi receives these as cues such as /osc:127.0.0.1:PORT/plant/bend.

    /plant/bend   0-1  stem bend (flex sensor)      sweeps and glides the arpeggio
    /plant/pinch  0-1  leaf pinch (FSR)             plays the chord progression and arpeggio
    /plant/tilt   0-1  pot tilt (MPU6050)           slow bloom
    /plant/touch  0/1  stem touch (MPR121 E0)       drum pattern

Usage:
    python3 plant_bridge.py                 play with the plant
    python3 plant_bridge.py --calibrate     save the plant's resting and playing positions
    python3 plant_bridge.py --demo          no Arduino needed: a scripted performance

Only the hardware modes need pyserial (pip install pyserial).
"""
import argparse
import json
import math
import os
import socket
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CALIBRATION_FILE = os.path.join(HERE, "calibration.json")
SENSORS = ("bend", "pinch", "tilt", "touch")
# The same starting values as the browser version (public/sensor-protocol.js).
DEFAULTS = {"flexRest": 275, "flexBent": 375, "forceRest": 0, "forceFull": 800,
            "upright": [0.0, 0.0, 1.0], "tiltMax": 30.0}
ARDUINO_VENDOR_IDS = {0x2341, 0x2A03}


# ---------------------------------------------------------------- OSC output
def _osc_string(text):
    data = text.encode("ascii") + b"\0"
    return data + b"\0" * (-len(data) % 4)


def osc_message(address, value):
    """Encodes one OSC message carrying a single float or int."""
    if isinstance(value, int):
        return _osc_string(address) + _osc_string(",i") + struct.pack(">i", value)
    return _osc_string(address) + _osc_string(",f") + struct.pack(">f", float(value))


class SonicPi:
    """Sends sensor values, skipping ones that haven't changed except for a regular refresh.

    The refresh means Sonic Pi picks up the current values right away after you press Run.
    """

    def __init__(self, host, port, refresh=0.5):
        self.address = (host, port)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.refresh = refresh
        self.sent = {}
        self.last_refresh = 0.0

    def send(self, values):
        now = time.monotonic()
        refresh = now - self.last_refresh >= self.refresh
        if refresh:
            self.last_refresh = now
        for name, value in values.items():
            previous = self.sent.get(name)
            if refresh or previous is None or abs(value - previous) >= 0.01 or (value == 0) != (previous == 0):
                self.socket.sendto(osc_message("/plant/" + name, value), self.address)
                self.sent[name] = value

    def silence(self):
        for name in SENSORS:
            value = 0 if name == "touch" else 0.0
            self.socket.sendto(osc_message("/plant/" + name, value), self.address)
            self.sent[name] = value


# ------------------------------------------------------------ sensor mapping
def parse(line):
    """Returns a frame from the sketch, or None for anything else on the port."""
    try:
        frame = json.loads(line)
    except ValueError:
        return None
    if not isinstance(frame, dict) or frame.get("type") != "plantband" or frame.get("v") not in (1, 2):
        return None
    try:
        reading = {"flex": int(frame["flex"]), "fsr": int(frame["fsr"]),
                   "touch": int(frame["touch"]) if frame.get("mpr") else 0,
                   "imu": frame.get("v") == 2 and bool(frame.get("imu")),
                   "a": [int(n) for n in frame.get("a", [0, 0, 0])][:3]}
    except (KeyError, TypeError, ValueError):
        return None
    if len(reading["a"]) != 3:
        return None
    return reading


def unit(vector):
    length = math.sqrt(sum(n * n for n in vector))
    return [n / length for n in vector] if length > 0 else None


def angle(a, b):
    dot = max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b))))
    return math.degrees(math.acos(dot))


def normalized(raw, start, end):
    return max(0.0, min(1.0, (raw - start) / (end - start)))


class Mapper:
    """Turns raw readings into 0-1 values, matching the browser version's thresholds."""

    def __init__(self, calibration, enabled):
        self.calibration = calibration
        self.enabled = enabled
        self.bending = False
        self.pinching = False

    def update(self, reading):
        c = self.calibration
        bend = normalized(reading["flex"], c["flexRest"], c["flexBent"])
        force = normalized(reading["fsr"], c["forceRest"], c["forceFull"])
        # Separate on and off thresholds keep sensor noise at rest from flickering.
        self.bending = bend >= (0.05 if self.bending else 0.10)
        self.pinching = force >= (0.035 if self.pinching else 0.07)
        tilt = 0.0
        gravity = unit(reading["a"]) if reading["imu"] else None
        if gravity and math.sqrt(sum(n * n for n in reading["a"])) > 4096:
            tilt = min(1.0, angle(c["upright"], gravity) / c["tiltMax"])
        values = {"bend": bend if self.bending else 0.0,
                  "pinch": force if self.pinching else 0.0,
                  "tilt": tilt,
                  "touch": 1 if reading["touch"] else 0}
        return {name: (value if name in self.enabled else (0 if name == "touch" else 0.0))
                for name, value in values.items()}


def load_calibration():
    calibration = dict(DEFAULTS)
    try:
        with open(CALIBRATION_FILE) as f:
            saved = json.load(f)
        calibration.update({k: saved[k] for k in DEFAULTS if k in saved})
    except FileNotFoundError:
        pass
    except ValueError:
        print("calibration.json is unreadable, so the starting values are in use.", file=sys.stderr)
    return calibration


# ------------------------------------------------------------ serial input
def open_port(name):
    try:
        import serial
        from serial.tools import list_ports
    except ImportError:
        sys.exit("This needs pyserial. Install it with:  python3 -m pip install pyserial")
    if not name:
        boards = [p.device for p in list_ports.comports() if p.vid in ARDUINO_VENDOR_IDS]
        if len(boards) != 1:
            found = ", ".join(p.device for p in list_ports.comports()) or "none"
            sys.exit(("No Arduino found." if not boards else "More than one Arduino found.")
                     + f" Choose one with --port. Serial ports: {found}")
        name = boards[0]
    try:
        port = serial.Serial(name, 115200, timeout=0.1)
    except serial.SerialException as error:
        sys.exit(f"Could not open {name}: {error}\nClose the browser page, Serial Monitor or anything else using the port.")
    print(f"Reading {name}. Waiting for the Plant Band sketch...")
    return port


def frames(port):
    """Yields each valid frame, or None roughly every 0.1 s while nothing valid arrives."""
    buffer = b""
    while True:
        chunk = port.read(512)
        if not chunk:
            yield None
            continue
        buffer += chunk
        lines = buffer.split(b"\n")
        buffer = lines.pop()
        if len(buffer) > 256:  # never let a runaway line grow without limit
            buffer = b""
        got = False
        for line in lines:
            reading = parse(line.decode("utf-8", "replace").strip())
            if reading:
                got = True
                yield reading
        if not got:
            yield None


def show(values):
    text = "  ".join(f"{name} {values[name]:.2f}" if name != "touch" else f"touch {values[name]}"
                     for name in SENSORS)
    sys.stdout.write("\r" + text + "   ")
    sys.stdout.flush()


# ------------------------------------------------------------------ modes
def play(args):
    enabled = set(args.sensors)
    mapper = Mapper(load_calibration(), enabled)
    sonic = SonicPi(args.host, args.osc_port)
    port = open_port(args.port)
    print(f"Sending to Sonic Pi at {args.host}:{args.osc_port}. Sensors on: {', '.join(s for s in SENSORS if s in enabled)}.")
    print("Press Ctrl+C to stop.")
    last_frame = time.monotonic()
    stale = False
    try:
        for reading in frames(port):
            now = time.monotonic()
            if reading is None:
                # A stopped stream must not leave sounds playing.
                if not stale and now - last_frame > 1.0:
                    stale = True
                    sonic.silence()
                    print("\nNo readings for a second, so everything is silenced. Check the USB cable and sketch.")
                continue
            if stale:
                print("Readings are back.")
            last_frame, stale = now, False
            values = mapper.update(reading)
            sonic.send(values)
            show(values)
    except KeyboardInterrupt:
        pass
    finally:
        sonic.silence()
        port.close()
        print("\nStopped. Sonic Pi has been told to go quiet.")


def wait_for(port, prompt):
    input(prompt + " Press Enter when ready.")
    port.reset_input_buffer()
    readings = []
    for reading in frames(port):
        if reading:
            readings.append(reading)
        if len(readings) >= 25:  # about half a second of data
            return readings


def calibrate(args):
    port = open_port(args.port)
    calibration = load_calibration()
    print("Calibration. Each step averages about half a second of readings.\n")
    try:
        average = lambda rs, key: round(sum(r[key] for r in rs) / len(rs))
        rs = wait_for(port, "1/6 Let the stem rest in its natural curl.")
        calibration["flexRest"] = average(rs, "flex")
        rs = wait_for(port, "2/6 Hold the stem at its full playing bend.")
        calibration["flexBent"] = average(rs, "flex")
        rs = wait_for(port, "3/6 Leave the leaf unpinched.")
        calibration["forceRest"] = average(rs, "fsr")
        rs = wait_for(port, "4/6 Pinch the leaf firmly.")
        calibration["forceFull"] = average(rs, "fsr")
        rs = wait_for(port, "5/6 Hold the pot still in its normal upright position.")
        if not all(r["imu"] for r in rs):
            sys.exit("The MPU6050 isn't reporting. Check VIN, GND, SDA (A4) and SCL (A5), then try again.")
        upright = unit([sum(r["a"][i] for r in rs) for i in range(3)])
        calibration["upright"] = upright
        rs = wait_for(port, "6/6 Tilt the pot as far as you'd tilt it while playing.")
        tilted = unit([sum(r["a"][i] for r in rs) for i in range(3)])
        calibration["tiltMax"] = angle(upright, tilted)
    except KeyboardInterrupt:
        sys.exit("\nCalibration cancelled. Nothing was saved.")
    finally:
        port.close()
    problems = []
    if abs(calibration["flexBent"] - calibration["flexRest"]) < 12:
        problems.append("the bend barely changed the flex reading")
    if calibration["forceFull"] - calibration["forceRest"] < 12:
        problems.append("the pinch barely changed the FSR reading")
    if not 6 <= calibration["tiltMax"] <= 60:
        problems.append(f"the tilt was {calibration['tiltMax']:.0f} degrees; aim for between 6 and 60")
    if problems:
        sys.exit("Not saved, because " + "; ".join(problems) + ". Try again.")
    with open(CALIBRATION_FILE, "w") as f:
        json.dump(calibration, f, indent=2)
    print(f"\nSaved to {CALIBRATION_FILE}:")
    print(f"  bend {calibration['flexRest']} -> {calibration['flexBent']}, "
          f"pinch {calibration['forceRest']} -> {calibration['forceFull']}, full tilt {calibration['tiltMax']:.0f} degrees")


def demo(args):
    """Plays each interaction in turn so you can hear the Sonic Pi side without the plant."""
    sonic = SonicPi(args.host, args.osc_port)
    # (seconds, description, function of seconds into the step -> values)
    steps = [
        (5, "Touch the stem: drums", lambda t: {"touch": 1}),
        (8, "Pinch a leaf: chord progression and arpeggio, getting firmer", lambda t: {"pinch": 0.3 + 0.7 * t / 8}),
        (8, "Bend the stem while pinching: sweep and glide", lambda t: {"pinch": 0.8, "bend": abs(math.sin(t * math.pi / 8))}),
        (10, "Tilt the pot: slow bloom", lambda t: {"tilt": min(1.0, t / 6)}),
        (12, "Everything together", lambda t: {"touch": 1, "pinch": 0.7, "bend": 0.5 + 0.5 * math.sin(t), "tilt": 0.6}),
    ]
    print(f"Demo: sending to Sonic Pi at {args.host}:{args.osc_port}. Press Ctrl+C to stop.")
    try:
        for seconds, description, shape in steps:
            print("\n" + description)
            start = time.monotonic()
            while (elapsed := time.monotonic() - start) < seconds:
                values = {"bend": 0.0, "pinch": 0.0, "tilt": 0.0, "touch": 0}
                values.update(shape(elapsed))
                sonic.send(values)
                show(values)
                time.sleep(0.02)
    except KeyboardInterrupt:
        pass
    finally:
        sonic.silence()
        print("\nDemo finished.")


def main():
    parser = argparse.ArgumentParser(description="Send Plant Band sensor readings to Sonic Pi.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--calibrate", action="store_true", help="save the plant's resting and playing positions")
    mode.add_argument("--demo", action="store_true", help="scripted performance, no Arduino needed")
    parser.add_argument("--port", help="serial port, such as /dev/cu.usbmodem1101 or COM13 (found automatically if omitted)")
    parser.add_argument("--sensors", default="bend,pinch,tilt,touch",
                        help="comma-separated sensors that are attached (default: all four)")
    parser.add_argument("--host", default="127.0.0.1", help="Sonic Pi's address (default: this computer)")
    parser.add_argument("--osc-port", type=int, default=4560, help="Sonic Pi's OSC port (default: 4560)")
    args = parser.parse_args()
    args.sensors = [s.strip() for s in args.sensors.split(",") if s.strip()]
    unknown = [s for s in args.sensors if s not in SENSORS]
    if unknown:
        parser.error(f"unknown sensor {', '.join(unknown)}; choose from {', '.join(SENSORS)}")
    if args.calibrate:
        calibrate(args)
    elif args.demo:
        demo(args)
    else:
        play(args)


if __name__ == "__main__":
    main()
