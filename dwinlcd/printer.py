# Printer state (Klipper status objects via Moonraker) and the G-code the display sends.
#
# Every command that moves the machine or heats something is built here, so the safety
# rules live in one place and are unit tested:
#   - nothing is sent unless Klipper is ready;
#   - motion (home, jog, extrude, Z offset, steppers off) only while not printing and not
#     busy, and jogs, extrusion and Z offset changes only with x, y and z homed;
#   - positions and temperatures are clamped to limits read from Klipper at runtime
#     (toolhead.axis_minimum/maximum, configfile settings), never hard-coded;
#   - the knob never runs PROBE_CALIBRATE or TESTZ; Z offset uses SET_GCODE_OFFSET
#     Z_ADJUST (babystep) and Z_OFFSET_APPLY_PROBE + SAVE_CONFIG to store it.

PRINTING_STATES = ("printing", "paused")
HOTEND_MARGIN = 15  # keep targets below max_temp (Marlin's HOTEND_OVERSHOOT)
BED_MARGIN = 10  # (Marlin's BED_OVERSHOOT)
DEFAULT_MIN_EXTRUDE_TEMP = 170.0
Z_JOG_FLOOR = 0.0  # jogs never go below Z=0, whatever position_min allows
MAX_EXTRUDE = 50.0  # mm per press
SPEED_RANGE = (10, 300)  # M220 percent
FLOW_RANGE = (50, 150)  # M221 percent
PROBE_SECTIONS = ("bltouch", "probe", "smart_effector")

# Status fields the display subscribes to. Kept small because Klipper sends a diff for
# every change about four times a second (position only changes while moving).
SUBSCRIPTION = {
	"webhooks": ["state", "state_message"],
	"print_stats": ["state", "filename", "print_duration", "total_duration", "message"],
	"virtual_sdcard": ["progress", "is_active"],
	"display_status": ["progress"],
	"toolhead": ["homed_axes", "axis_minimum", "axis_maximum", "position", "max_velocity", "max_accel",
		"square_corner_velocity", "minimum_cruise_ratio"],
	"extruder": ["temperature", "target", "can_extrude"],
	"heater_bed": ["temperature", "target"],
	"gcode_move": ["speed_factor", "extrude_factor", "homing_origin"],
	"fan": ["speed"],
	"idle_timeout": ["state"],
	"pause_resume": ["is_paused"],
}


class CommandRefused(Exception):
	"""The display will not send this command in the current printer state."""


def fmt_num(value, decimals=3):
	"""Compact number for G-code: 10.500 -> '10.5', 3.0 -> '3', 3000 -> '3000'."""
	text = "%.*f" % (decimals, value)
	if "." in text:
		text = text.rstrip("0").rstrip(".")
	return "0" if text in ("", "-0") else text


def _float(value):
	try:
		return float(value)
	except (TypeError, ValueError):
		return None


class PrinterState:
	def __init__(self):
		self.status = {}
		self.klippy_state = "disconnected"
		self.state_message = ""
		self.software_version = ""
		self.hostname = ""
		self.moonraker_version = ""
		self.settings = {}

	# --------------------------------------------------------------- updates

	def reset_status(self):
		self.status = {}

	def update(self, diff):
		"""Merge a Klipper status dict (query result or notify_status_update diff)."""
		if not isinstance(diff, dict):
			return
		for name, fields in diff.items():
			if isinstance(fields, dict):
				self.status.setdefault(name, {}).update(fields)

	def set_config_settings(self, settings):
		"""Keep only the configfile sections the display needs (settings can be large)."""
		keep = {}
		if isinstance(settings, dict):
			for section in ("printer", "extruder", "heater_bed") + PROBE_SECTIONS:
				values = settings.get(section)
				if isinstance(values, dict):
					keep[section] = dict(values)
		self.settings = keep

	def get(self, obj, field, default=None):
		value = self.status.get(obj, {}).get(field)
		return default if value is None else value

	def number(self, obj, field):
		return _float(self.status.get(obj, {}).get(field))

	def setting(self, section, option):
		return _float(self.settings.get(section, {}).get(option))

	# ------------------------------------------------------------ properties

	@property
	def ready(self):
		return self.klippy_state == "ready"

	@property
	def print_state(self):
		return str(self.get("print_stats", "state", "standby"))

	@property
	def is_printing(self):
		return self.print_state in PRINTING_STATES

	@property
	def is_paused(self):
		return self.print_state == "paused"

	@property
	def is_busy(self):
		"""Klipper is executing G-code (a print, a macro, homing from Mainsail...)."""
		return self.get("idle_timeout", "state") == "Printing"

	@property
	def homed_axes(self):
		return str(self.get("toolhead", "homed_axes", ""))

	def is_homed(self, axes="xyz"):
		homed = self.homed_axes
		return all(axis in homed for axis in axes)

	def axis_limits(self, axis):
		index = "xyz".index(axis)
		try:
			lo = float(self.get("toolhead", "axis_minimum")[index])
			hi = float(self.get("toolhead", "axis_maximum")[index])
		except (TypeError, ValueError, IndexError):
			return None
		return lo, hi

	def position(self, axis):
		try:
			return float(self.get("toolhead", "position")["xyz".index(axis)])
		except (TypeError, ValueError, IndexError):
			return None

	@property
	def hotend_temp(self):
		return self.number("extruder", "temperature")

	@property
	def hotend_target(self):
		return self.number("extruder", "target")

	@property
	def bed_temp(self):
		return self.number("heater_bed", "temperature")

	@property
	def bed_target(self):
		return self.number("heater_bed", "target")

	@property
	def has_bed(self):
		return "heater_bed" in self.status or "heater_bed" in self.settings

	@property
	def fan_percent(self):
		speed = self.number("fan", "speed")
		return None if speed is None else speed * 100.0

	@property
	def speed_factor(self):
		value = self.number("gcode_move", "speed_factor")
		return None if value is None else value * 100.0

	@property
	def flow_factor(self):
		value = self.number("gcode_move", "extrude_factor")
		return None if value is None else value * 100.0

	@property
	def z_gcode_offset(self):
		try:
			return float(self.get("gcode_move", "homing_origin")[2])
		except (TypeError, ValueError, IndexError):
			return None

	@property
	def can_extrude(self):
		return bool(self.get("extruder", "can_extrude", False))

	@property
	def progress(self):
		value = self.number("display_status", "progress")
		if value is None:
			value = self.number("virtual_sdcard", "progress")
		return value

	@property
	def print_duration(self):
		return self.number("print_stats", "print_duration")

	@property
	def total_duration(self):
		return self.number("print_stats", "total_duration")

	@property
	def filename(self):
		return str(self.get("print_stats", "filename", ""))

	@property
	def hotend_max_target(self):
		max_temp = self.setting("extruder", "max_temp")
		return None if max_temp is None else max(0.0, max_temp - HOTEND_MARGIN)

	@property
	def bed_max_target(self):
		max_temp = self.setting("heater_bed", "max_temp")
		return None if max_temp is None else max(0.0, max_temp - BED_MARGIN)

	@property
	def min_extrude_temp(self):
		value = self.setting("extruder", "min_extrude_temp")
		return DEFAULT_MIN_EXTRUDE_TEMP if value is None else value

	@property
	def probe_z_offset(self):
		for section in PROBE_SECTIONS:
			value = self.setting(section, "z_offset")
			if value is not None:
				return value
		return None

	def config_limit(self, option):
		"""Velocity limits from [printer] in printer.cfg (upper bounds for the Motion menu)."""
		return self.setting("printer", option)

	def remaining_time(self, estimate=None):
		"""Seconds left: file-progress based, or the slicer estimate early in the print."""
		duration = self.print_duration
		progress = self.progress
		if duration is None or progress is None:
			return None
		if progress >= 0.05 and duration > 0:
			return max(0.0, duration / progress - duration)
		if estimate:
			return max(0.0, float(estimate) - duration)
		if progress > 0.001 and duration > 0:
			return max(0.0, duration / progress - duration)
		return None


# ------------------------------------------------------------------ checks
# Each returns None when allowed, else a short reason for the screen.

def check_ready(state):
	if not state.ready:
		return "Klipper is not ready"
	return None


def check_not_printing(state):
	reason = check_ready(state)
	if reason:
		return reason
	if state.is_printing:
		return "Not allowed while printing"
	return None


def check_idle(state):
	reason = check_not_printing(state)
	if reason:
		return reason
	if state.is_busy:
		return "Printer is busy"
	return None


def check_homed(state):
	reason = check_idle(state)
	if reason:
		return reason
	if not state.is_homed("xyz"):
		return "Home all axes first"
	return None


def check_can_pause(state):
	reason = check_ready(state)
	if reason:
		return reason
	if state.print_state != "printing":
		return "Not printing"
	return None


def check_can_resume(state):
	reason = check_ready(state)
	if reason:
		return reason
	if state.print_state != "paused":
		return "Print is not paused"
	return None


def check_can_cancel(state):
	reason = check_ready(state)
	if reason:
		return reason
	if not state.is_printing:
		return "Not printing"
	return None


def _require(reason):
	if reason:
		raise CommandRefused(reason)


def _clamp_int(value, lo, hi):
	return int(min(hi, max(lo, int(round(float(value))))))


# ---------------------------------------------------------------- commands

def home(state):
	_require(check_idle(state))
	return "G28"


def disable_steppers(state):
	_require(check_idle(state))
	return "M84"


def jog(state, axis, target, cfg):
	"""Move one axis to an absolute toolhead position with a relative move.

	The target is clamped to the runtime axis limits (and Z >= 0). Returns None when
	there is nothing to do. The caller must re-read toolhead.position right before
	(ui.UI.jog does, and refuses if it changed since the edit started).
	"""
	_require(check_homed(state))
	axis = str(axis).lower()
	if axis not in ("x", "y", "z"):
		raise CommandRefused("Unknown axis")
	limits = state.axis_limits(axis)
	current = state.position(axis)
	if limits is None or current is None:
		raise CommandRefused("Axis position unknown")
	lo, hi = limits
	if axis == "z":
		lo = max(lo, Z_JOG_FLOOR)
	if lo > hi:
		raise CommandRefused("Axis limits invalid")
	target = min(hi, max(lo, float(target)))
	delta = round(target - current, 3)
	if abs(delta) < 0.001:
		return None
	speed = cfg.jog_speed_z if axis == "z" else cfg.jog_speed_xy
	return ("SAVE_GCODE_STATE NAME=_dwin_jog\nG91\nG1 %s%s F%s\nRESTORE_GCODE_STATE NAME=_dwin_jog"
		% (axis.upper(), fmt_num(delta), fmt_num(speed * 60, 0)))


def extrude(state, amount, cfg):
	_require(check_homed(state))
	if not state.can_extrude:
		raise CommandRefused("Nozzle below %d C" % state.min_extrude_temp)
	amount = min(MAX_EXTRUDE, max(-MAX_EXTRUDE, float(amount)))
	if abs(amount) < 0.05:
		return None
	return ("SAVE_GCODE_STATE NAME=_dwin_extrude\nM83\nG1 E%s F%s\nRESTORE_GCODE_STATE NAME=_dwin_extrude"
		% (fmt_num(amount, 2), fmt_num(cfg.extrude_speed * 60, 0)))


def set_hotend(state, temp):
	_require(check_ready(state))
	hi = state.hotend_max_target
	if hi is None:
		raise CommandRefused("Temperature limits unknown")
	# While printing never drop below the extrusion minimum (a knob slip would stop the print).
	lo = min(hi, state.min_extrude_temp) if state.is_printing else 0
	return "SET_HEATER_TEMPERATURE HEATER=extruder TARGET=%d" % _clamp_int(temp, lo, hi)


def set_bed(state, temp):
	_require(check_ready(state))
	hi = state.bed_max_target
	if hi is None:
		raise CommandRefused("Bed limits unknown")
	return "SET_HEATER_TEMPERATURE HEATER=heater_bed TARGET=%d" % _clamp_int(temp, 0, hi)


def preheat(state, preset):
	_require(check_not_printing(state))
	lines = [set_hotend(state, preset.hotend_temp)]
	if state.has_bed:
		lines.append(set_bed(state, preset.bed_temp))
	return "\n".join(lines)


def cooldown(state):
	_require(check_not_printing(state))
	return "TURN_OFF_HEATERS\nM107"


def set_fan(state, percent):
	_require(check_ready(state))
	percent = min(100.0, max(0.0, float(percent)))
	return "M106 S%d" % int(round(percent * 255 / 100))


def set_speed_factor(state, percent):
	_require(check_ready(state))
	return "M220 S%d" % _clamp_int(percent, *SPEED_RANGE)


def set_flow(state, percent):
	_require(check_ready(state))
	return "M221 S%d" % _clamp_int(percent, *FLOW_RANGE)


def z_offset_adjust(state, new_offset, cfg):
	"""Babystep to an absolute G-code Z offset (SET_GCODE_OFFSET Z_ADJUST, MOVE=1)."""
	_require(check_homed(state))
	current = state.z_gcode_offset
	if current is None:
		raise CommandRefused("Z offset unknown")
	limit = cfg.z_offset_limit
	new_offset = round(min(limit, max(-limit, float(new_offset))), 3)
	delta = round(new_offset - current, 3)
	if abs(delta) < 0.0005:
		return None
	return "SET_GCODE_OFFSET Z_ADJUST=%s MOVE=1" % fmt_num(delta)


def z_offset_save(state):
	"""Store the babystep in the probe's z_offset. SAVE_CONFIG restarts Klipper."""
	_require(check_idle(state))
	if state.probe_z_offset is None:
		raise CommandRefused("No probe z_offset in config")
	current = state.z_gcode_offset
	if current is None or abs(current) < 0.0005:
		raise CommandRefused("No Z offset change to save")
	return "Z_OFFSET_APPLY_PROBE\nSAVE_CONFIG"


def new_probe_offset(state):
	"""What Z_OFFSET_APPLY_PROBE will store: probe z_offset minus the G-code Z offset."""
	probe = state.probe_z_offset
	current = state.z_gcode_offset
	if probe is None or current is None:
		return None
	return round(probe - current, 3)


def velocity_limits(state, velocity=None, accel=None, scv=None, mcr=None):
	"""SET_VELOCITY_LIMIT, capped at the printer.cfg values (the knob can only lower them)."""
	_require(check_idle(state))
	parts = []
	for name, key, value, lo, decimals in (
			("VELOCITY", "max_velocity", velocity, 1.0, 0),
			("ACCEL", "max_accel", accel, 10.0, 0),
			("SQUARE_CORNER_VELOCITY", "square_corner_velocity", scv, 0.0, 1)):
		if value is None:
			continue
		cap = state.config_limit(key)
		if cap is None:
			raise CommandRefused("printer.cfg limits unknown")
		parts.append("%s=%s" % (name, fmt_num(min(cap, max(lo, float(value))), decimals)))
	if mcr is not None:
		parts.append("MINIMUM_CRUISE_RATIO=%s" % fmt_num(min(0.99, max(0.0, float(mcr))), 2))
	if not parts:
		return None
	return "SET_VELOCITY_LIMIT " + " ".join(parts)


def restore_velocity_limits(state):
	_require(check_idle(state))
	values = {
		"velocity": state.config_limit("max_velocity"),
		"accel": state.config_limit("max_accel"),
		"scv": state.config_limit("square_corner_velocity"),
		"mcr": state.config_limit("minimum_cruise_ratio"),
	}
	if values["velocity"] is None or values["accel"] is None:
		raise CommandRefused("printer.cfg limits unknown")
	return velocity_limits(state, **values)
