#!/bin/sh
# One-time root setup for the assistant. Run with sudo from the user that runs it:
#   sudo sh ~/assistant/setup-root.sh
# Everything works without it; this adds port 80, the Reboot button, the hotspot's
# captive portal, and (on an Arduino UNO Q) access to the microcontroller Bridge.
set -e

USER_NAME=${SUDO_USER:-$(logname 2>/dev/null || echo "")}
if [ -z "$USER_NAME" ] || [ "$USER_NAME" = root ]; then
  echo "Run this with sudo from your normal user account."
  exit 1
fi

# Files from when this project was called ha-agent.
rm -f /etc/sysctl.d/50-ha-agent.conf /etc/polkit-1/rules.d/50-ha-agent.rules \
      /etc/NetworkManager/dnsmasq-shared.d/ha-agent-captive.conf

# 1. Let the web UI listen on port 80, so it's just http://<name>.local and http://10.42.0.1
echo "net.ipv4.ip_unprivileged_port_start=80" > /etc/sysctl.d/50-assistant.conf
sysctl -q -p /etc/sysctl.d/50-assistant.conf

# 2. Let the user reboot / power off from the UI.
if [ -d /etc/polkit-1/rules.d ]; then
  cat > /etc/polkit-1/rules.d/50-assistant.rules <<EOF
polkit.addRule(function(action, subject) {
    if (subject.user == "$USER_NAME" && (
            action.id == "org.freedesktop.login1.reboot" ||
            action.id == "org.freedesktop.login1.reboot-multiple-sessions" ||
            action.id == "org.freedesktop.login1.power-off" ||
            action.id == "org.freedesktop.login1.power-off-multiple-sessions")) {
        return polkit.Result.YES;
    }
});
EOF
fi

# 3. Captive portal: while the setup hotspot is on, answer every DNS name with this machine,
#    so phones pop up the setup page automatically after joining the hotspot.
if [ -d /etc/NetworkManager ]; then
  mkdir -p /etc/NetworkManager/dnsmasq-shared.d
  echo "address=/#/10.42.0.1" > /etc/NetworkManager/dnsmasq-shared.d/assistant-captive.conf
fi

# 4. Arduino UNO Q: let the assistant talk to the microcontroller over the Bridge (header-pin LED).
if getent group arduino-router >/dev/null; then
  usermod -aG arduino-router "$USER_NAME"
fi
# Hardware access used by buttons and LEDs, where these groups exist (e.g. Raspberry Pi OS).
for g in gpio gpiod input audio; do
  getent group "$g" >/dev/null && usermod -aG "$g" "$USER_NAME"
done

echo "Done. Reboot so the new settings and group memberships take effect: sudo reboot"
