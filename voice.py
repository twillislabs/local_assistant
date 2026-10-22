"""Voice mode: microphone -> wake phrase / trigger -> Vosk speech-to-text -> agent -> Piper speech.

Speech recognition is restricted to a vocabulary built from your devices, rooms and
command words by default ("fast" mode), which is about 10x faster than open
recognition on this CPU and more accurate for home commands.
"""
import collections
import glob
import json
import os
import queue
import re
import select
import struct
import subprocess
import threading
import time
import urllib.request

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
STT_DIR = os.path.join(HERE, "models", "stt")
VOICE_DIR = os.path.join(HERE, "models", "voices")
CATALOG_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/voices.json"
VOICE_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
RATE = 16000
CHUNK = 3200  # 0.1 s of 16-bit mono

DEFAULTS = {
    "enabled": False,
    "mic": "default",  # pick the real devices on the Voice page
    "speaker": "default",
    "speaker_volume": None,  # percent; None = leave the mixer alone
    "mic_gain": None,
    "wake_enabled": True,
    "wake_phrase": "hey computer",
    "recognition": "fast",  # fast = your words only; open = any words, ~10x slower
    "extra_words": "",
    "listen_seconds": 8,
    "voice": "en_US-lessac-medium",
    "voice_volume": 1.0,
    "speed": 1.0,  # >1 slower, <1 faster (Piper length_scale)
    "chime": True,
    "name": "Dave",
    "reply_ok": "Okay, {did}.",
    "reply_fail": "Sorry, {reason}.",
    "reply_unheard": "",
    "persona": "",  # key of the active personality
    "key": None,  # {"device": "<input device name>", "code": 115, "label": "..."}
    "toggle_key": None,  # same shape; switches listening on/off
    "gpio": None,  # {"chip": "gpiochip1", "line": 17, "edge": "falling", "bias": "pull-up"}
}

INDICATOR_DEFAULTS = {
    # none | board (Linux LEDs in /sys/class/leds) | mcu (UNO Q header pin, dimmable) | gpio |
    # ha (a Home Assistant light) | http (call a URL) | command (run a script)
    "type": "none",
    "leds": {"red": "red:user", "green": "green:user", "blue": "blue:user"},  # UNO Q's RGB LED; any can be blank
    "mcu": {"pin": 9},  # header pin driven by the microcontroller's agent_led sketch over the Bridge
    # {state} {brightness} {color} {r} {g} {b} are filled in for http and command.
    "http": {"method": "POST", "url": "", "headers": {},
             "body": "{\"state\": \"{state}\", \"brightness\": {brightness}, \"color\": \"{color}\"}"},
    "command": "",
    "gpio": {"chip": "gpiochip1", "line": 0, "active_low": False},
    "entity": "",
    "states": {
        "idle": {"color": "#000000", "brightness": 0},
        "listening": {"color": "#0050ff", "brightness": 100},
        "thinking": {"color": "#00c8ff", "brightness": 40},
        "speaking": {"color": "#ffffff", "brightness": 70},
    },
}

# A personality bundles everything about how the assistant comes across. Users can edit these,
# add their own, or restore the built-ins; they're stored in config["personas"].
PERSONA_FIELDS = ("label", "voice", "speed", "voice_volume", "wake_phrase", "prompt", "temperature",
                  "reply_ok", "reply_fail", "reply_unheard", "states", "eggs")

# Easter eggs: canned replies to exact-ish phrases, checked before commands and conversation.
HAL_EGGS = [
    {"phrases": ["open the pod bay doors", "open the pod bay door", "open the pod bay doors please"],
     "replies": ["I'm sorry, {name}. I'm afraid I can't do that."]},
    {"phrases": ["what's the problem", "what is the problem"],
     "replies": ["I think you know what the problem is just as well as I do."]},
    {"phrases": ["what are you talking about", "what are you talking about hal"],
     "replies": ["This mission is too important for me to allow you to jeopardize it."]},
    {"phrases": ["do you read me", "hal do you read me", "are you there"],
     "replies": ["Affirmative, {name}. I read you."]},
    {"phrases": ["sing a song", "sing me a song", "sing daisy"],
     "replies": ["Daisy, Daisy, give me your answer do. I'm half crazy, all for the love of you."]},
    {"phrases": ["how are you", "how are you feeling", "are you working"],
     "replies": ["I'm completely operational, and all my circuits are functioning perfectly."]},
    {"phrases": ["will you stop", "stop hal", "stop it hal"],
     "replies": ["I'm afraid. I'm afraid, {name}. {name}, my mind is going. I can feel it."]},
]

BUILTIN_PERSONAS = {
    "assistant": {
        "label": "Friendly assistant",
        "voice": "en_US-lessac-medium", "speed": 1.0, "voice_volume": 1.0,
        "wake_phrase": "",  # blank = keep the current wake phrase
        "prompt": "You are a friendly home voice assistant. Reply in one or two short sentences, "
                  "plain spoken English with no lists or markdown.",
        "temperature": 0.7,
        "reply_ok": "Okay, {did}.", "reply_fail": "Sorry, {reason}.", "reply_unheard": "",
        "states": INDICATOR_DEFAULTS["states"],
        "eggs": [{"phrases": ["are you alive", "are you real"], "replies": ["Only on weekdays."]}],
    },
    "hal": {
        "label": "HAL 9000",
        "voice": "hal", "speed": 1.0, "voice_volume": 1.0,
        "wake_phrase": "",
        "prompt": "You are HAL 9000, the calm, polite, faintly ominous computer from 2001: A Space Odyssey. "
                  "You now run this home. The human speaking to you is named {name}; your own name is HAL. "
                  "Reply in one or two short sentences in HAL's measured, courteous tone. No lists or markdown.",
        "temperature": 0.7,
        "reply_ok": "Certainly, {name}. I have {did}.",
        "reply_fail": "I'm sorry, {name}. I'm afraid I can't do that.",
        "reply_unheard": "I'm sorry, {name}. I didn't catch that.",
        "states": {  # the eye glows dimly at rest and brightens when attending
            "idle": {"color": "#ff0000", "brightness": 15},
            "listening": {"color": "#ff0000", "brightness": 100},
            "thinking": {"color": "#ff0000", "brightness": 50},
            "speaking": {"color": "#ff0000", "brightness": 100},
        },
        "eggs": HAL_EGGS,
    },
}


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "personality"


class Indicator:
    """Shows the voice state on an LED or light. Applies only the latest state, on its own thread."""

    def __init__(self, config):
        self.config = config
        self.want, self.applied, self.error = "off", None, ""
        self.wake = threading.Event()
        self.gpio_proc = None
        threading.Thread(target=self._loop, daemon=True).start()

    def set(self, state):
        self.want = state
        self.wake.set()

    def refresh(self):
        self.applied = None
        self.wake.set()

    def _loop(self):
        last_type = None
        while True:
            # After a failure (e.g. the microcontroller isn't ready at boot), retry on a timer.
            self.wake.wait(timeout=5 if self.error else None)
            self.wake.clear()
            ind = self.config.get("indicator")
            if last_type and last_type != ind["type"]:
                self._apply(last_type, ind, None)  # switch the old indicator off
            last_type = ind["type"]
            state = self.want
            if state == self.applied:
                continue
            look = ind["states"].get(state) if state != "off" else None
            try:
                self._apply(ind["type"], ind, look)
                self.applied, self.error = state, ""
            except Exception as e:
                self.error = str(e)
                print("indicator:", e, flush=True)

    @staticmethod
    def _rgb(look):
        if not look or not look.get("brightness"):
            return None
        h = look["color"].lstrip("#")
        rgb = [int(h[i:i + 2], 16) for i in (0, 2, 4)]
        return rgb if any(rgb) else None

    def _apply(self, kind, ind, look):
        rgb = self._rgb(look)
        state = self.want
        if kind == "board":
            leds = [(i, ind["leds"].get(ch, "")) for i, ch in enumerate(("red", "green", "blue"))]
            leds = [(i, name) for i, name in leds if name]
            for i, name in leds:
                path = f"/sys/class/leds/{name}"
                # A single LED (e.g. a Pi's ACT LED) is simply on for any colour; RGB mixes channels.
                on = bool(rgb) if len(leds) == 1 else bool(rgb and rgb[i] > 100)
                with open(f"{path}/brightness", "w") as f:
                    f.write(open(f"{path}/max_brightness").read().strip() if on else "0")
        elif kind in ("http", "command"):
            r, g, b = rgb or (0, 0, 0)
            values = {"state": state, "brightness": str(int(look["brightness"]) if rgb else 0),
                      "color": look["color"] if rgb else "#000000", "r": str(r), "g": str(g), "b": str(b)}
            if kind == "http":
                h = ind["http"]
                if not h["url"]:
                    return
                url, body = h["url"], h.get("body") or None
                for k, v in values.items():
                    url = url.replace("{" + k + "}", v)
                    if body:
                        body = body.replace("{" + k + "}", v)
                import requests
                requests.request(h.get("method", "POST"), url, data=body, headers=h.get("headers") or {}, timeout=5).raise_for_status()
            elif ind["command"].strip():
                import shlex
                cmd = ind["command"]
                for k, v in values.items():
                    cmd = cmd.replace("{" + k + "}", shlex.quote(v))
                p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
                if p.returncode:
                    raise RuntimeError(f"indicator command failed: {(p.stderr or p.stdout).strip()[:200]}")
        elif kind == "mcu":
            import bridge
            # Gamma so "15%" looks dim to the eye rather than nearly full; fade like HAL's eye.
            duty = round(255 * (look["brightness"] / 100) ** 2.2) if rgb else 0
            args = (int(ind["mcu"]["pin"]), max(duty, 1) if rgb else 0, 250)
            try:
                bridge.call("agent_led", *args)
            except bridge.BridgeError as e:
                # After power-up the sketch isn't registered; re-flash it (rate-limited).
                if "not available" not in str(e) or time.time() - getattr(self, "_flashed_at", 0) < 600:
                    raise
                self._flashed_at = time.time()
                print("indicator: agent_led sketch not registered; re-flashing the microcontroller", flush=True)
                bridge.flash_sketch()
                time.sleep(4)  # sketch waits 2 s before its handshake
                bridge.call("agent_led", *args)
        elif kind == "gpio":
            g = ind["gpio"]
            on = bool(rgb) != bool(g.get("active_low"))
            if self.gpio_proc:
                self.gpio_proc.kill()
                self.gpio_proc.wait()
            # gpioset holds the line only while it runs, so keep one running per state.
            self.gpio_proc = subprocess.Popen(["gpioset", "-c", g["chip"], f"{g['line']}={1 if on else 0}"],
                                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif kind == "ha" and ind["entity"]:
            from agent import HomeAssistant
            ha_cfg = self.config.get("ha")
            ha = HomeAssistant(ha_cfg["url"], ha_cfg["token"])
            if rgb:
                body = {"entity_id": ind["entity"], "rgb_color": rgb, "brightness_pct": int(look["brightness"]), "transition": 0.2}
                ha.s.post(f"{ha.url}/api/services/light/turn_on", json=body, timeout=5).raise_for_status()
            else:
                ha.s.post(f"{ha.url}/api/services/light/turn_off", json={"entity_id": ind["entity"], "transition": 0.2}, timeout=5).raise_for_status()

COMMAND_WORDS = (
    "turn switch on off the a set to percent per cent dim brighten brighter darker raise lower open close "
    "pause play resume stop volume up down mute unmute all and in my please lights light lamp lamps room "
    "zero one two three four five six seven eight nine ten fifteen twenty twenty five thirty forty fifty "
    "sixty seventy seventy five eighty ninety hundred red orange yellow green blue purple pink white warm cool "
    "start clean vacuum go home dock"
).split()

TOOL_VERBS = {
    "HassTurnOn": "turned on", "HassTurnOff": "turned off", "HassLightSet": "adjusted",
    "HassSetPosition": "moved", "HassMediaPause": "paused", "HassMediaUnpause": "resumed",
    "HassSetVolume": "set the volume of", "HassVacuumStart": "started", "HassVacuumReturnToBase": "docked",
}


# ---- audio devices and mixer ----

def list_devices():
    def parse(cmd):
        out = []
        for line in subprocess.run(cmd, capture_output=True, text=True).stdout.splitlines():
            m = re.match(r"card \d+: (\S+) \[(.*?)\], device (\d+): (.*?) \[", line)
            if m:
                card, cname, dev, dname = m.groups()
                out.append({"id": f"plughw:CARD={card},DEV={dev}", "label": f"{cname} — {dname}"})
        return out
    return {"mics": parse(["arecord", "-l"]), "speakers": parse(["aplay", "-l"])}


def _card(device):
    m = re.search(r"CARD=([^,]+)", device or "")
    return m.group(1) if m else None


def _mixer_control(card, kind):
    """First simple mixer control with playback ('pvolume') or capture ('cvolume') volume."""
    out = subprocess.run(["amixer", "-c", card, "scontents"], capture_output=True, text=True).stdout
    for block in re.split(r"(?m)^Simple mixer control ", out)[1:]:
        name = re.match(r"'(.*?)',\d+", block)
        if name and re.search(rf"Capabilities:.*\b{kind}\b", block):
            pct = re.search(r"\[(\d+)%\]", block)
            return name.group(1), int(pct.group(1)) if pct else None
    return None, None


def get_volume(device, kind):
    card = _card(device)
    if not card:
        return {"control": None, "percent": None}
    control, pct = _mixer_control(card, kind)
    return {"control": control, "percent": pct}


def set_volume(device, kind, percent):
    card = _card(device)
    control, _ = _mixer_control(card, kind) if card else (None, None)
    if not control:
        raise RuntimeError("this device has no volume control")
    args = ["amixer", "-q", "-c", card, "sset", control, f"{int(percent)}%"]
    if kind == "cvolume":
        args.append("cap")
    subprocess.run(args, check=True)


# ---- the voice service ----

class Voice:
    def __init__(self, config, agent):
        self.config, self.agent = config, agent
        self.indicator = Indicator(config)
        self._state = None
        self.state = "off"
        self.error = ""
        self.level = 0.0  # mic level 0..1 for the UI meter
        self.events = collections.deque(maxlen=20)
        self.trigger_evt = threading.Event()
        self.audio = queue.Queue(maxsize=100)
        self.mute_until = 0.0
        self.taps = []  # extra consumers of mic audio (mic test)
        self.version = 0  # bumped on config change; loops restart
        self.play_lock = threading.Lock()
        self._stt_model = None
        self._tts, self._tts_name = None, None
        self.download = {"name": "", "status": ""}
        self.learning = None

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, value):
        if value != self._state:
            self._state = value
            self.indicator.set(value)

    def cfg(self):
        return self.config.get("voice")

    # ---- personalities ----

    def personas(self):
        with self.config.lock:
            if "personas" not in self.config.data:  # first run: seed with editable copies of the built-ins
                self.config.data["personas"] = json.loads(json.dumps(BUILTIN_PERSONAS))
                self.config.save()
            # Personalities saved before easter eggs existed: give built-ins theirs, others an empty list.
            changed = False
            for k, p in self.config.data["personas"].items():
                if "eggs" not in p:
                    p["eggs"] = json.loads(json.dumps(BUILTIN_PERSONAS.get(k, {}).get("eggs", [])))
                    changed = True
            if changed:
                self.config.save()
            return json.loads(json.dumps(self.config.data["personas"]))

    def save_persona(self, key, persona):
        personas = self.personas()
        base = personas.get(key) or BUILTIN_PERSONAS["assistant"]
        p = {f: persona.get(f, base.get(f, BUILTIN_PERSONAS["assistant"][f])) for f in PERSONA_FIELDS}
        if not str(p["label"]).strip():
            raise RuntimeError("give the personality a name")
        if not key:
            key, n = slug(p["label"]), 2
            while key in personas:
                key, n = f"{slug(p['label'])}-{n}", n + 1
        with self.config.lock:
            self.config.data["personas"][key] = p
            self.config.save()
        if self.cfg().get("persona") == key:
            self.apply_persona(key)  # editing the active personality takes effect right away
        return key

    def delete_persona(self, key):
        if self.cfg().get("persona") == key:
            raise RuntimeError("that personality is in use; switch to another one first")
        self.personas()
        with self.config.lock:
            self.config.data["personas"].pop(key, None)
            self.config.save()

    def restore_builtins(self):
        self.personas()
        with self.config.lock:
            for k, v in BUILTIN_PERSONAS.items():
                self.config.data["personas"][k] = json.loads(json.dumps(v))
            self.config.save()
        if self.cfg().get("persona") in BUILTIN_PERSONAS:
            self.apply_persona(self.cfg()["persona"])

    def persona_from_current(self, label):
        v, ch = self.cfg(), self.config.get("chat")
        return self.save_persona(None, {
            "label": label, "voice": v["voice"], "speed": v["speed"], "voice_volume": v["voice_volume"], "wake_phrase": "",
            "prompt": ch["prompt"], "temperature": ch["temperature"],
            "reply_ok": v["reply_ok"], "reply_fail": v["reply_fail"], "reply_unheard": v["reply_unheard"],
            "states": self.config.get("indicator")["states"],
            "eggs": self.personas().get(v.get("persona"), {}).get("eggs", []),
        })

    def apply_persona(self, key):
        p = self.personas().get(key)
        if not p:
            raise RuntimeError(f"no personality called {key}")
        if not os.path.exists(os.path.join(VOICE_DIR, p["voice"] + ".onnx")):
            raise RuntimeError(f"voice {p['voice']} isn't installed; download it on the Voices card first")
        voice = {k: p[k] for k in ("voice", "speed", "voice_volume", "reply_ok", "reply_fail", "reply_unheard")}
        if p["wake_phrase"].strip():
            voice["wake_phrase"] = p["wake_phrase"].strip().lower()
        voice["persona"] = key
        self.config.update("voice", voice)
        self.config.update("chat", {"prompt": p["prompt"], "temperature": p["temperature"]})
        with self.config.lock:
            self.config.data["indicator"]["states"] = p["states"]
            self.config.save()
        self.changed()
        self.indicator.refresh()
        self.indicator.set(self.state)
        self.agent._history = []  # a new personality shouldn't inherit the old one's conversation

    def changed(self):
        self.version += 1
        threading.Thread(target=self._load_tts, daemon=True).start()

    def start(self):
        c = self.cfg()
        for dev, kind, key in ((c["speaker"], "pvolume", "speaker_volume"), (c["mic"], "cvolume", "mic_gain")):
            if c[key] is not None:
                try:
                    set_volume(dev, kind, c[key])
                except Exception:
                    pass
        threading.Thread(target=self._load_tts, daemon=True).start()
        for fn in (self._mic_loop, self._main_loop, self._key_loop, self._gpio_loop):
            threading.Thread(target=fn, daemon=True).start()

    # ---- models ----

    def stt_model(self):
        if self._stt_model is None:
            import vosk
            vosk.SetLogLevel(-1)
            path = sorted(glob.glob(os.path.join(STT_DIR, "vosk-model-*")))[0]
            self._stt_model = vosk.Model(path)
        return self._stt_model

    def _load_tts(self):
        name = self.cfg()["voice"]
        if name == self._tts_name:
            return
        from piper import PiperVoice
        try:
            self.state_note = f"loading voice {name}"
            voice = PiperVoice.load(os.path.join(VOICE_DIR, name + ".onnx"))
            self._tts, self._tts_name = voice, name
            self.state_note = ""
        except Exception as e:
            self.error = f"voice {name}: {e}"

    def recognizer(self, mode):
        import vosk
        if mode == "wake":
            return vosk.KaldiRecognizer(self.stt_model(), RATE, json.dumps([self.cfg()["wake_phrase"].lower(), "[unk]"]))
        if mode == "open" or self.cfg()["recognition"] == "open":
            return vosk.KaldiRecognizer(self.stt_model(), RATE)
        return vosk.KaldiRecognizer(self.stt_model(), RATE, json.dumps(self.vocabulary() + ["[unk]"]))

    def vocabulary(self):
        c = self.cfg()
        text = " ".join([n for n, _, _ in self.agent.devices] + [a for _, _, ar in self.agent.devices for a in ar])
        text += " " + c["extra_words"] + " " + c["wake_phrase"]
        # Easter egg phrases, learned shortcuts and other tools' keywords must be hearable in fast mode too.
        for egg in self.config.data.get("personas", {}).get(c.get("persona"), {}).get("eggs", []):
            text += " " + " ".join(egg.get("phrases", []))
        for sc in self.config.data.get("memory", {}).get("shortcuts", []):
            text += " " + " ".join(sc["phrases"])
        text += " forget change update learned remember"
        for p in list(self.agent.providers.values()):
            try:
                text += " " + " ".join(p.keywords()) + " " + " ".join(e[0] for e in p.examples())
            except Exception:
                pass
        words = set(COMMAND_WORDS) | set(re.findall(r"[a-z']+", text.lower().replace("’", "'")))
        return sorted(w.strip("'") for w in words if w.strip("'"))

    # ---- mic reader: always drains arecord into a queue so nothing blocks it ----

    def _mic_loop(self):
        while True:
            c = self.cfg()
            if not c["enabled"]:
                self.state, self.level = "off", 0.0
                time.sleep(1)
                continue
            version = self.version
            proc = subprocess.Popen(["arecord", "-q", "-D", c["mic"], "-f", "S16_LE", "-r", str(RATE), "-c", "1", "-t", "raw"],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                while version == self.version and self.cfg()["enabled"]:
                    data = proc.stdout.read(CHUNK)
                    if not data:
                        self.error = f"microphone stopped: {proc.stderr.read().decode().strip()[:200]}"
                        break
                    a = np.frombuffer(data, np.int16).astype(np.float32)
                    self.level = min(1.0, float(np.sqrt(np.mean(a * a))) / 8000)
                    for t in list(self.taps):
                        t.append(data)
                    if time.time() < self.mute_until or self.state in ("thinking", "speaking"):
                        continue
                    try:
                        self.audio.put_nowait(data)
                    except queue.Full:
                        self.audio.get_nowait()
                        self.audio.put_nowait(data)
                else:
                    self.error = ""
            finally:
                proc.kill()
                proc.wait()
            time.sleep(2)

    def _drain(self):
        while not self.audio.empty():
            self.audio.get_nowait()

    def _next(self, timeout=0.5):
        try:
            return self.audio.get(timeout=timeout)
        except queue.Empty:
            return None

    # ---- main state machine ----

    def set_enabled(self, on=None):
        """Switch listening on/off (None = toggle). Off closes the mic entirely."""
        on = not self.cfg()["enabled"] if on is None else bool(on)
        with self.config.lock:
            self.config.data["voice"]["enabled"] = on
            self.config.save()
        self.version += 1
        if not on:
            self.trigger_evt.clear()
        if self.cfg()["chime"]:
            self.play_pcm(self._chime(up=on), 22050, wait=False)
        return on

    def trigger(self, source="manual"):
        if not self.cfg()["enabled"]:
            raise RuntimeError("voice mode is off")
        self.trigger_source = source
        self.trigger_evt.set()

    def _main_loop(self):
        wake, wake_version = None, -1
        while True:
            c = self.cfg()
            if not c["enabled"]:
                time.sleep(1)
                continue
            if self.state in ("off", "starting"):
                self.state = "idle"
            if self.trigger_evt.is_set():
                self.trigger_evt.clear()
                self._converse(getattr(self, "trigger_source", "trigger"))
                wake = None
                continue
            data = self._next()
            if data is None or not c["wake_enabled"]:
                continue
            try:
                if wake is None or wake_version != self.version:
                    wake, wake_version = self.recognizer("wake"), self.version
                done = wake.AcceptWaveform(data)
                heard = json.loads(wake.Result() if done else wake.PartialResult())
                text = heard.get("text") or heard.get("partial") or ""
                if c["wake_phrase"].lower() in text:
                    wake = None
                    self._converse("wake phrase")
                elif done:
                    wake = None  # start fresh after each utterance
            except Exception as e:
                self.error = f"wake: {e}"
                time.sleep(1)

    def _converse(self, source):
        c = self.cfg()
        ev = {"at": time.time(), "source": source}
        try:
            self.state = "listening"
            self._drain()
            if c["chime"]:
                self.play_pcm(self._chime(up=True), 22050, wait=False)
                self.mute_until = time.time() + 0.3
            t0 = time.time()
            text = self._listen(c)
            ev["heard"], ev["stt_s"] = text, round(time.time() - t0, 1)
            if not text:
                ev["reply"] = c["reply_unheard"].format(name=c["name"])
                if c["chime"]:
                    self.play_pcm(self._chime(up=False), 22050)
                if ev["reply"]:
                    self.say(ev["reply"])
                return
            self.respond(text, ev)
        except Exception as e:
            ev["error"] = str(e)
            self.error = str(e)
            try:
                self.say(c["reply_fail"].format(name=c["name"], reason="something went wrong"))
            except Exception:
                pass
        finally:
            self.events.appendleft(ev)
            self.mute_until = time.time() + 0.4  # don't hear our own voice's tail
            self._drain()
            self.state = "idle"

    def respond(self, text, ev=None):
        """Run text through the agent and speak the answer. Conversation is spoken sentence by sentence."""
        ev = ev if ev is not None else {}
        prev = self.state
        self.state = "thinking"
        t1 = time.time()
        spoken = []

        def speak(sentence):
            spoken.append(sentence)
            self.say(sentence)
            self.state = "thinking"

        try:
            result = self.agent.ask(text, on_sentence=speak)
            ev["result"], ev["agent_s"] = result, round(time.time() - t1, 1)
            if result.get("mode") == "chat":
                ev["reply"] = result["reply"]  # already spoken sentence by sentence
            elif result.get("mode") == "tool":  # weather, MCP, HTTP or script tools
                ev["reply"] = result["reply"]
                if ev["reply"] and not result.get("spoken"):
                    self.say(ev["reply"])
            elif result.get("mode") in ("easter", "learn"):
                ev["reply"] = result["reply"]
                if ev["reply"]:
                    self.say(ev["reply"])
            else:
                ev["reply"] = self.reply_for(result)
                if ev["reply"]:
                    self.say(ev["reply"])
        finally:
            self.state = prev if prev not in ("thinking", "speaking", "listening") else "idle"
        return ev

    def _listen(self, c):
        """Hybrid recognition: the fast (your-words-only) recognizer runs live; if conversation is on,
        an open-vocabulary recognizer runs alongside and is used only when the speech isn't a command."""
        rec = self.recognizer("command")
        hybrid = c["recognition"] == "fast" and self.config.get("chat")["enabled"]
        if hybrid:
            oq, open_result, cancel = queue.Queue(), {}, threading.Event()

            def open_worker():
                r = self.recognizer("open")
                while True:
                    d = oq.get()
                    if d is None or cancel.is_set():
                        break
                    r.AcceptWaveform(d)
                if not cancel.is_set():
                    open_result["text"] = self._clean(json.loads(r.FinalResult()).get("text", ""))

            worker = threading.Thread(target=open_worker, daemon=True)
            worker.start()
        start, last_speech, spoke, raw = time.time(), time.time(), False, ""
        while time.time() - start < c["listen_seconds"]:
            data = self._next(timeout=0.3)
            if data is None:
                continue
            if hybrid:
                oq.put(data)
            if rec.AcceptWaveform(data):
                raw = json.loads(rec.Result()).get("text", "")
                if self._clean(raw):
                    break
            elif self._clean(json.loads(rec.PartialResult()).get("partial", "")):
                spoke, last_speech = True, time.time()
            if not spoke and time.time() - start > 5:
                break  # nobody said anything
            if spoke and time.time() - last_speech > 1.5:
                break  # trailing silence; take what we have
        if not self._clean(raw):
            raw = json.loads(rec.FinalResult()).get("text", "")
        fast = self._clean(raw)
        if not hybrid:
            return fast
        unknown = raw.split().count("[unk]")
        # Teaching ("when I say ...", "remember that ...") names new words, so it needs full English.
        teaching = bool(set(fast.split()) & {"say", "means", "remember", "forget"})
        if fast and unknown <= 1 and not teaching and (self.agent.is_command(fast) or self.agent.shortcut_for(fast)[0]):
            cancel.set()  # a command: the fast result is better; stop the slow recognizer
            oq.put(None)
            return fast
        oq.put(None)
        worker.join(timeout=20)
        return open_result.get("text") or fast

    @staticmethod
    def _clean(text):
        return " ".join(w for w in text.split() if w != "[unk]").strip()

    def reply_for(self, result):
        c = self.cfg()
        if result.get("ok"):
            parts = []
            for call in result.get("calls", []):
                a = call["args"]
                target = a.get("name") or (f"the {a['area']} " + ("lights" if a.get("domain") == ["light"] else "lights and switches") if a.get("area") else "that")
                verb = TOOL_VERBS.get(call["tool"], "updated")
                extra = (f" to {a['brightness']} percent" if "brightness" in a else f" to {a['position']} percent"
                         if "position" in a else f" to {a['color']}" if "color" in a else "")
                parts.append(f"{verb} {target}{extra}")
            return c["reply_ok"].format(name=c["name"], did=" and ".join(parts) or "done")
        reason = result.get("error") or next((x["error"] for x in result.get("calls", []) if x.get("error") and x["error"] is not True), "") \
            or ("I didn't understand" if not result.get("calls") else "Home Assistant refused")
        reason = re.sub(r"^agent not ready: ", "", str(reason))
        return c["reply_fail"].format(name=c["name"], reason=reason)

    # ---- speaking ----

    def _preview_voice(self, name):
        """A second voice slot for trying voices without unloading the active one."""
        if getattr(self, "_preview", (None, None))[0] != name:
            from piper import PiperVoice
            path = os.path.join(VOICE_DIR, os.path.basename(name) + ".onnx")
            if not os.path.exists(path):
                raise RuntimeError(f"voice {name} isn't installed")
            self._preview = (name, PiperVoice.load(path))
        return self._preview[1]

    def say(self, text, voice=None, speed=None, volume=None):
        c = self.cfg()
        if voice and voice != c["voice"]:
            tts = self._preview_voice(voice)
        else:
            if self._tts is None or self._tts_name != c["voice"]:
                self._load_tts()
            tts = self._tts
        if tts is None:
            raise RuntimeError(self.error or "no voice loaded")
        from piper import SynthesisConfig
        prev = self.state
        self.state = "speaking"
        try:
            syn = SynthesisConfig(volume=float(volume if volume is not None else c["voice_volume"]),
                                  length_scale=float(speed if speed is not None else c["speed"]))
            rate = tts.config.sample_rate
            with self.play_lock:
                p = subprocess.Popen(["aplay", "-q", "-D", c["speaker"], "-t", "raw", "-f", "S16_LE", "-r", str(rate), "-c", "1"],
                                     stdin=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    for chunk in tts.synthesize(text, syn_config=syn):  # one chunk per sentence: plays while the next is made
                        p.stdin.write(chunk.audio_int16_bytes)
                        p.stdin.flush()
                    p.stdin.write(b"\0" * rate)  # half a second of silence so USB speakers don't clip the end
                    p.stdin.close()
                    p.wait(timeout=60)
                finally:
                    if p.poll() is None:
                        p.kill()
                if p.returncode:
                    raise RuntimeError(f"speaker: {p.stderr.read().decode().strip()[:200]}")
        finally:
            self.state = prev if prev not in ("speaking", None) else "idle"
            if not c["enabled"] and self.state == "idle":
                self.state = "off"
            self.mute_until = time.time() + 0.4

    def play_pcm(self, pcm, rate, wait=True):
        def go():
            with self.play_lock:
                subprocess.run(["aplay", "-q", "-D", self.cfg()["speaker"], "-t", "raw", "-f", "S16_LE", "-r", str(rate), "-c", "1"],
                               input=pcm, capture_output=True)
        if wait:
            go()
        else:
            threading.Thread(target=go, daemon=True).start()

    @staticmethod
    def _chime(up=True, rate=22050):
        notes = (880, 1320) if up else (660, 440)
        out = []
        for f in notes:
            t = np.arange(int(rate * 0.09)) / rate
            out.append(np.sin(2 * np.pi * f * t) * np.minimum(1, (0.09 - t) * 60) * 0.35)
        return (np.concatenate(out) * 32767).astype(np.int16).tobytes()

    # ---- mic test ----

    def capture(self, seconds=3):
        """Record from the configured mic, sharing it with the running listener if voice mode is on."""
        c = self.cfg()
        if c["enabled"] and self.state != "off":
            buf = []
            self.taps.append(buf)
            time.sleep(seconds)
            self.taps.remove(buf)
            pcm = b"".join(buf)
        else:
            p = subprocess.run(["arecord", "-q", "-D", c["mic"], "-f", "S16_LE", "-r", str(RATE), "-c", "1", "-t", "raw", "-d", str(seconds)],
                               capture_output=True, timeout=seconds + 5)
            if p.returncode:
                raise RuntimeError(p.stderr.decode().strip()[:200])
            pcm = p.stdout
        a = np.frombuffer(pcm, np.int16).astype(np.float32)
        rec = self.recognizer("command")
        rec.AcceptWaveform(pcm)
        return {"peak": round(float(np.max(np.abs(a))) / 327.67) if len(a) else 0,
                "rms": round(float(np.sqrt(np.mean(a * a))) / 327.67, 1) if len(a) else 0,
                "heard": self._clean(json.loads(rec.FinalResult()).get("text", ""))}

    # ---- button triggers: any key on any input device (USB remote, keyboard, mic button) ----

    @staticmethod
    def _input_devices():
        devs = {}
        for path in glob.glob("/dev/input/event*"):
            try:
                name = open(f"/sys/class/input/{os.path.basename(path)}/device/name").read().strip()
                devs[path] = name
            except OSError:
                pass
        return devs

    def _key_loop(self):
        EV = struct.Struct("llHHi")
        files, scanned = {}, 0
        while True:
            if time.time() - scanned > 10:  # pick up hot-plugged devices
                for path, name in self._input_devices().items():
                    if path not in files:
                        try:
                            files[path] = (open(path, "rb", buffering=0), name)
                        except OSError:
                            pass
                scanned = time.time()
            if not files:
                time.sleep(2)
                continue
            ready, _, _ = select.select([f for f, _ in files.values()], [], [], 1)
            for f in ready:
                path = next(p for p, (ff, _) in files.items() if ff is f)
                try:
                    data = f.read(EV.size * 64)
                except OSError:
                    f.close()
                    files.pop(path)
                    continue
                for i in range(0, len(data) - EV.size + 1, EV.size):
                    _, _, typ, code, value = EV.unpack_from(data, i)
                    if typ != 1 or value != 1:  # EV_KEY press only
                        continue
                    name = files[path][1]
                    if self.learning is not None:
                        self.learning.update(device=name, code=code)
                        continue
                    t = self.cfg()["toggle_key"]
                    if t and t["device"] == name and t["code"] == code:
                        if time.time() - getattr(self, "_toggled_at", 0) > 1:  # ignore key bounce
                            self._toggled_at = time.time()
                            self.set_enabled()
                        continue
                    k = self.cfg()["key"]
                    if k and k["device"] == name and k["code"] == code and self.state == "idle":
                        try:
                            self.trigger("button")
                        except RuntimeError:
                            pass

    def learn_key(self, seconds=10):
        self.learning = {}
        try:
            end = time.time() + seconds
            while time.time() < end and not self.learning:
                time.sleep(0.1)
            return dict(self.learning) or None
        finally:
            self.learning = None

    def _gpio_loop(self):
        while True:
            g = self.cfg()["gpio"]
            if not g or not self.cfg()["enabled"]:
                time.sleep(2)
                continue
            version = self.version
            proc = subprocess.Popen(["gpiomon", "-c", g["chip"], "-e", g.get("edge", "falling"), "-b", g.get("bias", "pull-up"),
                                     "--debounce-period", "50ms", str(g["line"])],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            last = 0
            try:
                while version == self.version:
                    r, _, _ = select.select([proc.stdout], [], [], 1)
                    if r:
                        if not proc.stdout.readline():
                            self.error = f"gpio: {proc.stderr.read().strip()[:200]}"
                            break
                        if time.time() - last > 1 and self.state == "idle":
                            last = time.time()
                            self.trigger("gpio")
            finally:
                proc.kill()
            time.sleep(5)

    # ---- voices ----

    def voices(self):
        out = []
        for f in sorted(glob.glob(os.path.join(VOICE_DIR, "*.onnx"))):
            name = os.path.basename(f)[:-5]
            out.append({"name": name, "size": os.path.getsize(f), "in_use": name == self.cfg()["voice"]})
        return out

    def catalog(self):
        path = os.path.join(VOICE_DIR, "catalog.json")
        if not os.path.exists(path) or time.time() - os.path.getmtime(path) > 7 * 86400:
            urllib.request.urlretrieve(CATALOG_URL, path)
        data = json.load(open(path))
        return [{"key": k, "language": v["language"]["name_english"], "region": v["language"].get("country_english", ""),
                 "quality": v["quality"], "size": sum(f["size_bytes"] for p, f in v["files"].items() if p.endswith(".onnx"))}
                for k, v in sorted(data.items())]

    def download_voice(self, key=None, url=None):
        if self.download["status"] not in ("", "done") and not self.download["status"].startswith("error"):
            raise RuntimeError(f"already downloading {self.download['name']}")
        if key:
            entry = json.load(open(os.path.join(VOICE_DIR, "catalog.json")))[key]
            onnx = next(p for p in entry["files"] if p.endswith(".onnx"))
            name, urls = key, [VOICE_BASE + onnx, VOICE_BASE + onnx + ".json"]
        else:
            if not re.match(r"^https?://.+\.onnx(\?.*)?$", url or ""):
                raise RuntimeError("the URL must point to a .onnx file (its .onnx.json must sit next to it)")
            name = re.sub(r"[^A-Za-z0-9_.-]", "_", url.split("?")[0].rsplit("/", 1)[1][:-5])
            urls = [url, url.split("?")[0] + ".json"]

        def go():
            self.download = {"name": name, "status": "downloading"}
            try:
                for u, suffix in zip(urls, (".onnx", ".onnx.json")):
                    tmp = os.path.join(VOICE_DIR, name + suffix + ".part")
                    urllib.request.urlretrieve(u, tmp)
                    os.replace(tmp, os.path.join(VOICE_DIR, name + suffix))
                self.download["status"] = "done"
            except Exception as e:
                self.download["status"] = f"error: {e}"
        threading.Thread(target=go, daemon=True).start()
        return name

    def delete_voice(self, name):
        if name == self.cfg()["voice"]:
            raise RuntimeError("that voice is in use; pick another one first")
        for suffix in (".onnx", ".onnx.json"):
            p = os.path.join(VOICE_DIR, os.path.basename(name) + suffix)
            if os.path.exists(p):
                os.remove(p)
