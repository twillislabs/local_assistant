"""Tool providers the assistant can use besides Home Assistant.

Each provider is configured on the Tools page and stored in config["tools"]:
  weather  - current conditions and tomorrow's forecast from Open-Meteo (no API key)
  mcp      - any MCP server, over HTTP (streamable HTTP / SSE replies) or a local command (stdio)
  http     - one tool that calls an HTTP API you describe
  command  - one tool that runs a script or command you describe

A provider exposes tools (name, description, JSON-schema parameters), runs them, and
optionally phrases the result itself; otherwise the chat model summarises it.
"""
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


class Weather(Provider):
    KEYWORDS = {"weather", "temperature", "forecast", "rain", "raining", "snow", "snowing", "sunny", "cloudy",
                "outside", "umbrella", "wind", "windy", "humid", "humidity", "hot", "cold", "warm", "degrees"}
    _geo = {}

    def tools(self):
        return [{"name": "get_weather",
                 "description": "Current weather and forecast. Use for any question about weather, temperature or rain.",
                 "parameters": {"type": "object", "properties": {
                     "location": {"type": "string", "description": "City, only if the user names one"},
                     "when": {"type": "string", "enum": ["now", "today", "tomorrow"]}}}}]

    def keywords(self):
        return self.KEYWORDS | super().keywords()

    def examples(self):
        return [["what's the weather like", "get_weather", {"when": "now"}],
                ["will it rain tomorrow", "get_weather", {"when": "tomorrow"}]]

    def guess_args(self, text):
        words = _words(text)
        if "tomorrow" in words:
            return {"when": "tomorrow"}
        if words & {"today", "tonight"}:
            return {"when": "today"}
        return {}

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
            "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 2,
            "current": "temperature_2m,weather_code,wind_speed_10m,relative_humidity_2m",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
            "temperature_unit": "fahrenheit" if f else "celsius", "wind_speed_unit": "mph" if f else "kmh",
        }).json()
        return json.dumps({"place": label, "when": args.get("when") or "now", "current": d["current"], "daily": d["daily"]}), False

    def reply(self, tool, args, text):
        d = json.loads(text)
        cur, day = d["current"], d["daily"]
        i = 1 if d["when"] == "tomorrow" else 0
        hi, lo = round(day["temperature_2m_max"][i]), round(day["temperature_2m_min"][i])
        rain = day["precipitation_probability_max"][i] or 0
        sky = WMO.get(day["weather_code"][i], "mixed weather")
        if i == 1:
            return f"Tomorrow in {d['place']}: {sky}, a high of {hi} and a low of {lo}, with a {rain} percent chance of rain."
        now = f"Right now in {d['place']} it's {round(cur['temperature_2m'])} degrees and {WMO.get(cur['weather_code'], 'mixed')}."
        return f"{now} Today's high is {hi} and the low is {lo}" + (f", with a {rain} percent chance of rain." if rain >= 20 else ".")


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
