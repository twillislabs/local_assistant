"""Config plus the engine: natural language -> Ollama tool call -> Home Assistant or other tools.

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

import tools as tools_mod

CONFIG_PATH = os.path.expanduser("~/.config/assistant/config.json")
HA_ID = "homeassistant"

DEFAULTS = {
    "ui": {"password": "", "port": 0},  # port 0 = 80 if allowed, else 8080
    # Tool providers the model can use, managed on the Tools page. Home Assistant's own
    # settings live in "ha"; other providers keep theirs in their entry (see tools.py).
    "tools": [{"id": HA_ID, "type": "homeassistant", "label": "Home Assistant", "enabled": True}],
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
    # Learned shortcuts ({"id", "phrases", "say", "tool", "args"}) and facts; see Agent.teach.
    "memory": {"shortcuts": [], "facts": []},
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


_UNITS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                    "fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split(), 2)}


def spoken_numbers(text):
    """'set it to twenty five percent' -> 'set it to 25 percent'. Speech recognition spells numbers
    out, and the small model handles digits much more reliably."""
    out, value, words = [], None, text.split()
    for i, w in enumerate(words):
        lw = w.lower().strip(",.!?")
        nxt = words[i + 1].lower().strip(",.!?") if i + 1 < len(words) else ""
        if lw in _UNITS and value is not None and value >= 20 and value % 10 == 0 and _UNITS[lw] < 10:
            value += _UNITS[lw]  # "twenty" + "five"
            continue
        if lw == "hundred" and (value is not None or (out and out[-1].lower() == "a")):
            if value is None:
                out.pop()  # "a hundred"
            value = (value or 1) * 100
            continue
        if value is not None:  # anything else ends the number being built
            out.append(str(value))
            value = None
        if lw in _UNITS:
            value = _UNITS[lw]
        elif lw in _TENS:
            value = _TENS[lw]
        else:
            out.append(w)
    if value is not None:
        out.append(str(value))
    return " ".join(out)


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
        self.ha = None
        self.all_tools = []  # every tool HA offers, for the Home Assistant page
        self.tools, self.routes, self.devices = [], {}, []
        self.providers, self.status = {}, {}
        self.names, self.areas, self.domains = {}, {}, {}
        self.system, self.examples = "", []
        self._signature = None
        self._history = []
        self._reload_lock = threading.Lock()

    # ---- loading ----

    def _load_ha(self):
        ha_cfg = self.config.get("ha")
        if not ha_cfg["token"]:
            raise RuntimeError("Home Assistant token is not set")
        ha = HomeAssistant(ha_cfg["url"], ha_cfg["token"])
        all_tools, tools = [], []
        for t in ha.mcp("tools/list")["tools"]:
            short = t["name"].split("__")[-1]
            all_tools.append({"name": short, "description": t.get("description", "")})
            if short not in ha_cfg["tools"]:
                continue
            schema = copy.deepcopy(t["inputSchema"])
            # These cost prompt tokens and tiny models fill them with made-up values.
            for unused in ("device_class", "floor"):
                schema.get("properties", {}).pop(unused, None)
            tools.append({"name": short, "full": t["name"], "description": t.get("description", ""), "parameters": schema})
        return ha, tools, all_tools, self._load_devices(ha, set(ha_cfg["domains"]))

    def reload(self, warm=True):
        """Rebuild tools, devices and prompt from every enabled provider. A provider that fails is
        reported on the Tools page and skipped; the rest still work. Returns True if all loaded."""
        # One at a time: several quick changes each start a reload, and an older one finishing
        # last would otherwise put back tools that were just removed.
        with self._reload_lock:
            return self._reload(warm)

    def _reload(self, warm):
        ol = self.config.get("ollama")
        tools, routes, status, providers, provider_examples = [], {}, {}, {}, []
        ha, all_tools, devices = None, [], []

        def add(name, route, description, parameters):
            if name in routes:  # two providers with the same tool name
                name = f"{route[0]}_{name}"
            routes[name] = route
            tools.append({"type": "function", "function": {"name": name, "description": description, "parameters": parameters}})
            return name

        for entry in self.config.get("tools"):
            if not entry.get("enabled", True):
                continue
            pid = entry["id"]
            try:
                if entry["type"] == "homeassistant":
                    ha, ha_tools, all_tools, devices = self._load_ha()
                    for t in ha_tools:
                        add(t["name"], (pid, t["full"]), t["description"], t["parameters"])
                    status[pid] = {"ok": True, "message": f"{len(ha_tools)} tools, {len(devices)} devices",
                                   "tools": [t["name"] for t in ha_tools]}
                    continue
                old = self.providers.get(pid)
                p = old if old is not None and old.cfg == entry else tools_mod.make(entry)
                renamed = {t["name"]: add(t["name"], (pid, t["name"]), t["description"], t["parameters"]) for t in p.tools()}
                provider_examples += [[said, renamed[tool], args] for said, tool, args in p.examples() if tool in renamed]
                providers[pid] = p
                status[pid] = {"ok": True, "message": f"{len(renamed)} tool{'s' if len(renamed) != 1 else ''}",
                               "tools": list(renamed)}
            except Exception as e:
                status[pid] = {"ok": False, "message": f"{type(e).__name__}: {e}", "tools": []}
        for pid, old in self.providers.items():  # stop MCP server processes we no longer use
            if providers.get(pid) is not old and hasattr(old, "close"):
                old.close()

        system = "You are a helpful assistant that acts by calling tools. Always call exactly one tool.\n"
        if ha:
            system += ("To control the home, use the device name exactly as listed. Brightness is 0-100. "
                       "If the user names a room instead of a device, use area.\n")
        if ol["extra_prompt"].strip():
            system += ol["extra_prompt"].strip() + "\n"
        if ha:
            system += ("Rooms: " + ", ".join(sorted({a for _, _, areas in devices for a in areas})) + "\n"
                       + "Devices:\n" + "\n".join(f"- {n} ({d}{', ' + ', '.join(a) if a else ''})" for n, d, a in devices))
        # Worked examples as real chat turns, in the model's own tool-call format.
        examples = []
        for said, tool, args in [e for e in ol["examples"] if e[1] in routes] + provider_examples:
            examples += [
                {"role": "user", "content": said},
                {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": tool, "arguments": args}}]},
                {"role": "tool", "content": "done"},
            ]
        failed = [f"{e.get('label', e['id'])}: {status[e['id']]['message']}" for e in self.config.get("tools")
                  if e["id"] in status and not status[e["id"]]["ok"]]
        signature = json.dumps([system, tools, examples], sort_keys=True)
        with self.lock:
            self.ha, self.all_tools, self.devices = ha, all_tools, devices
            self.tools, self.routes, self.providers, self.status = tools, routes, providers, status
            self.names = {n.lower(): n for n, _, _ in devices}
            self.domains = {n.lower(): d for n, d, _ in devices}
            self.areas = {a.lower(): a for _, _, areas in devices for a in areas}
            self.system, self.examples = system, examples
            self.ready = bool(tools)
            self.error = "; ".join(failed) or ("" if tools else "no tools are installed or working")
            changed, self._signature = signature != self._signature, signature
            if changed:
                self.warm_state = "cold"
        if warm and changed and tools:  # an unchanged prompt is still cached; don't pay for a warm-up
            threading.Thread(target=self.warm, daemon=True).start()
        return not failed

    def refresh_if_changed(self):
        """Retry failed providers and pick up Home Assistant device changes. The prompt only
        changes (costing a warm-up) when something actually changed."""
        return self.reload()

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

    def _ha_signal(self, words):
        """Does this sound like home control?"""
        if not self.ha:
            return False
        if words & self.COMMAND_VERBS:
            return True
        known = {w for n in list(self.names) + list(self.areas) for w in re.findall(r"[a-z']+", n)}
        return bool(words & {"on", "off"} and words & (known | {"lights", "light", "lamp", "lamps"}))

    def _matched_providers(self, words):
        """Non-Home-Assistant providers whose keywords appear in the request."""
        return [pid for pid, p in list(self.providers.items()) if words & p.keywords()]

    def is_command(self, text):
        """Cheap router: phrasing for any installed tool goes to the tool path, everything else is conversation."""
        words = set(re.findall(r"[a-z']+", text.lower()))
        return self._ha_signal(words) or bool(self._matched_providers(words))

    def converse(self, text, on_sentence=None):
        """Short in-character reply, streamed so speech can start after the first sentence."""
        ch = self.config.get("chat")
        now = time.time()
        if now - getattr(self, "_chat_at", 0) > 300:
            self._history = []  # forget the conversation after 5 quiet minutes
        self._chat_at = now
        t0 = time.time()
        reply = self._stream_chat([*self._history[-2 * ch["history_turns"]:], {"role": "user", "content": text}], on_sentence)
        self._history += [{"role": "user", "content": text}, {"role": "assistant", "content": reply}]
        return {"ok": True, "mode": "chat", "reply": reply, "seconds": round(time.time() - t0, 1)}

    def summarize(self, question, data, on_sentence=None):
        """Turn a tool's raw result into a short spoken answer, in the active personality's voice."""
        if not self.config.get("chat")["enabled"]:
            return data.strip()[:300]
        return self._stream_chat([{"role": "user", "content":
            f"Information:\n{data[:1500]}\n\nUsing only the information above, answer this in one or two short "
            f"spoken sentences: {question}\nIf the information doesn't answer it, say you don't know."}], on_sentence)

    def _stream_chat(self, messages, on_sentence=None):
        """Stream a reply from the conversation instance, handing each finished sentence to on_sentence."""
        ch = self.config.get("chat")
        name = self.config.data.get("voice", {}).get("name", "")
        messages = [{"role": "system", "content": ch["prompt"].replace("{name}", name) + self.facts_prompt()}, *messages]
        reply, pending, truncated = "", "", False
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
        return reply.strip()

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

    # ---- memory: learned shortcuts and facts ----
    #
    # Shortcuts map phrases ("sit height") to a resolved tool call, run directly without the model.
    # Facts ("my wife's name is Ali") are given to the conversation model only, so the command
    # prompt (and its cache) never changes when something is learned.

    # Words that may surround a shortcut phrase without changing what's asked ("set the desk to sit height").
    FILLER = {"the", "a", "an", "my", "please", "set", "go", "to", "put", "move", "make", "it", "can", "you", "could",
              "would", "hey", "now", "mode", "position", "into", "switch", "get", "me", "for", "do", "run", "your",
              "computer", "ok", "okay", "thanks", "thank", "at", "on", "in", "level"}
    VERBS_FOR_SPLIT = COMMAND_VERBS | {"move", "put", "go", "run", "do", "play", "get", "tell", "check", "what", "is"}

    def _memory(self):
        return self.config.get("memory")

    def _save_memory(self, mem):
        with self.config.lock:
            self.config.data["memory"] = mem
            self.config.save()

    def describe(self, sc):
        """A spoken description of a shortcut's action, e.g. 'set Office Desk to 25 percent'."""
        a, tool = sc.get("args") or {}, sc["tool"]
        target = a.get("name") or (f"the {a['area']} lights" if a.get("area") else "")
        if tool == "HassSetPosition" and "position" in a:
            return f"set {target} to {a['position']} percent"
        if tool == "HassLightSet":
            parts = [f"{a['brightness']} percent" if "brightness" in a else "", a.get("color", "")]
            return f"set {target} to " + " ".join(p for p in parts if p) if any(parts) else f"adjust {target}"
        if tool == "HassSetVolume" and "volume_level" in a:
            return f"set the volume of {target} to {a['volume_level']} percent"
        if tool in ("HassTurnOn", "HassTurnOff"):
            return f"turn {'on' if tool == 'HassTurnOn' else 'off'} {target}"
        if sc.get("say"):
            return sc["say"]
        return f"run {tool}"

    def shortcut_for(self, text):
        """The learned shortcut that this request is (only) asking for, if any."""
        said = self._norm(text).split()
        wake = set(self._norm(self.config.data.get("voice", {}).get("wake_phrase", "")).split())
        best = None
        for sc in self._memory()["shortcuts"]:
            target = set(self._norm(" ".join(str(v) for k, v in (sc.get("args") or {}).items() if k in ("name", "area"))).split())
            allowed = self.FILLER | wake | target
            for phrase in sc["phrases"]:
                p = self._norm(phrase).split()
                if not p:
                    continue
                n = len(p)
                for i in range(len(said) - n + 1):
                    if said[i:i + n] == p and all(w in allowed for w in said[:i] + said[i + n:]):
                        if not best or n > best[1]:
                            best = (sc, n, phrase)
                if not best and difflib.SequenceMatcher(None, " ".join(p), " ".join(said)).ratio() >= 0.9:
                    best = (sc, n, phrase)  # a slightly misheard phrase on its own
        return (best[0], best[2]) if best else (None, None)

    def run_shortcut(self, sc, phrase, text, dry_run=False, on_sentence=None):
        route = self.routes.get(sc["tool"])
        if not route:
            return {"ok": False, "error": f"the tool for '{phrase}' ({sc['tool']}) isn't installed"}
        t0 = time.time()
        args = dict(sc.get("args") or {})
        if route[0] == HA_ID:
            entry = {"tool": sc["tool"], "args": args, "shortcut": phrase}
            if dry_run:
                entry["result"] = "(dry run: not sent)"
            else:
                res = self.ha.mcp("tools/call", {"name": route[1], "arguments": args})
                entry["error"] = res.get("isError", False)
                entry["result"] = self.ha.text(res)[:500]
            return {"ok": not entry.get("error"), "calls": [entry], "shortcut": phrase, "seconds": round(time.time() - t0, 1)}
        entry, reply, spoken = self._provider_call(self.providers[route[0]], route, sc["tool"], args, text, dry_run, on_sentence)
        entry["shortcut"] = phrase
        out = {"ok": not entry.get("error"), "calls": [entry], "shortcut": phrase, "seconds": round(time.time() - t0, 1)}
        if reply:
            out.update(mode="tool", reply=reply, spoken=spoken)
        return out

    def learn(self, phrases, command_text):
        """Save (or update) a shortcut from phrases and a command said the normal way. Returns a reply."""
        phrases = [p.strip(" '\".,") for p in phrases if p.strip(" '\".,")]
        if not phrases:
            return False, "I didn't catch the phrase to learn."
        mem = self._memory()
        # "standing also means stand height": point new phrases at an existing shortcut.
        existing, _ = self.shortcut_for(command_text)
        if existing:
            for sc in mem["shortcuts"]:
                if sc["id"] == existing["id"]:
                    sc["phrases"] += [p for p in phrases if p.lower() not in (x.lower() for x in sc["phrases"])]
            self._remove_phrases(mem, phrases, keep_id=existing["id"])
            self._save_memory(mem)
            return True, f"Got it. '{phrases[0]}' now also means '{existing['phrases'][0]}'."
        result = self.command(command_text, dry_run=True)
        call = next((c for c in result.get("calls", []) if not c.get("error")), None)
        if not call:
            why = next((c.get("error") for c in result.get("calls", []) if c.get("error")), None) or result.get("error")
            return False, f"I couldn't work out how to {command_text}" + (f" ({why})" if why else "") + ". Try saying it the way you'd ask me to do it."
        action = {"say": spoken_numbers(command_text), "tool": call["tool"], "args": call["args"]}
        # Teaching a phrase it already knows updates that shortcut's action, keeping its other phrases.
        lower = {p.lower() for p in phrases}
        sc = next((s for s in mem["shortcuts"] if lower & {p.lower() for p in s["phrases"]}), None)
        updated = sc is not None
        if updated:
            sc.update(action)
            sc["phrases"] += [p for p in phrases if p.lower() not in (x.lower() for x in sc["phrases"])]
            self._remove_phrases(mem, phrases, keep_id=sc["id"])
        else:
            sc = {"id": f"s{int(time.time() * 1000)}", "phrases": phrases, **action}
            mem["shortcuts"].append(sc)
        self._save_memory(mem)
        return True, f"{'Updated' if updated else 'Got it'}. When you say '{phrases[0]}', I'll {self.describe(sc)}."

    def _remove_phrases(self, mem, phrases, keep_id=None):
        """Take these phrases off other shortcuts (dropping shortcuts left with none). Returns True if any were taken."""
        lower, taken = {p.lower() for p in phrases}, False
        for sc in mem["shortcuts"]:
            if sc["id"] != keep_id and any(p.lower() in lower for p in sc["phrases"]):
                sc["phrases"] = [p for p in sc["phrases"] if p.lower() not in lower]
                taken = True
        mem["shortcuts"] = [sc for sc in mem["shortcuts"] if sc["phrases"]]
        return taken

    def set_number(self, sc_id, value):
        """Change a shortcut's amount ("change sit height to 30 percent")."""
        mem = self._memory()
        for sc in mem["shortcuts"]:
            if sc["id"] == sc_id:
                key = next((k for k in ("position", "brightness", "volume_level") if k in sc["args"]), None)
                if not key:
                    return False, f"'{sc['phrases'][0]}' doesn't have a number to change."
                sc["args"][key] = value
                sc["say"] = re.sub(r"\b\d+\b", str(value), sc.get("say", ""), count=1)
                self._save_memory(mem)
                return True, f"Updated. '{sc['phrases'][0]}' will now {self.describe(sc)}."
        return False, "I don't know that shortcut."

    def remember(self, fact):
        mem = self._memory()
        fact = fact.strip().rstrip(".")
        similar = next((f for f in mem["facts"] if difflib.SequenceMatcher(None, f.lower(), fact.lower()).ratio() >= 0.75), None)
        if similar:
            mem["facts"][mem["facts"].index(similar)] = fact  # "...name is Alisia" updates "...name is Ali"
        else:
            mem["facts"] = (mem["facts"] + [fact])[-30:]
        self._save_memory(mem)
        return "Updated. I'll remember that instead." if similar else "Got it, I'll remember that."

    def forget(self, what):
        what_n = self._norm(what)
        mem = self._memory()
        for sc in mem["shortcuts"]:
            for p in sc["phrases"]:
                if self._norm(p) == what_n or difflib.SequenceMatcher(None, self._norm(p), what_n).ratio() >= 0.85:
                    sc["phrases"].remove(p)
                    mem["shortcuts"] = [s for s in mem["shortcuts"] if s["phrases"]]
                    self._save_memory(mem)
                    return f"Done. I've forgotten '{p}'."
        best = max(mem["facts"], key=lambda f: difflib.SequenceMatcher(None, self._norm(f), what_n).ratio(), default=None)
        if best and (what_n in self._norm(best) or difflib.SequenceMatcher(None, self._norm(best), what_n).ratio() >= 0.6):
            mem["facts"].remove(best)
            self._save_memory(mem)
            return f"Done. I've forgotten that {best}."
        return f"I don't have anything learned about {what}."

    def recall(self):
        mem = self._memory()
        parts = [f"'{sc['phrases'][0]}'" + (f" (or {', '.join(sc['phrases'][1:])})" if len(sc["phrases"]) > 1 else "")
                 + f" means {self.describe(sc)}" for sc in mem["shortcuts"]]
        out = ("I know these shortcuts: " + "; ".join(parts) + ".") if parts else "I haven't learned any shortcuts yet."
        if mem["facts"]:
            out += " And I remember that " + "; ".join(mem["facts"]) + "."
        return out

    def teach(self, text):
        """Handle 'when I say X, do Y', 'X also means Y', 'change X to 30 percent', 'remember that ...',
        'forget ...' and 'what have you learned'. Returns a reply, or None if this isn't teaching."""
        raw = text.strip()
        t = self._norm(raw)
        if re.fullmatch(r"(what (have|did) you learn(ed)?|what do you (remember|know)|list (your )?(shortcuts|memories))( so far)?", t):
            return self.recall()
        m = re.fullmatch(r"(please )?forget (about )?(that )?(.+)", t)
        if m:
            return self.forget(m.group(4))
        m = re.fullmatch(r"(change|update|make|set) (.+?) (to|at) (\d+|[a-z ]+?)( percent)?", spoken_numbers(t))
        if m:
            sc, phrase = self.shortcut_for(m.group(2))
            if sc and self._norm(phrase) == self._norm(m.group(2)) and m.group(4).isdigit():
                return self.set_number(sc["id"], int(m.group(4)))[1]
        m = re.match(r"(?:remember (?:that )?)?(?:when|whenever|if) i say (.+)", raw, re.I)
        if m:
            rest = m.group(1)
            if "," in rest:  # typed: "when I say sit height, set the desk to 25%"
                phrase, command = rest.split(",", 1)
            else:  # spoken: split where the command's verb starts
                words = rest.split()
                cut = next((i for i, w in enumerate(words) if i > 0 and self._norm(w) in self.VERBS_FOR_SPLIT), None)
                if cut is None:
                    return "Tell me what to do too, for example: when I say sit height, set the desk to 25 percent."
                phrase, command = " ".join(words[:cut]), " ".join(words[cut:])
            command = re.sub(r"^\s*(then|you should|please|i mean|it means|that means|do|run)\s+", "", command.strip(), flags=re.I)
            return self.learn([phrase], command)[1]
        m = re.fullmatch(r"(remember (that )?)?(.+?) (also )?means (.+)", t)
        if m and (m.group(1) or m.group(4)):
            return self.learn([m.group(3)], m.group(5))[1]
        m = re.match(r"remember (?:that )?(.+)", raw, re.I)
        if m:
            return self.remember(m.group(1))
        return None

    def facts_prompt(self):
        facts = self._memory()["facts"]
        return ("\nThings the user has told you (\"I\" and \"my\" mean the user):\n" + "\n".join(f"- {f}" for f in facts)) if facts else ""

    def ask(self, text, dry_run=False, on_sentence=None):
        egg = self.easter_egg(text)
        if egg is not None:
            return {"ok": True, "mode": "easter", "reply": egg, "seconds": 0}
        taught = self.teach(text)
        if taught is not None:
            return {"ok": True, "mode": "learn", "reply": taught, "seconds": 0}
        sc, phrase = self.shortcut_for(text)
        if sc:
            return self.run_shortcut(sc, phrase, text, dry_run, on_sentence)
        if self.config.get("chat")["enabled"] and not self.is_command(text):
            return self.converse(text, on_sentence)
        return self.command(text, dry_run, on_sentence)

    def _fit_tool(self, name, args):
        """Small models use the dimmer tool for a desk or blind (and vice versa). Match the tool to the
        device type: a number means position for covers, brightness for lights, volume for players."""
        domain = self.domains.get(str(args.get("name", "")).lower())
        num = next((args[k] for k in ("position", "brightness", "volume_level") if k in args), None)
        if not domain or num is None:
            return name, args
        want = {"cover": ("HassSetPosition", "position"), "valve": ("HassSetPosition", "position"),
                "light": ("HassLightSet", "brightness"), "media_player": ("HassSetVolume", "volume_level")}.get(domain)
        if not want or want[0] == name or want[0] not in self.routes or name not in ("HassLightSet", "HassSetPosition", "HassSetVolume"):
            return name, args
        fitted = {k: v for k, v in args.items() if k not in ("position", "brightness", "volume_level", "color")}
        fitted[want[1]] = num
        return want[0], fitted

    def _ha_call(self, text, name, full, raw, dry_run):
        args = self.fix_args(raw, name)
        fitted, args = self._fit_tool(name, args)
        if fitted != name:
            name, full = fitted, self.routes[fitted][1]
        entry = {"tool": name, "args": args, "raw_args": raw}
        if "name" not in args and "area" not in args:
            room = raw.get("area") or raw.get("name")
            entry["error"] = (f"no devices exposed to Assist in '{room}'" if room and raw.get("area")
                              else "couldn't tell which device")
        elif not self.mentioned(text, args.get("name") or args.get("area")):
            entry["error"] = "couldn't tell which device"  # the model guessed a target you didn't say
        elif dry_run:
            entry["result"] = "(dry run: not sent)"
        else:
            res = self.ha.mcp("tools/call", {"name": full, "arguments": args})
            entry["error"] = res.get("isError", False)
            entry["result"] = self.ha.text(res)[:500]
        return entry

    def command(self, text, dry_run=False, on_sentence=None):
        if not self.ready:
            return {"ok": False, "error": f"not ready: {self.error}"}
        text = spoken_numbers(text)
        with self.lock:
            t0 = time.time()
            msg = self._chat(text)
            llm_s = time.time() - t0
            calls = msg.get("tool_calls") or []
            routes, providers = dict(self.routes), dict(self.providers)
        words = set(re.findall(r"[a-z']+", text.lower()))
        matched, ha_signal = self._matched_providers(words), self._ha_signal(words)
        if not calls:
            # The model answered in text instead of calling a tool. That text is invented (it even
            # writes fake <tool_response> blocks), so never use it. If the request clearly named one
            # single-tool provider ("weather"), call it directly; otherwise treat it as conversation.
            single = [p for p in matched if len(providers[p].tools()) == 1]
            if len(matched) == 1 and single and not ha_signal:
                tool = providers[single[0]].tools()[0]["name"]
                name = next((n for n, r in routes.items() if r == (single[0], tool)), tool)
                calls = [{"function": {"name": name, "arguments": {}}}]
            elif self.config.get("chat")["enabled"] and not ha_signal:
                return self.converse(text, on_sentence)
            else:
                return {"ok": False, "calls": [], "error": "couldn't tell what to do", "seconds": round(llm_s, 1)}
        done, replies, spoken = [], [], False
        for c in calls:
            name, raw = c["function"]["name"], c["function"].get("arguments") or {}
            route = routes.get(name)
            # Tool safety check: a small model sometimes picks the wrong tool ("will it rain" -> dice).
            # Only run a provider's tool if the request mentioned that provider; if exactly one other
            # provider was mentioned and it has a single tool, use that instead.
            mentioned = route and (ha_signal if route[0] == HA_ID else route[0] in matched)
            if route and not mentioned:
                single = [p for p in matched if len(providers[p].tools()) == 1]
                if len(matched) == 1 and single:
                    tool = providers[single[0]].tools()[0]["name"]
                    name = next((n for n, r in routes.items() if r == (single[0], tool)), tool)
                    route, raw = (single[0], tool), {}
                elif route[0] != HA_ID:
                    done.append({"tool": name, "args": raw, "error": "couldn't tell which tool to use"})
                    continue
            if not route:
                done.append({"tool": name, "args": raw, "error": "unknown tool"})
            elif route[0] == HA_ID:
                done.append(self._ha_call(text, name, route[1], raw, dry_run))
            else:
                provider = providers[route[0]]
                entry, reply, was_spoken = self._provider_call(provider, route, name, {**raw, **provider.guess_args(text)},
                                                               text, dry_run, on_sentence)
                done.append(entry)
                if reply:
                    replies.append(reply)
                    spoken = spoken or was_spoken
        out = {"ok": all(not d.get("error") for d in done), "calls": done,
               "seconds": round(time.time() - t0, 1), "llm_seconds": round(llm_s, 1)}
        if replies:
            out.update(mode="tool", reply=" ".join(replies), spoken=spoken)
        return out

    def _provider_call(self, provider, route, name, args, text, dry_run, on_sentence):
        """Run a non-HA tool and phrase its result (its own template, the text itself, or the chat model).
        Returns (entry, reply, spoken)."""
        entry = {"tool": name, "args": args, "provider": route[0]}
        if dry_run:
            entry["result"] = "(dry run: not run)"
            return entry, None, False
        try:
            result, failed = provider.call(route[1], args)
        except Exception as e:
            result, failed = f"{type(e).__name__}: {e}", True
        entry["result"] = result[:500]
        if failed:
            entry["error"] = result[:200] or "the tool failed"
            return entry, None, False
        reply = provider.reply(route[1], args, result)
        if reply is None and len(result) <= 300 and not result.lstrip().startswith(("{", "[", "<")):
            reply = result.strip()  # short plain text: say it as is rather than risk a garbled summary
        if reply is None:
            return entry, self.summarize(text, result, on_sentence), bool(on_sentence)
        return entry, reply, False
