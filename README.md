# Assistant

A private, fully local voice assistant that runs on small Linux machines: an **Arduino UNO Q**,
a **Raspberry Pi**, an old PC or a VM. It uses a small language model through [Ollama](https://ollama.com)
and acts through **tools**. Home Assistant is installed by default, and you can add weather, any
**MCP server**, HTTP APIs or your own scripts from the web page. It can listen for a wake word, talk back in
voices like **HAL 9000**, and show what it's doing on an LED, a smart light, a URL or a script.
Everything is set up from a web page, including Wi-Fi, so a headless board needs no keyboard or screen.

Nothing leaves your network unless a tool you add calls out (the weather tool uses Open-Meteo).
Speech recognition, the language model and speech synthesis all run locally.

## Features

- **Tools**, added and removed on the Tools page:
  - **Home Assistant** (installed by default): "turn off the office", "dim the floor lamp to 30%", "pause the tv".
    Naming just a room switches its lights and switches.
  - **Weather**: current conditions and tomorrow's forecast from Open-Meteo, no API key.
  - **MCP servers**: any Model Context Protocol server, over HTTP or by running a local command (stdio).
  - **HTTP API** and **script** tools: describe one tool (name, what it does, parameters) and the URL or
    command to run, and optionally how to say the result.
- **Conversation**: anything that isn't for a tool gets a short, in-character spoken reply, on a second
  Ollama instance so chatting never slows down tools.
- **Safety checks**: a tool or device only runs if your words actually named it. A small model sometimes
  guesses; those guesses are refused or redirected instead of switching the wrong thing.
- **Voice mode**: a wake phrase of your choice, or a trigger from a button (any USB key, remote or mic button),
  a GPIO pin, an HTTP call, a script, or Home Assistant. Listening can be switched off completely (the mic is closed).
- **Local or network models**: run the language models on this machine or on an Ollama server elsewhere on your
  network (e.g. a GPU PC), with automatic fallback.
- **Learning**: teach it shortcuts ("when I say sit height, set the desk to 25 percent") and facts
  ("remember that …"); update or forget them by asking, or on the Memory page.
- **Personalities**: voice, speed, how it talks, its spoken replies, light colours and **easter eggs**
  ("Open the pod bay doors, HAL"). HAL 9000 and a friendly assistant are built in; edit them or make your own.
- **Indicator**: built-in LEDs, a GPIO LED, a dimmable LED on a UNO Q header pin, any Home Assistant light
  (e.g. a WLED ring), or a URL or script called on every change.
- **Web setup**: Wi-Fi (scan, join, forget), static IP with automatic rollback, tools, models, voices,
  volume and mic gain, and a chat page for testing.
- **Setup hotspot**: if a Wi-Fi machine can't get online, it starts its own network so you can reach the
  setup page, and turns it off again once it's connected.

## Runs on

| Machine | Notes |
|---|---|
| **Arduino UNO Q** (4 GB) | Built-in support: the RGB LED, header-pin LEDs over the Bridge, Wi-Fi setup and hotspot. About 4 s per command. |
| **Raspberry Pi** 4/5 | Wi-Fi setup and hotspot with Raspberry Pi OS Bookworm or later (NetworkManager), GPIO buttons and LEDs. |
| **PC or VM** (Debian, Ubuntu, …) | Everything except hardware it doesn't have. Without NetworkManager, the network is left to the system. |

Hardware options only appear on the web page when the machine has them. Any 64-bit Linux with systemd,
Python 3.10+ and Ollama should work. Expect about 1.5 GB of disk use.

## Install

```sh
git clone <this repo> ~/assistant
cd ~/assistant
./install.sh              # add --with-hal for the HAL 9000 voice
```

Requirements: `python3` with `venv`, `curl` and [Ollama](https://ollama.com) installed as a system service.
Optional: NetworkManager (Wi-Fi setup, hotspot), `alsa-utils` (voice) and `gpiod` (GPIO). The installer
says what's missing and what it affects.

`install.sh` creates a Python environment and downloads the speech model, a default voice and the
`qwen2.5:0.5b` language model (into both Ollama instances). It also installs two user services,
`assistant` and `ollama-chat`, and sets them to start at boot. It's safe to run again after pulling updates.

Then open **http://&lt;machine-name&gt;.local:8080** and:

1. **Home Assistant** page: enter your Home Assistant URL and a long-lived access token (Home Assistant needs the
   **Model Context Protocol Server** integration), then press **Test** and **Save & reload**.
   Don't use Home Assistant? Remove it on the Tools page.
2. **Chat** page: try "turn on the kitchen" (tick **Dry run** first to see what it would do).
3. **Tools** page: add weather, MCP servers or your own tools.
4. **Model** page: replace the Home Assistant examples with your own device names for better accuracy.
5. **Voice** page (optional): pick the microphone and speaker, switch **Listening** on, and choose a personality.

### Optional one-time root setup

```sh
sudo sh ~/assistant/setup-root.sh && sudo reboot
```

This moves the web page to port 80 (just `http://<machine-name>.local`), enables the **Reboot** button,
makes phones open the setup page automatically when they join the hotspot, and adds your user to the
hardware groups that exist (`gpio`, `input`, `audio`, and on a UNO Q `arduino-router` for the header-pin LED).
Without it, everything else still works.

## Configuration

All settings are made on the web page and saved to `~/.config/assistant/config.json`, outside the repo,
so tokens and API keys are never committed. Delete that file to go back to the defaults.

| Default | Value | Change it on |
|---|---|---|
| Web page | port 80 if allowed, otherwise 8080 | System |
| Tools | Home Assistant | Tools |
| Setup hotspot | `assistant-setup`, password `hasetup123` | Network (**change this**) |
| UI password | none (anyone on your network can use it) | System (**set one**) |
| Wake phrase | "hey computer" | Voice |
| Language model | `qwen2.5:0.5b` for tools and conversation | Model |

### Copying an assistant to another machine

Settings can be saved as a **profile** and committed, so another machine can start out identical:

```sh
./profile.sh export home                     # writes profiles/home.json; commit and push it
./install.sh --profile profiles/home.json    # on the new machine, after cloning
./profile.sh import profiles/home.json       # or apply it to an installed assistant, then restart
```

Profiles leave out secrets (the Home Assistant token, UI and hotspot passwords, and tool header values such as
API keys); enter them on the new machine. Importing never blanks secrets the machine already has. For a
fully hands-off copy, `./profile.sh export home --with-secrets` writes `profiles/home.secret.json`, which is
git-ignored. Copy it across yourself and don't commit it. Wi-Fi networks and passwords are stored by
NetworkManager and are never part of a profile. The System page has the same Export and Import buttons.

## Using an Ollama server on your network

The language models can run on another computer, such as a PC with a GPU, instead of this machine. On a small board
that's the biggest possible speed-up: commands go from about 4 seconds to under one, and you can use a 7–8B model
(e.g. `qwen2.5:7b`), which picks tools and plays personalities far better. Speech recognition and the voice still
run locally.

1. On the server, install Ollama and let it accept network connections (`OLLAMA_HOST=0.0.0.0`).
2. On the **Model** page, enter its address (e.g. `http://192.168.1.20:11434`) as the server for commands and/or
   conversation, press **Test**, pick a model (or download one to it from the same page) and save.
3. Optionally set a **fallback**, e.g. this machine's own Ollama. If the network server is off or doesn't have the
   model, requests go to the fallback after 3 seconds, and the network server is tried again a minute later.
   The fallback is kept warm, so switching costs no warm-up. The header shows when it's in use.

## Teaching it

Teach it by talking or typing; manage everything on the **Memory** page.

| Say | What happens |
|---|---|
| "When I say sit height, set the standing desk to 25 percent" | Learns a **shortcut**. It works out the action now (without doing it) and confirms what it will do. |
| "Standing also means stand height" | Adds another phrase to an existing shortcut. |
| "Change sit height to 30 percent" | Changes the shortcut's amount. Re-teaching a phrase also updates it. |
| "Forget sit height" | Removes a shortcut. |
| "Remember that my favorite color is green" | Remembers a **fact** for conversation. A corrected version replaces it; "forget that …" removes it. |
| "What have you learned?" | Lists shortcuts and facts. |

Shortcuts run instantly, without the language model, when the phrase is the point of what you say ("sit height",
"desk to sit height"), not when it just appears in a sentence ("is the standing lamp on?"). Facts are only given
to the conversation model, so learning never slows commands down. Everything learned is part of settings profiles.

## Tools

Requests that mention a tool (its keywords or tool names, or home-control phrasing for Home Assistant)
go to the model with every installed tool; everything else is conversation. Results are spoken using
the tool's own wording (weather), your **spoken reply** template (`{result}`), the result itself if it's
short, or a one-sentence summary from the chat model.

- **MCP server, HTTP**: the server's URL (streamable HTTP; JSON and SSE replies both work) and any headers,
  e.g. `Authorization: Bearer …`. **Test** lists its tools; tick the ones to offer (fewer is better for a small model).
- **MCP server, command**: e.g. `npx -y @modelcontextprotocol/server-everything`. It's started on demand and kept running.
- **HTTP API**: method, URL and optional body with `{parameter}` placeholders, plus parameters as `name: description` lines.
- **Script**: a command with `{parameter}` placeholders (safely quoted). Runs as your user with a 30-second limit.

Give each tool a few **keywords** ("joke, funny") and an **example request** so the small model knows when to
use it. Header values (API keys) are stored in the config file and shown as dots on the page.

## Voice and the indicator

- **Triggers**: wake phrase, a learned button, GPIO, `POST /api/voice/listen`, `./listen.sh`, or Home Assistant.
- **Toggle listening**: header switch, a second learned button, `POST /api/voice/toggle`, or `./toggle.sh [on|off]`.
- **Speech recognition** uses [Vosk](https://alphacephei.com/vosk/). In the default *fast* mode, commands are
  recognised from a vocabulary built from your devices, rooms and tool keywords (about 0.3 s on a UNO Q), and
  conversation uses full English in parallel. If it mishears a name, add how you say it under **Extra words**.
- **Voices** are [Piper](https://github.com/rhasspy/piper) models. Download more on the Voice page, or add any
  Piper `.onnx` voice by URL.
- **Indicator**: states are `off`, `idle`, `listening`, `thinking` and `speaking`, and each personality sets a
  colour and brightness for each. For **URL** and **script** indicators, `{state}`, `{brightness}` (0–100),
  `{color}` (`#rrggbb`) and `{r}` `{g}` `{b}` are filled in, e.g. `~/bin/ring.sh {state} {color}`.

### LED on a UNO Q header pin

The UNO Q's header pins are driven by its microcontroller, not by Linux. The assistant flashes a tiny sketch
(`mcu/agent_led`) to the microcontroller and talks to it over the Arduino Bridge, which gives smooth dimming.
This replaces whatever sketch the microcontroller was running, and it's re-flashed automatically after
power-up (the Bridge handshake doesn't survive a cold boot).

Wiring: **pin (D2–D13) → 220 Ω resistor → LED long leg; LED short leg → GND.**
Needs the root setup above.

## HTTP API

Requests from the machine itself never need the UI password.

| Method and path | What it does |
|---|---|
| `POST /ask` `{"text": "...", "dry_run": false, "speak": false}` | Run a request (tool or chat) |
| `POST /api/voice/listen` | Start listening, as if the wake phrase was said |
| `POST /api/voice/toggle` `{"on": true}` | Switch listening on/off (no body = toggle) |
| `POST /api/voice/say` `{"text": "..."}` | Speak text on the speaker |
| `GET /api/status` | Network, tools, model, voice and system status |
| `GET /api/platform` | What hardware this machine has |

Home Assistant example (`configuration.yaml`):

```yaml
rest_command:
  assistant_listen:
    url: "http://assistant-host.local/api/voice/listen"
    method: post
  assistant_say:
    url: "http://assistant-host.local/api/voice/say"
    method: post
    content_type: "application/json"
    payload: '{"text": "{{ text }}"}'
```

## How it works

| File | Role |
|---|---|
| `web.py` | Web server, JSON API, platform detection and startup |
| `agent.py` | Settings, Home Assistant, routing, tool calls, safety checks and conversation |
| `tools.py` | Weather, MCP (HTTP and stdio), HTTP API and script tools |
| `voice.py` | Microphone, wake phrase, speech recognition, speech, personalities, indicator |
| `netmgr.py` | Wi-Fi, static IP and the setup hotspot (NetworkManager) |
| `bridge.py` | Arduino UNO Q Bridge client and sketch flashing |
| `profiles.py` | Export and import settings profiles (`profile.sh`) |
| `profiles/` | Committed settings profiles |
| `index.html` | The web page |
| `mcu/agent_led/` | UNO Q microcontroller sketch for the header-pin LED |

Speed comes from keeping each model's prompt identical between requests, so Ollama reuses its cache and
only your words are processed each time. Adding or changing tools rebuilds that prompt once (about a minute
on a UNO Q). Tools and conversation use separate Ollama instances for the same reason.

## Troubleshooting

- **Web page not loading:** `systemctl --user status assistant` and `journalctl --user -u assistant -f`.
- **A tool shows an error on the Tools page:** it's retried every minute; the other tools keep working.
- **Home Assistant devices missing:** the assistant only sees devices that are exposed to Assist in Home Assistant.
- **First request slow after changing tools or settings:** the prompt cache is being rebuilt (about a minute).
- **Machine unreachable after Wi-Fi changes:** join the `assistant-setup` hotspot and open http://10.42.0.1:8080
  (or http://10.42.0.1 after the root setup).

## Credits

- Speech recognition: [Vosk](https://alphacephei.com/vosk/). Speech: [Piper](https://github.com/rhasspy/piper).
  Language model: [Qwen2.5](https://ollama.com/library/qwen2.5) via Ollama. Weather: [Open-Meteo](https://open-meteo.com).
- The optional HAL 9000 voice is [OccultAI/HAL-9000-Piper-TTS](https://huggingface.co/OccultAI/HAL-9000-Piper-TTS)
  (Apache-2.0 on its model card). It was trained on audio from *2001: A Space Odyssey*, so keep it for personal use.
  `install.sh` only downloads it when you pass `--with-hal`.
