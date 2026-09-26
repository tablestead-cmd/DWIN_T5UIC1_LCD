import os
import tempfile
import unittest

from helpers import Harness
from dwinlcd import ui as U
from dwinlcd.config import Config, load_config


class BootTest(unittest.TestCase):
	def test_idle_main_menu_with_status(self):
		h = Harness()
		self.assertIsInstance(h.page, U.MainMenu)
		self.assertTrue(h.screen.has("[Print*]") and h.screen.has("[Info]"))
		self.assertTrue(h.screen.has("mockpi"))  # title = hostname
		h.printer.set("extruder", temperature=205.4, target=210.0)
		h.pump()
		self.assertTrue(h.screen.has("205/210"))
		self.assertTrue(h.screen.has("+0.000"))

	def test_runs_without_display_and_ignores_input(self):
		h = Harness(display=False)
		self.assertFalse(h.app.ui.online)
		h.press()
		h.rotate(3)
		self.assertEqual(h.printer.scripts, [])
		# display appears later: connected on the next retry and fully drawn
		h.port.present = True
		h.app._lcd_watchdog()
		h.pump()
		self.assertTrue(h.app.ui.online)
		self.assertTrue(h.screen.has("[Prepare]"))
		h.rotate(1)
		self.assertTrue(h.screen.has("[Print] [Prepare*]"))

	def test_status_area_only_redraws_changes(self):
		h = Harness()
		before = h.port.bytes_written
		h.pump()
		h.pump()
		self.assertEqual(h.port.bytes_written, before, "idle ticks send nothing")


class PrepareTest(unittest.TestCase):
	def setUp(self):
		self.h = Harness()
		self.h.rotate(1)  # main menu: Prepare
		self.h.press()
		self.assertIsInstance(self.h.page, U.PrepareMenu)

	def test_home_needs_confirmation_and_cancel_is_default(self):
		h = self.h
		h.open("Auto home")
		self.assertIsInstance(h.page, U.Confirm)
		self.assertIs(h.screen.confirm_selected(), False)
		h.press()  # default = Cancel
		self.assertEqual(h.printer.scripts, [])
		self.assertIsInstance(h.page, U.PrepareMenu)
		h.open("Auto home")
		h.confirm()
		self.assertEqual(h.printer.scripts, ["G28"])
		self.assertEqual(h.app.state.homed_axes, "xyz")
		self.assertIsInstance(h.page, U.PrepareMenu)

	def test_move_refused_until_homed(self):
		h = self.h
		h.open("Move", "Move X")
		self.assertIsInstance(h.page, U.Message)
		self.assertTrue(h.screen.has("Home all axes first"))
		h.press()
		self.assertIsInstance(h.page, U.MoveMenu)
		self.assertEqual(h.printer.scripts, [])

	def test_jog_clamped_to_runtime_limits(self):
		h = self.h
		h.home()
		h.open("Move", "Move X")
		self.assertIsNotNone(h.page.editing)
		h.rotate(5)
		self.assertTrue(h.screen.has("160.0"))
		h.press()
		self.assertEqual(h.printer.scripts[-3:-1], ["G91", "G1 X5 F3000"])
		self.assertEqual(h.app.state.position("x"), 160.0)
		h.press()  # edit X again, turn far past the 235 mm limit
		for _ in range(40):
			h.rotate(5)
		h.press()
		self.assertEqual(h.printer.scripts[-2], "G1 X75 F3000")
		self.assertEqual(h.app.state.position("x"), 235.0)

	def test_z_jog_near_bed_asks_and_stops_at_zero(self):
		h = self.h
		h.home()
		h.open("Move", "Move Z")
		h.rotate(-200)  # 10 mm down in 0.1 steps, floor at Z=0 (position_min is -2)
		self.assertTrue(h.screen.has("0.0"))
		h.press()
		self.assertIsInstance(h.page, U.Confirm)
		h.confirm()
		self.assertEqual(h.printer.scripts[-2], "G1 Z-10 F300")
		self.assertEqual(h.app.state.position("z"), 0.0)

	def test_extrude_needs_hot_nozzle(self):
		h = self.h
		h.open("Move", "Extrude")
		self.assertTrue(h.screen.has("Heat the nozzle to 170 C"))
		h.press()
		h.printer.set("extruder", temperature=215.0, target=215.0)
		h.pump()
		h.open("Extrude")
		h.rotate(3)
		h.press()
		self.assertIn("G1 E3 F300", h.printer.scripts)

	def test_z_offset_babystep_and_save(self):
		h = self.h
		h.home()
		h.open("Z offset", "Live adjust")
		h.rotate(-5)
		self.assertTrue(h.screen.has("-0.050"))
		h.press()
		self.assertIsInstance(h.page, U.Confirm)
		self.assertTrue(h.screen.has("nozzle moves down"))
		h.confirm()
		self.assertEqual(h.printer.scripts[-1], "SET_GCODE_OFFSET Z_ADJUST=-0.05 MOVE=1")
		self.assertAlmostEqual(h.app.state.z_gcode_offset, -0.05)
		self.assertTrue(h.screen.has("-0.050"))
		h.open("Save to config")
		self.assertIsInstance(h.page, U.Confirm)
		self.assertTrue(h.screen.has("2.500 -> 2.550"))
		h.confirm()
		self.assertEqual(h.printer.scripts[-2:], ["Z_OFFSET_APPLY_PROBE", "SAVE_CONFIG"])
		self.assertEqual(h.printer.config_saves, 1)
		# SAVE_CONFIG restarts Klipper; the display follows and comes back to the main menu
		self.assertIsInstance(h.page, U.MainMenu)
		self.assertEqual(h.app.state.probe_z_offset, 2.55)
		self.assertEqual(h.app.state.z_gcode_offset, 0.0)

	def test_fast_turn_accelerates_except_z_offset(self):
		h = self.h
		h.home()
		h.open("Move", "Move X")
		h.rotate(1)
		h.rotate(1, fast=True)  # detents 1 ms apart: x5
		self.assertEqual(h.page.editing[1], 161.0)
		h.press()
		h.open("Back", "Z offset", "Live adjust")
		h.rotate(-1)
		h.rotate(-1, fast=True)
		self.assertAlmostEqual(h.page.editing[1], -0.02)

	def test_z_save_warns_about_other_pending_changes(self):
		h = self.h
		h.home()
		h.printer.handle("printer.gcode.script", {"script": "SET_GCODE_OFFSET Z_ADJUST=0.1"})
		h.printer.pending_config["bed_mesh default"] = {"points": "..."}
		h.printer.set("configfile", save_config_pending_items={"bed_mesh default": {"points": "..."}})
		h.pump()
		h.open("Z offset", "Save to config")
		self.assertTrue(h.screen.has("Also saves pending:"))
		self.assertTrue(h.screen.has("bed_mesh default"))
		h.confirm(yes=False)
		self.assertNotIn("SAVE_CONFIG", h.printer.scripts)

	def test_preheat_and_cooldown(self):
		h = self.h
		h.open("Preheat PLA")
		self.assertEqual(h.printer.scripts[-2:], ["SET_HEATER_TEMPERATURE HEATER=extruder TARGET=200",
			"SET_HEATER_TEMPERATURE HEATER=heater_bed TARGET=60"])
		h.open("Cooldown")
		self.assertEqual(h.printer.scripts[-2:], ["TURN_OFF_HEATERS", "M107"])

	def test_gcode_errors_are_shown(self):
		h = self.h
		h.printer.fail_commands["M84"] = "Stepper enable failed (simulated)"
		h.open("Disable steppers")
		self.assertIsInstance(h.page, U.Message)
		self.assertTrue(h.screen.has("Stepper enable failed"))


class ControlTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.path = os.path.join(self.tmp.name, "dwin_lcd.conf")
		with open(self.path, "w") as fh:
			fh.write("# comment kept\n[preheat_1]\nname = PLA\nhotend_temp = 200\nbed_temp = 60\n")
		self.h = Harness(cfg=load_config(self.path))
		self.h.rotate(2)
		self.h.press()
		self.assertIsInstance(self.h.page, U.ControlMenu)

	def tearDown(self):
		self.tmp.cleanup()

	def test_temperature_menu_sends_targets(self):
		h = self.h
		h.open("Temperature", "Nozzle target")
		h.rotate(1)
		h.press()
		self.assertEqual(h.printer.scripts[-1], "SET_HEATER_TEMPERATURE HEATER=extruder TARGET=1")
		h.open("Bed target")
		h.rotate(1)
		h.press()
		self.assertEqual(h.printer.scripts[-1], "SET_HEATER_TEMPERATURE HEATER=heater_bed TARGET=1")

	def test_preset_edit_and_save_persist(self):
		h = self.h
		h.open("Temperature", "PLA preheat", "Nozzle")
		h.rotate(5)
		h.press()
		h.open("Save")
		self.assertTrue(h.screen.has("Saved"))
		with open(self.path) as fh:
			text = fh.read()
		self.assertIn("# comment kept", text)
		self.assertEqual(load_config(self.path).presets[0].hotend_temp, 205)
		self.assertEqual(h.printer.scripts, [], "editing a preset sends nothing")

	def test_motion_limits_capped(self):
		h = self.h
		h.open("Motion", "Max velocity")
		h.rotate(10)  # above printer.cfg max_velocity 300: stays at 300
		self.assertTrue(h.screen.has("300"))
		h.rotate(-2)
		h.press()
		self.assertEqual(h.printer.scripts[-1], "SET_VELOCITY_LIMIT VELOCITY=290")
		h.open("Restore printer.cfg")
		self.assertTrue(h.printer.scripts[-1].startswith("SET_VELOCITY_LIMIT VELOCITY=300 ACCEL=4500"))

	def test_info_page(self):
		h = self.h
		h.open("Info")
		self.assertTrue(h.screen.has("235x235x250 mm"))
		self.assertTrue(h.screen.has("v0.13.0-mock"))
		self.assertTrue(h.screen.has("v0.10.0-mock"))
		h.press()
		self.assertIsInstance(h.page, U.ControlMenu)


class PrintTest(unittest.TestCase):
	def start_print(self, h):
		h.press()  # main menu: Print
		self.assertIsInstance(h.page, U.FileMenu)
		labels = [item.label for item in h.page.items]
		self.assertEqual(labels, ["Back", "bracket_v2", "calibration_cube"])  # newest first, .gcode only
		h.open("calibration_cube")
		self.assertIsInstance(h.page, U.Confirm)
		h.confirm()
		self.assertIsInstance(h.page, U.PrintPage)

	def test_print_pause_resume_cancel(self):
		h = Harness()
		self.start_print(h)
		self.assertEqual(h.printer.status["print_stats"]["filename"], "calibration_cube.gcode")
		self.assertTrue(h.screen.has("calibration_cube.gcode"))
		h.rotate(1)
		h.press()
		self.assertIsInstance(h.page, U.Confirm)
		h.confirm()
		self.assertEqual(h.printer.status["print_stats"]["state"], "paused")
		self.assertTrue(h.screen.has("Paused") and h.screen.has("[Resume*]"))
		h.press()
		h.confirm()
		self.assertEqual(h.printer.status["print_stats"]["state"], "printing")
		h.rotate(1)
		h.press()
		h.press()  # Cancel preselected in the popup: nothing happens
		self.assertEqual(h.printer.status["print_stats"]["state"], "printing")
		h.press()
		h.confirm()
		self.assertEqual(h.printer.status["print_stats"]["state"], "cancelled")
		self.assertIsInstance(h.page, U.Message)
		self.assertTrue(h.screen.has("Print cancelled"))
		h.press()
		self.assertIsInstance(h.page, U.MainMenu)

	def test_tune_changes_only_speed_flow_and_temps(self):
		h = Harness()
		self.start_print(h)
		h.press()  # Tune
		self.assertIsInstance(h.page, U.TuneMenu)
		self.assertEqual([item.label for item in h.page.items], ["Back", "Print speed", "Flow", "Nozzle temp",
			"Bed temp"])
		h.open("Bed temp")
		h.rotate(3)
		h.press()
		self.assertEqual(h.printer.scripts[-1], "SET_HEATER_TEMPERATURE HEATER=heater_bed TARGET=3")
		h.open("Nozzle temp")
		h.rotate(-5)  # from 0: clamps at min_extrude_temp while printing
		h.press()
		self.assertEqual(h.printer.scripts[-1], "SET_HEATER_TEMPERATURE HEATER=extruder TARGET=170")
		h.open("Print speed")
		h.rotate(-10)
		h.press()
		self.assertEqual(h.printer.scripts[-1], "M220 S90")
		h.open("Flow")
		h.rotate(2)
		h.press()
		self.assertEqual(h.printer.scripts[-1], "M221 S102")

	def test_print_started_elsewhere_leaves_motion_menus(self):
		h = Harness()
		h.home()
		h.rotate(1)
		h.press()
		h.open("Move", "Move X")
		self.assertIsNotNone(h.page.editing)
		h.printer.start_print("calibration_cube.gcode")  # e.g. from Mainsail
		h.pump()
		self.assertIsInstance(h.page, U.PrintPage)
		self.assertEqual(len(h.app.ui.stack), 1)
		h.press()  # Tune, not a jog
		self.assertIsInstance(h.page, U.TuneMenu)
		self.assertNotIn("G91", h.printer.scripts)

	def test_complete_screen(self):
		h = Harness()
		self.start_print(h)
		h.printer.advance(60)
		h.pump()
		self.assertTrue(h.screen.has("50%"))
		h.printer.finish_print()
		h.pump()
		self.assertIsInstance(h.page, U.PrintPage)
		self.assertTrue(h.page.done)
		self.assertTrue(h.screen.has("Print complete") and h.screen.has("100%"))
		h.press()
		self.assertIsInstance(h.page, U.MainMenu)

	def test_klipper_shutdown_during_print(self):
		h = Harness()
		self.start_print(h)
		h.printer.shutdown("Heater extruder not heating at expected rate")
		h.pump()
		self.assertIsInstance(h.page, U.StatusScreen)
		self.assertTrue(h.screen.has("Klipper shutdown"))
		self.assertTrue(h.screen.has("Heater extruder not heating"))
		self.assertTrue(h.screen.has("FIRMWARE_RESTART"))


class ResilienceTest(unittest.TestCase):
	def test_firmware_restart_behind_confirmation(self):
		h = Harness()
		h.printer.shutdown("MCU 'mcu' shutdown: Timer too close")
		h.pump()
		self.assertIsInstance(h.page, U.StatusScreen)
		h.press()
		self.assertIsInstance(h.page, U.Confirm)
		h.press()  # Cancel
		self.assertEqual(h.printer.klippy_state, "shutdown")
		h.press()
		h.confirm()
		self.assertIn(("printer.firmware_restart", None), h.client.calls)
		self.assertIsInstance(h.page, U.MainMenu)

	def test_klipper_error_state_at_startup(self):
		printer = __import__("dwinlcd.mock", fromlist=["FakePrinter"]).FakePrinter()
		printer.klippy_state = "error"
		printer.state_message = "Option 'max_temp' in section 'extruder' must be specified"
		h = Harness(printer=printer)
		self.assertIsInstance(h.page, U.StatusScreen)
		self.assertTrue(h.screen.has("Klipper error"))
		self.assertTrue(h.screen.has("max_temp"))
		printer.klippy_state = "ready"
		h.app._poll()  # the 5 s poll notices Klipper came back
		h.pump()
		self.assertIsInstance(h.page, U.MainMenu)

	def test_moonraker_restart(self):
		h = Harness()
		h.client.disconnect()
		h.pump()
		self.assertIsInstance(h.page, U.StatusScreen)
		self.assertTrue(h.screen.has("Waiting for Moonraker"))
		h.press()  # nothing to do on this screen
		self.assertIsInstance(h.page, U.StatusScreen)
		h.client.start()
		h.pump()
		self.assertIsInstance(h.page, U.MainMenu)

	def test_klipper_restart_resubscribes(self):
		h = Harness()
		subscribes = sum(1 for method, _ in h.client.calls if method == "printer.objects.subscribe")
		h.printer.restart()
		h.pump()
		self.assertIsInstance(h.page, U.MainMenu)
		self.assertEqual(sum(1 for method, _ in h.client.calls if method == "printer.objects.subscribe"),
			subscribes + 1)
		h.printer.set("extruder", target=190.0)
		h.pump()
		self.assertTrue(h.screen.has("/190"))

	def test_internal_error_recovers(self):
		h = Harness()
		h.app.ui.page.press = lambda: 1 / 0
		with self.assertLogs("dwinlcd.app", "ERROR"):
			h.press()
		self.assertIsInstance(h.page, U.MainMenu)
		h.rotate(1)
		self.assertEqual(h.page.sel, 1)


if __name__ == "__main__":
	unittest.main()
