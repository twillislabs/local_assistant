#!/bin/sh
# Install or update the assistant. Run as your normal user from the repo folder:
#   ./install.sh              standard install
#   ./install.sh --with-hal   also download the HAL 9000 voice
# Safe to run again; it skips what's already done.
set -e

DIR=$(cd "$(dirname "$0")" && pwd)
PY="$DIR/venv/bin/python"
MODEL=qwen2.5:0.5b
WITH_HAL=0
for arg in "$@"; do
  case "$arg" in
    --with-hal) WITH_HAL=1 ;;
    *) echo "unknown option: $arg"; exit 1 ;;
  esac
done

need() {
  command -v "$1" >/dev/null 2>&1 || { echo "Missing $1. Install it first: $2"; exit 1; }
}
need python3 "sudo apt install python3 python3-venv"
need nmcli "sudo apt install network-manager"
need arecord "sudo apt install alsa-utils"
need curl "sudo apt install curl"
need ollama "curl -fsSL https://ollama.com/install.sh | sh"

echo "== Python environment"
[ -x "$PY" ] || python3 -m venv "$DIR/venv"
"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q -r "$DIR/requirements.txt"

echo "== Speech recognition model"
mkdir -p "$DIR/models/stt" "$DIR/models/voices"
if ! ls -d "$DIR"/models/stt/vosk-model-* >/dev/null 2>&1; then
  curl -fL --progress-bar -o "$DIR/models/stt/vosk.zip" https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
  "$PY" -c "import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$DIR/models/stt/vosk.zip" "$DIR/models/stt"
  rm "$DIR/models/stt/vosk.zip"
fi

echo "== Voices"
get_voice() {
  [ -f "$DIR/models/voices/$1.onnx" ] && return 0
  curl -fL --progress-bar -o "$DIR/models/voices/$1.onnx.json" "$2.json"
  curl -fL --progress-bar -o "$DIR/models/voices/$1.onnx" "$2"
}
get_voice en_US-lessac-medium https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx
if [ "$WITH_HAL" = 1 ]; then
  get_voice hal https://huggingface.co/OccultAI/HAL-9000-Piper-TTS/resolve/main/hal.onnx
fi

echo "== Services"
UNITS="$HOME/.config/systemd/user"
mkdir -p "$UNITS"
# A second Ollama instance for conversation, so chatting never evicts the
# command prompt from the main instance's cache.
cat > "$UNITS/ollama-chat.service" <<EOF
[Unit]
Description=Second Ollama instance for conversation (keeps its own prompt cache)

[Service]
Environment=OLLAMA_HOST=127.0.0.1:11436
Environment=OLLAMA_MODELS=%h/.ollama/models
Environment=OLLAMA_KEEP_ALIVE=-1
Environment=OLLAMA_NUM_PARALLEL=1
Environment=OLLAMA_MAX_LOADED_MODELS=1
ExecStart=$(command -v ollama) serve
Restart=on-failure

[Install]
WantedBy=default.target
EOF
cat > "$UNITS/assistant.service" <<EOF
[Unit]
Description=Assistant: web UI, voice and Home Assistant control
After=network-online.target ollama-chat.service

[Service]
ExecStart=$PY $DIR/web.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF
chmod +x "$DIR"/*.sh
systemctl --user daemon-reload
systemctl --user enable --now ollama-chat
# Keep the services running without a login session, and start them at boot.
loginctl enable-linger "$USER" 2>/dev/null || echo "Note: couldn't enable lingering; run: sudo loginctl enable-linger $USER"

echo "== Language models"
ollama pull "$MODEL"  # commands, on the main Ollama service
for i in $(seq 1 30); do
  curl -s -o /dev/null http://127.0.0.1:11436/api/tags && break
  sleep 1
done
OLLAMA_HOST=127.0.0.1:11436 ollama pull "$MODEL"  # conversation

systemctl --user enable assistant
systemctl --user restart assistant

PORT=8080
[ "$(cat /proc/sys/net/ipv4/ip_unprivileged_port_start 2>/dev/null)" -le 80 ] 2>/dev/null && PORT=80
echo
echo "Done. Open http://$(hostname).local$([ $PORT = 80 ] || echo ":$PORT") and set up Home Assistant on the Home Assistant page."
echo "Optional one-time root setup (port 80, reboot button, captive portal, header-pin LED):"
echo "  sudo sh $DIR/setup-root.sh && sudo reboot"
