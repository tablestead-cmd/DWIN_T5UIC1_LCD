import unittest

import helpers  # noqa: F401
from dwinlcd import printer as P
from dwinlcd.config import Config, Preset
from dwinlcd.mock import default_status


def make_state(**overrides):
	state = P.PrinterState()
	state.klippy_state = "ready"
	status = default_status()
	state.set_config_settings(status.pop("configfile")["settings"])
	state.update(status)
	for name, fields in overrides.items():
		state.update({name: fields})
	return state


def homed_state(**overrides):
	overrides.setdefault("toolhead", {})
	overrides["toolhead"].setdefault("homed_axes", "xyz")
	overrides["toolhead"].setdefault("position", [100.0, 100.0, 10.0, 0.0])
	return make_state(**overrides)


class StateTest(unittest.TestCase):
	def test_notify_diff_merges_per_field(self):
		state = make_state()
		state.update({"extruder": {"temperature": 201.5}, "print_stats": {"state": "printing"}})
		self.assertEqual(state.hotend_temp, 201.5)
		self.assertEqual(state.hotend_target, 0.0)
		self.assertTrue(state.is_printing)
		self.assertFalse(state.is_paused)
		state.update({"print_stats": {"state": "paused"}})
		self.assertTrue(state.is_paused)

	def test_derived_values(self):
		state = make_state(gcode_move={"speed_factor": 1.5, "extrude_factor": 0.95,
			"homing_origin": [0, 0, -0.05, 0]}, fan={"speed": 0.5})
		self.assertEqual(state.speed_factor, 150.0)
		self.assertAlmostEqual(state.flow_factor, 95.0)
		self.assertEqual(state.z_gcode_offset, -0.05)
		self.assertEqual(state.fan_percent, 50.0)
		self.assertEqual(state.axis_limits("x"), (0.0, 235.0))
		self.assertEqual(state.axis_limits("z"), (-2.0, 250.0))
		self.assertEqual(state.hotend_max_target, 245.0)
		self.assertEqual(state.bed_max_target, 100.0)
		self.assertEqual(state.min_extrude_temp, 170.0)
		self.assertEqual(state.probe_z_offset, 2.5)
		self.assertTrue(state.has_bed)

	def test_unknown_values_are_none(self):
		state = P.PrinterState()
		self.assertIsNone(state.axis_limits("x"))
		self.assertIsNone(state.hotend_max_target)
		self.assertIsNone(state.z_gcode_offset)
		self.assertEqual(state.print_state, "standby")
		self.assertFalse(state.ready)

	def test_settings_keep_only_needed_sections(self):
		state = P.PrinterState()
		state.set_config_settings({"printer": {"max_velocity": 300}, "gcode_macro pause": {"gcode": "x" * 5000},
			"bltouch": {"z_offset": 1.9}})
		self.assertEqual(sorted(state.settings), ["bltouch", "printer"])

	def test_remaining_time(self):
		state = make_state(print_stats={"print_duration": 600.0}, display_status={"progress": 0.25})
		self.assertEqual(state.remaining_time(), 1800.0)
		state.update({"display_status": {"progress": 0.01}})
		self.assertEqual(state.remaining_time(estimate=3600.0), 3000.0)
		state.update({"print_stats": {"print_duration": 0.0}, "display_status": {"progress": 0.0}})
		self.assertIsNone(state.remaining_time())


class GateTest(unittest.TestCase):
	def test_motion_refused_unless_ready_idle_and_homed(self):
		cfg = Config()
		not_homed = make_state()
		with self.assertRaisesRegex(P.CommandRefused, "Home"):
			P.jog(not_homed, "x", 10, cfg)
		printing = homed_state(print_stats={"state": "printing"})
		for builder, args in ((P.jog, ("x", 10, cfg)), (P.home, ()), (P.disable_steppers, ()),
				(P.extrude, (5, cfg)), (P.z_offset_adjust, (0.1, cfg)), (P.z_offset_save, ()),
				(P.velocity_limits, ()), (P.preheat, (Preset("PLA", 200, 60),)), (P.cooldown, ())):
			with self.assertRaisesRegex(P.CommandRefused, "printing"):
				builder(printing, *args)
		paused = homed_state(print_stats={"state": "paused"})
		with self.assertRaises(P.CommandRefused):
			P.home(paused)
		busy = homed_state(idle_timeout={"state": "Printing"})
		with self.assertRaisesRegex(P.CommandRefused, "busy"):
			P.jog(busy, "x", 10, cfg)
		shutdown = homed_state()
		shutdown.klippy_state = "shutdown"
		with self.assertRaisesRegex(P.CommandRefused, "not ready"):
			P.set_hotend(shutdown, 200)

	def test_print_control_checks(self):
		self.assertIsNone(P.check_can_pause(make_state(print_stats={"state": "printing"})))
		self.assertIsNotNone(P.check_can_pause(make_state(print_stats={"state": "paused"})))
		self.assertIsNone(P.check_can_resume(make_state(print_stats={"state": "paused"})))
		self.assertIsNone(P.check_can_cancel(make_state(print_stats={"state": "paused"})))
		self.assertIsNotNone(P.check_can_cancel(make_state()))


class CommandTest(unittest.TestCase):
	def setUp(self):
		self.cfg = Config()

	def test_home_and_steppers(self):
		self.assertEqual(P.home(make_state()), "G28")
		self.assertEqual(P.disable_steppers(make_state()), "M84")

	def test_jog_is_relative_and_clamped_to_runtime_limits(self):
		state = homed_state()
		self.assertEqual(P.jog(state, "x", 110.5, self.cfg),
			"SAVE_GCODE_STATE NAME=_dwin_jog\nG91\nG1 X10.5 F3000\nRESTORE_GCODE_STATE NAME=_dwin_jog")
		self.assertIn("G1 Y135 F3000", P.jog(state, "y", 999, self.cfg))  # 235 max
		self.assertIn("G1 X-100 F3000", P.jog(state, "x", -50, self.cfg))  # 0 min
		self.assertIn("G1 Z-10 F300", P.jog(state, "z", -1.5, self.cfg))  # floor Z=0, not position_min -2
		self.assertIsNone(P.jog(state, "x", 100.0004, self.cfg))
		state.update({"toolhead": {"axis_maximum": [180.0, 180.0, 180.0, 0.0]}})
		self.assertIn("G1 X80 F3000", P.jog(state, "x", 200, self.cfg))

	def test_jog_needs_known_position(self):
		state = homed_state()
		state.status["toolhead"]["position"] = None
		with self.assertRaisesRegex(P.CommandRefused, "unknown"):
			P.jog(state, "x", 10, self.cfg)
		with self.assertRaises(P.CommandRefused):
			P.jog(homed_state(), "e", 10, self.cfg)

	def test_extrude(self):
		cold = make_state()
		with self.assertRaisesRegex(P.CommandRefused, "170"):
			P.extrude(cold, 5, self.cfg)
		hot = make_state(extruder={"temperature": 210.0, "can_extrude": True})
		self.assertEqual(P.extrude(hot, 5, self.cfg), "SAVE_GCODE_STATE NAME=_dwin_extrude\nM83\n"
			"G1 E5 F300\nRESTORE_GCODE_STATE NAME=_dwin_extrude")
		self.assertIn("G1 E-50 ", P.extrude(hot, -500, self.cfg))
		self.assertIsNone(P.extrude(hot, 0, self.cfg))

	def test_temperatures_clamped_to_klipper_limits(self):
		state = make_state()
		self.assertEqual(P.set_hotend(state, 300), "SET_HEATER_TEMPERATURE HEATER=extruder TARGET=245")
		self.assertEqual(P.set_hotend(state, -5), "SET_HEATER_TEMPERATURE HEATER=extruder TARGET=0")
		self.assertEqual(P.set_bed(state, 110), "SET_HEATER_TEMPERATURE HEATER=heater_bed TARGET=100")
		self.assertEqual(P.set_bed(state, 60.4), "SET_HEATER_TEMPERATURE HEATER=heater_bed TARGET=60")
		unknown = make_state()
		unknown.settings = {}
		with self.assertRaisesRegex(P.CommandRefused, "unknown"):
			P.set_hotend(unknown, 200)

	def test_nozzle_never_below_min_extrude_temp_while_printing(self):
		printing = make_state(print_stats={"state": "printing"})
		self.assertEqual(P.set_hotend(printing, 0), "SET_HEATER_TEMPERATURE HEATER=extruder TARGET=170")
		self.assertEqual(P.set_hotend(printing, 215), "SET_HEATER_TEMPERATURE HEATER=extruder TARGET=215")

	def test_bed_temp_in_tune_sets_the_bed(self):
		# Upstream bug: Tune > Bed temp called setTargetHotend(bed_value, 0), i.e. set the nozzle.
		self.assertIn("HEATER=heater_bed", P.set_bed(make_state(print_stats={"state": "printing"}), 65))

	def test_preheat_cooldown_fan_speed_flow(self):
		state = make_state()
		self.assertEqual(P.preheat(state, Preset("ABS", 260, 120)),
			"SET_HEATER_TEMPERATURE HEATER=extruder TARGET=245\nSET_HEATER_TEMPERATURE HEATER=heater_bed TARGET=100")
		self.assertEqual(P.cooldown(state), "TURN_OFF_HEATERS\nM107")
		self.assertEqual(P.set_fan(state, 50), "M106 S128")
		self.assertEqual(P.set_fan(state, 150), "M106 S255")
		self.assertEqual(P.set_speed_factor(state, 1000), "M220 S300")
		self.assertEqual(P.set_flow(state, 97), "M221 S97")
		self.assertEqual(P.set_flow(state, 10), "M221 S50")

	def test_z_offset_babystep_and_save(self):
		state = homed_state()
		self.assertEqual(P.z_offset_adjust(state, -0.05, self.cfg), "SET_GCODE_OFFSET Z_ADJUST=-0.05 MOVE=1")
		state.update({"gcode_move": {"homing_origin": [0, 0, -0.05, 0]}})
		self.assertEqual(P.z_offset_adjust(state, -0.02, self.cfg), "SET_GCODE_OFFSET Z_ADJUST=0.03 MOVE=1")
		self.assertEqual(P.z_offset_adjust(state, -5, self.cfg), "SET_GCODE_OFFSET Z_ADJUST=-0.95 MOVE=1")
		self.assertIsNone(P.z_offset_adjust(state, -0.05, self.cfg))
		self.assertEqual(P.new_probe_offset(state), 2.55)
		self.assertEqual(P.z_offset_save(state), "Z_OFFSET_APPLY_PROBE\nSAVE_CONFIG")
		with self.assertRaisesRegex(P.CommandRefused, "Home"):
			P.z_offset_adjust(make_state(), 0.1, self.cfg)
		with self.assertRaisesRegex(P.CommandRefused, "No Z offset"):
			P.z_offset_save(homed_state())
		no_probe = homed_state(gcode_move={"homing_origin": [0, 0, 0.1, 0]})
		no_probe.settings.pop("bltouch")
		with self.assertRaisesRegex(P.CommandRefused, "probe"):
			P.z_offset_save(no_probe)

	def test_knob_never_sends_probe_calibrate_or_testz(self):
		# No string literal in the package (G-code is built from literals) may contain these.
		import ast
		import os
		import re
		package = os.path.dirname(P.__file__)
		for name in os.listdir(package):
			if not name.endswith(".py") or name == "mock.py":
				continue
			with open(os.path.join(package, name)) as fh:
				tree = ast.parse(fh.read())
			docstrings = {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Expr)}
			for node in ast.walk(tree):
				if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
					self.assertIsNone(re.search(r"\b(PROBE_CALIBRATE|TESTZ|ACCEPT|ABORT)\b", node.value),
						"%s: %r" % (name, node.value))

	def test_velocity_limits_capped_at_printer_cfg(self):
		state = make_state()
		self.assertEqual(P.velocity_limits(state, velocity=400), "SET_VELOCITY_LIMIT VELOCITY=300")
		self.assertEqual(P.velocity_limits(state, accel=3000, scv=7.5),
			"SET_VELOCITY_LIMIT ACCEL=3000 SQUARE_CORNER_VELOCITY=5")
		self.assertEqual(P.velocity_limits(state, mcr=0.35), "SET_VELOCITY_LIMIT MINIMUM_CRUISE_RATIO=0.35")
		self.assertEqual(P.restore_velocity_limits(state), "SET_VELOCITY_LIMIT VELOCITY=300 ACCEL=4500 "
			"SQUARE_CORNER_VELOCITY=5 MINIMUM_CRUISE_RATIO=0.5")
		self.assertIsNone(P.velocity_limits(state))

	def test_fmt_num(self):
		self.assertEqual(P.fmt_num(10.0), "10")
		self.assertEqual(P.fmt_num(-0.0001), "0")
		self.assertEqual(P.fmt_num(0.125), "0.125")
		self.assertEqual(P.fmt_num(3000, 0), "3000")


if __name__ == "__main__":
	unittest.main()
