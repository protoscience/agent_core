"""Daily 24h weather message to a WhatsApp recipient via OpenClaw.

Renders a text summary with emojis (no image — OpenClaw's WhatsApp media
send is currently unreliable). Intended to run from a systemd timer.
"""
import argparse
import logging
import os
import shlex
import subprocess
import sys

from tools import weather

VPS_HOST = os.environ.get("WEATHER_VPS_HOST", "admin@minions.protoscience.org")
DEFAULT_TARGET = os.environ.get("WEATHER_TARGET", "+19289005070")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("weather-daily")


def _openclaw_send_text(target: str, text: str) -> None:
    # ssh's remote shell re-splits on whitespace, so each arg must be
    # shell-quoted before being joined into the remote command string.
    remote_cmd = shlex.join([
        "openclaw", "message", "send",
        "--channel", "whatsapp",
        "--target", target,
        "--message", text,
    ])
    subprocess.run(
        ["ssh", "-o", "ConnectTimeout=10", VPS_HOST, remote_cmd],
        check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default=DEFAULT_TARGET, help="E.164 WhatsApp recipient")
    parser.add_argument("--dry-run", action="store_true", help="Print the message; skip send")
    args = parser.parse_args()

    data = weather.fetch_forecast()
    view = weather.build_card_view(data)
    message = weather.format_message(view)

    if args.dry_run:
        print(message)
        return

    _openclaw_send_text(args.target, message)
    log.info(f"sent to {args.target} ({view['current_temp']}° {view['current_label']})")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        log.error(f"subprocess failed: {e}")
        sys.exit(1)
