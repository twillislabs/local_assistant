#!/usr/bin/env python3
"""Web UI + JSON API for the assistant. Standard library only (plus requests), so it
works in hotspot mode with no internet.

  POST /ask {"text": "...", "dry_run": false}   - same API as before
  /                                              - setup + chat UI
"""
import base64
import glob
import hmac
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import requests

import agent as agent_mod
import bridge
import profiles
import tools as tools_mod
import voice as voice_mod
from agent import HA_ID, Agent, Config, HomeAssistant
from netmgr import NetManager, NmError

HERE = os.path.dirname(os.path.abspath(__file__))
agent_mod.DEFAULTS["voice"] = voice_mod.DEFAULTS
agent_mod.DEFAULTS["indicator"] = voice_mod.INDICATOR_DEFAULTS
config = Config()
agent = Agent(config)
net = NetManager(config)
voice = voice_mod.Voice(config, agent)
pull_state = {"name": "", "status": "", "completed": 0, "total": 0}
PORT = 0


MASK = "••••••"


def mask_tool(entry):
    """Hide header values (API keys) when sending a tool's settings to the browser."""
    e = json.loads(json.dumps(entry))
    if e.get("headers"):
        e["headers"] = {k: (MASK if v else "") for k, v in e["headers"].items()}
    return e


def masked_config():
    voice.personas()  # make sure the editable personalities exist
    c = json.loads(json.dumps(config.data))
    c["ha"]["token_set"] = bool(c["ha"].pop("token"))
    c["ui"]["password_set"] = bool(c["ui"].pop("password"))
    c["tools"] = [mask_tool(t) for t in c["tools"]]
    return c


def platform_info():
    """What this machine is and which hardware features the UI should offer."""
    def read(path):
        try:
            return open(path).read().strip("\0\n ")
        except OSError:
            return ""
    model = read("/proc/device-tree/model") or read("/sys/class/dmi/id/product_name") or platform.machine()
    virt = subprocess.run(["systemd-detect-virt"], capture_output=True, text=True).stdout.strip() if shutil.which("systemd-detect-virt") else ""
    leds = []
    for path in sorted(glob.glob("/sys/class/leds/*")):
        name = os.path.basename(path)
        if not re.match(r"input\d+::|mmc\d+::", name):  # keyboard and SD-card activity LEDs aren't useful
            leds.append({"name": name, "writable": os.access(f"{path}/brightness", os.W_OK)})
    return {
        "model": model, "arch": platform.machine(), "virtual": virt if virt and virt != "none" else "",
        "unoq": "Arduino" in model and ("UNO Q" in model or "Imola" in model) or os.path.exists(bridge.SOCKET),
        "bridge": os.path.exists(bridge.SOCKET),
        "network_managed": net.managed, "wifi": net.wifi,
        "audio": bool(shutil.which("arecord") and shutil.which("aplay")),
        "gpio": sorted(os.path.basename(p) for p in glob.glob("/dev/gpiochip*")),
        "leds": leds,
    }


def ollama_url(target="command"):
    return config.get("chat" if target == "chat" else "ollama")["url"].rstrip("/")


def ollama_info(target="command"):
    try:
        tags = requests.get(f"{ollama_url(target)}/api/tags", timeout=5).json().get("models", [])
        ps = requests.get(f"{ollama_url(target)}/api/ps", timeout=5).json().get("models", [])
        return {"up": True, "models": [{"name": m["name"], "size": m["size"]} for m in tags], "loaded": [m["name"] for m in ps]}
    except Exception as e:
        return {"up": False, "error": str(e), "models": [], "loaded": []}


def warm_chat():
    """Load the conversation model so the first chat isn't slow."""
    ch = config.get("chat")
    try:
        requests.post(f"{ch['url'].rstrip('/')}/api/generate", json={"model": ch["model"], "keep_alive": -1}, timeout=300)
    except Exception as e:
        print("chat warm-up:", e, flush=True)


def pull_model(name, target="command"):
    pull_state.update(name=name, status="starting", completed=0, total=0, target=target)
    try:
        with requests.post(f"{ollama_url(target)}/api/pull", json={"name": name}, stream=True, timeout=3600) as r:
            for line in r.iter_lines():
                if line:
                    d = json.loads(line)
                    if "error" in d:
                        raise RuntimeError(d["error"])
                    pull_state.update(status=d.get("status", ""), completed=d.get("completed", pull_state["completed"]), total=d.get("total", pull_state["total"]))
        pull_state["status"] = "done"
    except Exception as e:
        pull_state["status"] = f"error: {e}"


def system_info():
    mem = dict(line.split(":", 1) for line in open("/proc/meminfo"))
    kb = lambda k: int(mem[k].split()[0]) * 1024
    du = shutil.disk_usage("/")
    return {
        "hostname": socket.gethostname(),
        "uptime_s": int(float(open("/proc/uptime").read().split()[0])),
        "mem_total": kb("MemTotal"), "mem_available": kb("MemAvailable"),
        "disk_total": du.total, "disk_free": du.free,
        "load": os.getloadavg()[0],
    }


def run_later(cmd, delay=1.0):
    threading.Timer(delay, lambda: subprocess.Popen(cmd, start_new_session=True)).start()


class Handler(BaseHTTPRequestHandler):
    server_version = "assistant"

    def log_message(self, fmt, *args):
        pass

    # ---- plumbing ----

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _authorized(self):
        pw = config.get("ui")["password"]
        # Scripts and buttons on the board itself don't need the password.
        if not pw or self.client_address[0] in ("127.0.0.1", "::1"):
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                _, _, given = base64.b64decode(auth[6:]).decode().partition(":")
                if hmac.compare_digest(given, pw):
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="assistant"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _pre(self):
        """Auth, hotspot bookkeeping, and captive-portal redirect. Returns False if the request was answered."""
        client = self.client_address[0]
        if client.startswith("10.42.0."):
            net.last_hotspot_request = time.time()
            host = (self.headers.get("Host") or "").split(":")[0]
            if host and host not in ("10.42.0.1", socket.gethostname(), socket.gethostname() + ".local"):
                self.send_response(302)  # phone connectivity checks land here; send them to the UI
                self.send_header("Location", f"http://10.42.0.1{'' if PORT == 80 else f':{PORT}'}/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return False
        return self._authorized()

    def do_GET(self):
        if not self._pre():
            return
        url = urlparse(self.path)
        q = parse_qs(url.query)
        try:
            if url.path in ("/", "/index.html"):
                return self._send(200, open(os.path.join(HERE, "index.html"), "rb").read(), "text/html; charset=utf-8")
            if url.path == "/api/status":
                return self._send(200, {
                    "net": net.status(),
                    "agent": {"ready": agent.ready, "error": agent.error, "warm": agent.warm_state,
                              "devices": len(agent.devices), "tools": len(agent.tools),
                              "ha": agent.ha is not None, "providers": agent.status},
                    "ollama": ollama_info(),
                    "chat_ollama": ollama_info("chat"),
                    "pull": pull_state,
                    "voice": {"state": voice.state, "error": voice.error, "level": round(voice.level, 3),
                              "download": voice.download, "note": getattr(voice, "state_note", ""),
                              "indicator_error": voice.indicator.error},
                    "system": system_info(),
                })
            if url.path == "/api/profile/export":
                with_secrets = q.get("secrets", ["0"])[0] == "1"
                with config.lock:
                    body = json.dumps(profiles.export(config.data, with_secrets), indent=2).encode()
                name = socket.gethostname() + (".secret" if with_secrets else "")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Disposition", f'attachment; filename="{name}.json"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if url.path == "/api/platform":
                return self._send(200, platform_info())
            if url.path == "/api/tools":
                return self._send(200, {"tools": [mask_tool(t) for t in config.get("tools")], "status": agent.status})
            if url.path == "/api/voice/level":  # polled fast by the mic meter
                return self._send(200, {"level": round(voice.level, 3), "state": voice.state})
            if url.path == "/api/voice/devices":
                c = config.get("voice")
                return self._send(200, {**voice_mod.list_devices(),
                                        "speaker_volume": voice_mod.get_volume(c["speaker"], "pvolume"),
                                        "mic_gain": voice_mod.get_volume(c["mic"], "cvolume")})
            if url.path == "/api/voice/voices":
                return self._send(200, voice.voices())
            if url.path == "/api/voice/catalog":
                return self._send(200, voice.catalog())
            if url.path == "/api/voice/events":
                return self._send(200, list(voice.events))
            if url.path == "/api/config":
                return self._send(200, masked_config())
            if url.path == "/api/wifi/scan":
                return self._send(200, net.scan(rescan=q.get("rescan", ["1"])[0] == "1"))
            if url.path == "/api/wifi/saved":
                return self._send(200, net.saved())
            if url.path == "/api/agent":
                return self._send(200, {"all_tools": agent.all_tools, "devices": agent.devices,
                                        "system_prompt": agent.system, "error": agent.error})
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    def do_POST(self):
        if not self._pre():
            return
        path = urlparse(self.path).path
        try:
            b = self._body()
            if path == "/ask":
                if b.get("speak") and not b.get("dry_run"):
                    return self._send(200, voice.respond(b["text"]).get("result", {}))
                return self._send(200, agent.ask(b["text"], dry_run=bool(b.get("dry_run"))))
            if path == "/api/profile/import":
                profile = b["profile"]
                if not isinstance(profile, dict) or not ({"ollama", "voice", "tools"} & set(profile)):
                    return self._send(400, {"error": "that doesn't look like an assistant settings profile"})
                with config.lock:
                    profiles.write_settings(profiles.merge(config.data, profile))
                # Settings are read all over the app at startup; a restart applies them cleanly.
                run_later(["systemctl", "--user", "restart", "assistant"])
                return self._send(200, {"ok": True})
            if path == "/api/tools/save":
                entry = b["tool"]
                if entry.get("type") not in ("homeassistant", *tools_mod.KINDS):
                    return self._send(400, {"error": "unknown tool type"})
                with config.lock:
                    current = config.data["tools"]
                    old = next((t for t in current if t["id"] == entry.get("id")), None)
                    if old is None:
                        if entry["type"] == "homeassistant":
                            entry["id"] = HA_ID
                        else:
                            base = re.sub(r"[^a-z0-9]+", "-", (entry.get("label") or entry["type"]).lower()).strip("-") or entry["type"]
                            entry["id"], n = base, 2
                            while any(t["id"] == entry["id"] for t in current) or entry["id"] == HA_ID:
                                entry["id"], n = f"{base}-{n}", n + 1
                        if any(t["id"] == entry["id"] for t in current):
                            return self._send(400, {"error": "that tool is already installed"})
                        current.append(entry)
                    else:
                        # Masked header values mean "keep the saved one".
                        for k, v in list((entry.get("headers") or {}).items()):
                            if v == MASK:
                                entry["headers"][k] = (old.get("headers") or {}).get(k, "")
                        current[current.index(old)] = entry
                    config.save()
                threading.Thread(target=agent.reload, daemon=True).start()
                return self._send(200, {"id": entry["id"], "config": masked_config()})
            if path == "/api/tools/delete":
                with config.lock:
                    config.data["tools"] = [t for t in config.data["tools"] if t["id"] != b["id"]]
                    config.save()
                threading.Thread(target=agent.reload, daemon=True).start()
                return self._send(200, {"config": masked_config()})
            if path == "/api/tools/test":
                entry = dict(b["tool"])
                old = next((t for t in config.get("tools") if t["id"] == entry.get("id")), {})
                for k, v in list((entry.get("headers") or {}).items()):
                    if v == MASK:
                        entry["headers"][k] = (old.get("headers") or {}).get(k, "")
                entry.setdefault("id", "test")
                p = tools_mod.make(entry)
                try:
                    found = p.all_tool_names() if hasattr(p, "all_tool_names") else [t["name"] for t in p.tools()]
                    out = {"ok": True, "tools": found}
                    if b.get("args") is not None:  # optionally run the first tool once
                        text, failed = p.call(p.tools()[0]["name"], b["args"])
                        out.update(result=text[:1000], failed=failed, reply=None if failed else p.reply(p.tools()[0]["name"], b["args"], text))
                    return self._send(200, out)
                finally:
                    if hasattr(p, "close"):
                        p.close()
            if path == "/api/voice/listen":
                voice.trigger(b.get("source", "http"))
                return self._send(200, {"ok": True})
            if path == "/api/voice/toggle":
                return self._send(200, {"enabled": voice.set_enabled(b.get("on"))})
            if path == "/api/voice/say":
                kw = {k: b[k] for k in ("voice", "speed", "volume") if b.get(k) not in (None, "")}
                if b.get("wait"):
                    voice.say(b["text"], **kw)
                else:
                    threading.Thread(target=voice.say, args=(b["text"],), kwargs=kw, daemon=True).start()
                return self._send(200, {"ok": True})
            if path == "/api/personas/save":
                key = voice.save_persona(b.get("key") or None, b["persona"])
                if b.get("apply"):
                    voice.apply_persona(key)
                return self._send(200, {"key": key, "config": masked_config()})
            if path == "/api/personas/delete":
                voice.delete_persona(b["key"])
                return self._send(200, {"config": masked_config()})
            if path == "/api/personas/restore":
                voice.restore_builtins()
                return self._send(200, {"config": masked_config()})
            if path == "/api/personas/capture":
                key = voice.persona_from_current(b.get("label") or "My personality")
                return self._send(200, {"key": key, "config": masked_config()})
            if path == "/api/voice/volume":
                c = config.get("voice")
                if b.get("speaker_volume") is not None:
                    voice_mod.set_volume(c["speaker"], "pvolume", b["speaker_volume"])
                    config.update("voice", {"speaker_volume": int(b["speaker_volume"])})
                if b.get("mic_gain") is not None:
                    voice_mod.set_volume(c["mic"], "cvolume", b["mic_gain"])
                    config.update("voice", {"mic_gain": int(b["mic_gain"])})
                return self._send(200, {"ok": True})
            if path == "/api/voice/test-mic":
                return self._send(200, voice.capture(int(b.get("seconds", 3))))
            if path == "/api/voice/learn-key":
                k = voice.learn_key()
                if not k:
                    return self._send(400, {"error": "no button press seen in 10 seconds"})
                k["label"] = f"{k['device']} key {k['code']}"
                which = "toggle_key" if b.get("which") == "toggle" else "key"
                other = config.get("voice")["key" if which == "toggle_key" else "toggle_key"]
                if other and other["device"] == k["device"] and other["code"] == k["code"]:
                    return self._send(400, {"error": "that button is already used for the other action"})
                with config.lock:
                    config.data["voice"][which] = k
                    config.save()
                return self._send(200, k)
            if path in ("/api/voice/persona", "/api/personas/apply"):
                voice.apply_persona(b.get("key") or b["name"])
                return self._send(200, masked_config())
            if path == "/api/voice/download":
                return self._send(200, {"name": voice.download_voice(key=b.get("key"), url=b.get("url"))})
            if path == "/api/voice/delete":
                voice.delete_voice(b["name"])
                return self._send(200, {"ok": True})
            if path == "/api/indicator/test":
                voice.indicator.set(b.get("state", "listening"))
                threading.Timer(3, lambda: voice.indicator.set(voice.state)).start()
                return self._send(200, {"ok": True})
            if path.startswith("/api/config/"):
                section = path.rsplit("/", 1)[1]
                if section not in ("ui", "ha", "ollama", "hotspot", "voice", "chat", "indicator"):
                    return self._send(404, {"error": "unknown section"})
                # Blank secret fields mean "keep the current value" unless clear_<field> is set.
                for secret in ("token", "password"):
                    clear = b.pop("clear_" + secret, False)
                    if b.get(secret) == "" and not clear:
                        b.pop(secret)
                if section == "hotspot" and "password" in b and len(b["password"]) < 8:
                    return self._send(400, {"error": "hotspot password must be at least 8 characters"})
                if section in ("voice", "indicator"):
                    # Replace whole nested objects (key, gpio, states) rather than merging into stale ones.
                    with config.lock:
                        config.data[section].update(b)
                        config.save()
                else:
                    config.update(section, b)
                if section in ("ha", "ollama"):
                    threading.Thread(target=agent.reload, daemon=True).start()
                if section == "voice":
                    voice.changed()
                if section == "indicator":
                    voice.indicator.refresh()
                    voice.indicator.set(voice.state)
                if section == "chat":
                    threading.Thread(target=warm_chat, daemon=True).start()
                return self._send(200, masked_config())
            if path == "/api/ha/test":
                cfg = config.get("ha")
                ha = HomeAssistant(b.get("url") or cfg["url"], b.get("token") or cfg["token"])
                msg = ha.ping()
                tools = ha.mcp("tools/list")["tools"]
                return self._send(200, {"ok": True, "message": f"{msg}; MCP server offers {len(tools)} tools"})
            if path == "/api/agent/reload":
                ok = agent.reload()
                return self._send(200, {"ok": ok, "error": agent.error})
            if path == "/api/wifi/connect":
                return self._send(200, net.connect(b["ssid"], b.get("password", ""), b.get("hidden", False), b.get("ipv4")))
            if path == "/api/wifi/ipv4":
                return self._send(200, net.set_ipv4(b["name"], b["ipv4"]))
            if path == "/api/wifi/forget":
                net.forget(b["name"])
                return self._send(200, {"ok": True})
            if path == "/api/hotspot":
                return self._send(200, net.hotspot_now(bool(b.get("on"))))
            if path == "/api/ollama/pull":
                if pull_state["status"] not in ("", "done") and not pull_state["status"].startswith("error"):
                    return self._send(409, {"error": f"already pulling {pull_state['name']}"})
                threading.Thread(target=pull_model, args=(b["name"], b.get("target", "command")), daemon=True).start()
                return self._send(200, {"ok": True})
            if path == "/api/ollama/delete":
                target = b.get("target", "command")
                if b["name"] == config.get("chat" if target == "chat" else "ollama")["model"]:
                    return self._send(400, {"error": "that model is in use; pick another one first"})
                r = requests.delete(f"{ollama_url(target)}/api/delete", json={"name": b["name"]}, timeout=30)
                return self._send(200 if r.ok else 500, {"ok": r.ok, "error": r.text})
            if path == "/api/system/restart":
                run_later(["systemctl", "--user", "restart", "assistant"])
                return self._send(200, {"ok": True})
            if path == "/api/system/reboot":
                p = subprocess.run(["systemctl", "reboot", "--no-ask-password"], capture_output=True, text=True)
                if p.returncode:
                    return self._send(500, {"error": "not permitted. Run the one-time setup-root.sh with sudo to allow reboot from the UI."})
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})
        except NmError as e:
            return self._send(400, {"error": str(e)})
        except KeyError as e:
            return self._send(400, {"error": f"missing field {e}"})
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})


def bind():
    port = config.get("ui")["port"]
    for p in ([port] if port else [80, 8080]):
        try:
            return ThreadingHTTPServer(("0.0.0.0", p), Handler), p
        except PermissionError:
            continue
        except OSError as e:
            print(f"port {p}: {e}", flush=True)
    sys.exit("could not bind a port")


def main():
    global PORT
    server, PORT = bind()
    print(f"web UI on port {PORT}", flush=True)
    threading.Thread(target=net.watchdog, daemon=True).start()
    voice.personas()  # seed/upgrade saved personalities (e.g. add easter eggs) before anything reads them
    voice.start()
    threading.Thread(target=warm_chat, daemon=True).start()

    def load_agent():
        # Retry failing tools every minute (e.g. Home Assistant unreachable in hotspot mode) and
        # check for device changes every 10 minutes. Unchanged prompts aren't re-warmed.
        last = None
        while True:
            ok = agent.refresh_if_changed()
            summary = f"{len(agent.tools)} tools, {len(agent.devices)} devices" + ("" if ok else f"; problems: {agent.error}")
            if summary != last:
                print("tools:", summary, flush=True)
                last = summary
            time.sleep(600 if ok else 60)

    threading.Thread(target=load_agent, daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
