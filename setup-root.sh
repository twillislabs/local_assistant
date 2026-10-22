#!/bin/sh
# One-time root setup for the assistant. Run with: sudo sh ~/assistant/setup-root.sh
# Everything else (Wi-Fi, static IP, hotspot) already works without root.
set -e

# Files from when this project was called ha-agent.
rm -f /etc/sysctl.d/50-ha-agent.conf /etc/polkit-1/rules.d/50-ha-agent.rules \
      /etc/NetworkManager/dnsmasq-shared.d/ha-agent-captive.conf

# 1. Let the web UI listen on port 80, so it's just http://unoq1.local and http://10.42.0.1
echo "net.ipv4.ip_unprivileged_port_start=80" > /etc/sysctl.d/50-assistant.conf
sysctl -q -p /etc/sysctl.d/50-assistant.conf

# 2. Let the arduino user reboot / power off from the UI.
cat > /etc/polkit-1/rules.d/50-assistant.rules <<'EOF'
polkit.addRule(function(action, subject) {
    if (subject.user == "arduino" && (
            action.id == "org.freedesktop.login1.reboot" ||
            action.id == "org.freedesktop.login1.reboot-multiple-sessions" ||
            action.id == "org.freedesktop.login1.power-off" ||
            action.id == "org.freedesktop.login1.power-off-multiple-sessions")) {
        return polkit.Result.YES;
    }
});
EOF

# 3. Captive portal: while the setup hotspot is on, answer every DNS name with the board,
#    so phones pop up the setup page automatically after joining the hotspot.
mkdir -p /etc/NetworkManager/dnsmasq-shared.d
echo "address=/#/10.42.0.1" > /etc/NetworkManager/dnsmasq-shared.d/assistant-captive.conf

# 4. Let the assistant talk to the microcontroller over the Bridge (for the header-pin LED).
usermod -aG arduino-router arduino

echo "Done. Reboot so the new group membership and settings take effect: sudo reboot"
