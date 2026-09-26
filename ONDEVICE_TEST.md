# On-device test checklist

For the local session with SSH access to `pi@ender3v2` (Raspberry Pi 3B+, MainsailOS 3).
Branch `main` (merged from `klipper-013-trixie-pi3`). Nothing here has run on real hardware yet;
[CHANGES.md](CHANGES.md) lists what is UNVERIFIED.

## Ground rules

- Do the steps in order. **Stop at the first failure**, collect the log, and report back.
  Do not work around a failure by changing code on the Pi.
- Change wiring **only with the Pi powered off**: `sudo poweroff`, wait for the green LED
  to stop, then unplug.
- For steps that move the printer (6, 7, 8): bed clear, printer idle, someone at the
  printer with a hand on the power switch, and Mainsail open (its Emergency Stop button,
  or `M112` in the console).
- The screen is no longer connected to the mainboard, so Klipper does not depend on this
  service. Stopping it is always safe.
- **Stop at any time:** `sudo systemctl stop dwin-lcd`
- **Full rollback:** `~/DWIN_T5UIC1_LCD/uninstall.sh --purge`, then remove the
  `~/DWIN_T5UIC1_LCD` directory. apt packages and group memberships that were already
  present stay as they were.
- Log commands: `journalctl -u dwin-lcd -f` (live) and
  `journalctl -u dwin-lcd -b --no-pager` (since boot).
- Foreground debug run, which logs every knob step:
  ```bash
  sudo systemctl stop dwin-lcd
  cd /tmp && LG_WD=/tmp python3 ~/DWIN_T5UIC1_LCD/run.py --config ~/printer_data/config/dwin_lcd.conf --log-level debug
  # Ctrl+C to quit (expect "dwinlcd stopping"), then: sudo systemctl start dwin-lcd
  ```

## Step 0: preconditions (no changes)

```bash
hostname; grep PRETTY /etc/os-release; python3 --version
systemctl is-active klipper moonraker
ls -l /dev/serial0 ~/printer_data/comms/moonraker.sock
grep -E '^(enable_uart|dtoverlay)' /boot/firmware/config.txt
grep -o 'console=[^ ]*' /boot/firmware/cmdline.txt
id -nG
```

Expected:
- `ender3v2`, Debian 13, `Python 3.13.5`
- `active` twice
- `/dev/serial0 -> ttyAMA0`, and the socket exists
- `enable_uart=1` and `dtoverlay=disable-bt`
- no `console=serial0` or `console=ttyAMA0`
- `dialout` and `gpio` in the group list

Get the code and run the hardware-free checks:

```bash
cd ~ && git clone https://github.com/tablestead-cmd/DWIN_T5UIC1_LCD.git
cd ~/DWIN_T5UIC1_LCD
python3 -m unittest discover -s tests          # expect: "Ran 87 tests" ... "OK"
python3 tools/check_moonraker.py               # read-only; sends no G-code
```

`tools/check_moonraker.py` asks the **real** Moonraker for every method and field the
display uses. Expect `RESULT: everything the display needs is available`. `WARN` lines
are acceptable. Report every `FAIL` line: these are the UNVERIFIED API assumptions.

Optional: `python3 run.py --mock` shows the menus as text with a simulated printer. Use
`d`/`a` to turn, Enter to press, `q` to quit.

## Step 1: the service starts and logs, with no screen connected

Screen not connected at all.

```bash
cd ~/DWIN_T5UIC1_LCD && ./install.sh
systemctl status dwin-lcd --no-pager
journalctl -u dwin-lcd -n 30 --no-pager
ps -o pid,pcpu,rss,args -C python3 | grep run.py
```

Expected log lines (the order can vary a little; values in `<>` depend on the printer):

```
INFO dwinlcd.app: dwinlcd 2.0.0 starting (Python 3.13.5, config /home/pi/printer_data/config/dwin_lcd.conf)
INFO dwinlcd.encoder: input: pin factory LGPIOFactory; knob A=GPIO19 B=GPIO26 button=GPIO13 pull_up=True reverse=False
WARNING dwinlcd.app: DWIN: no reply from the display on /dev/serial0 at 115200 baud (check display TX -> Pi RX/GPIO15, 5V and GND, baudrate); retrying every 10 s
INFO dwinlcd.moonraker: Moonraker: connected to /home/pi/printer_data/comms/moonraker.sock
INFO dwinlcd.app: Klipper state: ready
INFO dwinlcd.app: Klipper limits: nozzle target <= <max_temp-15> C, bed target <= 100.0 C, probe z_offset <z_offset>
INFO dwinlcd.app: Klipper ready: print state standby, homed axes '', axis max [<x>, <y>, <z>, 0.0]
INFO dwinlcd.app: screen: offline -> idle
```

Pass:
- the service is `active (running)` and no traceback appears
- the pin factory is `LGPIOFactory` (anything else, or an `input: knob/button
  unavailable` error, is a FAIL to report)
- the `Klipper limits` and `axis max` values match printer.cfg
- `pcpu` stays around 1–2 % or less

Resilience checks (printer idle):
- `sudo systemctl restart moonraker`: the log shows `connection lost`,
  `screen: idle -> offline`, then `Moonraker: connected` and `screen: offline -> idle`
  within about 15 s.
- `sudo systemctl restart klipper`: the log shows `Klipper state: disconnected`, then
  `Klipper state: ready` and `screen: offline -> idle`.

Stop or roll back: `sudo systemctl stop dwin-lcd`, or `./uninstall.sh`.

## Step 2: electrical check, then the screen shows the home page

**2a. Measure before connecting any signal line.**
1. Power off the Pi.
2. Connect **only** the screen's 5 V to pin 2 and GND to pin 6. Power on.
3. With a multimeter (black probe on GND), measure at the screen's connector:
   - **TX** (screen → Pi): expect about 3.3 V at idle.
   - **ENT, A, B**: measure with the knob at rest, then again while pressing the knob.

Results:
- Any line above **3.6 V**: **stop**. That line needs a 3.3 V level shifter (or the
  screen's pull-up removed) before it may touch the Pi. With a shifter that has its own
  pull-ups, set `pull_up = false` in `dwin_lcd.conf`.
- About 0 V, or floating: fine. The Pi's internal pull-ups (on by default) are used.

Record the readings in the report.

**2b. Connect the serial lines.**
1. Power off.
2. Connect screen RX → pin 8 (GPIO14) and screen TX → pin 10 (GPIO15).
3. Power on. The service starts at boot.

Expected:
- The screen shows Creality's boot picture, then the home page: title `ender3v2`, the
  logo, four buttons (Print selected) and temperatures at the bottom.
- The log shows
  `INFO dwinlcd.app: DWIN: display found on /dev/serial0 at 115200 baud`.

If the log still says `no reply`:
- Check the TX/RX wiring (a swap is the usual cause), 5 V/GND, and that no serial
  console is enabled.
- As a one-off diagnosis only, set `skip_handshake = true` and restart the service. If
  the screen now draws, the screen → Pi line (TX → pin 10) is the problem. Set it back
  to `false` afterwards.

Report any drawing glitches with a photo: overlapping text, wrong icons or garbled labels
(garbled labels would mean the screen's flash is not the stock DWIN_SET).

Stop or roll back: `sudo systemctl stop dwin-lcd`, then power off and disconnect.

## Step 3: knob rotation and press are logged and move the cursor

Power off. Connect ENT → pin 33 (GPIO13), A → pin 35 (GPIO19) and B → pin 37 (GPIO26).
Power on, then start the foreground debug run (see Ground rules).

1. Turn **one click clockwise**. Expect `DEBUG dwinlcd.app: input: rotate +1 (x1)` and
   the highlight moves from Print to Prepare.
2. One click counter-clockwise. Expect `input: rotate -1 (x1)` and the highlight goes back
   to Print.
3. Ten slow clicks each way. Expect exactly ten `rotate` lines each way, and no step in
   the wrong direction.
4. Press. Expect exactly one `INFO dwinlcd.app: input: button press` and the file list
   opens. Turn to Back and press to return.

Fixes, in `dwin_lcd.conf` (then restart):

| Symptom | Setting |
|---|---|
| Wrong direction | `reverse = true` |
| Two steps per click | `pulses_per_step = 2` |
| One step per two clicks | `pulses_per_step = 8` |
| Double presses | `button_debounce_ms = 50` |
| Occasional backwards steps | `encoder_debounce_ms = 1` |

Record which settings were needed. Press Ctrl+C: the screen should show "Display service
stopped". Then `sudo systemctl start dwin-lcd`.

## Step 4: status shows temperatures and state

With the service running:
1. Compare the bottom area with Mainsail: nozzle `current/target`, bed, speed `100%` and
   Z offset `+0.000`.
2. In Mainsail, set the bed target to 40. Within about 2 s the screen shows `/40`. Set it
   back to 0.
3. Main → Info. Check the build volume (axis max), Klipper `v0.13.0-642-g77d5d942e`,
   Moonraker `v0.10.0-19-g1ed102e` and host `ender3v2`. Press to go back.

Shutdown screen (printer idle):
1. Send `M112` in the Mainsail console. The screen shows "Klipper shutdown", Klipper's
   message, and "Press the knob to run FIRMWARE_RESTART".
2. Press. A confirmation appears with Cancel highlighted. Press again: nothing happens.
3. Press, turn one click counter-clockwise (Confirm highlighted), press. Klipper restarts
   and the home page returns. Log: `confirm 'Run FIRMWARE_RESTART?': yes` and
   `request: printer.firmware_restart`.

## Step 5: preheat and cooldown

1. Prepare → Preheat PLA. Mainsail shows targets 200 / 60. Log:
   `gcode: SET_HEATER_TEMPERATURE HEATER=extruder TARGET=200 | SET_HEATER_TEMPERATURE HEATER=heater_bed TARGET=60`
2. Wait for a few degrees of rise, then Prepare → Cooldown. Targets go to 0. Log:
   `gcode: TURN_OFF_HEATERS | M107`

Preset persistence:
1. Control → Temperature → PLA preheat → Nozzle. Press, turn +5, press, then Save.
   Expect a "Saved" popup.
2. `grep -A3 preheat_1 ~/printer_data/config/dwin_lcd.conf` shows `hotend_temp = 205`.
3. `sudo systemctl restart dwin-lcd`, then check that the menu still shows 205.
4. Set it back to 200 and Save.

Limit clamp:
1. Control → Temperature → Nozzle target. Press, then turn clockwise quickly. The value
   stops at `max_temp - 15`.
2. **Turn it back to 0 before pressing**: pressing applies the shown value.

Rollback: Cooldown, or `TURN_OFF_HEATERS` in Mainsail.

## Step 6: home and a small jog (printer idle, bed clear)

1. Prepare → Move → Move X, press. Expect a "Not available / Home all axes first" popup.
   Press to close.
2. Prepare → Auto home. The popup has Cancel preselected; press: nothing happens.
3. Open Auto home again, turn left, press. The "Homing" popup shows while the printer
   homes (safe_z_home at 155,130 with the BLTouch), then closes. Log:
   `confirm 'Home all axes?': yes` and `gcode: G28`.
4. Move → Move X. Press (the value is highlighted), turn **+10 slow clicks**, press. X
   moves 10 mm; check against Mainsail. Log:
   `gcode: SAVE_GCODE_STATE NAME=_dwin_jog | G91 | G1 X10 F3000 | RESTORE_GCODE_STATE NAME=_dwin_jog`
5. Move Z. Press, turn +50 slow clicks (+5.0 mm), press. Z rises 5 mm.
6. Clamp: Move X, press, turn clockwise fast. The value stops at the axis maximum. Turn
   back to within 10 mm of the current value, then press.
7. Z floor: Move Z, press, turn counter-clockwise. The value stops at `0.0`. Pressing
   below 2 mm asks "Move Z to ... mm?". **Choose Cancel** unless you really want the
   nozzle at the bed.
8. Live position: with the Move menu open, jog X by 10 mm from Mainsail. The Move X
   value follows within a second.
9. Stale-edit guard: start editing Move X (press), turn a few clicks, jog X from
   Mainsail, then press. Expect a "Position changed" popup and **no** move from the
   screen. Log: `jog x refused: toolhead moved from ... to ... while editing`.
10. Prepare → Disable steppers. Motors release, and Move X is refused again.

Rollback: Emergency Stop in Mainsail (then `FIRMWARE_RESTART`), or
`sudo systemctl stop dwin-lcd`.

## Step 7: pause, resume and cancel on a throwaway "air print"

`tests/data/dwin_air_test.gcode` homes, lifts to Z=30 and draws squares in the air. It
has no heating and no extrusion, and takes about 5 minutes.

```bash
cp ~/DWIN_T5UIC1_LCD/tests/data/dwin_air_test.gcode ~/printer_data/gcodes/
```

1. Main → Print → `dwin_air_test` (newest first), then confirm (turn left, press). The
   screen shows "Printing", the file name, progress and times. Log:
   `request: printer.print.start {'filename': 'dwin_air_test.gcode'}`.
2. Only the print screen and Tune are reachable: there is no Prepare or Move.
3. Tune → Print speed: set 90 and press (Mainsail speed factor 90 %), then back to 100.
   Tune → Flow: set 102 (Mainsail extrusion factor 102 %), then back to 100. Back.
4. Middle button, Pause, then confirm. The PAUSE macro parks the head. The title shows
   "Paused" and the middle button becomes Resume. Log: `request: printer.print.pause`.
   An "Extruder not hot enough" console message is expected for a cold print.
5. Resume, then confirm. The head returns and continues. Log: `printer.print.resume`.
6. Stop. The first press with Cancel preselected does nothing. Then turn left and press:
   CANCEL_PRINT runs, a "Print cancelled" popup appears, and a press goes back home. Log:
   `printer.print.cancel`.
7. Start the air test from **Mainsail** while the screen is in Prepare → Move. The screen
   switches to the print screen by itself. Cancel from Mainsail: the screen shows "Print
   cancelled".
8. Optional: let one run finish. The screen shows "Print complete"; press to go home.

Rollback: Cancel from Mainsail, or Emergency Stop.

## Step 8 (optional): Z offset babystep and save

Back up first:
`cp ~/printer_data/config/printer.cfg ~/printer_data/config/printer.cfg.bak-dwin-$(date +%F)`

1. Home (step 6). Prepare → Z offset. "Probe z_offset" shows the value from printer.cfg
   (the SAVE_CONFIG block).
2. Live adjust: press, turn +5 clicks (`+0.050`), press. The confirmation says "the
   nozzle moves up now". Turn left and press. The nozzle rises 0.05 mm, Mainsail shows
   Z offset 0.05, and the bottom area shows `+0.050`. Log:
   `gcode: SET_GCODE_OFFSET Z_ADJUST=0.05 MOVE=1`
3. Fast turning must not speed up this editor: one click is always 0.01 mm.
4. Set the live adjust back to `0.000` and confirm. Save to config is then refused
   ("Adjust the live offset first").
5. Only if you want to change z_offset: set the offset, then Save to config. The
   confirmation shows `z_offset A -> B` and lists any other pending SAVE_CONFIG changes.
   Klipper restarts, and afterwards "Probe z_offset" shows B.

Rollback: copy the backup back over printer.cfg, then `FIRMWARE_RESTART`.

## Step 9: reboot

`sudo reboot`. After boot, the home page appears without intervention.
`journalctl -u dwin-lcd -b --no-pager` shows the step 1 sequence plus `display found`.

## Report back

- The steps passed and failed.
- The step 2a voltages.
- Any config changes that were needed (step 3 table).
- The output of `python3 tools/check_moonraker.py`.
- `journalctl -u dwin-lcd -b --no-pager > ~/dwin-lcd.log`.
- Photos of any screen glitches.
