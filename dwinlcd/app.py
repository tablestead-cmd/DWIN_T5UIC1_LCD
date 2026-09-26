# Main loop of the display service.
#
# One thread owns the UI, the display's serial port and the printer state. The Moonraker
# reader thread and the GPIO callback thread only put events on a queue, so screen
# frames never interleave and no locking is needed in the UI. The loop blocks on the
# queue (no busy waiting); timers drive the 1 Hz status refresh, Klipper polling while
# it is not ready, and display reconnects.

import argparse
import logging
import os
import queue
import signal
import sys
import time

from . import __version__
from . import dwin as D
from . import ui as U
from .config import DEFAULT_CONFIG_PATH, LOG_LEVELS, load_config, save_presets
from .dwin import T5UIC1_LCD
from .moonraker import MoonrakerClient, error_message
from .printer import PrinterState, SUBSCRIPTION

log = logging.getLogger(__name__)

POLL_INTERVAL = 5.0  # server.info while Klipper is not ready
LCD_RETRY_INTERVAL = 10.0
LCD_KEEPALIVE_INTERVAL = 60.0
GCODE_EXTENSIONS = (".gcode", ".g", ".gco")
CLIENT_NAME = "DWIN_T5UIC1_LCD"
CLIENT_URL = "https://github.com/tablestead-cmd/DWIN_T5UIC1_LCD"


class Timer:
	def __init__(self, interval, callback, delay=None):
		self.interval = interval
		self.callback = callback
		self.due = time.monotonic() + (interval if delay is None else delay)


class App:
	def __init__(self, cfg, client_factory=MoonrakerClient, serial_factory=None, input_factory=None):
		"""input_factory: None = GPIO via gpiozero, False = no knob, or a callable."""
		self.cfg = cfg
		self.events = queue.Queue()
		self.state = PrinterState()
		self.ui = U.UI(self)
		self.client = client_factory(cfg.moonraker_socket, self._client_event)
		self._serial_factory = serial_factory or self._open_serial
		self._input_factory = input_factory
		self.input = None
		self.port = None
		self.lcd = None
		self.moonraker_connected = False
		self.klippy_ready = False
		self.mode = None
		self._initializing = False
		self._running = False
		self._estimates = {}
		self._last_rotate = 0.0
		self._lcd_failures = 0
		self._lcd_last_log = 0.0
		self._keepalive_due = 0.0
		self._keepalive_failures = 0
		self._timers = [
			Timer(cfg.status_interval, self._tick),
			Timer(POLL_INTERVAL, self._poll),
			Timer(LCD_RETRY_INTERVAL, self._lcd_watchdog),
		]

	@property
	def title(self):
		return self.cfg.title or self.state.hostname or "Klipper"

	# ------------------------------------------------------------------ loop

	def run(self):
		self.start()
		try:
			while self._running:
				self.step(self._next_delay())
		finally:
			self.shutdown()

	def start(self):
		self._running = True
		log.info("dwinlcd %s starting (Python %s, config %s)", __version__, sys.version.split()[0], self.cfg.path)
		self.mode = "offline"
		self.ui.reset(U.StatusScreen(self.ui))
		self.client.start()
		self._start_input()
		self._connect_lcd()

	def stop(self):
		"""Thread- and signal-safe."""
		self.events.put(("quit",))

	def step(self, timeout=0.0):
		try:
			if timeout > 0:
				event = self.events.get(timeout=timeout)
			else:
				event = self.events.get_nowait()
		except queue.Empty:
			event = None
		if event is not None:
			self._handle_batch(event)
		self._run_timers()
		self._flush_lcd()

	def process_events(self):
		"""Handle everything queued (tests and the mock)."""
		while not self.events.empty():
			self.step(0)
		self._flush_lcd()

	def _next_delay(self):
		now = time.monotonic()
		return max(0.0, min(1.0, min(timer.due for timer in self._timers) - now))

	def _run_timers(self):
		now = time.monotonic()
		for timer in self._timers:
			if now >= timer.due:
				timer.due = now + timer.interval
				self._safe(timer.callback)

	def _handle_batch(self, event):
		batch = [event]
		while len(batch) < 200:
			try:
				batch.append(self.events.get_nowait())
			except queue.Empty:
				break
		steps = 0
		for item in batch:
			if item[0] == "rotate":
				steps += item[1]  # consecutive detents become one move / redraw
				continue
			if steps:
				self._safe(self._on_rotate, steps)
				steps = 0
			self._safe(self._dispatch, item)
		if steps:
			self._safe(self._on_rotate, steps)

	def _dispatch(self, event):
		kind = event[0]
		if kind == "press":
			self._on_press()
		elif kind == "mr":
			self._on_moonraker(event[1], event[2])
		elif kind == "call":
			event[1]()
		elif kind == "quit":
			self._running = False

	def _safe(self, func, *args):
		try:
			func(*args)
		except Exception:
			log.exception("internal error; returning to a safe screen")
			self._recover()

	def _recover(self):
		self.mode = None
		try:
			self._update_mode()
		except Exception:
			log.exception("recovery failed")

	def shutdown(self):
		log.info("dwinlcd stopping")
		if self.lcd is not None:
			try:
				lcd = self.lcd
				lcd.Frame_Clear(D.Color_Bg_Black)
				lcd.Draw_String(False, False, D.font8x16, D.Color_White, D.Color_Bg_Black, 44, 220,
					"Display service stopped")
				lcd.UpdateLCD()
				lcd.flush()
			except (OSError, ValueError):
				pass
		if self.input is not None:
			self.input.close()
			self.input = None
		self.client.stop()
		self._close_port()

	# ---------------------------------------------------------------- input

	def _start_input(self):
		if self._input_factory is False:
			log.info("input: knob disabled (--no-gpio)")
			return
		factory = self._input_factory or self._gpio_input
		try:
			self.input = factory(self.cfg, self._post_rotate, self._post_press)
		except Exception as exc:
			log.error("input: knob/button unavailable (%s: %s); the screen will only show status",
				type(exc).__name__, exc)
			self.input = None

	@staticmethod
	def _gpio_input(cfg, on_rotate, on_press):
		from .encoder import GpioInput
		return GpioInput(cfg, on_rotate, on_press)

	def _post_rotate(self, steps):
		self.events.put(("rotate", steps))

	def _post_press(self):
		self.events.put(("press",))

	def _on_rotate(self, steps):
		now = time.monotonic()
		rate = abs(steps) / max(now - self._last_rotate, 0.001)
		self._last_rotate = now
		mult = 5 if rate >= 15 else (2 if rate >= 7 else 1)
		log.debug("input: rotate %+d (x%d)", steps, mult)
		if not self.ui.online:
			log.debug("input: ignored, no display")
			return
		self.ui.rotate(steps, mult)

	def _on_press(self):
		log.info("input: button press")
		if not self.ui.online:
			log.info("input: ignored, no display")
			return
		self.ui.press()

	# ---------------------------------------------------------------- display

	def _open_serial(self):
		import serial  # python3-serial

		return serial.Serial(self.cfg.serial_port, self.cfg.baudrate, timeout=0.05, write_timeout=2.0)

	def _connect_lcd(self):
		cfg = self.cfg
		try:
			if self.port is None:
				self.port = self._serial_factory()
			lcd = T5UIC1_LCD(self.port)
			found = cfg.skip_handshake or lcd.handshake()
		except (OSError, ValueError) as exc:
			self._lcd_failed("cannot use %s (%s)" % (cfg.serial_port, exc))
			self._close_port()
			return
		if not found:
			self._lcd_failed("no reply from the display on %s at %d baud (check display TX -> Pi RX/GPIO15, "
				"5V and GND, baudrate)" % (cfg.serial_port, cfg.baudrate))
			return
		log.info("DWIN: display found on %s at %d baud%s", cfg.serial_port, cfg.baudrate,
			" (handshake skipped)" if cfg.skip_handshake else "")
		self._lcd_failures = 0
		self._keepalive_failures = 0
		self._keepalive_due = time.monotonic() + LCD_KEEPALIVE_INTERVAL
		self.lcd = lcd
		try:
			lcd.init_display(cfg.brightness)
			self.ui.attach(lcd)
			self._flush_lcd()
		except (OSError, ValueError) as exc:
			log.warning("DWIN: write failed (%s)", exc)
			self._drop_lcd()

	def _lcd_failed(self, message):
		self._lcd_failures += 1
		now = time.monotonic()
		if self._lcd_failures == 1 or now - self._lcd_last_log >= 300:
			log.warning("DWIN: %s; retrying every %d s", message, LCD_RETRY_INTERVAL)
			self._lcd_last_log = now
		else:
			log.debug("DWIN: %s", message)

	def _lcd_watchdog(self):
		if self.lcd is None:
			self._connect_lcd()
			return
		if self.cfg.skip_handshake or time.monotonic() < self._keepalive_due:
			return
		self._keepalive_due = time.monotonic() + LCD_KEEPALIVE_INTERVAL
		try:
			found = self.lcd.handshake(timeout=0.3)
		except (OSError, ValueError):
			found = False
		if found:
			self._keepalive_failures = 0
			return
		self._keepalive_failures += 1
		if self._keepalive_failures >= 2:
			log.warning("DWIN: display stopped answering; redrawing when it is back")
			self._drop_lcd()

	def _drop_lcd(self):
		self.lcd = None
		self.ui.detach()

	def _close_port(self):
		if self.port is not None:
			try:
				self.port.close()
			except (OSError, ValueError):
				pass
		self.port = None

	def _flush_lcd(self):
		if self.lcd is None or not self.lcd.pending:
			return
		try:
			self.lcd.UpdateLCD()
			self.lcd.flush()
		except (OSError, ValueError) as exc:
			log.warning("DWIN: write failed (%s)", exc)
			self._drop_lcd()
			self._close_port()

	def _tick(self):
		if self.ui.online:
			self.ui.tick()

	# -------------------------------------------------------------- moonraker

	def _client_event(self, kind, payload):
		self.events.put(("mr", kind, payload))

	def rpc(self, method, params=None, callback=None, timeout=30.0):
		return self.client.call(method, params, callback, timeout)

	def query(self, objects, done=None):
		"""printer.objects.query; merges the result into the state, then done(error)."""
		def finished(result, error):
			if not error and isinstance(result, dict):
				self.state.update(result.get("status") or {})
			if done is not None:
				done(error)
		self.rpc("printer.objects.query", {"objects": objects}, finished, timeout=10)

	def query_position(self, done=None):
		self.query({"toolhead": ["position", "homed_axes", "axis_minimum", "axis_maximum"]}, done)

	def _on_moonraker(self, kind, payload):
		if kind == "response":
			callback, result, error = payload
			callback(result, error)
		elif kind == "notification":
			self._on_notification(*payload)
		elif kind == "connected":
			self._on_connected()
		elif kind == "disconnected":
			self._on_disconnected()

	def _on_connected(self):
		self.moonraker_connected = True
		self.klippy_ready = False
		self._initializing = False
		self.rpc("server.connection.identify", {"client_name": CLIENT_NAME, "version": __version__,
			"type": "display", "url": CLIENT_URL}, self._on_identify, timeout=10)
		self._request_server_info()
		self._update_mode()

	def _on_identify(self, result, error):
		if error:
			log.warning("Moonraker: identify failed (%s); continuing", error_message(error))
		else:
			log.debug("Moonraker: identified as %s", result)

	def _on_disconnected(self):
		self.moonraker_connected = False
		self.klippy_ready = False
		self._initializing = False
		self.state.klippy_state = "disconnected"
		self._update_mode()

	def _request_server_info(self):
		self.rpc("server.info", None, self._on_server_info, timeout=10)

	def _on_server_info(self, result, error):
		if error or not isinstance(result, dict):
			log.warning("Moonraker: server.info failed (%s)", error_message(error))
			return
		self.state.moonraker_version = str(result.get("moonraker_version") or "")
		klippy_state = str(result.get("klippy_state") or "disconnected")
		self._set_klippy_state(klippy_state)
		if klippy_state == "ready":
			self._init_klippy()
		elif result.get("klippy_connected"):
			self.rpc("printer.info", None, self._on_printer_info, timeout=10)

	def _on_printer_info(self, result, error):
		if error or not isinstance(result, dict):
			log.debug("Moonraker: printer.info failed (%s)", error_message(error))
			return
		state = self.state
		state.software_version = str(result.get("software_version") or "")
		state.hostname = str(result.get("hostname") or "")
		state.state_message = str(result.get("state_message") or "").strip()
		klippy_state = result.get("state")
		if klippy_state in ("startup", "shutdown", "error") and klippy_state != state.klippy_state:
			self._set_klippy_state(klippy_state)

	def _set_klippy_state(self, klippy_state):
		previous = self.state.klippy_state
		self.state.klippy_state = klippy_state
		if klippy_state != "ready":
			self.klippy_ready = False
		if klippy_state != previous:
			log.info("Klipper state: %s", klippy_state)
		self._update_mode()

	def _init_klippy(self):
		if self.klippy_ready or self._initializing:
			return
		self._initializing = True
		self.rpc("printer.info", None, self._on_printer_info, timeout=10)
		self.rpc("printer.objects.query", {"objects": {"configfile": ["settings"]}}, self._on_config_settings,
			timeout=20)
		self.rpc("printer.objects.subscribe", {"objects": SUBSCRIPTION}, self._on_subscribed, timeout=20)

	def _on_config_settings(self, result, error):
		if error or not isinstance(result, dict):
			log.warning("Klipper: reading configfile settings failed (%s)", error_message(error))
			return
		settings = (result.get("status") or {}).get("configfile", {}).get("settings")
		self.state.set_config_settings(settings)
		state = self.state
		log.info("Klipper limits: nozzle target <= %s C, bed target <= %s C, probe z_offset %s",
			state.hotend_max_target, state.bed_max_target, state.probe_z_offset)
		if self.ui.online:
			self.ui.tick()

	def _on_subscribed(self, result, error):
		self._initializing = False
		if error or not isinstance(result, dict):
			log.warning("Klipper: subscribe failed (%s); retrying", error_message(error))
			return
		self.state.reset_status()
		self.state.update(result.get("status") or {})
		self.klippy_ready = self.state.klippy_state == "ready"
		log.info("Klipper ready: print state %s, homed axes '%s', axis max %s", self.state.print_state,
			self.state.homed_axes, self.state.get("toolhead", "axis_maximum"))
		self._update_mode()

	def _on_notification(self, method, params):
		if method == "notify_status_update":
			if isinstance(params, list) and params:
				self.state.update(params[0])
				self._after_status_update()
		elif method == "notify_klippy_ready":
			self._set_klippy_state("ready")
			self._init_klippy()
		elif method == "notify_klippy_shutdown":
			self._set_klippy_state("shutdown")
			self.rpc("printer.info", None, self._on_printer_info, timeout=10)
		elif method == "notify_klippy_disconnected":
			self.state.state_message = ""
			self._set_klippy_state("disconnected")

	def _after_status_update(self):
		webhooks = self.state.status.get("webhooks", {})
		webhooks_state = webhooks.get("state")
		if webhooks_state in ("shutdown", "error") and self.state.klippy_state != webhooks_state:
			self.state.state_message = str(webhooks.get("state_message") or "").strip()
			self._set_klippy_state(webhooks_state)
			return
		self._update_mode()

	def _poll(self):
		if self.moonraker_connected and not self.klippy_ready and not self._initializing:
			self._request_server_info()

	# ------------------------------------------------------------ screen mode

	def _compute_mode(self):
		if not (self.moonraker_connected and self.klippy_ready and self.state.ready):
			return "offline"
		return "printing" if self.state.is_printing else "idle"

	def _update_mode(self):
		mode = self._compute_mode()
		if mode == self.mode:
			return
		previous, self.mode = self.mode, mode
		log.info("screen: %s -> %s", previous, mode)
		ui = self.ui
		if mode == "offline":
			ui.reset(U.StatusScreen(ui))
		elif mode == "printing":
			self._fetch_estimate(self.state.filename)
			ui.reset(U.PrintPage(ui))
		else:
			print_state = self.state.print_state
			if previous == "printing" and print_state == "complete":
				ui.reset(U.MainMenu(ui), U.PrintPage(ui, done=True))
			else:
				ui.reset(U.MainMenu(ui))
				name = os.path.basename(self.state.filename)
				if previous == "printing" and print_state == "cancelled":
					ui.message("Print cancelled", name)
				elif previous == "printing" and print_state == "error":
					ui.message("Print failed", str(self.state.get("print_stats", "message", "")) or name)

	def go_home(self):
		self.ui.reset(U.MainMenu(self.ui))

	# ------------------------------------------------------------------ files

	def list_files(self, done):
		"""done(paths, error): G-code files, newest first."""
		def finished(result, error):
			if error:
				done(None, error_message(error))
				return
			files = []
			for entry in result if isinstance(result, list) else []:
				if not isinstance(entry, dict):
					continue
				path = str(entry.get("path") or entry.get("filename") or "")
				if not path.lower().endswith(GCODE_EXTENSIONS) or os.path.basename(path).startswith("."):
					continue
				try:
					modified = float(entry.get("modified") or 0)
				except (TypeError, ValueError):
					modified = 0.0
				files.append((modified, path))
			files.sort(reverse=True)
			done([path for _modified, path in files[:self.cfg.max_files]], None)
		self.rpc("server.files.list", {"root": "gcodes"}, finished, timeout=20)

	def _fetch_estimate(self, filename):
		if not filename or filename in self._estimates:
			return
		self._estimates[filename] = None

		def finished(result, error):
			if not error and isinstance(result, dict):
				try:
					self._estimates[filename] = float(result.get("estimated_time"))
				except (TypeError, ValueError):
					pass
		self.rpc("server.files.metadata", {"filename": filename}, finished, timeout=10)

	def file_estimate(self, filename):
		return self._estimates.get(filename)

	def save_presets(self):
		try:
			save_presets(self.cfg.path, self.cfg.presets)
		except OSError as exc:
			log.error("config: saving presets to %s failed: %s", self.cfg.path, exc)
			self.ui.message("Save failed", str(exc))
			return
		log.info("config: presets saved to %s", self.cfg.path)
		self.ui.message("Saved", ["Preheat presets written to", os.path.basename(self.cfg.path)])


def _setup_logging(level):
	if sys.stderr.isatty():
		pattern = "%(asctime)s %(levelname)s %(name)s: %(message)s"
	else:
		pattern = "%(levelname)s %(name)s: %(message)s"  # journald adds timestamps
	logging.basicConfig(level=level, format=pattern)


def main(argv=None):
	parser = argparse.ArgumentParser(prog="dwinlcd",
		description="Ender 3 V2 DWIN T5UIC1 display for Klipper via Moonraker")
	parser.add_argument("-c", "--config", default=DEFAULT_CONFIG_PATH,
		help="config file (default: %(default)s)")
	parser.add_argument("--log-level", type=str.upper, choices=LOG_LEVELS,
		help="override [general] log_level")
	parser.add_argument("--no-gpio", action="store_true", help="run without the knob and button")
	parser.add_argument("--mock", action="store_true",
		help="simulated printer and display, controlled from the keyboard (no hardware needed)")
	parser.add_argument("--version", action="version", version=__version__)
	args = parser.parse_args(argv)

	_setup_logging(logging.INFO)
	cfg = load_config(args.config)
	logging.getLogger().setLevel(args.log_level or cfg.log_level)

	if args.mock:
		from .mock import run_interactive
		return run_interactive(cfg)

	app = App(cfg, input_factory=False if args.no_gpio else None)
	signal.signal(signal.SIGTERM, lambda *_args: app.stop())
	signal.signal(signal.SIGINT, lambda *_args: app.stop())
	app.run()
	return 0
