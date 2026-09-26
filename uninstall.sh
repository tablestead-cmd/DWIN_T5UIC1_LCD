#!/usr/bin/env bash
# Remove the DWIN display service installed by install.sh.
#
#   ./uninstall.sh [--purge] [--printer-data DIR]
#
# Stops and removes the systemd unit. The config file (~/printer_data/config/dwin_lcd.conf)
# is kept unless --purge is given. apt packages are left installed because other software
# (e.g. Klipper's own tools) may use them; group memberships are left as they are.
set -euo pipefail

SERVICE=dwin-lcd
UNIT_FILE="/etc/systemd/system/$SERVICE.service"

usage() {
	awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
}

if [ "$(id -u)" -eq 0 ]; then
	SUDO=()
	SERVICE_USER="${SUDO_USER:-pi}"
else
	SUDO=(sudo)
	SERVICE_USER="$(id -un)"
fi
PURGE=0
USER_HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6 || true)"
PRINTER_DATA="${USER_HOME:-/home/$SERVICE_USER}/printer_data"

while [ $# -gt 0 ]; do
	case "$1" in
		--purge) PURGE=1; shift ;;
		--printer-data) PRINTER_DATA="${2:?--printer-data needs a directory}"; shift 2 ;;
		-h|--help) usage; exit 0 ;;
		*) usage; echo "ERROR: unknown option: $1" >&2; exit 2 ;;
	esac
done

if systemctl list-unit-files "$SERVICE.service" >/dev/null 2>&1 && [ -f "$UNIT_FILE" ]; then
	"${SUDO[@]}" systemctl disable --now "$SERVICE" 2>/dev/null || true
	"${SUDO[@]}" rm -f "$UNIT_FILE"
	"${SUDO[@]}" systemctl daemon-reload
	"${SUDO[@]}" systemctl reset-failed "$SERVICE" 2>/dev/null || true
	echo "removed $UNIT_FILE"
else
	echo "$SERVICE is not installed"
fi

CONFIG_FILE="$PRINTER_DATA/config/dwin_lcd.conf"
if [ -f "$CONFIG_FILE" ]; then
	if [ "$PURGE" -eq 1 ]; then
		rm -f "$CONFIG_FILE"
		echo "removed $CONFIG_FILE"
	else
		echo "kept $CONFIG_FILE (use --purge to delete it)"
	fi
fi
echo "apt packages (python3-serial, python3-gpiozero, python3-lgpio) were left installed."
