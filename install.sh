#!/usr/bin/env bash
# Install the DWIN display service on MainsailOS 3 / Debian 13 (trixie) on a Raspberry Pi.
#
#   ./install.sh [--user NAME] [--printer-data DIR] [--no-start]
#
# Run it as the printer user (pi); it asks for sudo where needed. Safe to run again:
# it installs only missing apt packages, never overwrites an existing dwin_lcd.conf,
# and does not touch Klipper, Moonraker or the boot files (it only checks them).
# Everything it needs comes from apt and the Python standard library, so no pip and
# no virtualenv are used (Debian 13 blocks system-wide pip installs, PEP 668).
set -euo pipefail

SERVICE=dwin-lcd
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APT_PACKAGES=(python3 python3-serial python3-gpiozero python3-lgpio)

usage() {
	awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
}

step() { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

if [ "$(id -u)" -eq 0 ]; then
	SUDO=()
	SERVICE_USER="${SUDO_USER:-}"
else
	SUDO=(sudo)
	SERVICE_USER="$(id -un)"
fi
PRINTER_DATA=""
START=1

while [ $# -gt 0 ]; do
	case "$1" in
		--user) SERVICE_USER="${2:?--user needs a name}"; shift 2 ;;
		--printer-data) PRINTER_DATA="${2:?--printer-data needs a directory}"; shift 2 ;;
		--no-start) START=0; shift ;;
		-h|--help) usage; exit 0 ;;
		*) usage; die "unknown option: $1" ;;
	esac
done

if [ -z "$SERVICE_USER" ] || [ "$SERVICE_USER" = "root" ]; then
	die "run this as the printer user (e.g. pi): ./install.sh  (or pass --user pi)"
fi
id "$SERVICE_USER" >/dev/null 2>&1 || die "user '$SERVICE_USER' does not exist"
SERVICE_GROUP="$(id -gn "$SERVICE_USER")"
USER_HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
PRINTER_DATA="${PRINTER_DATA:-$USER_HOME/printer_data}"
CONFIG_FILE="$PRINTER_DATA/config/dwin_lcd.conf"
MOONRAKER_SOCKET="$PRINTER_DATA/comms/moonraker.sock"
UNIT_FILE="/etc/systemd/system/$SERVICE.service"

step "Checking the system"
if [ -r /etc/os-release ]; then
	# shellcheck disable=SC1091
	. /etc/os-release
	echo "OS: ${PRETTY_NAME:-unknown} ($(uname -m)), user $SERVICE_USER, repo $REPO_DIR"
	[ "${VERSION_CODENAME:-}" = "trixie" ] || warn "tested for Debian 13 (trixie); this is ${VERSION_CODENAME:-unknown}"
fi
[ -d "$PRINTER_DATA/config" ] || die "$PRINTER_DATA/config not found (use --printer-data DIR)"
case "$REPO_DIR" in
	"$USER_HOME"/*) ;;
	*) warn "the repo is outside $USER_HOME; make sure $SERVICE_USER can read $REPO_DIR" ;;
esac

step "Installing apt packages (only missing ones)"
missing=()
for package in "${APT_PACKAGES[@]}"; do
	dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q "install ok installed" || missing+=("$package")
done
if [ ${#missing[@]} -gt 0 ]; then
	echo "installing: ${missing[*]}"
	"${SUDO[@]}" apt-get update
	"${SUDO[@]}" apt-get install -y --no-install-recommends "${missing[@]}"
else
	echo "already installed: ${APT_PACKAGES[*]}"
fi
python3 -c 'import serial, gpiozero, lgpio' || die "python3 cannot import serial/gpiozero/lgpio"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 7))' || die "Python 3.7 or newer is required"

step "Checking groups (serial port and GPIO access)"
for group in dialout gpio; do
	if ! getent group "$group" >/dev/null; then
		warn "group '$group' does not exist"
	elif id -nG "$SERVICE_USER" | tr ' ' '\n' | grep -qx "$group"; then
		echo "$SERVICE_USER is in $group"
	else
		"${SUDO[@]}" usermod -aG "$group" "$SERVICE_USER"
		echo "added $SERVICE_USER to $group (the service picks this up when it starts)"
	fi
done

step "Checking the serial port setup (read only; this script does not edit boot files)"
BOOT_DIR=/boot/firmware
[ -f "$BOOT_DIR/config.txt" ] || BOOT_DIR=/boot
if [ -f "$BOOT_DIR/config.txt" ]; then
	grep -Eq '^[[:space:]]*enable_uart=1' "$BOOT_DIR/config.txt" ||
		warn "enable_uart=1 is missing from $BOOT_DIR/config.txt"
	grep -Eq '^[[:space:]]*dtoverlay=(pi3-)?disable-bt' "$BOOT_DIR/config.txt" ||
		warn "dtoverlay=disable-bt is missing from $BOOT_DIR/config.txt (/dev/serial0 may be the mini UART)"
fi
if [ -f "$BOOT_DIR/cmdline.txt" ] && grep -Eq 'console=(serial0|ttyAMA0|ttyS0)' "$BOOT_DIR/cmdline.txt"; then
	warn "a serial console is enabled in $BOOT_DIR/cmdline.txt; it will fight with the display"
fi
if [ -e /dev/serial0 ]; then
	echo "/dev/serial0 -> $(readlink -f /dev/serial0)"
else
	warn "/dev/serial0 does not exist; set serial_port in $CONFIG_FILE"
fi
[ -e /dev/gpiochip0 ] || warn "/dev/gpiochip0 not found; the knob will not work"
[ -S "$MOONRAKER_SOCKET" ] || warn "$MOONRAKER_SOCKET not found (is Moonraker running?); the service waits for it"

step "Config file"
if [ -f "$CONFIG_FILE" ]; then
	echo "keeping existing $CONFIG_FILE"
else
	tmp_config="$(mktemp)"
	sed "s#/home/pi/printer_data/comms/moonraker.sock#$MOONRAKER_SOCKET#" "$REPO_DIR/dwin_lcd.conf.example" > "$tmp_config"
	"${SUDO[@]}" install -m 644 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$tmp_config" "$CONFIG_FILE"
	rm -f "$tmp_config"
	echo "created $CONFIG_FILE (editable in Mainsail)"
fi

if [ -f /lib/systemd/system/simpleLCD.service ] || [ -f /etc/systemd/system/simpleLCD.service ]; then
	warn "the old simpleLCD.service is installed and would fight over the serial port."
	warn "disable it with: sudo systemctl disable --now simpleLCD.service"
fi

step "systemd unit $UNIT_FILE"
tmp_unit="$(mktemp)"
sed -e "s#@USER@#$SERVICE_USER#g" -e "s#@GROUP@#$SERVICE_GROUP#g" -e "s#@REPO@#$REPO_DIR#g" \
	-e "s#@CONFIG@#$CONFIG_FILE#g" "$REPO_DIR/dwin-lcd.service.in" > "$tmp_unit"
if [ -f "$UNIT_FILE" ] && cmp -s "$tmp_unit" "$UNIT_FILE"; then
	echo "unit is up to date"
else
	"${SUDO[@]}" install -m 644 "$tmp_unit" "$UNIT_FILE"
	"${SUDO[@]}" systemctl daemon-reload
	echo "unit installed"
fi
rm -f "$tmp_unit"
"${SUDO[@]}" systemctl enable "$SERVICE" >/dev/null 2>&1 && echo "enabled at boot"

if [ "$START" -eq 1 ]; then
	step "Starting $SERVICE"
	"${SUDO[@]}" systemctl restart "$SERVICE"
	sleep 3
	systemctl --no-pager --lines=0 status "$SERVICE" || true
	echo
	journalctl -u "$SERVICE" -n 20 --no-pager 2>/dev/null || "${SUDO[@]}" journalctl -u "$SERVICE" -n 20 --no-pager || true
else
	echo "not started (--no-start). Start with: sudo systemctl start $SERVICE"
fi

cat <<EOF

Done.
  Config:  $CONFIG_FILE  (restart after changes: sudo systemctl restart $SERVICE)
  Logs:    journalctl -u $SERVICE -f
  Stop:    sudo systemctl stop $SERVICE
  Remove:  $REPO_DIR/uninstall.sh
EOF
