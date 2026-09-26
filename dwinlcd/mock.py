# Hardware-free doubles for tests and for trying the menus without a printer:
#   MockSerial / VirtualScreen  - a DWIN display on a fake serial port, with a text model
#                                 of what would be on screen;
#   FakePrinter                 - a small simulation of Klipper objects and Moonraker methods;
#   MockMoonrakerServer         - FakePrinter behind a unix socket speaking Moonraker's
#                                 JSON-RPC + ETX framing (exercises the real client);
#   FakeClient                  - in-process replacement for MoonrakerClient (fast tests);
#   run_interactive()           - `python3 run.py --mock`: drive the menus from the keyboard.

import copy
import json
import logging
import os
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time

from . import dwin as D
from . import ui as U
from .moonraker import ETX, make_error

log = logging.getLogger(__name__)

ICON_NAMES = {U.ICON_Confirm_E: "[Confirm]", U.ICON_Cancel_E: "[Cancel]"}


# -------------------------------------------------------------------- display

class VirtualScreen:
	"""Approximate model of the screen: strings, stock labels, cursor and selections."""

	def __init__(self):
		self.items = {}  # (x, y) -> (kind, text)
		self.frames = 0
		self.updates = 0

	def _fill(self, x1, y1, x2, y2):
		for x, y in list(self.items):
			if x1 <= x <= x2 and y1 <= y <= y2:
				del self.items[(x, y)]

	def apply(self, cmd, payload):
		self.frames += 1

		def word(offset):
			return int.from_bytes(payload[offset:offset + 2], "big")

		if cmd in (0x01, 0x22):  # clear screen / show boot picture
			self.items.clear()
		elif cmd == 0x05:  # rectangle: mode, colour, x1, y1, x2, y2
			mode, color = payload[0], word(1)
			x1, y1, x2, y2 = word(3), word(5), word(7), word(9)
			if mode == 1:
				self._fill(x1, y1, x2, y2)
				if color == D.Rectangle_Color and x1 == 0:
					self.items[(0, y1 + 18)] = ("cursor", ">")
			elif color == D.Select_Color:
				self.items[(x1, y1)] = ("sel", "*")
			elif self.items.get((x1, y1), ("",))[0] == "sel":
				del self.items[(x1, y1)]
		elif cmd == 0x11:  # string: flags, colour, background, x, y, text
			self.items[(word(5), word(7))] = ("text", payload[9:].decode("ascii", "replace"))
		elif cmd == 0x23:  # icon: x, y, library, id
			x, y = word(0), word(2)
			if self.items.get((x, y), ("",))[0] == "sel":
				del self.items[(x, y)]
			name = ICON_NAMES.get(payload[5])
			if name:
				self.items[(x, y)] = ("icon", name)
		elif cmd == 0x27:  # area copy: cache, source rectangle, x, y
			name = U.LABEL_NAMES.get((word(1), word(3), word(5), word(7)))
			if name:
				self.items[(word(9), word(11))] = ("label", "[%s]" % name)
		elif cmd == 0x3D:
			self.updates += 1

	def lines(self):
		"""Screen content as text lines, top to bottom (items within 6 px share a line)."""
		result = []
		current_y = None
		current = []
		for (x, y), (_kind, text) in sorted(self.items.items(), key=lambda item: (item[0][1], item[0][0])):
			if current_y is None or y - current_y > 6:
				if current:
					result.append(current)
				current_y, current = y, []
			current.append((x, text))
		if current:
			result.append(current)
		lines = []
		for line in result:
			words = []
			for _x, text in sorted(line):
				text = text.strip()
				if text and not (text == "*" and words and words[-1] == "*"):
					words.append(text)
			lines.append(" ".join(words))
		return lines

	def text(self):
		return "\n".join(self.lines())

	def has(self, text):
		return any(text in line for line in self.lines())

	def cursor_line(self):
		for line in self.lines():
			if line.startswith(">"):
				return line[1:].strip()
		return None

	def confirm_selected(self):
		"""True/False for a Confirm popup's highlighted button, None if no popup."""
		if (25, 279) in self.items or (145, 279) in self.items:
			return (25, 279) in self.items
		return None


class MockSerial:
	"""pyserial-like port with a DWIN display on the other end."""

	def __init__(self, screen=None, present=True):
		self.screen = screen or VirtualScreen()
		self.present = present
		self.is_open = True
		self.writes = 0
		self.bytes_written = 0
		self._rx = bytearray()
		self._tx = bytearray()

	@property
	def in_waiting(self):
		return len(self._rx)

	def write(self, data):
		if not self.is_open:
			raise OSError("port closed")
		self.writes += 1
		self.bytes_written += len(data)
		self._tx += data
		while True:
			end = self._tx.find(D.TAIL)
			if end < 0:
				break
			frame = bytes(self._tx[:end])
			del self._tx[:end + len(D.TAIL)]
			if len(frame) >= 2 and frame[0] == D.FHONE:
				if frame[1] == 0x00:
					if self.present:
						self._rx += D.HANDSHAKE_REPLY + D.TAIL
				elif self.present:
					self.screen.apply(frame[1], frame[2:])
		return len(data)

	def read(self, size=1):
		if not self._rx:
			time.sleep(0.002)  # behave like a port timeout without spinning
			return b""
		data = bytes(self._rx[:size])
		del self._rx[:size]
		return data

	def reset_input_buffer(self):
		self._rx.clear()

	def flush(self):
		pass

	def close(self):
		self.is_open = False


# -------------------------------------------------------------------- printer

class FakeError(Exception):
	pass


AMBIENT = 23.0


def default_status():
	return {
		"webhooks": {"state": "ready", "state_message": "Printer is ready"},
		"print_stats": {"state": "standby", "filename": "", "print_duration": 0.0, "total_duration": 0.0,
			"message": "", "filament_used": 0.0},
		"virtual_sdcard": {"progress": 0.0, "is_active": False, "file_position": 0},
		"display_status": {"progress": 0.0, "message": None},
		"toolhead": {"homed_axes": "", "axis_minimum": [0.0, 0.0, -2.0, 0.0],
			"axis_maximum": [235.0, 235.0, 250.0, 0.0], "position": [0.0, 0.0, 0.0, 0.0],
			"max_velocity": 300.0, "max_accel": 4500.0, "square_corner_velocity": 5.0,
			"minimum_cruise_ratio": 0.5},
		"extruder": {"temperature": 24.0, "target": 0.0, "power": 0.0, "can_extrude": False,
			"pressure_advance": 0.85},
		"heater_bed": {"temperature": AMBIENT, "target": 0.0, "power": 0.0},
		"gcode_move": {"speed_factor": 1.0, "extrude_factor": 1.0, "homing_origin": [0.0, 0.0, 0.0, 0.0],
			"absolute_coordinates": True, "absolute_extrude": True},
		"fan": {"speed": 0.0, "rpm": None},
		"idle_timeout": {"state": "Idle", "printing_time": 0.0},
		"pause_resume": {"is_paused": False},
		"configfile": {
			"settings": {
				"printer": {"kinematics": "cartesian", "max_velocity": 300.0, "max_accel": 4500.0,
					"square_corner_velocity": 5.0, "minimum_cruise_ratio": 0.5, "max_z_velocity": 5.0},
				"extruder": {"max_temp": 260.0, "min_temp": 0.0, "min_extrude_temp": 170.0,
					"pressure_advance": 0.85},
				"heater_bed": {"max_temp": 110.0, "min_temp": 0.0},
				"bltouch": {"z_offset": 2.5, "x_offset": -44.0, "y_offset": -6.0},
				"safe_z_home": {"home_xy_position": [155.0, 130.0], "z_hop": 10.0},
			},
			"save_config_pending": False,
			"save_config_pending_items": {},
		},
	}


class FakePrinter:
	"""Enough of Klipper + Moonraker to exercise the display without a printer."""

	def __init__(self):
		self.lock = threading.RLock()
		self.listeners = []  # callables(method, params) for notifications
		self.klippy_state = "ready"
		self.state_message = "Printer is ready"
		self.status = default_status()
		self.subscription = None
		self.files = [
			{"path": "calibration_cube.gcode", "modified": 1758800000.0, "size": 102400, "permissions": "rw"},
			{"path": "parts/bracket_v2.gcode", "modified": 1758900000.0, "size": 204800, "permissions": "rw"},
			{"path": "notes.txt", "modified": 1758950000.0, "size": 10, "permissions": "rw"},
		]
		self.scripts = []  # every G-code line received
		self.requests = []  # every (method, params)
		self.fail_commands = {}  # G-code word -> error message (tests)
		self.fail_queries = set()  # object names whose next printer.objects.query fails once
		self.print_seconds = 120.0  # simulated print length for advance()
		self.pending_config = {}
		self.config_saves = 0
		self._relative = False
		self._relative_e = False
		self._saved_states = {}

	# ------------------------------------------------------------ plumbing

	def notify(self, method, params=None):
		for listener in list(self.listeners):
			listener(method, params)

	def _changed(self, diff):
		for name, fields in diff.items():
			self.status.setdefault(name, {}).update(copy.deepcopy(fields))
		if self.subscription is None or self.klippy_state not in ("ready", "shutdown"):
			return
		filtered = {}
		for name, fields in diff.items():
			if name not in self.subscription:
				continue
			wanted = self.subscription[name]
			selected = {key: value for key, value in fields.items() if not wanted or key in wanted}
			if selected:
				filtered[name] = copy.deepcopy(selected)
		if filtered:
			self.notify("notify_status_update", [filtered, time.monotonic()])

	def set(self, name, **fields):
		"""Change status fields as Klipper would (sends a notification if subscribed)."""
		with self.lock:
			self._changed({name: fields})
			self._update_can_extrude()

	def handle(self, method, params):
		"""Run a Moonraker JSON-RPC method: returns (result, error)."""
		with self.lock:
			self.requests.append((method, copy.deepcopy(params)))
			handler = getattr(self, "_m_" + str(method).replace(".", "_"), None)
			if handler is None:
				return None, make_error("Method not found: %s" % method, -32601)
			try:
				return handler(params or {}), None
			except FakeError as exc:
				return None, make_error(str(exc), 400)

	def _require_klippy(self):
		if self.klippy_state == "disconnected":
			raise FakeError("Klippy Disconnected")

	def _require_ready(self):
		self._require_klippy()
		if self.klippy_state != "ready":
			raise FakeError("Klippy is not ready: %s" % self.klippy_state)

	def _select(self, objects):
		result = {}
		for name, fields in (objects or {}).items():
			if name not in self.status:
				continue
			values = self.status[name]
			result[name] = copy.deepcopy(values if not fields else
				{key: values[key] for key in fields if key in values})
		return result

	# ------------------------------------------------------------ methods

	def _m_server_connection_identify(self, params):
		return {"connection_id": 1}

	def _m_server_info(self, params):
		return {"klippy_connected": self.klippy_state != "disconnected", "klippy_state": self.klippy_state,
			"components": ["file_manager", "klippy_apis"], "failed_components": [], "warnings": [],
			"websocket_count": 1, "moonraker_version": "v0.10.0-mock", "api_version": [1, 5, 0],
			"api_version_string": "1.5.0"}

	def _m_printer_info(self, params):
		self._require_klippy()
		return {"state": self.klippy_state, "state_message": self.state_message, "hostname": "mockpi",
			"software_version": "v0.13.0-mock", "klipper_path": "/home/pi/klipper"}

	def _m_printer_objects_query(self, params):
		self._require_klippy()
		failing = self.fail_queries.intersection(params.get("objects") or {})
		if failing:
			self.fail_queries.difference_update(failing)
			raise FakeError("simulated query failure")
		return {"eventtime": time.monotonic(), "status": self._select(params.get("objects"))}

	def _m_printer_objects_subscribe(self, params):
		self._require_klippy()
		objects = params.get("objects") or {}
		self.subscription = {name: list(fields or []) for name, fields in objects.items()}
		return {"eventtime": time.monotonic(), "status": self._select(objects)}

	def _m_printer_gcode_script(self, params):
		self._require_ready()
		for line in str(params.get("script", "")).splitlines():
			self._gcode(line)
		return "ok"

	def _m_printer_print_start(self, params):
		self._require_ready()
		self.start_print(params.get("filename", ""))
		return "ok"

	def _m_printer_print_pause(self, params):
		self._require_ready()
		self._g_PAUSE({})
		return "ok"

	def _m_printer_print_resume(self, params):
		self._require_ready()
		self._g_RESUME({})
		return "ok"

	def _m_printer_print_cancel(self, params):
		self._require_ready()
		self._g_CANCEL_PRINT({})
		return "ok"

	def _m_printer_firmware_restart(self, params):
		self._require_klippy()
		self.restart()
		return "ok"

	def _m_printer_emergency_stop(self, params):
		self._require_klippy()
		self.shutdown("Shutdown due to webhooks request")
		return "ok"

	def _m_server_files_list(self, params):
		return copy.deepcopy(self.files)

	def _m_server_files_metadata(self, params):
		return {"filename": params.get("filename"), "estimated_time": 1800.0, "slicer": "MockSlicer"}

	# ------------------------------------------------------------ G-code

	def _gcode(self, line):
		line = line.split(";", 1)[0].strip()
		if not line:
			return
		self.scripts.append(line)
		parts = line.split()
		word = parts[0].upper()
		if word in self.fail_commands:
			raise FakeError(self.fail_commands[word])
		args = {}
		for part in parts[1:]:
			if "=" in part:
				key, value = part.split("=", 1)
				args[key.upper()] = value
			else:
				args[part[0].upper()] = part[1:]
		handler = getattr(self, "_g_" + word, None)
		if handler is None:
			raise FakeError('Unknown command:"%s"' % word)
		handler(args)

	def _position(self):
		return list(self.status["toolhead"]["position"])

	def _g_G28(self, args):
		position = self._position()
		position[:3] = [155.0, 130.0, 10.0]
		self._changed({"toolhead": {"homed_axes": "xyz", "position": position}})

	def _g_M84(self, args):
		self._changed({"toolhead": {"homed_axes": ""}})

	_g_M18 = _g_M84

	def _g_G90(self, args):
		self._relative = False

	def _g_G91(self, args):
		self._relative = True

	def _g_M82(self, args):
		self._relative_e = False

	def _g_M83(self, args):
		self._relative_e = True

	def _g_SAVE_GCODE_STATE(self, args):
		self._saved_states[args.get("NAME", "default")] = (self._relative, self._relative_e)

	def _g_RESTORE_GCODE_STATE(self, args):
		name = args.get("NAME", "default")
		if name not in self._saved_states:
			raise FakeError("Unknown g-code state: %s" % name)
		self._relative, self._relative_e = self._saved_states[name]

	def _g_G1(self, args):
		toolhead = self.status["toolhead"]
		position = self._position()
		for index, axis in enumerate("XYZ"):
			if axis not in args:
				continue
			value = float(args[axis])
			new = position[index] + value if self._relative else value
			if axis.lower() not in toolhead["homed_axes"]:
				raise FakeError("Must home axis first: %.3f %.3f %.3f [%.3f]" % tuple(position))
			if not toolhead["axis_minimum"][index] <= new <= toolhead["axis_maximum"][index]:
				raise FakeError("Move out of range: %.3f %.3f %.3f [%.3f]" % tuple(position))
			position[index] = new
		if "E" in args:
			if not self.status["extruder"]["can_extrude"]:
				raise FakeError("Extrude below minimum temp")
			value = float(args["E"])
			position[3] = position[3] + value if (self._relative or self._relative_e) else value
		self._changed({"toolhead": {"position": position}})

	_g_G0 = _g_G1

	def _set_target(self, heater, target):
		max_temp = self.status["configfile"]["settings"][heater]["max_temp"]
		if not 0.0 <= target <= max_temp:
			raise FakeError("Requested temperature (%.1f) out of range (0.0:%.1f)" % (target, max_temp))
		self._changed({heater: {"target": target}})

	def _g_SET_HEATER_TEMPERATURE(self, args):
		heater = args.get("HEATER", "")
		if heater not in ("extruder", "heater_bed"):
			raise FakeError('Unknown heater "%s"' % heater)
		self._set_target(heater, float(args.get("TARGET", 0)))

	def _g_M104(self, args):
		self._set_target("extruder", float(args.get("S", 0)))

	def _g_M140(self, args):
		self._set_target("heater_bed", float(args.get("S", 0)))

	def _g_TURN_OFF_HEATERS(self, args):
		self._set_target("extruder", 0.0)
		self._set_target("heater_bed", 0.0)

	def _g_M106(self, args):
		self._changed({"fan": {"speed": min(255.0, float(args.get("S", 255))) / 255.0}})

	def _g_M107(self, args):
		self._changed({"fan": {"speed": 0.0}})

	def _g_M220(self, args):
		self._changed({"gcode_move": {"speed_factor": float(args.get("S", 100)) / 100.0}})

	def _g_M221(self, args):
		self._changed({"gcode_move": {"extrude_factor": float(args.get("S", 100)) / 100.0}})

	def _g_SET_GCODE_OFFSET(self, args):
		origin = list(self.status["gcode_move"]["homing_origin"])
		if "Z_ADJUST" in args:
			delta = float(args["Z_ADJUST"])
		elif "Z" in args:
			delta = float(args["Z"]) - origin[2]
		else:
			return
		if int(args.get("MOVE", 0)):
			if "z" not in self.status["toolhead"]["homed_axes"]:
				raise FakeError("Must home axis first")
			position = self._position()
			position[2] += delta
			self._changed({"toolhead": {"position": position}})
		origin[2] = round(origin[2] + delta, 6)
		self._changed({"gcode_move": {"homing_origin": origin}})

	def _g_Z_OFFSET_APPLY_PROBE(self, args):
		probe = self.status["configfile"]["settings"]["bltouch"]["z_offset"]
		offset = self.status["gcode_move"]["homing_origin"][2]
		self.pending_config["bltouch"] = {"z_offset": "%.3f" % (probe - offset)}
		self._changed({"configfile": {"save_config_pending": True,
			"save_config_pending_items": copy.deepcopy(self.pending_config)}})

	def _g_SAVE_CONFIG(self, args):
		settings = self.status["configfile"]["settings"]
		for section, values in self.pending_config.items():
			settings.setdefault(section, {}).update({key: float(value) for key, value in values.items()})
		self.pending_config = {}
		self.config_saves += 1
		self.status["configfile"]["save_config_pending"] = False
		self.status["configfile"]["save_config_pending_items"] = {}
		self.restart()

	def _g_SET_VELOCITY_LIMIT(self, args):
		names = {"VELOCITY": "max_velocity", "ACCEL": "max_accel",
			"SQUARE_CORNER_VELOCITY": "square_corner_velocity", "MINIMUM_CRUISE_RATIO": "minimum_cruise_ratio"}
		self._changed({"toolhead": {field: float(args[key]) for key, field in names.items() if key in args}})

	def _g_FIRMWARE_RESTART(self, args):
		self.restart()

	def _g_M112(self, args):
		self.shutdown("Shutdown due to M112 command")

	def _g_PAUSE(self, args):
		if self.status["print_stats"]["state"] != "printing":
			raise FakeError("Print is not active")
		self._changed({"print_stats": {"state": "paused"}, "pause_resume": {"is_paused": True},
			"virtual_sdcard": {"is_active": False}})

	def _g_RESUME(self, args):
		if self.status["print_stats"]["state"] != "paused":
			raise FakeError("Print is not paused, resume aborted")
		self._changed({"print_stats": {"state": "printing"}, "pause_resume": {"is_paused": False},
			"virtual_sdcard": {"is_active": True}})

	def _g_CANCEL_PRINT(self, args):
		if self.status["print_stats"]["state"] not in ("printing", "paused"):
			raise FakeError("Print is not active")
		self._changed({"print_stats": {"state": "cancelled"}, "pause_resume": {"is_paused": False},
			"virtual_sdcard": {"is_active": False}, "idle_timeout": {"state": "Ready"}})
		self._g_TURN_OFF_HEATERS({})

	# ------------------------------------------------------------ scenarios

	def _update_can_extrude(self):
		extruder = self.status["extruder"]
		minimum = self.status["configfile"]["settings"]["extruder"]["min_extrude_temp"]
		can_extrude = extruder["temperature"] >= minimum
		if extruder["can_extrude"] != can_extrude:
			self._changed({"extruder": {"can_extrude": can_extrude}})

	def start_print(self, filename):
		with self.lock:
			if self.status["print_stats"]["state"] in ("printing", "paused"):
				raise FakeError("Printer is busy")
			if filename not in [entry["path"] for entry in self.files]:
				raise FakeError("File not found: %s" % filename)
			self._changed({
				"print_stats": {"state": "printing", "filename": filename, "print_duration": 0.0,
					"total_duration": 0.0, "message": ""},
				"virtual_sdcard": {"progress": 0.0, "is_active": True},
				"display_status": {"progress": 0.0},
				"idle_timeout": {"state": "Printing"},
				"pause_resume": {"is_paused": False},
			})

	def finish_print(self):
		with self.lock:
			self._changed({"print_stats": {"state": "complete"}, "virtual_sdcard": {"progress": 1.0, "is_active": False},
				"display_status": {"progress": 1.0}, "idle_timeout": {"state": "Ready"}})

	def fail_print(self, message):
		with self.lock:
			self._changed({"print_stats": {"state": "error", "message": message},
				"virtual_sdcard": {"is_active": False}, "idle_timeout": {"state": "Ready"}})

	def shutdown(self, message):
		with self.lock:
			self.klippy_state = "shutdown"
			self.state_message = (message + "\nOnce the underlying issue is corrected, use the\n"
				"\"FIRMWARE_RESTART\" command to reset the firmware, reload the\n"
				"config, and restart the host software.\nPrinter is shutdown")
			if self.status["print_stats"]["state"] in ("printing", "paused"):
				self._changed({"print_stats": {"state": "error", "message": message}})
			self._changed({"webhooks": {"state": "shutdown", "state_message": self.state_message}})
			self.notify("notify_klippy_shutdown")

	def disconnect_klippy(self):
		with self.lock:
			self.klippy_state = "disconnected"
			self.subscription = None
			self.notify("notify_klippy_disconnected")

	def restart(self):
		"""FIRMWARE_RESTART / SAVE_CONFIG: Klipper goes away and comes back ready."""
		with self.lock:
			self.disconnect_klippy()
			status = self.status
			settings = status["configfile"]["settings"]
			status["toolhead"].update({"homed_axes": "", "position": [0.0, 0.0, 0.0, 0.0],
				"max_velocity": settings["printer"]["max_velocity"], "max_accel": settings["printer"]["max_accel"],
				"square_corner_velocity": settings["printer"]["square_corner_velocity"],
				"minimum_cruise_ratio": settings["printer"]["minimum_cruise_ratio"]})
			status["extruder"]["target"] = 0.0
			status["heater_bed"]["target"] = 0.0
			status["gcode_move"].update({"homing_origin": [0.0, 0.0, 0.0, 0.0], "speed_factor": 1.0,
				"extrude_factor": 1.0})
			status["fan"]["speed"] = 0.0
			status["print_stats"].update({"state": "standby", "filename": "", "message": ""})
			status["virtual_sdcard"].update({"progress": 0.0, "is_active": False})
			status["idle_timeout"]["state"] = "Idle"
			status["webhooks"] = {"state": "ready", "state_message": "Printer is ready"}
			self.klippy_state = "ready"
			self.state_message = "Printer is ready"
			self.notify("notify_klippy_ready")

	def advance(self, seconds):
		"""Simulate time passing: heaters approach targets, prints progress."""
		with self.lock:
			if self.klippy_state != "ready":
				return
			for heater in ("extruder", "heater_bed"):
				current = self.status[heater]["temperature"]
				target = self.status[heater]["target"] or AMBIENT
				if abs(target - current) > 0.2:
					step = (target - current) * min(1.0, 0.25 * seconds)
					self._changed({heater: {"temperature": round(current + step, 2)}})
			self._update_can_extrude()
			stats = self.status["print_stats"]
			if stats["state"] == "printing":
				progress = min(1.0, self.status["virtual_sdcard"]["progress"] + seconds / self.print_seconds)
				self._changed({"print_stats": {"print_duration": stats["print_duration"] + seconds,
						"total_duration": stats["total_duration"] + seconds},
					"virtual_sdcard": {"progress": progress}, "display_status": {"progress": progress}})
				if progress >= 1.0:
					self.finish_print()
			elif stats["state"] == "paused":
				self._changed({"print_stats": {"total_duration": stats["total_duration"] + seconds}})


# ------------------------------------------------------------------ transport

class MockMoonrakerServer:
	"""FakePrinter behind a unix socket using Moonraker's JSON-RPC + ETX framing."""

	def __init__(self, printer, path):
		self.printer = printer
		self.path = path
		self._server = None
		self._connections = []
		self._lock = threading.Lock()
		self._send_lock = threading.Lock()
		printer.listeners.append(self._broadcast)

	@property
	def connection_count(self):
		with self._lock:
			return len(self._connections)

	def start(self):
		if os.path.exists(self.path):
			os.unlink(self.path)
		server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
		server.bind(self.path)
		server.listen(4)
		self._server = server
		threading.Thread(target=self._accept, args=(server,), name="mock-moonraker", daemon=True).start()

	def stop(self):
		"""Close everything, like a Moonraker restart."""
		with self._lock:
			server, self._server = self._server, None
		if server is not None:
			try:
				server.shutdown(socket.SHUT_RDWR)
			except OSError:
				pass
			server.close()
		with self._lock:
			connections, self._connections = self._connections, []
		for connection in connections:
			try:
				connection.shutdown(socket.SHUT_RDWR)
			except OSError:
				pass
			connection.close()
		if os.path.exists(self.path):
			os.unlink(self.path)

	def _accept(self, server):
		while True:
			try:
				connection, _address = server.accept()
			except OSError:
				return
			with self._lock:
				if self._server is not server:  # stopped while accepting
					connection.close()
					return
				self._connections.append(connection)
			threading.Thread(target=self._serve, args=(connection,), daemon=True).start()

	def _serve(self, connection):
		buffer = bytearray()
		try:
			while True:
				chunk = connection.recv(65536)
				if not chunk:
					break
				buffer += chunk
				while True:
					end = buffer.find(ETX)
					if end < 0:
						break
					request = json.loads(bytes(buffer[:end]))
					del buffer[:end + 1]
					result, error = self.printer.handle(request.get("method"), request.get("params"))
					reply = {"jsonrpc": "2.0", "id": request.get("id")}
					if error is not None:
						reply["error"] = error
					else:
						reply["result"] = result
					self._send(connection, reply)
		except (OSError, ValueError):
			pass
		finally:
			with self._lock:
				if connection in self._connections:
					self._connections.remove(connection)
			connection.close()

	def _send(self, connection, message):
		data = json.dumps(message).encode("utf-8") + ETX
		with self._send_lock:
			connection.sendall(data)

	def _broadcast(self, method, params):
		message = {"jsonrpc": "2.0", "method": method}
		if params is not None:
			message["params"] = params
		with self._lock:
			connections = list(self._connections)
		for connection in connections:
			try:
				self._send(connection, message)
			except OSError:
				pass


class FakeClient:
	"""Drop-in for MoonrakerClient backed directly by a FakePrinter (no sockets, no threads)."""

	def __init__(self, printer, on_event):
		self.printer = printer
		self._on_event = on_event
		self.connected = False
		self.calls = []
		self.hold = False  # True: keep responses until release() (a slow Moonraker)
		self.held = []
		printer.listeners.append(self._notify)

	def start(self):
		self.connected = True
		self._on_event("connected", None)

	def stop(self):
		self.connected = False

	def call(self, method, params=None, callback=None, timeout=30.0):
		self.calls.append((method, copy.deepcopy(params)))
		if self.connected:
			result, error = self.printer.handle(method, params or {})
		else:
			result, error = None, make_error("Moonraker is not connected")
		if callback is not None:
			event = ("response", (callback, copy.deepcopy(result), error))
			if self.hold:
				self.held.append(event)
			else:
				self._on_event(*event)
		return len(self.calls)

	def release(self):
		self.hold = False
		held, self.held = self.held, []
		for event in held:
			self._on_event(*event)

	def _notify(self, method, params):
		if self.connected:
			self._on_event("notification", (method, copy.deepcopy(params)))

	def disconnect(self):
		self.connected = False
		self._on_event("disconnected", "test")


# --------------------------------------------------------------- interactive

HELP = """\
Mock mode: simulated Ender 3 V2 + Klipper; nothing is sent to real hardware.
  d / a        turn the knob one step clockwise / counter-clockwise (e.g. "ddd")
  <Enter>      press the knob            s   show the screen again
  print        start a print from "Mainsail"   shutdown   fake a Klipper shutdown
  restart      restart the fake Moonraker      q          quit
"""


def run_interactive(cfg):
	from .app import App

	workdir = tempfile.mkdtemp(prefix="dwinlcd-mock-")
	cfg.moonraker_socket = os.path.join(workdir, "moonraker.sock")
	if cfg.path and os.path.exists(cfg.path):
		shutil.copy(cfg.path, os.path.join(workdir, "dwin_lcd.conf"))
	cfg.path = os.path.join(workdir, "dwin_lcd.conf")  # never write the real config in mock mode
	printer = FakePrinter()
	server = MockMoonrakerServer(printer, cfg.moonraker_socket)
	server.start()
	port = MockSerial()
	app = App(cfg, serial_factory=lambda: port, input_factory=False)
	stop = threading.Event()

	def show():
		app._flush_lcd()
		print("\n+" + "-" * 44 + "+")
		for line in port.screen.lines():
			print("| " + line[:42].ljust(42) + " |")
		print("+" + "-" * 44 + "+")
		print("> ", end="", flush=True)

	def later(func, delay=0.3):
		timer = threading.Timer(delay, lambda: app.events.put(("call", func)))
		timer.daemon = True
		timer.start()

	def simulate():
		while not stop.wait(1.0):
			printer.advance(1.0)

	def restart_moonraker():
		server.stop()
		time.sleep(3)
		server.start()

	def read_keys():
		for raw in sys.stdin:
			command = raw.strip().lower()
			if command in ("q", "quit", "exit"):
				break
			if command == "":
				app.events.put(("press",))
			elif command == "s":
				pass
			elif command == "print":
				try:
					printer.start_print(printer.files[0]["path"])
				except FakeError as exc:
					print("print failed:", exc)
			elif command == "shutdown":
				printer.shutdown("Heater extruder not heating at expected rate (simulated)")
			elif command == "restart":
				threading.Thread(target=restart_moonraker, daemon=True).start()
			elif set(command) <= set("ad+-"):
				for char in command:
					app.events.put(("rotate", 1 if char in "d+" else -1))
			else:
				print(HELP)
			later(show)
		app.stop()

	print(HELP)
	threading.Thread(target=simulate, daemon=True).start()
	threading.Thread(target=read_keys, daemon=True).start()
	signal.signal(signal.SIGINT, lambda *_args: app.stop())
	later(show, 1.0)
	try:
		app.run()
	finally:
		stop.set()
		server.stop()
		shutil.rmtree(workdir, ignore_errors=True)
	return 0
