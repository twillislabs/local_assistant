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


def _words(text):
    return {w for w in re.findall(r"[a-z]+", str(text).lower()) if len(w) > 2}


class Provider:
    def __init__(self, cfg):
        self.cfg = cfg
        self.id = cfg["id"]

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


KINDS = {"weather": Weather, "mcp": Mcp, "http": Http, "command": Command}


def make(cfg):
    return KINDS[cfg["type"]](cfg)
