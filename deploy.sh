#!/usr/bin/env bash
# Deploy cruise-control to a target host.
# Usage: ./deploy.sh <host> [interface]
# Example: ./deploy.sh hopper enP7s7
# Example: ./deploy.sh gb10-work enp3s0
set -euo pipefail

HOST=${1:?Usage: deploy.sh <host> [interface]}
IFACE=${2:-}  # optional — detected on remote if omitted

echo "==> Deploying cruise-control to $HOST"

# 1. Copy app
ssh "$HOST" "sudo mkdir -p /opt/cruise-control"
scp app.py "$HOST":/tmp/cruise_control_app.py
ssh "$HOST" "sudo cp /tmp/cruise_control_app.py /opt/cruise-control/app.py && sudo chmod 644 /opt/cruise-control/app.py"

# 2. Detect interface if not specified
if [[ -z "$IFACE" ]]; then
  IFACE=$(ssh "$HOST" "ip route get 1.1.1.1 2>/dev/null | awk '/dev/{for(i=1;i<=NF;i++) if(\$i==\"dev\") print \$(i+1)}' | head -1")
  echo "    Detected interface: $IFACE"
fi

# 3. Install systemd service
scp gb10-throttle.service "$HOST":/tmp/cruise-control.service
ssh "$HOST" "
  sudo sed -i 's|ExecStart=.*|ExecStart=/usr/bin/python3 /opt/cruise-control/app.py|' /tmp/cruise-control.service
  sudo sed -i 's|WorkingDirectory=.*|WorkingDirectory=/opt/cruise-control|' /tmp/cruise-control.service
  sudo sed -i 's|Environment=THROTTLE_IFACE=.*|Environment=THROTTLE_IFACE=$IFACE|' /tmp/cruise-control.service
  sudo cp /tmp/cruise-control.service /etc/systemd/system/cruise-control.service
  sudo systemctl daemon-reload
  sudo systemctl enable cruise-control
  sudo systemctl restart cruise-control
"

# 4. Verify
sleep 2
STATUS=$(ssh "$HOST" "sudo systemctl is-active cruise-control 2>/dev/null")
echo "==> Service status: $STATUS"
echo "==> Web UI: http://$(ssh "$HOST" "hostname -I | awk '{print \$1}'"):8090"
