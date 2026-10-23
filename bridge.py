"""Minimal client for the UNO Q Bridge (arduino-router): msgpack-RPC over a Unix socket.

Functions the microcontroller sketch registers with Bridge.provide() are called by name.
The socket belongs to the arduino-router group; setup-root.sh adds the arduino user to it.
"""
import glob
import itertools
import os
import socket
import subprocess
import threading
import time

import msgpack

SOCKET = "/var/run/arduino-router.sock"
SKETCH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcu", "agent_led")
_ids = itertools.count(1)
_flash_lock = threading.Lock()
_flashed_at = 0.0


def flash_sketch():
    """(Re)flash the agent_led sketch onto the microcontroller.

    After a power-up the sketch's Bridge handshake is lost (the router starts
    long after the microcontroller) and it never registers. A plain reset doesn't
    recover it, but flashing does, which is also what Arduino App Lab does when it
    starts an app. Takes about 7 seconds.
    """
    home = os.path.expanduser("~/.arduino15/packages/arduino")
    remoteocd = sorted(glob.glob(f"{home}/tools/remoteocd/*/remoteocd"))[-1]
    variant = sorted(glob.glob(f"{home}/hardware/zephyr/*/variants/arduino_uno_q_stm32u585xx"))[-1]
    binary = os.path.join(SKETCH, "build", "agent_led.ino.elf-zsk.bin")
    source = os.path.join(SKETCH, "agent_led.ino")
    if not os.path.exists(binary) or os.path.getmtime(source) > os.path.getmtime(binary):
        subprocess.run(["arduino-cli", "compile", "--profile", "default", "--fqbn", "arduino:zephyr:unoq",
                        "--output-dir", "build", "."], cwd=SKETCH, check=True, capture_output=True, timeout=600)
    subprocess.run([remoteocd, "upload", "-f", f"{variant}/flash_sketch.cfg", binary],
                   check=True, capture_output=True, timeout=120)


class BridgeError(Exception):
    pass


def call(method, *params, timeout=3.0):
    msgid = next(_ids) & 0x7FFFFFFF
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(SOCKET)
            s.sendall(msgpack.packb([0, msgid, method, list(params)]))
            unpacker = msgpack.Unpacker(raw=False)
            while True:
                data = s.recv(4096)
                if not data:
                    raise BridgeError("router closed the connection")
                unpacker.feed(data)
                for msg in unpacker:
                    # [1, msgid, error, result]; ignore anything else the router sends
                    if isinstance(msg, list) and len(msg) == 4 and msg[0] == 1 and msg[1] == msgid:
                        if msg[2]:
                            raise BridgeError(str(msg[2]))
                        return msg[3]
    except PermissionError:
        raise BridgeError("no access to the Bridge; run setup-root.sh once and reboot")
    except FileNotFoundError:
        raise BridgeError("Bridge (arduino-router) isn't running")
    except socket.timeout:
        raise BridgeError(f"no reply from the microcontroller for {method}; is the agent_led sketch flashed?")


def call_sketch(method, *params):
    """Call one of the agent_led sketch's functions, re-flashing the sketch if it isn't registered.

    After power-up the sketch often isn't registered (see flash_sketch), and an older sketch may lack a
    newer function. Re-flashing fixes both; it's rate-limited to once every 10 minutes.
    """
    global _flashed_at
    try:
        return call(method, *params)
    except BridgeError as e:
        if "not available" not in str(e):
            raise
        with _flash_lock:
            try:  # another caller may have re-flashed while we waited
                return call(method, *params)
            except BridgeError:
                if time.time() - _flashed_at < 600:
                    raise
            _flashed_at = time.time()
            print(f"bridge: {method} isn't registered; re-flashing the microcontroller", flush=True)
            flash_sketch()
            time.sleep(4)  # the sketch waits 2 s before its handshake
        return call(method, *params)
