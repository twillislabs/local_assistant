"""Settings profiles: copy an assistant's settings to another machine.

  ./profile.sh export NAME                 -> profiles/NAME.json (no secrets; safe to commit)
  ./profile.sh export NAME --with-secrets  -> profiles/NAME.secret.json (git-ignored)
  ./profile.sh import FILE                 -> replaces the settings, keeping secrets the file lacks

A profile is the settings file plus a "_profile" note. Without secrets, the Home Assistant
token, UI password, hotspot password and header values (API keys) are blank; enter them on
the new machine, or import over a machine that already has them.
"""
import copy
import json
import os
import shutil
import socket
import sys
import time

from agent import CONFIG_PATH

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(HERE, "profiles")
SECRETS = [("ha", "token"), ("ui", "password"), ("hotspot", "password")]


def _header_holders(data):
    """Every {name: value} headers dict in the settings: tools and the URL indicator."""
    holders = [(f"tool:{t.get('id')}", t) for t in data.get("tools", [])]
    if isinstance(data.get("indicator", {}).get("http"), dict):
        holders.append(("indicator", data["indicator"]["http"]))
    return [(key, h) for key, h in holders if isinstance(h.get("headers"), dict)]


def export(data, include_secrets=False):
    p = copy.deepcopy(data)
    if not include_secrets:
        for section, key in SECRETS:
            if p.get(section, {}).get(key):
                p[section][key] = ""
        for _, holder in _header_holders(p):
            holder["headers"] = {k: "" for k in holder["headers"]}
    p["_profile"] = {"exported": time.strftime("%Y-%m-%d %H:%M"), "from": socket.gethostname(),
                     "secrets": include_secrets}
    return p


def merge(current, profile):
    """The settings to save: the profile, with blank secrets filled from the current settings."""
    new = copy.deepcopy(profile)
    new.pop("_profile", None)
    for section, key in SECRETS:
        if not new.get(section, {}).get(key) and current.get(section, {}).get(key):
            new.setdefault(section, {})[key] = current[section][key]
    old_headers = dict(_header_holders(current))
    for name, holder in _header_holders(new):
        for k, v in holder["headers"].items():
            if not v:
                holder["headers"][k] = old_headers.get(name, {}).get("headers", {}).get(k, "")
    return new


def write_settings(data):
    """Save settings, keeping a backup of the old file next to it."""
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    if os.path.exists(CONFIG_PATH):
        shutil.copy2(CONFIG_PATH, CONFIG_PATH + time.strftime(".before-import-%Y%m%d-%H%M%S"))
    tmp = CONFIG_PATH + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


def _current():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def main(argv):
    if len(argv) >= 2 and argv[0] == "export":
        secrets = "--with-secrets" in argv
        name = argv[1]
        path = os.path.join(PROFILE_DIR, f"{name}.secret.json" if secrets else f"{name}.json")
        os.makedirs(PROFILE_DIR, exist_ok=True)
        with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if secrets else 0o644), "w") as f:
            json.dump(export(_current(), secrets), f, indent=2)
            f.write("\n")
        print(f"Wrote {os.path.relpath(path, HERE)}" + (" (contains secrets; git-ignored, don't share it)" if secrets else ""))
        return 0
    if len(argv) == 2 and argv[0] == "import":
        path = argv[1]
        if not os.path.exists(path) and os.path.exists(os.path.join(PROFILE_DIR, path + ".json")):
            path = os.path.join(PROFILE_DIR, path + ".json")
        with open(path) as f:
            profile = json.load(f)
        settings = merge(_current(), profile)
        write_settings(settings)
        print(f"Imported {path}. Restart to apply: systemctl --user restart assistant")
        if not settings.get("ha", {}).get("token") and any(t.get("type") == "homeassistant" for t in settings.get("tools", [])):
            print("Note: no Home Assistant token yet; add it on the Home Assistant page.")
        return 0
    print(__doc__.strip())
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
