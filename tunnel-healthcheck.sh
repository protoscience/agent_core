#!/usr/bin/env bash
# Detects when the reverse SSH tunnel to the VPS has gone zombie:
# autossh's ssh child may keep the TCP session open while the forward
# channel is silently broken. We probe the bridge from the VPS side and
# restart trading-ssh-tunnel.service if it's unreachable.
set -u

VPS_HOST="admin@minions.protoscience.org"
BRIDGE_URL="http://localhost:4000/"
TUNNEL_UNIT="trading-ssh-tunnel.service"

if ssh -o BatchMode=yes -o ConnectTimeout=5 -o ServerAliveInterval=5 -o ServerAliveCountMax=2 \
       "$VPS_HOST" "curl -sS -o /dev/null --max-time 5 '$BRIDGE_URL'" >/dev/null 2>&1; then
    exit 0
fi

echo "tunnel-healthcheck: VPS cannot reach $BRIDGE_URL — restarting $TUNNEL_UNIT"
systemctl --user restart "$TUNNEL_UNIT"
