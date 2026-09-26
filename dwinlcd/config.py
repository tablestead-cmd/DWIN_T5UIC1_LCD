# Configuration for the DWIN display service.
#
# One INI file (default ~/printer_data/config/dwin_lcd.conf) so it can be edited from
# Mainsail. Bad or missing values fall back to the defaults below with a warning; the
# service never refuses to start because of the config file. The display only ever
# writes the [preheat_N] values back, editing them in place so comments survive.

import configparser
import dataclasses
import logging
import os
import re
import tempfile

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = os.path.expanduser("~/printer_data/config/dwin_lcd.conf")
DEFAULT_MOONRAKER_SOCKET = os.path.expanduser("~/printer_data/comms/moonraker.sock")

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
PRESET_COUNT = 2
PRESET_NAME_MAX = 8


@dataclasses.dataclass
class Preset:
	name: str
	hotend_temp: int
	bed_temp: int


def default_presets():
	return [Preset("PLA", 200, 60), Preset("ABS", 240, 100)]


@dataclasses.dataclass
class Config:
	path: str = None
	# [general]
	log_level: str = "INFO"
	# [display]
	serial_port: str = "/dev/serial0"
	baudrate: int = 115200
	skip_handshake: bool = False
	brightness: int = None
	# [encoder]
	pin_a: int = 19
	pin_b: int = 26
	pin_button: int = 13
	reverse: bool = False
	pull_up: bool = True
	pulses_per_step: int = 4
	encoder_debounce_ms: float = 0.0
	button_debounce_ms: float = 30.0
	# [moonraker]
	moonraker_socket: str = DEFAULT_MOONRAKER_SOCKET
	# [ui]
	title: str = ""
	status_interval: float = 1.0
	jog_speed_xy: float = 50.0
	jog_speed_z: float = 5.0
	extrude_speed: float = 5.0
	z_offset_limit: float = 1.0
	max_files: int = 50
	presets: list = dataclasses.field(default_factory=default_presets)


# (section, option, attribute, kind, minimum, maximum)
_OPTIONS = (
	("general", "log_level", "log_level", "level", None, None),
	("display", "serial_port", "serial_port", "str", None, None),
	("display", "baudrate", "baudrate", "int", 1200, 1000000),
	("display", "skip_handshake", "skip_handshake", "bool", None, None),
	("display", "brightness", "brightness", "optint", 0, 255),
	("encoder", "pin_a", "pin_a", "int", 0, 27),
	("encoder", "pin_b", "pin_b", "int", 0, 27),
	("encoder", "pin_button", "pin_button", "int", 0, 27),
	("encoder", "reverse", "reverse", "bool", None, None),
	("encoder", "pull_up", "pull_up", "bool", None, None),
	("encoder", "pulses_per_step", "pulses_per_step", "int", 1, 8),
	("encoder", "encoder_debounce_ms", "encoder_debounce_ms", "float", 0.0, 10.0),
	("encoder", "button_debounce_ms", "button_debounce_ms", "float", 0.0, 200.0),
	("moonraker", "socket", "moonraker_socket", "str", None, None),
	("ui", "title", "title", "str", None, None),
	("ui", "status_interval", "status_interval", "float", 0.5, 10.0),
	("ui", "jog_speed_xy", "jog_speed_xy", "float", 1.0, 200.0),
	("ui", "jog_speed_z", "jog_speed_z", "float", 0.5, 20.0),
	("ui", "extrude_speed", "extrude_speed", "float", 0.5, 20.0),
	("ui", "z_offset_limit", "z_offset_limit", "float", 0.05, 2.0),
	("ui", "max_files", "max_files", "int", 1, 200),
)

_PRESET_LIMITS = {"hotend_temp": (0, 350), "bed_temp": (0, 150)}


def _parse(kind, raw):
	raw = raw.strip()
	if kind in ("str",):
		return os.path.expanduser(raw) if raw.startswith("~") else raw
	if kind == "level":
		value = raw.upper()
		if value not in LOG_LEVELS:
			raise ValueError("expected one of %s" % ", ".join(LOG_LEVELS))
		return value
	if kind == "bool":
		value = raw.lower()
		if value in ("1", "yes", "true", "on"):
			return True
		if value in ("0", "no", "false", "off"):
			return False
		raise ValueError("expected true or false")
	if kind == "optint":
		return None if raw == "" else int(raw)
	if kind == "int":
		return int(raw)
	if kind == "float":
		return float(raw)
	raise ValueError("unknown option kind %r" % kind)


def load_config(path=None):
	"""Read the config file. Missing file or bad values: defaults plus a warning."""
	cfg = Config(path=path or DEFAULT_CONFIG_PATH)
	parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"))
	try:
		with open(cfg.path, encoding="utf-8") as fh:
			parser.read_file(fh)
	except FileNotFoundError:
		log.warning("config: %s not found, using defaults", cfg.path)
		return cfg
	except (OSError, configparser.Error) as exc:
		log.error("config: cannot read %s (%s), using defaults", cfg.path, exc)
		return cfg

	known = {}
	for section, option, attr, kind, lo, hi in _OPTIONS:
		known.setdefault(section, set()).add(option)
		if not parser.has_option(section, option):
			continue
		raw = parser.get(section, option)
		try:
			value = _parse(kind, raw)
			if value is not None and lo is not None and not (lo <= value <= hi):
				raise ValueError("must be between %s and %s" % (lo, hi))
			if kind == "str" and not value and option != "title":
				raise ValueError("must not be empty")
		except ValueError as exc:
			log.warning("config: [%s] %s = %r ignored (%s); using %r",
				section, option, raw, exc, getattr(cfg, attr))
			continue
		setattr(cfg, attr, value)

	for index in range(PRESET_COUNT):
		section = "preheat_%d" % (index + 1)
		known[section] = {"name", "hotend_temp", "bed_temp"}
		if not parser.has_section(section):
			continue
		preset = cfg.presets[index]
		name = parser.get(section, "name", fallback=preset.name).strip()
		if name:
			preset.name = name[:PRESET_NAME_MAX]
		for option, (lo, hi) in _PRESET_LIMITS.items():
			if not parser.has_option(section, option):
				continue
			raw = parser.get(section, option)
			try:
				value = int(float(raw))
				if not lo <= value <= hi:
					raise ValueError("must be between %d and %d" % (lo, hi))
			except ValueError as exc:
				log.warning("config: [%s] %s = %r ignored (%s)", section, option, raw, exc)
				continue
			setattr(preset, option, value)

	for section in parser.sections():
		if section not in known:
			log.warning("config: unknown section [%s] ignored", section)
			continue
		for option in parser.options(section):
			if option not in known[section]:
				log.warning("config: unknown option [%s] %s ignored", section, option)

	if len({cfg.pin_a, cfg.pin_b, cfg.pin_button}) != 3:
		log.error("config: pin_a, pin_b and pin_button must be different GPIOs; using defaults 19/26/13")
		cfg.pin_a, cfg.pin_b, cfg.pin_button = 19, 26, 13
	return cfg


_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]")


def _set_section_values(text, section, values):
	"""Set `key = value` lines inside [section], keeping comments and unknown lines."""
	lines = text.splitlines()
	start = None
	end = len(lines)
	for index, line in enumerate(lines):
		match = _SECTION_RE.match(line)
		if not match:
			continue
		if start is None and match.group(1).strip() == section:
			start = index
		elif start is not None:
			end = index
			break
	if start is None:
		if lines and lines[-1].strip():
			lines.append("")
		lines.append("[%s]" % section)
		lines.extend("%s = %s" % (key, value) for key, value in values.items())
		return "\n".join(lines) + "\n"

	missing = dict(values)
	for index in range(start + 1, end):
		match = re.match(r"^(\s*)([A-Za-z0-9_]+)(\s*[:=]\s*)([^#;]*?)(\s*[#;].*)?$", lines[index])
		if not match or match.group(2).lower() not in missing:
			continue
		key = match.group(2).lower()
		lines[index] = "%s%s%s%s%s" % (match.group(1), match.group(2), match.group(3),
			missing.pop(key), match.group(5) or "")
	if missing:
		insert_at = end
		while insert_at > start + 1 and not lines[insert_at - 1].strip():
			insert_at -= 1
		lines[insert_at:insert_at] = ["%s = %s" % (key, value) for key, value in missing.items()]
	return "\n".join(lines) + "\n"


_NEW_FILE_HEADER = "# DWIN display settings (see dwin_lcd.conf.example in the DWIN_T5UIC1_LCD repo)\n"


def save_presets(path, presets):
	"""Write the preheat presets into the config file (atomic replace)."""
	try:
		with open(path, encoding="utf-8") as fh:
			text = fh.read()
		mode = os.stat(path).st_mode & 0o777
	except FileNotFoundError:
		text = _NEW_FILE_HEADER
		mode = 0o644
	for index, preset in enumerate(presets[:PRESET_COUNT]):
		text = _set_section_values(text, "preheat_%d" % (index + 1), {
			"name": preset.name,
			"hotend_temp": str(int(preset.hotend_temp)),
			"bed_temp": str(int(preset.bed_temp)),
		})
	directory = os.path.dirname(os.path.abspath(path))
	fd, tmp_path = tempfile.mkstemp(prefix=".dwin_lcd.", dir=directory)
	try:
		with os.fdopen(fd, "w", encoding="utf-8") as fh:
			fh.write(text)
			fh.flush()
			os.fsync(fh.fileno())
		os.chmod(tmp_path, mode)
		os.replace(tmp_path, path)
	except BaseException:
		try:
			os.unlink(tmp_path)
		except OSError:
			pass
		raise
