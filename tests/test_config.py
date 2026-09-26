import os
import tempfile
import unittest

import helpers  # noqa: F401
from dwinlcd.config import Preset, load_config, save_presets

EXAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dwin_lcd.conf.example")


class ConfigTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.path = os.path.join(self.tmp.name, "dwin_lcd.conf")

	def tearDown(self):
		self.tmp.cleanup()

	def write(self, text):
		with open(self.path, "w") as fh:
			fh.write(text)

	def read(self):
		with open(self.path) as fh:
			return fh.read()

	def test_missing_file_gives_defaults(self):
		with self.assertLogs("dwinlcd.config", "WARNING"):
			cfg = load_config(self.path)
		self.assertEqual((cfg.pin_a, cfg.pin_b, cfg.pin_button), (19, 26, 13))
		self.assertEqual(cfg.serial_port, "/dev/serial0")
		self.assertEqual(cfg.baudrate, 115200)
		self.assertFalse(cfg.reverse)
		self.assertEqual([p.name for p in cfg.presets], ["PLA", "ABS"])

	def test_values_are_parsed(self):
		self.write("[display]\nserial_port = /dev/ttyAMA0\nbaudrate = 57600\nbrightness = 200\n"
			"[encoder]\npin_a = 5\npin_b = 6\npin_button = 12\nreverse = yes  # knob backwards\n"
			"pull_up = false\n[moonraker]\nsocket = /tmp/m.sock\n[ui]\nstatus_interval = 2\n"
			"[preheat_2]\nname = PETG\nhotend_temp = 235\nbed_temp = 80\n")
		cfg = load_config(self.path)
		self.assertEqual(cfg.serial_port, "/dev/ttyAMA0")
		self.assertEqual(cfg.baudrate, 57600)
		self.assertEqual(cfg.brightness, 200)
		self.assertEqual((cfg.pin_a, cfg.pin_b, cfg.pin_button), (5, 6, 12))
		self.assertTrue(cfg.reverse)
		self.assertFalse(cfg.pull_up)
		self.assertEqual(cfg.moonraker_socket, "/tmp/m.sock")
		self.assertEqual(cfg.status_interval, 2.0)
		self.assertEqual(cfg.presets[1], Preset("PETG", 235, 80))
		self.assertEqual(cfg.presets[0], Preset("PLA", 200, 60))

	def test_bad_values_fall_back_with_warning(self):
		self.write("[display]\nbaudrate = fast\n[encoder]\npin_a = 99\nreverse = maybe\n"
			"[ui]\nstatus_interval = 0.01\n[preheat_1]\nhotend_temp = 999\n[bogus]\nx = 1\n")
		with self.assertLogs("dwinlcd.config", "WARNING") as logs:
			cfg = load_config(self.path)
		self.assertEqual(cfg.baudrate, 115200)
		self.assertEqual(cfg.pin_a, 19)
		self.assertFalse(cfg.reverse)
		self.assertEqual(cfg.status_interval, 1.0)
		self.assertEqual(cfg.presets[0].hotend_temp, 200)
		self.assertTrue(any("bogus" in line for line in logs.output))

	def test_duplicate_pins_rejected(self):
		self.write("[encoder]\npin_a = 13\n")
		with self.assertLogs("dwinlcd.config", "ERROR"):
			cfg = load_config(self.path)
		self.assertEqual((cfg.pin_a, cfg.pin_b, cfg.pin_button), (19, 26, 13))

	def test_example_file_loads_cleanly(self):
		cfg = load_config(EXAMPLE)
		self.assertEqual((cfg.pin_a, cfg.pin_b, cfg.pin_button), (19, 26, 13))
		self.assertEqual(cfg.moonraker_socket, "/home/pi/printer_data/comms/moonraker.sock")

	def test_save_presets_keeps_comments(self):
		self.write("# my display\n[encoder]\nreverse = true  # wired backwards\n\n"
			"[preheat_1]\nname = PLA\nhotend_temp = 200  # usual\n\n[ui]\ntitle = Ender\n")
		save_presets(self.path, [Preset("PLA", 210, 65), Preset("TPU", 225, 50)])
		text = self.read()
		self.assertIn("# my display", text)
		self.assertIn("reverse = true  # wired backwards", text)
		self.assertIn("hotend_temp = 210  # usual", text)
		self.assertIn("[preheat_2]", text)
		cfg = load_config(self.path)
		self.assertEqual(cfg.presets, [Preset("PLA", 210, 65), Preset("TPU", 225, 50)])
		self.assertTrue(cfg.reverse)
		self.assertEqual(cfg.title, "Ender")
		# bed_temp was missing in [preheat_1]: added inside the section, before [ui]
		self.assertLess(text.index("bed_temp = 65"), text.index("[ui]"))

	def test_save_presets_creates_file(self):
		save_presets(self.path, [Preset("PLA", 190, 55), Preset("ABS", 240, 100)])
		self.assertEqual(load_config(self.path).presets[0], Preset("PLA", 190, 55))
		self.assertEqual([name for name in os.listdir(self.tmp.name)], ["dwin_lcd.conf"])


if __name__ == "__main__":
	unittest.main()
