"""Wi-Fi, IP and setup-hotspot management through NetworkManager (nmcli).

The arduino user is allowed to change NetworkManager settings via polkit, so none of
this needs root. Slow operations run on a background thread and report through `job`.
"""
import ipaddress
import subprocess
import threading
import time

IFACE = "wlan0"
HOTSPOT = "assistant-hotspot"


class NmError(Exception):
    pass


def nm(*args, wait=None, timeout=60):
    cmd = ["nmcli"] + (["-w", str(wait)] if wait else []) + list(args)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise NmError((p.stderr or p.stdout).strip().replace("Error: ", ""))
    return p.stdout


def split_terse(line):
    """Split one line of nmcli -t output on unescaped colons."""
    out, cur, i = [], "", 0
    while i < len(line):
        if line[i] == "\\" and i + 1 < len(line):
            cur += line[i + 1]
            i += 2
            continue
        if line[i] == ":":
            out.append(cur)
            cur = ""
        else:
            cur += line[i]
        i += 1
    return out + [cur]


def props(*args):
    """nmcli -t key:value output (con show NAME / dev show) as a dict; repeated keys like IP4.DNS[1] are joined."""
    out = {}
    for line in nm("-t", *args).splitlines():
        key, _, val = line.partition(":")
        val = val.replace("\\:", ":").replace("\\\\", "\\")
        key = key.split("[")[0]
        out[key] = f"{out[key]}, {val}" if key in out and val else (val or out.get(key, ""))
    return out


class NetManager:
    def __init__(self, config):
        self.config = config
        self.op = threading.Lock()  # one network change at a time, including the watchdog's
        self.job = {"state": "idle", "message": ""}
        self.last_hotspot_request = 0  # set by the web server when a hotspot client is using the UI
        self.offline_since = None
        self.next_retry = 0

    # ---- reading ----

    def status(self):
        d = props("-f", "GENERAL.STATE,GENERAL.CONNECTION,IP4.ADDRESS,IP4.GATEWAY,IP4.DNS", "dev", "show", IFACE)
        conn = d.get("GENERAL.CONNECTION", "")
        st = {
            "connected": d.get("GENERAL.STATE", "").startswith("100") and conn != HOTSPOT,
            "hotspot": conn == HOTSPOT,
            "connection": conn if conn and conn != "--" else "",
            "state": d.get("GENERAL.STATE", ""),
            "address": d.get("IP4.ADDRESS", ""),
            "gateway": d.get("IP4.GATEWAY", ""),
            "dns": d.get("IP4.DNS", ""),
            "job": self.job,
        }
        if st["connection"] and not st["hotspot"]:
            st["ipv4"] = self.ipv4_of(st["connection"])
        return st

    def scan(self, rescan=True):
        nets = {}
        out = nm("-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "dev", "wifi", "list", "--rescan", "yes" if rescan else "auto", timeout=40)
        for line in out.splitlines():
            in_use, ssid, signal, security = (split_terse(line) + ["", "", "", ""])[:4]
            if not ssid:
                continue
            sig, using = int(signal or 0), in_use == "*"
            if ssid not in nets or sig > nets[ssid]["signal"]:
                using = using or nets.get(ssid, {}).get("in_use", False)
                nets[ssid] = {"ssid": ssid, "signal": sig, "security": security, "in_use": using}
            elif using:
                nets[ssid]["in_use"] = True
        return sorted(nets.values(), key=lambda n: -n["signal"])

    def saved(self):
        out = []
        for line in nm("-t", "-f", "NAME,TYPE,AUTOCONNECT-PRIORITY", "con", "show").splitlines():
            name, typ, prio = (split_terse(line) + ["", "", "0"])[:3]
            if typ == "802-11-wireless" and name != HOTSPOT:
                p = props("-f", "802-11-wireless.ssid", "con", "show", name)
                out.append({"name": name, "ssid": p.get("802-11-wireless.ssid", name), "priority": int(prio or 0), "ipv4": self.ipv4_of(name)})
        return out

    def ipv4_of(self, name):
        p = props("-f", "ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.dns", "con", "show", name)
        return {"method": p.get("ipv4.method", "auto"), "address": p.get("ipv4.addresses", ""),
                "gateway": p.get("ipv4.gateway", ""), "dns": p.get("ipv4.dns", "")}

    # ---- background jobs ----

    def _run(self, title, fn, *args):
        if self.job["state"] == "running":
            raise NmError(f"busy: {self.job['message']}")

        def go():
            with self.op:
                self.job = {"state": "running", "message": title}
                try:
                    msg = fn(*args)
                    self.job = {"state": "ok", "message": msg, "at": time.time()}
                except Exception as e:
                    self.job = {"state": "error", "message": f"{title} failed: {e}", "at": time.time()}

        threading.Thread(target=go, daemon=True).start()
        return {"started": title}

    def connect(self, ssid, password="", hidden=False, ipv4=None):
        return self._run(f"Connecting to {ssid}", self._connect, ssid, password, hidden, ipv4)

    def set_ipv4(self, name, ipv4):
        return self._run(f"Applying IP settings to {name}", self._set_ipv4, name, ipv4)

    def forget(self, name):
        nm("con", "delete", name)

    def hotspot_now(self, on):
        return self._run("Starting hotspot" if on else "Stopping hotspot", self._start_hotspot if on else self._leave_hotspot)

    # ---- the actual work (always called holding self.op) ----

    @staticmethod
    def _ipv4_args(ipv4):
        if not ipv4:
            return []
        if ipv4.get("method") == "manual":
            addr = ipv4["address"].strip()
            if "/" not in addr:
                addr += "/24"
            ipaddress.ip_interface(addr)  # validates
            gw = ipv4.get("gateway", "").strip()
            if gw:
                ipaddress.ip_address(gw)
            dns = ",".join(d.strip() for d in ipv4.get("dns", "").replace(" ", ",").split(",") if d.strip())
            return ["ipv4.method", "manual", "ipv4.addresses", addr, "ipv4.gateway", gw, "ipv4.dns", dns]
        dns = ",".join(d.strip() for d in ipv4.get("dns", "").replace(" ", ",").split(",") if d.strip())
        return ["ipv4.method", "auto", "ipv4.addresses", "", "ipv4.gateway", "", "ipv4.dns", dns]

    def _connect(self, ssid, password, hidden, ipv4):
        before = self.status()
        existing = next((s["name"] for s in self.saved() if s["ssid"] == ssid), None)
        name, created = existing or ssid, existing is None
        if created:
            args = ["con", "add", "type", "wifi", "ifname", IFACE, "con-name", name, "ssid", ssid, "connection.autoconnect", "yes"]
            if password:
                args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
            if hidden:
                args += ["802-11-wireless.hidden", "yes"]
            nm(*args)
        elif password:
            nm("con", "modify", name, "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password)
        if ipv4:
            nm("con", "modify", name, *self._ipv4_args(ipv4))
        if before["hotspot"]:
            nm("con", "down", HOTSPOT)
            time.sleep(3)
        try:
            nm("con", "up", name, wait=45, timeout=60)
        except NmError as e:
            if created:
                nm("con", "delete", name)  # don't leave a profile with a bad password retrying forever
            if before["connected"]:
                try:
                    nm("con", "up", before["connection"], wait=45, timeout=60)
                except NmError:
                    pass
            if not self.status()["connected"]:
                self._start_hotspot()
            raise NmError(e)
        self.offline_since = None
        return f"Connected to {ssid} as {self.status()['address']}"

    def _set_ipv4(self, name, ipv4):
        prev = self.ipv4_of(name)
        nm("con", "modify", name, *self._ipv4_args(ipv4))
        if self.status()["connection"] != name:
            return f"Saved IP settings for {name} (applies next time it connects)"
        try:
            nm("con", "up", name, wait=30, timeout=45)
            st = self.status()
            gw = st["gateway"]
            ok = bool(gw) and subprocess.run(["ping", "-c", "2", "-W", "2", gw], capture_output=True).returncode == 0
        except NmError:
            ok = False
        if ok:
            return f"Applied. The board is now at {st['address']}"
        # Can't reach the router with the new settings: put the old ones back so the board isn't lost.
        nm("con", "modify", name, *self._ipv4_args(prev if prev["method"] == "manual" else {"method": "auto", "dns": prev["dns"]}))
        nm("con", "up", name, wait=30, timeout=45)
        raise NmError("couldn't reach the router with those settings, so the previous settings were restored")

    def _start_hotspot(self):
        hs = self.config.get("hotspot")
        try:
            nm("con", "delete", HOTSPOT)
        except NmError:
            pass
        nm("con", "add", "type", "wifi", "ifname", IFACE, "con-name", HOTSPOT, "connection.autoconnect", "no",
           "ssid", hs["ssid"], "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg", "ipv4.method", "shared",
           "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", hs["password"],
           "wifi-sec.proto", "rsn", "wifi-sec.pairwise", "ccmp", "wifi-sec.group", "ccmp")
        nm("con", "up", HOTSPOT, wait=30, timeout=45)
        self.next_retry = time.time() + hs["retry_minutes"] * 60
        return f"Hotspot '{hs['ssid']}' is on"

    def _leave_hotspot(self):
        """Drop the hotspot and try each saved network; bring the hotspot back if none connects."""
        if self.status()["hotspot"]:
            nm("con", "down", HOTSPOT)
            time.sleep(3)
        try:
            nm("dev", "wifi", "rescan")
            time.sleep(6)
        except NmError:
            pass
        for s in sorted(self.saved(), key=lambda s: -s["priority"]):
            try:
                nm("con", "up", s["name"], wait=30, timeout=45)
                self.offline_since = None
                return f"Connected to {s['ssid']}"
            except NmError:
                continue
        if self.config.get("hotspot")["enabled"]:
            self._start_hotspot()
            raise NmError("no saved network connected; the hotspot is back on")
        raise NmError("no saved network connected")

    # ---- watchdog ----

    def _hotspot_in_use(self):
        if time.time() - self.last_hotspot_request < 180:
            return True
        out = subprocess.run(["ip", "neigh", "show", "dev", IFACE], capture_output=True, text=True).stdout
        return any(s in out for s in ("REACHABLE", "DELAY", "PROBE"))

    def watchdog(self):
        time.sleep(20)  # let NetworkManager autoconnect after boot
        while True:
            try:
                self._tick()
            except Exception as e:
                print("watchdog:", e, flush=True)
            time.sleep(10)

    def _tick(self):
        if not self.op.acquire(blocking=False):
            return  # a user-started change is in progress
        try:
            hs, st = self.config.get("hotspot"), self.status()
            if st["connected"]:
                self.offline_since = None
            elif st["hotspot"]:
                if not hs["enabled"]:
                    self._safe(self._leave_hotspot)
                elif time.time() > self.next_retry and self.saved() and not self._hotspot_in_use():
                    self._safe(self._leave_hotspot)
                    self.next_retry = time.time() + hs["retry_minutes"] * 60
            elif hs["enabled"]:
                self.offline_since = self.offline_since or time.time()
                if time.time() - self.offline_since > hs["grace_seconds"]:
                    self._safe(self._start_hotspot)
        finally:
            self.op.release()

    def _safe(self, fn):
        try:
            print("watchdog:", fn(), flush=True)
        except Exception as e:
            print("watchdog:", e, flush=True)
