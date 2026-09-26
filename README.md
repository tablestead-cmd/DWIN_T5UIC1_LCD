# DWIN_T5UIC1_LCD

Use the Creality Ender 3 V2's stock screen (DWIN T5UIC1) and its knob with
[Klipper](https://www.klipper3d.org) and [Moonraker](https://github.com/Arksine/moonraker),
by wiring the screen to a Raspberry Pi's GPIO header instead of the printer's mainboard.

This is a fork of [odwdinc/DWIN_T5UIC1_LCD](https://github.com/odwdinc/DWIN_T5UIC1_LCD)
(unmaintained since February 2024), updated for Klipper 0.13, Moonraker (API 1.5),
MainsailOS 3 (Debian 13 "trixie") and Python 3.13. Most of the code was rewritten; the
screen protocol and the look of the stock menus come from upstream and from Marlin's DWIN
UI. [CHANGES.md](CHANGES.md) lists every change and why.

> **Status:** covered by unit tests with a simulated screen and printer, but **not yet
> tested on real hardware**. Follow [ONDEVICE_TEST.md](ONDEVICE_TEST.md) step by step
> the first time.

## What it does

- Talks to Moonraker over its local unix socket (`~/printer_data/comms/moonraker.sock`):
  no API key, no OctoPrint, no extra Python packages.
- Starts before or after Klipper/Moonraker, shows Klipper's shutdown/error message, and
  offers `FIRMWARE_RESTART` behind a confirmation. Reconnects after restarts.
- Motion only when the printer is idle and homed, clamped to the axis limits Klipper
  reports at run time (and never below Z=0). While printing, only speed, flow,
  temperatures and pause/resume/cancel are available.
- Z offset: live babystep with `SET_GCODE_OFFSET Z_ADJUST=… MOVE=1`, saved with
  `Z_OFFSET_APPLY_PROBE` + `SAVE_CONFIG`, each behind a confirmation. The knob never runs
  `PROBE_CALIBRATE` or `TESTZ`, and never touches the bed mesh.
- Preheat presets are stored in `~/printer_data/config/dwin_lcd.conf` (editable in
  Mainsail) and kept across restarts.
- Light on a Pi 3B+: one event loop blocking on a queue, status redrawn at most once a
  second, only changed values are sent to the screen.

## Menus

```
Main ─ Print    : G-code files, newest first ─> confirm ─> print
     ─ Prepare  : Move (X, Y, Z, extrude)   Disable steppers   Auto home
                  Z offset (live adjust, probe z_offset, save to config)
                  Preheat 1   Preheat 2   Cooldown
     ─ Control  : Temperature (nozzle, bed, fan, preheat 1/2 settings + save)
                  Motion (max velocity, max accel, square corner velocity,
                          minimum cruise ratio, restore printer.cfg values)
                  Info
     ─ Info     : build volume, Klipper and Moonraker versions, host name
Printing ─ Tune (print speed, flow, nozzle, bed) ─ Pause/Resume ─ Stop
```

Turn the knob to move, press to open or to start editing a value; turn to change it and
press again to apply. In confirmation popups **Cancel is preselected**: turn the knob
counter-clockwise (left) to select Confirm, then press.

## MainsailOS 3 / Debian 13 / Pi 3B+ install

These steps assume MainsailOS 3.0 on a Raspberry Pi 3 Model B+, user `pi`, and the
standard `~/printer_data` layout. Nothing in Klipper's or Moonraker's configuration is
changed.

### 1. Check the serial port (read-only checks)

The display needs the Pi's full UART (PL011) on GPIO14/15, without a login console:

```bash
grep -E '^(enable_uart|dtoverlay)' /boot/firmware/config.txt   # expect enable_uart=1 and dtoverlay=disable-bt
grep -o 'console=[^ ]*' /boot/firmware/cmdline.txt             # must NOT show console=serial0 or ttyAMA0
ls -l /dev/serial0                                             # expect /dev/serial0 -> ttyAMA0
groups                                                         # expect dialout and gpio in the list
```

If something is missing: add `enable_uart=1` and `dtoverlay=disable-bt` to
`/boot/firmware/config.txt` (note: `/boot/firmware/`, not `/boot/`), remove
`console=serial0,115200` from `/boot/firmware/cmdline.txt`, and reboot.

### 2. Wire the display (Pi powered off)

| Display | Raspberry Pi | Header pin |
|---|---|---|
| RX  | GPIO14 (TXD) | 8 |
| TX  | GPIO15 (RXD) | 10 |
| ENT (knob press) | GPIO13 | 33 |
| A   | GPIO19 | 35 |
| B   | GPIO26 | 37 |
| VCC (5 V) | 5V | 2 |
| GND | GND | 6 |

The Pi's GPIO pins are **3.3 V only**. Before connecting the signal lines, measure the
display's TX, ENT, A and B pins against GND with only 5 V and GND connected (see
[ONDEVICE_TEST.md](ONDEVICE_TEST.md) step 2). Anything above 3.3 V needs a level shifter.
All pins, the serial device and the baud rate can be changed in the config file.

### 3. Install

```bash
cd ~
git clone -b klipper-013-trixie-pi3 https://github.com/tablestead-cmd/DWIN_T5UIC1_LCD.git
cd ~/DWIN_T5UIC1_LCD
python3 -m unittest discover -s tests    # optional: no hardware needed, under a minute on a Pi 3B+
python3 tools/check_moonraker.py         # optional, read-only: does Moonraker have what the display needs?
./install.sh                             # asks for your sudo password
journalctl -u dwin-lcd -f                # watch the log; Ctrl+C stops watching, not the service
```

(After this branch is merged, clone without `-b klipper-013-trixie-pi3`.)

`install.sh` is safe to run again. It:

- installs the missing apt packages among `python3-serial python3-gpiozero python3-lgpio`
  (no pip and no virtualenv: nothing outside apt and the standard library is needed,
  and Debian 13 does not allow system-wide pip installs);
- adds `pi` to the `dialout` and `gpio` groups if needed;
- checks, but never edits, `/boot/firmware/config.txt` and `cmdline.txt`;
- copies `dwin_lcd.conf.example` to `~/printer_data/config/dwin_lcd.conf` if that file
  does not exist yet;
- installs and starts `dwin-lcd.service` (runs as `pi`, `Nice=10`,
  `Restart=on-failure`, ordered after `moonraker.service`).

Options: `--no-start` (install without starting), `--user NAME`,
`--printer-data DIR`.

### 4. Configure

Edit `~/printer_data/config/dwin_lcd.conf` (Mainsail: Machine → Config Files), then
`sudo systemctl restart dwin-lcd`. It is not a Klipper file: do not `[include]` it.
The most likely changes:

- knob moves the wrong way: `[encoder] reverse = true`
- one click moves two rows: `pulses_per_step = 2`; two clicks per row: `8`
- a level shifter with its own pull-ups: `pull_up = false`
- other wiring: `pin_a`, `pin_b`, `pin_button` (BCM numbers), `serial_port`, `baudrate`

Every option is documented in [dwin_lcd.conf.example](dwin_lcd.conf.example).

### 5. Everyday commands

```bash
sudo systemctl status dwin-lcd      # is it running?
journalctl -u dwin-lcd -n 50        # recent log
sudo systemctl restart dwin-lcd     # after editing the config
cd ~/DWIN_T5UIC1_LCD && git pull && ./install.sh   # update
~/DWIN_T5UIC1_LCD/uninstall.sh      # remove the service (keeps the config; --purge deletes it)
```

Run in the foreground with every knob step logged (stop the service first):

```bash
sudo systemctl stop dwin-lcd
cd /tmp && LG_WD=/tmp python3 ~/DWIN_T5UIC1_LCD/run.py --config ~/printer_data/config/dwin_lcd.conf --log-level debug
# Ctrl+C to quit, then: sudo systemctl start dwin-lcd
```

### Optional: Moonraker update manager

Documentation only; the installer does not change `moonraker.conf`. To let Mainsail
update this repository, add this to `~/printer_data/config/moonraker.conf` yourself:

```ini
[update_manager dwin_lcd]
type: git_repo
path: ~/DWIN_T5UIC1_LCD
origin: https://github.com/tablestead-cmd/DWIN_T5UIC1_LCD.git
primary_branch: main
managed_services: dwin-lcd
```

`managed_services` only works if `dwin-lcd` is also listed in
`~/printer_data/moonraker.asvc`; without that line, remove `managed_services` and restart
the service yourself after updates. Until the branch is merged, the checkout is on
`klipper-013-trixie-pi3`, which the update manager will report as not on `main`.

## Trying it without hardware

```bash
python3 run.py --mock                       # simulated printer + screen, keyboard control
python3 -m unittest discover -s tests -v    # the test suite
```

In mock mode, `d`/`a` turn the knob, Enter presses it, and the screen is printed as text.
Nothing touches the real printer, Moonraker or the config file.

## Credits and license

- [odwdinc/DWIN_T5UIC1_LCD](https://github.com/odwdinc/DWIN_T5UIC1_LCD): the original
  Python port (screen protocol, layout, icons), with thanks to
  [wolfstlkr](https://www.reddit.com/r/ender3v2/comments/mdtjvk/octoprint_klipper_v2_lcd/gspae7y).
- [Marlin](https://github.com/MarlinFirmware/Marlin): the DWIN UI this is derived from.
- License: GNU GPL v3, see [LICENSE](LICENSE).
