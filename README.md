# Assistant

A private, fully local voice assistant and smart-home controller for the **Arduino UNO Q**
(and other small Debian boards). It runs a small language model with [Ollama](https://ollama.com),
controls your devices through the **Home Assistant MCP server**, and can listen for a wake word,
talk back in voices like **HAL 9000**, and light up an LED "eye" while it listens.
Everything is set up from a web page, including Wi-Fi, so the board can be configured without a keyboard or screen.

Nothing leaves your network: speech recognition, the language model and speech synthesis all run on the board.

## Features

- **Home control in plain English**: "turn off the office", "dim the floor lamp to 30%", "pause the tv".
  Naming just a room switches its lights and switches. Commands take about 4 seconds on a UNO Q.
- **Conversation**: anything that isn't a command gets a short, in-character spoken reply.
  It runs on a second Ollama instance, so chatting never slows down commands.
- **Voice mode**: a wake phrase of your choice, or a trigger from a button (any USB key, remote or mic button),
  a GPIO pin, an HTTP call, a script, or Home Assistant. Listening can be switched off completely (the mic is closed).
- **Personalities**: voice, speed, how it talks, its spoken replies, light colours and **easter eggs**
  ("Open the pod bay doors, HAL"). HAL 9000 and a friendly assistant are built in; edit them or make your own.
- **Indicator light**: the board's RGB LED, a dimmable LED on a header pin, a Linux GPIO line, or any
  Home Assistant light (e.g. a WLED ring).
- **Web setup**: Wi-Fi (scan, join, forget), static IP with automatic rollback, Home Assistant, models,
  voices, volume and mic gain, and a chat page for testing.
- **Setup hotspot**: if the board can't get online, it starts its own Wi-Fi network so you can reach the
  setup page, and turns it off again once it's connected.

## Requirements

- Arduino UNO Q (4 GB) or a similar ARM64/x86-64 board with Debian, NetworkManager and systemd.
- [Ollama](https://ollama.com) installed as a system service.
- Home Assistant with the **Model Context Protocol Server** integration added, and a long-lived access token.
- For voice: a USB microphone and a USB speaker (or the board's audio output).
- About 1.5 GB of free disk space.

## Install

```sh
git clone <this repo> ~/assistant
cd ~/assistant
./install.sh              # add --with-hal for the HAL 9000 voice
```

`install.sh` creates a Python environment and downloads the speech model, a default voice and the
`qwen2.5:0.5b` language model (into both Ollama instances). It also installs two user services,
`assistant` and `ollama-chat`, and sets them to start at boot. It's safe to run again after pulling updates.

Then open **http://&lt;board-name&gt;.local:8080** and:

1. **Home Assistant** page: enter your Home Assistant URL and token, then press **Test** and **Save & reload**.
2. **Chat** page: try "turn on the kitchen" (tick **Dry run** first to see what it would do).
3. **Model** page: replace the worked examples with your own device names for better accuracy.
4. **Voice** page (optional): pick the microphone and speaker, switch **Listening** on, and choose a personality.

### Optional one-time root setup

```sh
sudo sh ~/assistant/setup-root.sh && sudo reboot
```

This moves the web page to port 80 (just `http://<board-name>.local`), enables the **Reboot** button,
makes phones open the setup page automatically when they join the hotspot, and lets the assistant drive
an LED on a header pin. Without it, everything else still works.

## Configuration

All settings are made on the web page and saved to `~/.config/assistant/config.json`, outside the repo,
so your Home Assistant token is never committed. Delete that file to go back to the defaults.

| Default | Value | Change it on |
|---|---|---|
| Web page | port 80 if allowed, otherwise 8080 | System |
| Setup hotspot | `assistant-setup`, password `hasetup123` | Network (**change this**) |
| UI password | none (anyone on your network can use it) | System (**set one**) |
| Wake phrase | "hey computer" | Voice |
| Language model | `qwen2.5:0.5b` for commands and conversation | Model |

## Voice and the indicator light

- **Triggers**: wake phrase, a learned button, GPIO, `POST /api/voice/listen`, `./listen.sh`, or Home Assistant.
- **Toggle listening**: header switch, a second learned button, `POST /api/voice/toggle`, or `./toggle.sh [on|off]`.
- **Speech recognition** uses [Vosk](https://alphacephei.com/vosk/). In the default *fast* mode, commands are
  recognised from a vocabulary built from your device and room names (about 0.3 s), and conversation uses
  full English in parallel. If it mishears a device name, add how you say it under **Extra words**.
- **Voices** are [Piper](https://github.com/rhasspy/piper) models. Download more on the Voice page, or add any
  Piper `.onnx` voice by URL.

### LED on a header pin (UNO Q)

The UNO Q's header pins are driven by its microcontroller, not by Linux. The assistant flashes a tiny sketch
(`mcu/agent_led`) to the microcontroller and talks to it over the Arduino Bridge, which gives smooth dimming.
This replaces whatever sketch the microcontroller was running.

Wiring: **pin (D2–D13) → 220 Ω resistor → LED long leg; LED short leg → GND.**
Choose **LED on a header pin** on the Voice page, under Indicator light. This needs the root setup above.

## HTTP API

Requests from the board itself never need the UI password.

| Method and path | What it does |
|---|---|
| `POST /ask` `{"text": "...", "dry_run": false, "speak": false}` | Run a command or chat |
| `POST /api/voice/listen` | Start listening, as if the wake phrase was said |
| `POST /api/voice/toggle` `{"on": true}` | Switch listening on/off (no body = toggle) |
| `POST /api/voice/say` `{"text": "..."}` | Speak text on the speaker |
| `GET /api/status` | Network, Home Assistant, model, voice and system status |

Home Assistant example (`configuration.yaml`):

```yaml
rest_command:
  assistant_listen:
    url: "http://unoq1.local/api/voice/listen"
    method: post
  assistant_say:
    url: "http://unoq1.local/api/voice/say"
    method: post
    content_type: "application/json"
    payload: '{"text": "{{ text }}"}'
```

## How it works

| File | Role |
|---|---|
| `web.py` | Web server, JSON API and startup |
| `agent.py` | Settings, Home Assistant MCP client, command routing and conversation |
| `voice.py` | Microphone, wake phrase, speech recognition, speech, personalities, indicator light |
| `netmgr.py` | Wi-Fi, static IP and the setup hotspot (NetworkManager) |
| `bridge.py` | Arduino Bridge client and sketch flashing |
| `index.html` | The web page |
| `mcu/agent_led/` | Microcontroller sketch for the header-pin LED |

Speed comes from keeping each model's prompt identical between requests, so Ollama reuses its cache.
Only your words are processed each time. Commands and conversation use separate Ollama instances
for the same reason. Before a command runs, the assistant checks that the device or room it targets was
actually mentioned, so the small model can't switch something you didn't ask for.

## Troubleshooting

- **Web page not loading:** `systemctl --user status assistant` and `journalctl --user -u assistant -f`.
- **"HA not connected":** check the URL and token on the Home Assistant page. The assistant only sees
  devices that are exposed to Assist in Home Assistant.
- **Commands slow after changing settings:** the first request after a change rebuilds the cache
  (about a minute). Later requests are fast again.
- **Board unreachable after Wi-Fi changes:** join the `assistant-setup` hotspot and open http://10.42.0.1:8080
  (or http://10.42.0.1 after the root setup).

## Credits

- Speech recognition: [Vosk](https://alphacephei.com/vosk/). Speech: [Piper](https://github.com/rhasspy/piper).
  Language model: [Qwen2.5](https://ollama.com/library/qwen2.5) via Ollama.
- The optional HAL 9000 voice is [OccultAI/HAL-9000-Piper-TTS](https://huggingface.co/OccultAI/HAL-9000-Piper-TTS)
  (Apache-2.0 on its model card). It was trained on audio from *2001: A Space Odyssey*, so keep it for personal use.
  `install.sh` only downloads it when you pass `--with-hal`.
