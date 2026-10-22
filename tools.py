"""Tool providers the assistant can use besides Home Assistant.

Each provider is configured on the Tools page and stored in config["tools"]:
  weather  - current conditions and tomorrow's forecast from Open-Meteo (no API key)
  mcp      - any MCP server, over HTTP (streamable HTTP / SSE replies) or a local command (stdio)
  http     - one tool that calls an HTTP API you describe
  command  - one tool that runs a script or command you describe

A provider exposes tools (name, description, JSON-schema parameters), runs them, and
optionally phrases the result itself; otherwise the chat model summarises it.
"""
import datetime
import json
import re
import shlex
import subprocess
import threading
import urllib.parse

import requests

STOPWORDS = {"get", "set", "list", "the", "tool", "create", "update", "delete", "run", "call", "info", "data", "with"}

_UNITS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine".split())}
_TEENS = {w: i for i, w in enumerate("ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split(), 10)}
_TENS = {w: 10 * i for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split(), 2)}
_SCALES = {"thousand": 1000, "million": 1000000}


def spoken_numbers(text):
    """'set it to twenty five percent' -> 'set it to 25 percent', 'two thousand five hundred' -> '2500'.
    Speech recognition spells numbers out; the model and the calculator need digits."""
    out, words = [], text.split()
    total = cur = 0
    active, last = False, None  # last: the kind of the previous number word

    def flush():
        nonlocal total, cur, active, last
        if active:
            out.append(str(total + cur))
        total = cur = 0
        active, last = False, None

    for i, w in enumerate(words):
        lw = w.lower().strip(",.!?")
        nxt = words[i + 1].lower().strip(",.!?") if i + 1 < len(words) else ""
        joins = active and last in ("hundred", "scale")
        if lw in _UNITS or lw in _TEENS or lw in _TENS:
            v = _UNITS.get(lw, _TEENS.get(lw, _TENS.get(lw)))
            if joins or (active and last == "tens" and lw in _UNITS):
                cur += v  # "two hundred five", "twenty" + "five"
            else:
                flush()
                cur, active = v, True
            last = "tens" if lw in _TENS else "unit"
        elif lw == "hundred" and (active or (out and out[-1].lower() == "a")):
            if not active:
                out.pop()  # "a hundred"
            cur, active, last = (cur or 1) * 100, True, "hundred"
        elif lw in _SCALES and (active or (out and out[-1].lower() == "a")):
            if not active:
                out.pop()
            total, cur, active, last = total + (cur or 1) * _SCALES[lw], 0, True, "scale"
        elif lw == "and" and joins and (nxt in _UNITS or nxt in _TEENS or nxt in _TENS):
            continue  # "two hundred and five"
        else:
            flush()
            out.append(w)
    flush()
    return " ".join(out)


_ORDINALS = {w: i for i, w in enumerate(
    "zeroth first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth thirteenth fourteenth "
    "fifteenth sixteenth seventeenth eighteenth nineteenth twentieth".split())}
_ORDINALS["thirtieth"] = 30


def ordinal_numbers(text):
    """'twenty fifth' -> '25', 'third' -> '3', '25th' -> '25'."""
    def compound(m):
        return str({"twenty": 20, "thirty": 30}[m.group(1)] + _ORDINALS[m.group(2)])
    text = re.sub(r"\b(twenty|thirty)[\s-](" + "|".join(k for k in _ORDINALS if 0 < _ORDINALS[k] < 10) + r")\b", compound, text)
    text = re.sub(r"\b(" + "|".join(_ORDINALS) + r")\b", lambda m: str(_ORDINALS[m.group(1)]), text)
    return re.sub(r"\b(\d+)(st|nd|rd|th)\b", r"\1", text)


def _words(text):
    return {w for w in re.findall(r"[a-z]+", str(text).lower()) if len(w) > 2}


class Provider:
    # Built-ins that answer by rules set model_tool = False: they aren't offered to the model at all,
    # and direct() decides from the words alone whether a request is theirs.
    model_tool = True
    vocabulary = ""  # extra words speech recognition should know

    def __init__(self, cfg):
        self.cfg = cfg
        self.id = cfg["id"]

    def direct(self, text):
        """Arguments if this provider can answer the request by itself, without the model; else None."""
        return None

    def attach(self, providers):
        """Called after loading with all providers, for ones that use another's settings."""

    def tools(self):
        """[{"name", "description", "parameters"}]"""
        return []

    def call(self, tool, args):
        """Returns (text, is_error)."""
        raise NotImplementedError

    def keywords(self):
        """Words that mean a request is for this provider rather than conversation."""
        words = {w.strip().lower() for w in str(self.cfg.get("keywords", "")).split(",") if w.strip()}
        for t in self.tools():
            words |= {w for w in re.split(r"[_\W]+", t["name"].lower()) if len(w) > 3 and w not in STOPWORDS}
        return words

    def examples(self):
        """[[phrase, tool, args]] worked examples for the model."""
        ex = self.cfg.get("example", "").strip()
        tools = self.tools()
        return [[ex, tools[0]["name"], {}]] if ex and tools else []

    def reply(self, tool, args, text):
        """A spoken answer for a result, or None to let the chat model summarise it."""
        template = self.cfg.get("reply", "").strip()
        return template.replace("{result}", text.strip()) if template else None

    def guess_args(self, text):
        """Arguments that the user's own words make certain; they override the model's guesses."""
        return {}


# ---- weather ----

WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "foggy", 48: "foggy",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
       61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
       71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "rain showers", 81: "rain showers",
       82: "violent rain showers", 85: "snow showers", 86: "heavy snow showers", 95: "thunderstorms",
       96: "thunderstorms with hail", 99: "thunderstorms with hail"}


SNOW_CODES = {71, 73, 75, 77, 85, 86}
STORM_CODES = {95, 96, 99}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


class Weather(Provider):
    # What a question is about, checked in order; the first match decides the answer's focus.
    CONDITIONS = [
        ("storm", {"storm", "storms", "stormy", "thunder", "thunderstorm", "thunderstorms", "lightning"}),
        ("snow", {"snow", "snowing", "snowy", "snowfall", "sleet", "blizzard"}),
        ("rain", {"rain", "raining", "rainy", "umbrella", "wet", "shower", "showers", "drizzle", "precipitation"}),
        ("fog", {"fog", "foggy", "mist", "misty", "haze", "hazy"}),
        ("sun", {"sun", "sunny", "sunshine"}),
        ("cloud", {"cloud", "clouds", "cloudy", "overcast", "grey", "gray", "gloomy"}),
        ("wind", {"wind", "windy", "breezy", "breeze", "gusty", "gusts"}),
        ("humid", {"humid", "humidity", "muggy", "sticky"}),
        ("jacket", {"jacket", "coat", "sweater", "hoodie"}),
        # "cool" and "bright" are left out: "that's cool" and "make it bright" aren't weather questions.
        ("temp", {"hot", "cold", "warm", "chilly", "freezing", "temperature", "degrees"}),
    ]
    KEYWORDS = {"weather", "forecast", "outside", "weekend"} | {w for _, words in CONDITIONS for w in words}
    _geo = {}

    def tools(self):
        return [{"name": "get_weather",
                 "description": "Weather now and the forecast for the coming week: rain, snow, sun, clouds, wind, temperature.",
                 "parameters": {"type": "object", "properties": {
                     "location": {"type": "string", "description": "City, only if the user names one"},
                     "when": {"type": "string", "enum": ["now", "today", "tomorrow", "weekend", "week", *WEEKDAYS]}}}}]

    def keywords(self):
        return self.KEYWORDS | super().keywords()

    def examples(self):
        return [["what's the weather like", "get_weather", {"when": "now"}],
                ["will it rain on saturday", "get_weather", {"when": "saturday"}]]

    PLACE = re.compile(r"\b(?:in|for|at)\s+([a-z][a-z .'-]*?)\s*(?=\b(?:this|today|tomorrow|tonight|on|next|over|"
                       r"right|now|later|during)\b|[?.!,]|$)")

    def _place(self, text):
        """A place the user named ("weather in London this weekend"), or "" for the configured one."""
        m = self.PLACE.search(text.lower())
        place = m.group(1).strip() if m else ""
        if not place or place.startswith(("the ", "my ", "our ")) or place in {"here", "town", "the", "home", "general"}:
            return ""
        return place.title()

    def guess_args(self, text):
        """Day, place and condition come from the user's words, never the model's guesses
        (it copies "tomorrow" from its examples and has invented a city called "City")."""
        words = _words(text)
        day = next((d for d in WEEKDAYS if d in words), None)
        when = ("tomorrow" if "tomorrow" in words else "weekend" if "weekend" in words else day
                or ("week" if "week" in words else "today" if words & {"today", "tonight"} else "now"))
        args = {"when": when, "location": self._place(text)}
        about = next((name for name, ws in self.CONDITIONS if words & ws), None)
        if about:
            args["about"] = about
        return args

    def geocode(self, name):
        if name not in self._geo:
            r = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                             params={"name": name, "count": 1}, timeout=10).json()
            if not r.get("results"):
                raise RuntimeError(f"couldn't find a place called {name}")
            g = r["results"][0]
            self._geo[name] = (g["latitude"], g["longitude"], g["name"])
        return self._geo[name]

    def call(self, tool, args):
        place = (args.get("location") or "").strip() or self.cfg.get("location", "").strip()
        if not place:
            return "no location set; add one on the Tools page", True
        lat, lon, label = self.geocode(place)
        f = self.cfg.get("units", "celsius") == "fahrenheit"
        d = requests.get("https://api.open-meteo.com/v1/forecast", timeout=10, params={
            "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 8,
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,wind_gusts_10m,relative_humidity_2m",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code,"
                     "snowfall_sum,sunshine_duration,daylight_duration,wind_speed_10m_max,wind_gusts_10m_max",
            "temperature_unit": "fahrenheit" if f else "celsius", "wind_speed_unit": "mph" if f else "kmh",
            "precipitation_unit": "inch" if f else "mm",
        }).json()
        return json.dumps({"place": label, "when": args.get("when") or "now", "about": args.get("about", ""),
                           "fahrenheit": f, "current": d["current"], "daily": d["daily"]}), False

    # ---- phrasing ----

    @staticmethod
    def _days(d):
        """Indexes into the daily forecast that the question is about."""
        dates = [datetime.date.fromisoformat(t) for t in d["daily"]["time"]]
        when = d["when"]
        if when in ("now", "today"):
            return [0]
        if when == "tomorrow":
            return [1]
        if when == "week":
            return list(range(min(7, len(dates))))
        if when == "weekend":
            sat = next((i for i, x in enumerate(dates) if x.weekday() == 5), None)
            sun = next((i for i, x in enumerate(dates) if x.weekday() == 6), None)
            return [sun] if sun == 0 or sat is None else [sat, sat + 1] if sat + 1 < len(dates) else [sat]
        if when in WEEKDAYS:
            return [next(i for i, x in enumerate(dates) if x.weekday() == WEEKDAYS.index(when))]
        return [0]

    @staticmethod
    def _name(d, i):
        if i == 0:
            return "today"
        if i == 1:
            return "tomorrow"
        return datetime.date.fromisoformat(d["daily"]["time"][i]).strftime("%A")

    @staticmethod
    def _wind_word(speed, fahrenheit):
        kmh = speed * 1.609 if fahrenheit else speed
        return "calm" if kmh < 10 else "light" if kmh < 20 else "breezy" if kmh < 35 else "windy" if kmh < 50 else "very windy"

    @staticmethod
    def _sky(d, i):
        """The day's sky. The daily weather code is the worst hour of the day, so one cloudy hour
        makes it "overcast"; for dry days, judge by how much of the daylight was sunny instead."""
        day = d["daily"]
        code = day["weather_code"][i]
        if code > 3:
            return WMO.get(code, "mixed weather")
        ratio = (day["sunshine_duration"][i] or 0) / max(day["daylight_duration"][i] or 1, 1)
        return ("sunny" if ratio > 0.8 else "mostly sunny" if ratio > 0.6 else "partly cloudy" if ratio > 0.35
                else "mostly cloudy" if ratio > 0.15 else "overcast")

    def _clause(self, d, i, about):
        """A short answer about one day, e.g. 'a 40 percent chance of rain'."""
        day, f = d["daily"], d["fahrenheit"]
        hi, lo = round(day["temperature_2m_max"][i]), round(day["temperature_2m_min"][i])
        rain, code = day["precipitation_probability_max"][i] or 0, day["weather_code"][i]
        sky = self._sky(d, i)
        if about == "storm":
            return f"thunderstorms likely, {rain} percent chance of rain" if code in STORM_CODES else "no storms expected"
        if about == "snow":
            amount = day["snowfall_sum"][i] or 0  # cm, or inches in Fahrenheit mode
            if amount >= 0.1 or code in SNOW_CODES:
                return f"snow expected" + (f", about {amount:.1f} {'inches' if f else 'centimeters'}" if amount >= 0.1 else "")
            return "no snow expected" + (f", it'll be too warm with a low of {lo}" if lo > (34 if f else 1) else "")
        if about == "rain":
            if rain >= 60:
                return f"rain likely, a {rain} percent chance"
            if rain >= 30:
                return f"maybe some rain, a {rain} percent chance"
            return f"no rain expected, a {rain} percent chance"
        if about == "fog":
            return "foggy" if code in (45, 48) else f"no fog, {sky}"
        if about in ("sun", "cloud"):
            hours = round((day["sunshine_duration"][i] or 0) / 3600)
            return f"{sky}, about {hours} hour{'s' if hours != 1 else ''} of sunshine"
        if about == "wind":
            w, g = round(day["wind_speed_10m_max"][i]), round(day["wind_gusts_10m_max"][i])
            unit = "miles per hour" if f else "kilometers per hour"
            word = self._wind_word(w, f)
            return (f"{'light winds' if word == 'light' else word}, up to {w} {unit}"
                    + (f", gusting to {g}" if g >= w + 8 else ""))
        if about == "jacket":
            if lo < (50 if f else 10):
                return f"bring a jacket, it gets down to {lo}"
            if hi < (62 if f else 17):
                return f"a light jacket would help, the high is only {hi}"
            return f"no jacket needed, {lo} to {hi} degrees"
        # temp, humid (no daily humidity) and a general forecast
        rain_note = f", a {rain} percent chance of rain" if rain >= 20 else ""
        return f"{sky}, a high of {hi} and a low of {lo}{rain_note}"

    def _now(self, d, about):
        """Answers about right now, from current conditions, or None to use today's forecast."""
        cur, place, f = d["current"], d["place"], d["fahrenheit"]
        code, t = cur["weather_code"], round(cur["temperature_2m"])
        sky = WMO.get(code, "mixed")
        if about in ("rain", "snow", "storm") and code >= 51:
            return f"Yes, it's {sky} in {place} right now."
        if about == "fog" and code in (45, 48):
            return f"Yes, it's foggy in {place} right now."
        if about in ("sun", "cloud"):
            return f"Right now in {place} it's {sky}. Today: {self._clause(d, 0, about)}."
        if about == "wind":
            w, g = round(cur["wind_speed_10m"]), round(cur["wind_gusts_10m"])
            unit = "miles per hour" if f else "kilometers per hour"
            word = self._wind_word(w, f)
            start = f"Winds are light in {place} right now" if word == "light" else f"It's {word} in {place} right now"
            return f"{start}, around {w} {unit}" + (f", gusting to {g}." if g >= w + 8 else ".")
        if about == "humid":
            h = round(cur["relative_humidity_2m"])
            feel = ", so it feels muggy" if h >= 70 and t >= (72 if f else 22) else ""
            return f"Humidity in {place} is {h} percent right now{feel}."
        if about in ("temp", None, ""):
            a = round(cur["apparent_temperature"])
            feels = f", feeling like {a}" if abs(a - t) >= 3 else ""
            hi, lo = round(d["daily"]["temperature_2m_max"][0]), round(d["daily"]["temperature_2m_min"][0])
            rain = d["daily"]["precipitation_probability_max"][0] or 0
            return (f"Right now in {place} it's {t} degrees and {sky}{feels}. Today's high is {hi} and the low is {lo}"
                    + (f", with a {rain} percent chance of rain." if rain >= 20 else "."))
        return None

    def reply(self, tool, args, text):
        d = json.loads(text)
        about, place = d.get("about") or None, d["place"]
        if d["when"] == "now":
            answer = self._now(d, about)
            if answer:
                return answer
        days = self._days(d)
        if about == "humid" and days != [0]:
            return f"I only have the current humidity: {self._now(d, 'humid')}"
        if len(days) == 1:
            name = self._name(d, days[0])
            return f"{name[0].upper() + name[1:]} in {place}: {self._clause(d, days[0], about)}."
        label = "this weekend" if d["when"] == "weekend" else "this week"
        if about in ("storm", "snow", "rain", "fog"):
            # Yes/no conditions: name only the days it applies to.
            noun = {"storm": "storms", "snow": "snow", "rain": "rain", "fog": "fog"}[about]
            hits = [(i, self._clause(d, i, about)) for i in days]
            hits = [(i, c) for i, c in hits if not c.startswith("no ")]
            if not hits:
                return f"No {noun} expected {label} in {place}."
            parts = [f"{self._name(d, i).capitalize()}, {c}" for i, c in hits]
            rest = f" No {noun} the other days." if len(hits) < len(days) else ""
            return f"{label.capitalize()} in {place}: " + "; ".join(parts) + "." + rest
        if about in (None, "temp"):
            # Several days: keep each one short enough to listen to.
            day = d["daily"]
            parts = [f"{self._name(d, i).capitalize()}, {self._sky(d, i)}, {round(day['temperature_2m_max'][i])} and "
                     f"{round(day['temperature_2m_min'][i])}" + (f", {day['precipitation_probability_max'][i]} percent chance of rain"
                     if (day["precipitation_probability_max"][i] or 0) >= 30 else "") for i in days]
        else:
            parts = [f"{self._name(d, i).capitalize()}, {self._clause(d, i, about)}" for i in days]
        return f"{label.capitalize()} in {place}: " + "; ".join(parts) + "."


# ---- MCP servers ----

class McpClient:
    """Minimal MCP client: JSON-RPC over streamable HTTP (JSON or SSE replies) or stdio."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self._id = 0
        self.proc = None
        self.session = None
        self.http = requests.Session()
        self.http.headers.update({"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
        self.http.headers.update(cfg.get("headers") or {})
        self.ready = False

    def _send_http(self, msg, expect_reply):
        headers = {"Mcp-Session-Id": self.session} if self.session else {}
        r = self.http.post(self.cfg["url"], json=msg, headers=headers, timeout=30, stream=True)
        r.raise_for_status()
        self.session = r.headers.get("Mcp-Session-Id", self.session)
        if not expect_reply:
            r.close()
            return None
        if "text/event-stream" in r.headers.get("Content-Type", ""):
            for line in r.iter_lines(decode_unicode=True):
                if line and line.startswith("data:"):
                    data = json.loads(line[5:].strip())
                    if data.get("id") == msg["id"]:
                        r.close()
                        return data
            raise RuntimeError("server closed the stream without a reply")
        return r.json()

    def _send_stdio(self, msg, expect_reply):
        if not self.proc or self.proc.poll() is not None:
            self.proc = subprocess.Popen(shlex.split(self.cfg["command"]), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, text=True, bufsize=1)
            self.ready = False
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        if not expect_reply:
            return None
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("MCP server exited")
            try:
                data = json.loads(line)
            except ValueError:
                continue  # servers sometimes log to stdout
            if data.get("id") == msg["id"]:
                return data

    def _rpc(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            self._id += 1
            msg["id"] = self._id
        send = self._send_stdio if self.cfg.get("transport") == "stdio" else self._send_http
        data = send(msg, not notify)
        if data and "error" in data:
            raise RuntimeError(data["error"].get("message", data["error"]))
        return data.get("result") if data else None

    def request(self, method, params=None):
        with self.lock:
            if not self.ready or (self.proc and self.proc.poll() is not None):
                self._rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                         "clientInfo": {"name": "assistant", "version": "1"}})
                self._rpc("notifications/initialized", notify=True)
                self.ready = True
            return self._rpc(method, params)

    def close(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()


def mcp_text(result):
    return "\n".join(c.get("text", "") for c in (result or {}).get("content", []) if c.get("type", "text") == "text")


class Mcp(Provider):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.client = McpClient(cfg)
        self._tools = None

    def tools(self):
        if self._tools is None:
            allow = set(self.cfg.get("tools") or [])
            self._tools = [{"name": t["name"], "description": t.get("description", ""),
                            "parameters": t.get("inputSchema") or {"type": "object", "properties": {}}}
                           for t in self.client.request("tools/list").get("tools", [])
                           if not allow or t["name"] in allow]
        return self._tools

    def all_tool_names(self):
        return [t["name"] for t in self.client.request("tools/list").get("tools", [])]

    def call(self, tool, args):
        res = self.client.request("tools/call", {"name": tool, "arguments": args})
        return mcp_text(res)[:2000], bool(res.get("isError"))

    def close(self):
        self.client.close()


# ---- one-tool providers you describe yourself ----

class _Described(Provider):
    """Shared by http and command: one tool with named string parameters."""

    def tools(self):
        params = self.cfg.get("params") or []
        return [{"name": self.cfg["name"], "description": self.cfg.get("description", ""),
                 "parameters": {"type": "object",
                                "properties": {p["name"]: {"type": "string", "description": p.get("description", "")} for p in params},
                                "required": [p["name"] for p in params if p.get("required")]}}]

    def _values(self, args):
        return {p["name"]: str(args.get(p["name"], p.get("default", ""))) for p in self.cfg.get("params") or []}


class Http(_Described):
    def call(self, tool, args):
        v = self._values(args)
        url = self.cfg["url"]
        for k, val in v.items():
            url = url.replace("{" + k + "}", urllib.parse.quote(val))
        body = self.cfg.get("body") or None
        if body:
            for k, val in v.items():
                body = body.replace("{" + k + "}", json.dumps(val)[1:-1])  # JSON-escaped, quotes supplied by the template
        r = requests.request(self.cfg.get("method", "GET"), url, data=body, headers=self.cfg.get("headers") or {}, timeout=20)
        return r.text[:2000], not r.ok


class Command(_Described):
    def call(self, tool, args):
        cmd = self.cfg["command"]
        for k, val in self._values(args).items():
            cmd = cmd.replace("{" + k + "}", shlex.quote(val))
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        out = (p.stdout or p.stderr).strip()
        return out[:2000], p.returncode != 0


# ---- calculator ----

class Calculator(Provider):
    """Exact arithmetic, percentages, powers, roots and unit conversions. Small language models guess
    at maths, so these are answered by rules, never by the model."""
    model_tool = False
    vocabulary = ("plus minus times multiplied divided by over percent of tip square cube root squared cubed power "
                  "point calculate compute convert half double fahrenheit celsius kelvin miles kilometers meters feet "
                  "inches centimeters yards pounds kilograms ounces grams cups liters milliliters gallons quarts pints "
                  "tablespoons teaspoons how many much")
    FILLER = {"what", "whats", "what's", "is", "are", "calculate", "compute", "how", "much", "equals", "equal",
              "please", "the", "answer", "hey", "computer", "can", "you", "tell", "me", "work", "out", "a", "an"}
    # Unit -> (kind, size in a base unit). Temperatures are handled separately.
    UNITS = {}
    for names, kind, size in [
        ("mile miles mi", "length", 1609.344), ("kilometer kilometers kilometre kilometres km kms", "length", 1000),
        ("meter meters metre metres m", "length", 1), ("centimeter centimeters centimetre centimetres cm", "length", 0.01),
        ("millimeter millimeters millimetre millimetres mm", "length", 0.001), ("inch inches", "length", 0.0254),
        ("foot feet ft", "length", 0.3048), ("yard yards yd", "length", 0.9144),
        ("pound pounds lb lbs", "mass", 0.45359237), ("ounce ounces oz", "mass", 0.028349523125),
        ("kilogram kilograms kilo kilos kg", "mass", 1), ("gram grams g", "mass", 0.001),
        ("cup cups", "volume", 0.2365882365), ("liter liters litre litres l", "volume", 1),
        ("milliliter milliliters millilitre millilitres ml", "volume", 0.001), ("gallon gallons", "volume", 3.785411784),
        ("quart quarts", "volume", 0.946352946), ("pint pints", "volume", 0.473176473),
        ("tablespoon tablespoons tbsp", "volume", 0.01478676478125), ("teaspoon teaspoons tsp", "volume", 0.00492892159375),
    ]:
        for n in names.split():
            UNITS[n] = (kind, size, names.split()[1] if len(names.split()) > 1 else n)
    TEMPS = {"fahrenheit": "F", "f": "F", "celsius": "C", "centigrade": "C", "c": "C", "kelvin": "K", "k": "K"}

    def tools(self):
        return [{"name": "calculate", "description": "Arithmetic, percentages and unit conversions", "parameters": {}}]

    @staticmethod
    def _prepare(text):
        t = ordinal_numbers(spoken_numbers(text.lower().replace(",", "")))
        t = re.sub(r"(\d)\s+point\s+(\d+)", r"\1.\2", t)  # "3 point 5"
        return re.sub(r"[?!]+|\.(?!\d)", " ", t).strip()

    def _unit(self, word):
        w = word.strip().removeprefix("degrees ").removeprefix("a ").removeprefix("an ").strip()
        if w in self.TEMPS:
            return ("temp", self.TEMPS[w], w)
        return self.UNITS.get(w) or self.UNITS.get(w.replace(" ", ""))

    def _conversion(self, t):
        m = (re.search(r"(-?\d+(?:\.\d+)?)\s*(?:degrees\s+)?([a-z ]+?)\s+(?:is |are )?(?:in|to|into|as)\s+(?:degrees\s+)?([a-z]+)$", t)
             or None)
        if m:
            amount, src, dst = float(m.group(1)), m.group(2).split()[-1], m.group(3)
        else:
            m = re.search(r"how many ([a-z]+) (?:are |is )?(?:in|to) (?:a |an |one )?(-?\d+(?:\.\d+)?)?\s*([a-z]+)$", t)
            if not m:
                return None
            dst, amount, src = m.group(1), float(m.group(2) or 1), m.group(3)
        a, b = self._unit(src), self._unit(dst)
        if not a or not b or a[0] != b[0]:
            return None
        return {"convert": [amount, src, dst]}

    def _expression(self, t):
        s = f" {t} "
        rules = [
            (r"\bwhat(?:'s| is)? (\d+(?:\.\d+)?) percent of (\d+(?:\.\d+)?)", r" (\1/100*\2) "),
            (r"\b(\d+(?:\.\d+)?)\s*(?:percent|%)\s+(?:tip\s+)?(?:of|on)\s+", r" \1/100* "),
            (r"\b(?:the )?square root of\b", " sqrt "), (r"\b(?:the )?cube root of\b", " cbrt "),
            (r"\b(?:the )?sum of (\S+) and (\S+)", r" \1 + \2 "), (r"\b(?:the )?product of (\S+) and (\S+)", r" \1 * \2 "),
            (r"\badd (\S+) (?:and|to) (\S+)", r" \1 + \2 "), (r"\bsubtract (\S+) from (\S+)", r" \2 - \1 "),
            (r"\bhalf of\b", " 0.5 * "), (r"\bdouble\b", " 2 * "), (r"\btwice\b", " 2 * "),
            (r"\bsquared\b", " ** 2 "), (r"\bcubed\b", " ** 3 "),
            (r"\b(?:to the power of|to the|raised to(?: the power of)?)\b|\^", " ** "),
            (r"\bmultiplied by\b|\btimes\b|×|(?<=\d)\s*x\s*(?=\d)", " * "),
            (r"\bdivided by\b|\bover\b|÷", " / "), (r"\bplus\b", " + "), (r"\bminus\b", " - "),
            (r"\bpercent\b|%", " / 100 "),
        ]
        for pat, rep in rules:
            s = re.sub(pat, rep, s)
        s = re.sub(r"\b(sqrt|cbrt)\s+(\d+(?:\.\d+)?)", r"\1(\2)", s)
        words = [w for w in re.findall(r"[a-z']+", s) if w not in ("sqrt", "cbrt")]
        if any(w not in self.FILLER for w in words):
            return None  # other words: not (only) a sum
        expr = " ".join(w for w in re.findall(r"\d+(?:\.\d+)?|\*\*|[-+*/()]|sqrt|cbrt", s))
        if not re.search(r"\d", expr) or not re.search(r"[-+*/]|sqrt|cbrt", expr):
            return None
        return {"expression": expr}

    def direct(self, text):
        t = self._prepare(text)
        conversion = self._conversion(t)  # "how many cups in a liter" has no digits
        if conversion or not re.search(r"\d", t):
            return conversion
        return self._expression(t)

    def call(self, tool, args):
        import ast
        import math
        if "convert" in args:
            amount, src, dst = args["convert"]
            a, b = self._unit(src), self._unit(dst)
            if a[0] == "temp":
                c = {"C": amount, "F": (amount - 32) * 5 / 9, "K": amount - 273.15}[a[1]]
                value = {"C": c, "F": c * 9 / 5 + 32, "K": c + 273.15}[b[1]]
            else:
                value = amount * a[1] / b[1]
            return json.dumps({"convert": [amount, src, dst], "value": value}), False

        def ev(node):
            if isinstance(node, ast.Expression):
                return ev(node.body)
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
                return node.value
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
                return -ev(node.operand) if isinstance(node.op, ast.USub) else ev(node.operand)
            if isinstance(node, ast.BinOp):
                a, b = ev(node.left), ev(node.right)
                if isinstance(node.op, ast.Pow) and (abs(b) > 100 or abs(a) > 1e6):
                    raise ValueError("that number is too big")
                ops = {ast.Add: lambda: a + b, ast.Sub: lambda: a - b, ast.Mult: lambda: a * b,
                       ast.Div: lambda: a / b, ast.Pow: lambda: a ** b}
                return ops[type(node.op)]()
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") in ("sqrt", "cbrt") and len(node.args) == 1:
                x = ev(node.args[0])
                return math.sqrt(x) if node.func.id == "sqrt" else math.copysign(abs(x) ** (1 / 3), x)
            raise ValueError("I can only do arithmetic")
        try:
            value = ev(ast.parse(args["expression"], mode="eval"))
        except ZeroDivisionError:
            return "You can't divide by zero.", True
        except (ValueError, SyntaxError, TypeError, KeyError) as e:
            return f"I couldn't work that out: {e}", True
        return json.dumps({"expression": args["expression"], "value": value}), False

    @staticmethod
    def number(v):
        if isinstance(v, float) and v.is_integer() and abs(v) < 1e15:
            v = int(v)
        if isinstance(v, int):
            return f"{v:,}"
        return f"{v:,.4f}".rstrip("0").rstrip(".")

    @staticmethod
    def spoken(expr):
        s = re.sub(r"\(?(\d+(?:\.\d+)?)/100\*(\d+(?:\.\d+)?)\)?", r"\1 percent of \2", expr.replace(" ", ""))
        s = re.sub(r"sqrt\((\d+(?:\.\d+)?)\)", r"the square root of \1", s)
        s = re.sub(r"cbrt\((\d+(?:\.\d+)?)\)", r"the cube root of \1", s)
        s = re.sub(r"\*\*2\b", " squared", s)
        s = re.sub(r"\*\*3\b", " cubed", s)
        for sym, word in (("**", " to the power of "), ("*", " times "), ("/", " divided by "), ("+", " plus "), ("-", " minus ")):
            s = s.replace(sym, word)
        return re.sub(r"\s+", " ", s.replace("(", "").replace(")", "")).strip()

    def reply(self, tool, args, text):
        d = json.loads(text)
        if "convert" in d:
            amount, src, dst = d["convert"]
            unit = lambda w, n: (w if w in self.TEMPS else self._unit(w)[2] if n != 1 else w)
            value = round(d["value"], 2) if abs(d["value"]) < 1000 else round(d["value"])
            return f"{self.number(amount)} {src} is {self.number(value)} {unit(dst, value)}."
        return f"{self.spoken(d['expression'])} is {self.number(d['value'])}."


# ---- clock ----

class Clock(Provider):
    """Time, date, day of the week, time elsewhere, and days until a date. Read from the clock,
    never guessed by the model."""
    model_tool = False
    vocabulary = ("what time date day today week until till days weeks christmas eve halloween thanksgiving easter "
                  "valentines new year independence fourth july birthday january february march april may june august "
                  "september october november december monday tuesday wednesday thursday friday saturday sunday weekend")
    LEAD = r"^(?:(?:hey|ok|okay)\s+)?(?:(?:computer|hal|assistant)\s+)?(?:(?:can|could) you\s+)?(?:please\s+)?(?:tell me\s+)?"
    MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
    WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    _tz_cache = {}

    def tools(self):
        return [{"name": "clock", "description": "Time, date and days until a date", "parameters": {}}]

    def attach(self, providers):
        self.weather = next((p for p in providers.values() if isinstance(p, Weather)), None)

    def _zone(self, place=None):
        """A ZoneInfo for a named place, the configured zone, or the weather location's zone."""
        from zoneinfo import ZoneInfo, available_timezones
        if place:
            key = place.strip().title().replace(" ", "_")
            match = next((z for z in sorted(available_timezones()) if z.endswith("/" + key)), None)
            if match:
                return ZoneInfo(match), place.strip().title()
            if place not in self._tz_cache:  # any town: ask Open-Meteo's geocoder for its time zone
                r = requests.get("https://geocoding-api.open-meteo.com/v1/search", params={"name": place, "count": 1}, timeout=5).json()
                g = (r.get("results") or [None])[0]
                self._tz_cache[place] = (g["timezone"], g["name"]) if g and g.get("timezone") else None
            hit = self._tz_cache[place]
            return (ZoneInfo(hit[0]), hit[1]) if hit else (None, place)
        tz = self.cfg.get("timezone", "").strip()
        if not tz and getattr(self, "weather", None) and self.weather.cfg.get("location"):
            try:
                loc = self.weather.cfg["location"]
                if loc not in self._tz_cache:
                    r = requests.get("https://geocoding-api.open-meteo.com/v1/search", params={"name": loc, "count": 1}, timeout=5).json()
                    g = (r.get("results") or [None])[0]
                    self._tz_cache[loc] = (g["timezone"], g["name"]) if g and g.get("timezone") else None
                tz = (self._tz_cache[loc] or ("", ""))[0]
            except requests.RequestException:
                tz = ""
        return (ZoneInfo(tz) if tz else None), None

    def direct(self, text):
        t = re.sub(r"[?!.,]", " ", text.lower().replace("’", "'"))
        t = re.sub(self.LEAD, "", re.sub(r"\s+", " ", t).strip())
        m = re.fullmatch(r"(?:what time is it|what's the time|what is the time|whats the time|the time|time) in ([a-z .'-]+?)(?: right now| now)?", t)
        if m:
            return {"what": "time", "place": m.group(1)}
        if re.fullmatch(r"(?:what time is it|what's the time|what is the time|whats the time|the time|time|do you have the time|what time do you have)(?: right)?(?: now)?", t):
            return {"what": "time"}
        if re.fullmatch(r"(?:what's|what is|whats) (?:the date|today's date|todays date|the date today|today)(?: today)?|what date is it(?: today)?|the date|date", t):
            return {"what": "date"}
        if re.fullmatch(r"what day (?:is it|is today|of the week is it|of the week is today)(?: today)?|what's the day|what is the day", t):
            return {"what": "day"}
        m = re.fullmatch(r"how (?:many|long) (days|weeks|sleeps)? ?(?:is it )?(?:until|till|til|before|to) (.+?)(?: is| day)?", t)
        if m:
            return {"what": "until", "target": m.group(2), "unit": m.group(1) or "days"}
        return None

    def _target_date(self, target, today):
        import datetime as dt
        t = ordinal_numbers(spoken_numbers(target.lower())).replace("'", "").strip()
        t = re.sub(r"^(the|this|next|my) ", "", t)
        y = today.year

        def upcoming(month, day):
            d = dt.date(y, month, day)
            return d if d >= today else dt.date(y + 1, month, day)

        def nth_weekday(year, month, weekday, n):
            d = dt.date(year, month, 1)
            return d + dt.timedelta(days=(weekday - d.weekday()) % 7 + 7 * (n - 1))

        def easter(year):  # Gregorian Easter (Meeus/Jones/Butcher)
            a, b, c = year % 19, year // 100, year % 100
            d, e = b // 4, b % 4
            f = (b + 8) // 25
            g = (b - f + 1) // 3
            h = (19 * a + b - d - g + 15) % 30
            i, k = c // 4, c % 4
            l = (32 + 2 * e + 2 * i - h - k) % 7
            m = (a + 11 * h + 22 * l) // 451
            n = h + l - 7 * m + 114
            return dt.date(year, n // 31, n % 31 + 1)

        holidays = {
            "christmas eve": lambda: upcoming(12, 24), "christmas": lambda: upcoming(12, 25),
            "new years eve": lambda: upcoming(12, 31), "new years day": lambda: upcoming(1, 1), "new years": lambda: upcoming(1, 1),
            "new year": lambda: upcoming(1, 1), "halloween": lambda: upcoming(10, 31), "valentines day": lambda: upcoming(2, 14),
            "valentines": lambda: upcoming(2, 14), "independence day": lambda: upcoming(7, 4), "4 of july": lambda: upcoming(7, 4),
            "july 4": lambda: upcoming(7, 4),
            "thanksgiving": lambda: next(d for d in (nth_weekday(y, 11, 3, 4), nth_weekday(y + 1, 11, 3, 4)) if d >= today),
            "easter": lambda: next(d for d in (easter(y), easter(y + 1)) if d >= today),
        }
        for name, fn in holidays.items():
            if t == name or t == name + " day":
                return fn(), name.title().replace("Years", "Year's").replace("Valentines", "Valentine's").replace("4 Of July", "the 4th of July")
        if t in ("the weekend", "weekend"):
            t = "saturday"
        if t in self.WEEKDAYS:
            ahead = (self.WEEKDAYS.index(t) - today.weekday()) % 7 or 7
            return today + dt.timedelta(days=ahead), t.title()
        m = re.fullmatch(r"([a-z]+) (\d{1,2})|(\d{1,2}) (?:of )?([a-z]+)", t)
        if m:
            month, day = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
            if month in self.MONTHS:
                try:
                    return upcoming(self.MONTHS.index(month) + 1, int(day)), f"{month.title()} {int(day)}"
                except ValueError:
                    return None, None
        return None, None

    def call(self, tool, args):
        import datetime as dt
        zone, label = self._zone(args.get("place"))
        if args.get("place") and zone is None:
            return f"I don't know the time zone for {args['place']}.", True
        now = dt.datetime.now(zone) if zone else dt.datetime.now().astimezone()
        out = {"what": args["what"], "now": now.isoformat(), "place": label}
        if args["what"] == "until":
            date, name = self._target_date(args["target"], now.date())
            if not date:
                return f"I don't know when {args['target']} is.", True
            out.update(days=(date - now.date()).days, date=date.isoformat(), name=name, unit=args.get("unit", "days"))
        return json.dumps(out), False

    def reply(self, tool, args, text):
        import datetime as dt
        d = json.loads(text)
        now = dt.datetime.fromisoformat(d["now"])
        clock = now.strftime("%I:%M %p").lstrip("0")
        if d["what"] == "time":
            return f"It's {clock} in {d['place']}." if d.get("place") else f"It's {clock}."
        if d["what"] == "date":
            return f"It's {now.strftime('%A, %B')} {now.day}, {now.year}."
        if d["what"] == "day":
            return f"It's {now.strftime('%A')}."
        when = dt.date.fromisoformat(d["date"])
        days, name = d["days"], d["name"]
        on = f"{when.strftime('%A, %B')} {when.day}"
        if days == 0:
            return f"{name} is today!"
        if days == 1:
            return f"{name} is tomorrow, {on}." if name.lower() not in self.WEEKDAYS else f"{name} is tomorrow."
        plural = lambda n, w: f"{n} {w}{'' if n == 1 else 's'}"
        if d["unit"] == "weeks":
            weeks = plural(days // 7, "week") + (f" and {plural(days % 7, 'day')}" if days % 7 else "")
            return f"{weeks} until {name}, on {on}."
        return f"{days} days until {name}, on {on}."


KINDS = {"weather": Weather, "mcp": Mcp, "http": Http, "command": Command, "calculator": Calculator, "clock": Clock}


def make(cfg):
    return KINDS[cfg["type"]](cfg)
