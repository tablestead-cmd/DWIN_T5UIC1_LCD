# Changes: Klipper 0.13 / MainsailOS 3 (Debian 13) / Pi 3B+ port

Branch `klipper-013-trixie-pi3`, based on odwdinc/DWIN_T5UIC1_LCD `4f825fe` (2024-02-11).
Target: Raspberry Pi 3B+, MainsailOS 3.0.0 (Debian 13 "trixie", arm64), Python 3.13.5,
Klipper v0.13.0-642, Moonraker v0.10.0 (API 1.5.0), Ender 3 V2 with the stock screen wired
to the Pi's GPIO.

Nothing here has run on the real printer, Pi or screen yet. Everything was tested with
unit tests, a simulated screen and a simulated Klipper/Moonraker (see "How this was
tested"). Items that need hardware to confirm are listed under **UNVERIFIED**, and each
one has a step in [ONDEVICE_TEST.md](ONDEVICE_TEST.md).

## Why most of the code was rewritten

Upstream was a line-by-line port of Marlin's DWIN UI running against OctoPrint. Patching
it would not have met the goals, for four reasons:

1. **Wrong API.** `printerInterface.py` called OctoPrint REST endpoints (`/api/files`,
   `/api/printer/printhead`, `/api/printer/tool`, `/api/printer/bed`,
   `/api/printerprofiles/_default`) on port 80. Moonraker only emulates part of OctoPrint,
   and only with `[octoprint_compat]` enabled; file listing, jogging, homing and
   temperatures were not available. It also opened Klipper's API socket at the
   hard-coded `/tmp/klippy_uds`, which does not exist on MainsailOS
   (`~/printer_data/comms/klippy.sock`).
2. **Unsafe threading.** The GPIO callback thread and a `multitimer` thread both drew on
   the screen at the same time, so serial frames could interleave, and both changed
   the menu state.
3. **Dangerous menu paths.** Tune → Bed temperature called `setTargetHotend(bed_value, 0)`,
   which set the *nozzle* to the bed temperature in the middle of a print. Z-offset ran
   `G28` + `PROBE_CALIBRATE` + `TESTZ` from the knob. Jogs used absolute positions from
   a stale copy of the position.
4. **Hangs and crashes.** `while not Handshake(): pass` blocked forever without a screen.
   Missing imports (`time`, `JSONDecodeError`) turned error paths into crashes, and
   `lcdExit` called methods that did not exist.

The screen protocol, the screen geometry, the icon numbers and the stock label bitmaps
were kept. Menus look like the stock UI.

## Changes by area

### Screen driver (`DWIN_Screen.py` → `dwinlcd/dwin.py`, history kept with `git mv`)
- Each command is built as its own frame and buffered; `flush()` writes a whole screen
  update in one call. Why: the old class-level list buffer sent the first frame without
  its `0xAA` header, and each frame took two `write()` calls plus a 1 ms sleep.
- `handshake()` reads for at most 0.5 s and looks for `AA 00 'O' 'K'`. Why: the old loop
  could spin forever, and it could crash on `chr(None)`.
- Numbers are clamped (negative or overflowing values raised `OverflowError`).
  Strings are ASCII-only (non-ASCII file names become `?`) and can be length-limited.
- Removed: the I2C leftovers (`self.bus`), the undefined `_MAX` in
  `Backlight_SetLuminance`, the wrong `0x28` animation-control opcode (Marlin uses 0x29),
  and the unused circle and QR helpers (a circle took thousands of frames).
- `NullLCD`: a no-op stand-in while no screen answers, so the rest keeps working.

### Knob and button (`encoder.py` → `dwinlcd/encoder.py`)
- Uses gpiozero 2.x. On Debian 13 it picks the lgpio pin factory; RPi.GPIO is no longer
  used (it only works there through the rpi-lgpio shim).
- New plain-Python quadrature decoder. It counts valid Gray-code edges and reports one
  step per `pulses_per_step` edges (4 = one full cycle per detent, as in Marlin for
  this screen). Contact bounce cancels out and a missed edge is tolerated. When the knob
  rests for 0.5 s it re-syncs, so it works whether the knob rests with both contacts
  open or both closed.
- Configurable: pins, internal pull-ups on/off, knob direction (`reverse`), edges per
  detent, and debounce times for the knob and the button.
- GPIO callbacks only put events on the app's queue. gpiozero keeps only weak references
  to pin callbacks, so they are bound methods of a long-lived object.
- Fast turning speeds up value editing (×2, ×5). The Z-offset babystep editor never
  speeds up.

### Moonraker connection (`printerInterface.py` → `dwinlcd/moonraker.py`)
- One JSON-RPC 2.0 client for Moonraker's unix socket
  (`/home/pi/printer_data/comms/moonraker.sock`, configurable), with ETX (0x03) framing.
  This replaces both the OctoPrint REST calls and the raw Klipper socket. The socket is
  local, so no API key is needed. Only the standard library is used (no `requests` or
  websocket package).
- A reader thread reconnects with backoff (1, 2, 3, 5, 10 s). Requests time out.
  Pending requests fail cleanly on disconnect. Bad messages are logged and skipped,
  and an unexpected error cannot end the thread for good.
- Status comes from `printer.objects.subscribe` with a small field list (Klipper sends
  diffs about 4× per second). The toolhead position is queried only when needed.

### Printer state and commands (`dwinlcd/printer.py`, new)
- Every command the knob can send is built and checked here:
  - Nothing is sent unless Klipper is `ready`.
  - Home, jog, extrude, steppers off, Z offset and velocity limits are refused while
    printing or paused, and while Klipper is busy (`idle_timeout.state == "Printing"`).
  - Jogs and Z-offset changes need `x`, `y` and `z` homed.
  - Jogs re-read `toolhead.position` right before moving. They use a relative move
    wrapped in `SAVE_GCODE_STATE`/`RESTORE_GCODE_STATE`, clamped to
    `toolhead.axis_minimum`/`axis_maximum` read at run time. Z never goes below 0, even
    if `position_min` is negative.
  - Temperatures are clamped to `configfile.settings.<heater>.max_temp` minus 15 °C
    (nozzle) or 10 °C (bed). While printing, the nozzle target can't go below
    `min_extrude_temp`.
  - Z offset: `SET_GCODE_OFFSET Z_ADJUST=<delta> MOVE=1` (limited to ±`z_offset_limit`,
    default 1 mm). Saving runs `Z_OFFSET_APPLY_PROBE` then `SAVE_CONFIG`. No
    `PROBE_CALIBRATE`, `TESTZ` or `ACCEPT` anywhere (a unit test checks every string
    in the package).
  - Motion menu: `SET_VELOCITY_LIMIT` with `VELOCITY`, `ACCEL`,
    `SQUARE_CORNER_VELOCITY` and `MINIMUM_CRUISE_RATIO`, capped at the printer.cfg values.
    Changes are not saved; "Restore printer.cfg" puts them back.
- Nothing touches the bed mesh: the start G-code's `BED_MESH_PROFILE LOAD=default` is
  left alone.

### Menus (`dwinlcd.py` → `dwinlcd/ui.py`)
- A stack of pages that only the main thread touches. List menus draw text labels
  (upstream used bitmaps of words from the screen's flash), which allows new items.
  The main menu and the print screen keep the stock bitmaps.
- Fixed from upstream's "Not working" list: preheat settings now persist (Save writes
  `dwin_lcd.conf`), and Control → Motion works (see above; it passed icon numbers as row
  numbers and had no handlers).
- Also fixed: Tune → Bed set the nozzle; Tune → Nozzle/Bed never entered edit mode;
  Control → Temperature never sent anything; the Temperature and Motion menus passed icon
  numbers as row numbers, so their icons were never drawn; the language item was
  unreachable and `HMI_ToggleLanguage` did not exist.
- Confirmations (Cancel preselected; turn left for Confirm): Auto home, start print,
  pause, resume, cancel, Z-offset apply, Z-offset save (shows old → new probe z_offset,
  warns about other pending SAVE_CONFIG changes), FIRMWARE_RESTART, and Z jogs to below
  2 mm.
- Refused actions show the reason ("Home all axes first", "Not allowed while
  printing", ...). Items that can't be used right now are shown in gray.
- While printing, only the print screen and Tune are reachable. Tune has print speed
  (M220), flow (M221, new), nozzle and bed temperature. If a print starts from Mainsail
  while a motion menu is open, the screen switches to the print screen and the stack is
  cleared.
- Klipper/Moonraker state screen: "Waiting for Moonraker", "Klipper is starting", or
  Klipper's shutdown/error `state_message` with "Press the knob to run
  FIRMWARE_RESTART" (confirmed first).
- Print end: "Print complete" screen; "Print cancelled" or "Print failed" (with
  `print_stats.message`) popups.
- Remaining time is based on file progress, or on the slicer estimate
  (`server.files.metadata`) early in the print.

### Main loop (`dwinlcd/app.py`, new) and `run.py`
- One thread owns the screen, the menus and the printer state. Moonraker and GPIO
  threads only queue events, so frames never interleave and the UI needs no locks.
- The loop blocks on the queue; there is no busy loop. Timers: status refresh every
  `status_interval` (1 s), Klipper poll every 5 s while it is not ready, screen retry
  every 10 s, screen handshake every 60 s. Only changed values are redrawn: an idle
  printer sends nothing to the screen.
- A missing screen, missing GPIO or missing Moonraker is logged (once, then rate
  limited) and retried; none of them is fatal. Knob input is ignored while no screen
  answers.
- An unexpected exception while handling an event is logged with a traceback, and the
  screen goes back to a safe page instead of the process dying.
- SIGTERM shows "Display service stopped" and exits 0.
- `run.py` ships with the repo (upstream asked users to write one with an OctoPrint API
  key). `python3 -m dwinlcd` also works.

### Configuration (`dwinlcd/config.py`, `dwin_lcd.conf.example`, new)
- `~/printer_data/config/dwin_lcd.conf`, editable in Mainsail. It covers the serial
  port, baud rate, optional backlight, pins, pull-ups, knob direction and edges per
  detent, debounce, Moonraker socket, status interval, jog and extrude speeds, the
  Z-offset limit, the file-list length, and two preheat presets (name, nozzle, bed).
- Bad values are logged and replaced with the defaults. Saving presets edits only the
  `[preheat_N]` values in place (comments are kept), with an atomic replace.

### Packaging (`install.sh`, `uninstall.sh`, `dwin-lcd.service.in`, new; `simpleLCD.service` removed)
- `install.sh` is idempotent. It installs only the missing packages among
  `python3-serial python3-gpiozero python3-lgpio`; there is no pip and no virtualenv,
  because nothing outside apt and the standard library is needed (`multitimer` and
  `requests` are gone). It adds the user to `dialout`/`gpio` if needed, and checks but
  never edits `/boot/firmware/config.txt` and `cmdline.txt`. It copies the example
  config if there is none, then installs, enables and starts the unit.
- `dwin-lcd.service` runs as `pi`, not root, with `Restart=on-failure`, `Nice=10` and
  `After=moonraker.service`. It gets a private runtime directory as lgpio's working
  directory (`LG_WD`). It logs to the journal instead of `/tmp/lcd.log`, and needs no
  `sleep 30`.
- `uninstall.sh` removes the unit and keeps the config unless `--purge` is given.
- The Moonraker `[update_manager]` snippet is documentation only (README).

### Tests and mock mode (new)
- `python3 -m unittest discover -s tests`: 78 tests, standard library only. The gpiozero
  tests use its MockFactory and are skipped if gpiozero is missing.
  - Decoder: direction for both rest states, bounce, missed edges, reverse, half-cycle
    knobs, re-sync.
  - Config: parsing, fallbacks, comment-preserving saves.
  - Screen driver: frame bytes checked against Marlin's encoding, clamping, handshake.
  - State parsing, and every command builder: output, clamps and refusals.
  - The real Moonraker client against a mock unix-socket server: late start, errors,
    notifications, Moonraker restart, timeouts, split messages.
  - Menu flows through the knob against a fake printer: home, jog clamps, Z-offset save
    and Klipper restart, preheat, Tune while printing (including the bed-temperature
    regression), pause/resume/cancel, complete, Klipper shutdown + FIRMWARE_RESTART,
    Klipper error at startup, Moonraker restart, no screen.
  - The on-device "air print" file stays cold and in bounds.
  - `tools/check_moonraker.py` against the mock server.
- `python3 run.py --mock`: a simulated printer and screen driven from the keyboard.
- `tools/check_moonraker.py`: a read-only check (no G-code) that the real Moonraker
  and Klipper provide every method and field the display uses. Run it on the Pi first
  (ONDEVICE step 0); it covers most of UNVERIFIED items 7 and 8 without touching
  hardware.
- CI (`.github/workflows/tests.yml`): Python 3.13 with pyserial 3.5 and gpiozero 2.0.1
  (the Debian 13 versions), plus shellcheck.

### Python 3.13 notes
- Upstream did not use `asyncore`, `asynchat`, `imp` or `distutils`. The real
  blockers were `multitimer` (PyPI only; system-wide pip is blocked by PEP 668 on
  Debian 13), the RPi.GPIO API, missing imports (`time` and `JSONDecodeError` were never
  imported, so error paths raised `NameError`), and `exit()` called from a background
  thread (it only ended that thread).
- The new code only uses the standard library plus python3-serial and python3-gpiozero
  (with python3-lgpio). There are no bare `except:`. The two deliberate broad catches
  (the main loop's crash guard and the Moonraker reader thread) log a traceback.

### Removed
- OctoPrint support (Moonraker only), and the raw Klipper socket.
- The dependencies `multitimer`, `requests` and `RPi.GPIO`.
- `simpleLCD.service` (root, `sleep 30`, log in `/tmp`).
- The unreachable "Leveling" and language items, and the unused "Steps/mm" and "Jerk"
  submenus. Klipper sets steps/mm in printer.cfg (`rotation_distance`) and has no jerk.

## Moonraker / Klipper interface used

- **JSON-RPC methods:** `server.connection.identify` (type `display`; a failure is only
  logged), `server.info`, `printer.info`, `printer.objects.query`,
  `printer.objects.subscribe`, `printer.gcode.script`, `printer.print.start`,
  `printer.print.pause`, `printer.print.resume`, `printer.print.cancel`,
  `printer.firmware_restart`, `server.files.list` (`root=gcodes`),
  `server.files.metadata`.
- **Notifications:** `notify_status_update`, `notify_klippy_ready`,
  `notify_klippy_shutdown`, `notify_klippy_disconnected`.
- **Status fields:**
  - `webhooks.state`, `webhooks.state_message`
  - `print_stats.state`, `print_stats.filename`, `print_stats.print_duration`,
    `print_stats.total_duration`, `print_stats.message`
  - `virtual_sdcard.progress`, `virtual_sdcard.is_active`, `display_status.progress`
  - `toolhead.homed_axes`, `toolhead.axis_minimum`, `toolhead.axis_maximum`,
    `toolhead.max_velocity`, `toolhead.max_accel`, `toolhead.square_corner_velocity`,
    `toolhead.minimum_cruise_ratio`, and `toolhead.position` (queried, not subscribed)
  - `extruder.temperature`, `extruder.target`, `extruder.can_extrude`
  - `heater_bed.temperature`, `heater_bed.target`
  - `gcode_move.speed_factor`, `gcode_move.extrude_factor`, `gcode_move.homing_origin`
  - `fan.speed`, `idle_timeout.state`, `pause_resume.is_paused`
  - `configfile.settings` (printer, extruder, heater_bed, bltouch/probe),
    `configfile.save_config_pending_items`
- **G-code sent:**
  - `G28`, `M84`
  - `SAVE_GCODE_STATE`/`G91`/`M83`/`G1`/`RESTORE_GCODE_STATE`
  - `SET_HEATER_TEMPERATURE`, `TURN_OFF_HEATERS`, `M106`, `M107`
  - `M220`, `M221`
  - `SET_GCODE_OFFSET Z_ADJUST= MOVE=1`, `Z_OFFSET_APPLY_PROBE`, `SAVE_CONFIG`
  - `SET_VELOCITY_LIMIT`
  - Pause, resume and cancel go through Moonraker, which runs Mainsail's
    `PAUSE`/`RESUME`/`CANCEL_PRINT` macros. FIRMWARE_RESTART goes through
    `printer.firmware_restart`.

## How this was tested

- 78 unit and integration tests on Python 3.13 (and 3.11 without gpiozero or pyserial),
  run five times in a row with no flaky results.
- Mock mode driven with scripted keystrokes.
- The service was run as a `pi` user with no screen, no GPIO and Moonraker started
  later. It logged each problem once, connected when the mock Moonraker appeared,
  noticed it leaving, and exited cleanly on SIGTERM.
- `install.sh` was run twice and `uninstall.sh` once for a fake `pi` user, with apt and
  systemctl shimmed. The second run changed nothing and kept user edits.
- Both scripts pass `shellcheck`. The rendered unit passes `systemd-analyze verify`.
- The Moonraker and Klipper documentation sites were not reachable from the build
  environment, so the API details above come from knowledge of Moonraker API 1.x and
  Klipper's status reference. The code tolerates missing fields and logs failed
  requests.

## UNVERIFIED (needs the real hardware)

1. **Electrical:** the logic voltage on the screen's TX, ENT, A and B lines, whether the
   screen board has its own pull-ups (to 5 V?), and the screen's 5 V current draw from
   the Pi header. Do not connect signal lines before measuring (ONDEVICE step 2).
2. **Screen output on the real T5UIC1:** the frame bytes match Marlin's encoding (tested),
   but the result has not been seen on a panel. Unchecked: the handshake timing, the
   init sequence kept from upstream (`JPG_ShowAndCache(0)`, `Frame_SetDir(1)`,
   `JPG_CacheTo1(1)`), and `UpdateLCD` after each batch.
3. **Icons and label bitmaps** assume the stock Creality DWIN_SET in the screen's flash
   (icon library 9, English label picture 1). A screen flashed with another DWIN_SET
   will show wrong icons and labels on the main and print screens.
4. **Layout of the new text elements** (popups, value fields, status screen, info page,
   gray items) has not been seen on the panel. Positions follow the stock grid but may
   need small adjustments.
5. **Knob:** the default direction (B leads A = clockwise = next item; taken from
   upstream's code and Marlin's DWIN encoder), 4 edges per detent, the rest position,
   and the debounce defaults (off for the knob, 30 ms for the button).
6. **gpiozero 2.0.1 / lgpio 0.2.2 on the Pi:** the expected `LGPIOFactory` pin factory,
   edge callbacks, internal pull-ups, lgpio debounce, and that `LG_WD` plus the runtime
   directory keep lgpio's notification files out of read-only directories.
7. **Moonraker API details:**
   - that `identify` accepts type `display` (a failure is harmless);
   - that unix-socket clients need no authentication;
   - that `server.files.list` returns `path` (`filename` is also accepted);
   - that `printer.gcode.script` answers only when the G-code has finished (the
     "Homing..." popup closes on that answer);
   - what Moonraker answers for an in-flight request during `SAVE_CONFIG`'s restart.
8. **Klipper 0.13 fields:**
   - `toolhead.minimum_cruise_ratio` and `configfile.settings.printer.minimum_cruise_ratio`
     (if missing, that Motion item is hidden or skipped);
   - `configfile.settings.bltouch.z_offset` reflecting the SAVE_CONFIG block;
   - `extruder.can_extrude`;
   - whether `min_extrude_temp` appears in `settings` when it is not set in printer.cfg
     (170 is assumed).
9. **Mainsail's PAUSE/RESUME/CANCEL_PRINT macros** when called from the display. Tested
   only against the fake printer; ONDEVICE step 7 uses a cold "air print".
10. **SAVE_CONFIG after Z-offset save:** that the display follows Klipper's restart
    and shows the new probe z_offset.
11. **install.sh and the unit on real MainsailOS 3:** apt, groups, systemd start order,
    `ProtectSystem=full`/`NoNewPrivileges`, and Nice=10.
12. **Load on the Pi 3B+:** CPU use and any effect on Klipper's timing are expected to
    be negligible but were not measured.

## Decisions for Lane

- **Babystepping during a print is not available** (the brief allows only
  pause/resume/cancel, speed/flow and temperatures while printing). First-layer
  babystepping is the most common use of a Z-offset knob. It could be added to Tune
  later behind a config switch, reusing the same confirmed editor.
- **Fan speed is not in Tune** for the same reason (it is in Control → Temperature when
  idle).
- **Preheat defaults** are PLA 200/60 and ABS 240/100 (upstream: 180/60 and 210/100).
  240 °C is near the limit of a stock PTFE-lined hotend; change it in `dwin_lcd.conf`.
- **Z jogs** stop at Z=0 and ask for confirmation below 2 mm.
- **OctoPrint support was dropped.**
