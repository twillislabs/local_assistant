# CLAUDE.md

Guidance for Claude Code when working in this repository. See README.md for what the project does and how users install it.

## What this is

A local voice assistant: a Python web server (`web.py`) that routes requests to tools (Home Assistant over MCP, weather, other MCP servers, HTTP/script tools) through a small Ollama model, with optional voice (Vosk + Piper), personalities, an indicator light, and Wi-Fi/hotspot setup. Primary target is the Arduino UNO Q (4 cores, 4 GB, CPU-only), but it must keep working on Raspberry Pi, PCs and VMs.

## Layout

| File | Role |
|---|---|
| `web.py` | HTTP server (stdlib `http.server`), JSON API, platform detection, startup threads |
| `agent.py` | `Config`, Home Assistant client, prompt building, routing, tool calls, safety checks, conversation |
| `tools.py` | Non-HA tool providers: `Weather`, `Mcp` (HTTP + stdio), `Http`, `Command` |
| `voice.py` | Mic loop, wake phrase, Vosk STT, Piper TTS, personalities/easter eggs, `Indicator` |
| `netmgr.py` | NetworkManager (nmcli) Wi-Fi, static IP, setup hotspot watchdog |
| `bridge.py` | UNO Q Bridge (arduino-router msgpack-RPC) client and sketch flashing |
| `profiles.py`, `profile.sh`, `profiles/` | Settings profiles: export/import settings to replicate an assistant |
| `index.html` | The whole UI: one file, vanilla JS, no build step |
| `mcu/agent_led/` | UNO Q microcontroller sketch for the header-pin LED |
| `install.sh`, `setup-root.sh` | User install (venv, models, services) and optional one-time root setup |

Runtime state that is not in the repo: `venv/`, `models/` (both made by `install.sh`), and settings in `~/.config/assistant/config.json`.

## Design rules (don't break these)

- **Prompt cache.** Speed depends on the command prompt (system text + tools + examples) being byte-identical between requests, so Ollama reuses its KV cache. Never put changing data (device states, times) in it. `Agent._reload` skips the warm-up when the prompt signature is unchanged.
- **Two Ollama instances.** Commands use the system Ollama (`:11434`); conversation and summaries use `ollama-chat` (`:11436`). Mixing them on one instance evicts the command cache (a command then takes ~90 s instead of ~4 s).
- **Safety checks.** The 0.5B model guesses. A Home Assistant call only runs if the device/room was mentioned (`Agent.mentioned`); a non-HA tool only runs if its provider's keywords matched (`command()`), otherwise it is redirected or refused. If the model answers in text instead of calling a tool, that text is never shown or spoken (it invents results, even fake `<tool_response>` blocks): the matched single-tool provider is called directly, or the request falls back to conversation. Arguments the user's words make certain come from `Provider.guess_args` and override the model's (it has copied "tomorrow" from examples and invented a city called "City"); the weather tool takes its day, place and condition only from the words. Keep these when changing routing.
- **Request order** in `Agent.ask`: easter egg → teaching (`teach`) → learned shortcut (`shortcut_for`, runs a stored tool call without the model) → tools or conversation. Learned facts go into the *chat* system prompt only (`facts_prompt`); never put learned data in the command prompt.
- **Device type wins over the model's tool choice** for Home Assistant numbers (`_fit_tool`): position for covers/valves, brightness for lights, volume for media players.
- **One reload at a time.** `Agent.reload` holds `_reload_lock`; tool changes trigger reloads from threads.
- **Defaults must work from a fresh clone.** New settings need a default in `agent.DEFAULTS` (or `voice.DEFAULTS` / `INDICATOR_DEFAULTS`). Saved config is merged over defaults, so existing installs pick up new keys automatically.
- **Platform-agnostic.** Guard hardware features (Bridge, sysfs LEDs, GPIO, NetworkManager, ALSA) so the app still runs without them; the UI hides options using `/api/platform`.
- **Secrets.** The HA token, UI password and tool header values are masked before being sent to the browser (`masked_config`, `mask_tool`); masked values sent back mean "keep the saved one". Committed profiles (`profiles/*.json`) must never contain them: `profiles.export` blanks `SECRETS` and all header values unless `--with-secrets`, which writes a git-ignored `*.secret.json`. A new secret setting must be added to `profiles.SECRETS` (or be a `headers` dict) too.
- **Dependencies.** Stdlib plus `requirements.txt` only. No web framework, no frontend build.

## Working on the dev board

The reference install is an Arduino UNO Q reachable as `arduino@192.168.10.218`, repo at `~/assistant`, running as the user service `assistant` on port 80.

```sh
# deploy a change and restart
scp agent.py arduino@192.168.10.218:~/assistant/
ssh arduino@192.168.10.218 'systemctl --user restart assistant'
ssh arduino@192.168.10.218 'journalctl --user -u assistant -f'

# wait for warm-up, then try requests without touching devices
curl -s localhost/api/status          # agent.warm becomes "warm (…)"
curl -s localhost/ask -d '{"text":"turn off the office","dry_run":true}'
```

- Prefer `dry_run` for Home Assistant commands; real ones switch the user's lights.
- When passing JSON or commit messages through `ssh '…'`, apostrophes break the quoting. Put multi-line content in a file and `scp` it.

## Checks before committing

```sh
venv/bin/python -m py_compile agent.py tools.py voice.py netmgr.py web.py bridge.py
sh -n install.sh && sh -n setup-root.sh
# index.html JS syntax (node isn't installed on the board; docker is):
python3 -c "import re;s=open('index.html').read();open('/tmp/c.js','w').write(re.search(r'<script>(.*)</script>',s,re.S).group(1))"
docker run --rm -v /tmp/c.js:/c.js:ro node:20-alpine node --check /c.js; docker rmi -f node:20-alpine
```

A fresh-clone test: clone to `/tmp`, symlink `venv` and `models` from `~/assistant`, and run `web.py` with `HOME` pointing at an empty folder whose `.config/assistant/config.json` is `{"ui": {"port": 8090}}`.

## UNO Q specifics

- Header pins belong to the microcontroller. The LED sketch is flashed with `remoteocd` and called over `/var/run/arduino-router.sock` (group `arduino-router`).
- After a cold boot the sketch's Bridge handshake is lost and a reset doesn't recover it; only re-flashing does (`bridge.flash_sketch`, triggered automatically by the indicator).
- Don't read GPIO lines 37/38/70 on `gpiochip1` with `gpioget`: it reconfigures the router's control lines.

## Git

- Commit as the user (`twillislabs`). **Do not add `Co-Authored-By: Claude` or any Claude attribution** to commits or PRs.
- Remote: https://github.com/twillislabs/local_assistant (private). The push token is in `~/.gh_token` on the board, outside the repo. Never print it or store it in git config or remote URLs.
