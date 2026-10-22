"""Config plus the chat engine: natural language -> Ollama tool call -> Home Assistant MCP.

The system prompt, examples and tool list are built once per reload and never change
between requests, so Ollama reuses its prompt cache and each request only pays for the
user's text.
"""
import copy
import difflib
import json
import os
import random
import re
import threading
import time

import requests

CONFIG_PATH = os.path.expanduser("~/.config/assistant/config.json")

DEFAULTS = {
    "ui": {"password": "", "port": 0},  # port 0 = 80 if allowed, else 8080
    "ha": {
        "url": "http://homeassistant.local:8123",
        "token": "",
        "tools": ["HassTurnOn", "HassTurnOff", "HassLightSet", "HassSetPosition",
                  "HassMediaPause", "HassMediaUnpause", "HassSetVolume"],
        "domains": ["light", "switch", "fan", "cover", "lock", "media_player", "vacuum", "valve", "scene", "script"],
        # "turn off the office" with no device type means these types in that room.
        "area_domains": ["light", "switch"],
    },
    "ollama": {
        "url": "http://localhost:11434",
        "model": "qwen2.5:0.5b",
        "temperature": 0,
        "num_ctx": 4096,
        "keep_alive": "-1",
        "extra_prompt": "",
        # Worked examples teach the small model the tool format. Replace the names with
        # your own devices on the Model page for better accuracy.
        "examples": [
            ["turn on the desk lamp", "HassTurnOn", {"name": "Desk Lamp"}],
            ["kill the bedroom lights", "HassTurnOff", {"area": "Bedroom", "domain": ["light"]}],
            ["turn off the office", "HassTurnOff", {"area": "Office"}],
            ["dim the floor lamp to 30%", "HassLightSet", {"name": "Floor Lamp", "brightness": 30}],
            ["make the desk lamp red", "HassLightSet", {"name": "Desk Lamp", "color": "red"}],
            ["open the garage door", "HassSetPosition", {"name": "Garage Door", "position": 100}],
            ["pause the tv", "HassMediaPause", {"name": "Living Room TV"}],
        ],
    },
    # Conversation runs on a second Ollama instance so it never evicts the command prompt from cache.
    "chat": {
        "enabled": True,
        "url": "http://127.0.0.1:11436",
        "model": "qwen2.5:0.5b",
        "temperature": 0.7,
        "max_tokens": 60,
        "history_turns": 3,
        "prompt": "You are a friendly home voice assistant. Reply in one or two short sentences, "
                  "plain spoken English with no lists or markdown.",
    },
    "hotspot": {
        "enabled": True,
        "ssid": "assistant-setup",
        "password": "hasetup123",
        "grace_seconds": 45,    # offline this long before the hotspot starts
        "retry_minutes": 5,     # while in hotspot mode, how often to retry saved networks
    },
}


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


class Config:
    def __init__(self):
        self.lock = threading.Lock()
        data = {}
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH) as f:
                data = json.load(f)
        self.data = _merge(DEFAULTS, data)
        legacy = os.path.expanduser("~/.ha_token")
        if not self.data["ha"]["token"] and os.path.exists(legacy):
            self.data["ha"]["token"] = open(legacy).read().strip()
            self.save()

    def get(self, section):
        with self.lock:
            return copy.deepcopy(self.data[section])

    def update(self, section, values):
        with self.lock:
            self.data[section] = _merge(self.data[section], values)
            self.save()

    def save(self):
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, CONFIG_PATH)


class HomeAssistant:
    def __init__(self, url, token):
        self.url = url.rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        })
        self._id = 0

    def ping(self):
        r = self.s.get(f"{self.url}/api/", timeout=8)
        r.raise_for_status()
        return r.json().get("message", "ok")

    def mcp(self, method, params=None):
        self._id += 1
        r = self.s.post(f"{self.url}/api/mcp", json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}, timeout=30)
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(body["error"])
        return body["result"]

    @staticmethod
    def text(result):
        return "\n".join(c.get("text", "") for c in result.get("content", []))


class Agent:
    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()  # one inference at a time; the board can't run two usefully
        self.ready = False
        self.error = "not loaded yet"
        self.warm_state = "cold"
        self.all_tools = []  # every tool HA offers, for the settings page
        self.tools, self.full_names, self.devices = [], {}, []
        self.names, self.areas = {}, {}
        self.system, self.examples = "", []

    # ---- loading ----

    def reload(self, warm=True):
        """Rebuild tools, devices and prompt from HA. Safe to call any time; errors are recorded, not raised."""
        ha_cfg, ol = self.config.get("ha"), self.config.get("ollama")
        try:
            if not ha_cfg["token"]:
                raise RuntimeError("Home Assistant token is not set")
            ha = HomeAssistant(ha_cfg["url"], ha_cfg["token"])
            all_tools, tools, full = [], [], {}
            for t in ha.mcp("tools/list")["tools"]:
                short = t["name"].split("__")[-1]
                all_tools.append({"name": short, "description": t.get("description", "")})
                if short not in ha_cfg["tools"]:
                    continue
                schema = copy.deepcopy(t["inputSchema"])
                # These cost prompt tokens and tiny models fill them with made-up values.
                for unused in ("device_class", "floor"):
                    schema.get("properties", {}).pop(unused, None)
                full[short] = t["name"]
                tools.append({"type": "function", "function": {"name": short, "description": t.get("description", ""), "parameters": schema}})
            devices = self._load_devices(ha, set(ha_cfg["domains"]))
        except Exception as e:
            with self.lock:
                self.ready, self.error, self.ha = False, f"{type(e).__name__}: {e}", None
            return False

        system = (
            "You control a smart home by calling tools. Always call exactly one tool. "
            "Use the device name exactly as listed. Brightness is 0-100. "
            "If the user names a room instead of a device, use area.\n"
            + (ol["extra_prompt"].strip() + "\n" if ol["extra_prompt"].strip() else "")
            + "Rooms: " + ", ".join(sorted({a for _, _, areas in devices for a in areas})) + "\n"
            + "Devices:\n" + "\n".join(f"- {n} ({d}{', ' + ', '.join(a) if a else ''})" for n, d, a in devices)
        )
        # Worked examples as real chat turns, in the model's own tool-call format.
        examples = []
        for said, tool, args in ol["examples"]:
            examples += [
                {"role": "user", "content": said},
                {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": tool, "arguments": args}}]},
                {"role": "tool", "content": "done"},
            ]
        with self.lock:
            self.ha, self.all_tools, self.tools, self.full_names, self.devices = ha, all_tools, tools, full, devices
            self.names = {n.lower(): n for n, _, _ in devices}
            self.areas = {a.lower(): a for _, _, areas in devices for a in areas}
            self.system, self.examples = system, examples
            self.ready, self.error, self.warm_state = True, "", "cold"
        if warm:
            threading.Thread(target=self.warm, daemon=True).start()
        return True

    def refresh_if_changed(self):
        """Reload (and re-warm) only when HA's exposed devices changed, since a new prompt costs a warm-up."""
        if not self.ready:
            return self.reload()
        try:
            current = self._load_devices(self.ha, set(self.config.get("ha")["domains"]))
        except Exception:
            return False
        return current != self.devices and self.reload()

    @staticmethod
    def _load_devices(ha, domains):
        """(name, domain, [areas]) per controllable entity. States are left out so the prompt never changes."""
        text = ha.text(ha.mcp("tools/call", {"name": "homeassistant__GetLiveContext", "arguments": {}}))
        try:
            text = json.loads(text)["result"]
        except (ValueError, KeyError, TypeError):
            pass
        devices = []
        for block in re.split(r"\n- ", text)[1:]:
            fields = dict(re.findall(r"^\s*(names|domain|areas): (.*)$", block, re.M))
            if fields.get("domain") in domains:
                name = fields["names"].split(",")[0].strip()
                areas = list(dict.fromkeys(a.strip() for a in fields.get("areas", "").split(",") if a.strip()))
                devices.append((name, fields["domain"], areas))
        return devices

    # ---- inference ----

    def _chat(self, text, **options):
        ol = self.config.get("ollama")
        keep = ol["keep_alive"]
        r = requests.post(f"{ol['url'].rstrip('/')}/api/chat", json={
            "model": ol["model"],
            "stream": False,
            "think": False,
            "keep_alive": int(keep) if str(keep).lstrip("-").isdigit() else keep,
            "options": {"temperature": ol["temperature"], "num_ctx": ol["num_ctx"], **options},
            "messages": [{"role": "system", "content": self.system}, *self.examples, {"role": "user", "content": text}],
            "tools": self.tools,
        }, timeout=900)
        r.raise_for_status()
        return r.json()["message"]

    def warm(self):
        """Process the fixed prompt once so later requests hit the cache. Never executes tools."""
        if not self.ready:
            return
        self.warm_state = "warming"
        t0 = time.time()
        try:
            with self.lock:
                self._chat("hi", num_predict=1)
            self.warm_state = f"warm ({time.time() - t0:.0f}s to load)"
        except Exception as e:
            self.warm_state = f"warm-up failed: {e}"

    def _area(self, text):
        """The room a phrase refers to, if any ('the office' -> 'Office')."""
        t = re.sub(r"^(the|my)\s+", "", str(text or "").strip().lower())
        t = re.sub(r"\s+(room|area)$", "", t) if t not in self.areas else t
        if t in self.areas:
            return self.areas[t]
        # Allow typos ("ofice") but not extra words ("living room tv" is a device, not the room).
        match = [a for a in difflib.get_close_matches(t, list(self.areas), n=1, cutoff=0.85) if abs(len(a) - len(t)) <= 2]
        return self.areas[match[0]] if match else None

    def fix_args(self, args, tool=None):
        """Tiny models invent values; keep only names, areas and domains that exist."""
        out = {}
        name, area = args.get("name"), args.get("area")
        # "turn off office": the model may put the room in either slot. A room beats
        # a fuzzy device match ("office" would otherwise match "Office Lamp").
        room = None
        if name and str(name).lower() not in self.names:
            room = self._area(name)
        if room:
            out["area"] = room
        else:
            # A device name put in the area slot is still a device name.
            if not name and area and not self._area(area):
                name = area
            if name:
                match = difflib.get_close_matches(str(name).lower(), list(self.names), n=1, cutoff=0.5)
                if match:
                    out["name"] = self.names[match[0]]
            # A matched name is enough; a wrong area alongside it makes HA reject the call.
            if "name" not in out and area and self._area(area):
                out["area"] = self._area(area)
        ha = self.config.get("ha")
        domains = ha["domains"]
        if "name" not in out and isinstance(args.get("domain"), list):
            picked = [d.rstrip("s") if d.rstrip("s") in domains else d for d in args["domain"]]
            picked = [d for d in picked if d in domains]
            if picked:
                out["domain"] = picked
        # A bare room with on/off means its lights and switches, not every device in it.
        if "area" in out and "domain" not in out and tool in ("HassTurnOn", "HassTurnOff"):
            out["domain"] = list(ha["area_domains"])
        for k, v in args.items():
            if k not in ("name", "area", "domain", "floor"):
                out[k] = v
        return out

    # ---- conversation ----

    COMMAND_VERBS = {
        "turn", "switch", "set", "dim", "brighten", "open", "close", "raise", "lower", "pause", "resume", "unpause",
        "play", "stop", "mute", "unmute", "volume", "start", "dock", "vacuum", "lock", "unlock", "activate",
        "deactivate", "kill", "shut", "change", "make",
    }

    def is_command(self, text):
        """Cheap router: home-control phrasing goes to the tool path, everything else is conversation."""
        words = set(re.findall(r"[a-z']+", text.lower()))
        if words & self.COMMAND_VERBS:
            return True
        known = {w for n in list(self.names) + list(self.areas) for w in re.findall(r"[a-z']+", n)}
        return bool(words & {"on", "off"}) and bool(words & (known | {"lights", "light", "lamp", "lamps"}))

    def converse(self, text, on_sentence=None):
        """Short in-character reply, streamed so speech can start after the first sentence."""
        ch = self.config.get("chat")
        name = self.config.data.get("voice", {}).get("name", "")
        now = time.time()
        if now - getattr(self, "_chat_at", 0) > 300:
            self._history = []  # forget the conversation after 5 quiet minutes
        self._chat_at = now
        messages = [{"role": "system", "content": ch["prompt"].replace("{name}", name)},
                    *self._history[-2 * ch["history_turns"]:], {"role": "user", "content": text}]
        t0, reply, pending, truncated = time.time(), "", "", False
        with requests.post(f"{ch['url'].rstrip('/')}/api/chat", stream=True, timeout=300, json={
            "model": ch["model"], "stream": True, "think": False, "keep_alive": -1, "messages": messages,
            "options": {"temperature": ch["temperature"], "num_predict": ch["max_tokens"], "num_ctx": 2048},
        }) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                d = json.loads(line)
                piece = d.get("message", {}).get("content", "")
                reply, pending = reply + piece, pending + piece
                # Hand each finished sentence to the speaker while the rest is generated.
                # A stop at the very end might be "3." of "3.5", so wait for the next token unless done.
                while True:
                    m = re.search(r"[.!?](\s|$)", pending)
                    if not m or (m.end() == len(pending) and not d.get("done")):
                        break
                    sentence, pending = pending[:m.end()].strip(), pending[m.end():]
                    if sentence and on_sentence:
                        on_sentence(sentence)
                if d.get("done"):
                    truncated = d.get("done_reason") == "length"
                    break
        if truncated:
            # Drop a half-finished last sentence rather than speak it.
            reply = reply[:max(reply.rfind("."), reply.rfind("!"), reply.rfind("?")) + 1] or reply
        elif pending.strip() and on_sentence:
            on_sentence(pending.strip())
        reply = reply.strip()
        self._history += [{"role": "user", "content": text}, {"role": "assistant", "content": reply}]
        return {"ok": True, "mode": "chat", "reply": reply, "seconds": round(time.time() - t0, 1)}

    # ---- easter eggs and grounding ----

    @staticmethod
    def _norm(text):
        text = str(text).lower().replace("’", "'").replace("'", "")
        return " ".join(re.findall(r"[a-z0-9]+", text))

    def easter_egg(self, text):
        """The active personality's canned reply if the text matches one of its phrases."""
        v = self.config.data.get("voice", {})
        persona = self.config.data.get("personas", {}).get(v.get("persona"), {})
        said = self._norm(text)
        for egg in persona.get("eggs", []):
            for phrase in egg.get("phrases", []):
                p = self._norm(phrase)
                if p and (f" {p} " in f" {said} " or difflib.SequenceMatcher(None, p, said).ratio() >= 0.85):
                    reply = random.choice(egg["replies"]) if egg.get("replies") else egg.get("reply", "")
                    return reply.replace("{name}", v.get("name", ""))
        return None

    NUMBER_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9"}
    GENERIC_WORDS = {"the", "a", "an", "my", "light", "lights", "lamp", "lamps", "switch", "switches"}

    def mentioned(self, text, target):
        """Did the user actually name this device or room? Stops the model inventing a target."""
        said = [self.NUMBER_WORDS.get(w, w) for w in self._norm(text).split()]
        joined = "".join(said)
        words = [w for w in self._norm(target).split() if w not in self.GENERIC_WORDS]
        if not words:
            return True
        for w in words:
            if w in said or (len(w) > 3 and w in joined):  # "sun lamp" said, "sunlamp" named
                return True
            if len(w) > 3 and difflib.get_close_matches(w, said, n=1, cutoff=0.8):
                return True
        return False

    def ask(self, text, dry_run=False, on_sentence=None):
        egg = self.easter_egg(text)
        if egg is not None:
            return {"ok": True, "mode": "easter", "reply": egg, "seconds": 0}
        if self.config.get("chat")["enabled"] and not self.is_command(text):
            return self.converse(text, on_sentence)
        return self.command(text, dry_run)

    def command(self, text, dry_run=False):
        if not self.ready:
            return {"ok": False, "error": f"agent not ready: {self.error}"}
        with self.lock:
            t0 = time.time()
            msg = self._chat(text)
            llm_s = time.time() - t0
            calls = msg.get("tool_calls") or []
            if not calls:
                return {"ok": False, "reply": msg.get("content", "").strip() or "(model returned nothing)", "seconds": round(llm_s, 1)}
            done = []
            for c in calls:
                name, raw = c["function"]["name"], c["function"].get("arguments") or {}
                args = self.fix_args(raw, name)
                entry = {"tool": name, "args": args, "raw_args": raw}
                if name not in self.full_names:
                    entry["error"] = "unknown tool"
                elif "name" not in args and "area" not in args:
                    room = raw.get("area") or raw.get("name")
                    entry["error"] = (f"no devices exposed to Assist in '{room}'" if room and raw.get("area")
                                      else "couldn't tell which device")
                elif not self.mentioned(text, args.get("name") or args.get("area")):
                    entry["error"] = "couldn't tell which device"  # the model guessed a target you didn't say
                elif dry_run:
                    entry["result"] = "(dry run: not sent)"
                else:
                    res = self.ha.mcp("tools/call", {"name": self.full_names[name], "arguments": args})
                    entry["error"] = res.get("isError", False)
                    entry["result"] = self.ha.text(res)[:500]
                done.append(entry)
            return {"ok": all(not d.get("error") for d in done), "calls": done,
                    "seconds": round(time.time() - t0, 1), "llm_seconds": round(llm_s, 1)}
