import os
import tempfile
import time
import unittest

import helpers  # noqa: F401
from dwinlcd import ui as U
from dwinlcd.app import App
from dwinlcd.config import Config
from dwinlcd.mock import FakePrinter, MockMoonrakerServer, MockSerial


class SocketIntegrationTest(unittest.TestCase):
	"""The real MoonrakerClient + App loop against the mock server over a unix socket."""

	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.path = os.path.join(self.tmp.name, "moonraker.sock")
		self.printer = FakePrinter()
		self.server = MockMoonrakerServer(self.printer, self.path)
		self.port = MockSerial()
		self.app = App(Config(moonraker_socket=self.path), serial_factory=lambda: self.port, input_factory=False)

	def tearDown(self):
		self.app.shutdown()
		self.server.stop()
		self.tmp.cleanup()

	def run_until(self, condition, timeout=8.0):
		deadline = time.monotonic() + timeout
		while time.monotonic() < deadline:
			self.app.step(0.05)
			if condition():
				return
		self.fail("timed out; screen:\n" + self.port.screen.text())

	def test_start_order_and_moonraker_restart(self):
		self.app.start()  # Moonraker is not up yet
		self.run_until(lambda: self.port.screen.has("Waiting for Moonraker"), 2)
		self.server.start()
		self.run_until(lambda: isinstance(self.app.ui.page, U.MainMenu))
		self.app.events.put(("rotate", 1))
		self.app.events.put(("press",))
		self.run_until(lambda: isinstance(self.app.ui.page, U.PrepareMenu))
		self.server.stop()
		self.run_until(lambda: isinstance(self.app.ui.page, U.StatusScreen))
		self.server.start()
		self.run_until(lambda: isinstance(self.app.ui.page, U.MainMenu))
		self.printer.set("heater_bed", temperature=58.0, target=60.0)
		self.run_until(lambda: self.port.screen.has("58/60"), 3)


class AirTestGcodeTest(unittest.TestCase):
	"""tests/data/dwin_air_test.gcode (used in ONDEVICE_TEST.md) must stay cold and in bounds."""

	def test_air_test_file_is_safe(self):
		path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "dwin_air_test.gcode")
		printer = FakePrinter()
		lowest_z = None
		with open(path) as fh:
			for line in fh:
				code = line.split(";", 1)[0].strip()
				if not code:
					continue
				word = code.split()[0].upper()
				self.assertIn(word, ("G28", "G90", "G1", "G4", "M84"), code)
				self.assertNotRegex(code, r"(^|\s)E-?[0-9.]")
				if word == "G4":
					continue
				printer._gcode(code)  # raises on moves outside the axis limits
				z = printer.status["toolhead"]["position"][2]
				if word == "G1":
					lowest_z = z if lowest_z is None else min(lowest_z, z)
		self.assertGreaterEqual(lowest_z, 30.0)
		self.assertEqual(printer.status["extruder"]["target"], 0.0)


class CheckToolTest(unittest.TestCase):
	"""tools/check_moonraker.py (read-only API check used in ONDEVICE_TEST.md)."""

	def test_against_mock_moonraker(self):
		import importlib.util
		path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools",
			"check_moonraker.py")
		spec = importlib.util.spec_from_file_location("check_moonraker", path)
		tool = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(tool)
		with tempfile.TemporaryDirectory() as tmp:
			sock = os.path.join(tmp, "moonraker.sock")
			printer = FakePrinter()
			server = MockMoonrakerServer(printer, sock)
			server.start()
			try:
				lines = []
				self.assertEqual(tool.run(sock, out=lines.append), 0, "\n".join(lines))
				self.assertNotIn("G1", " ".join(printer.scripts), "the check sends no G-code")
				del printer.status["toolhead"]["axis_maximum"]
				lines = []
				self.assertEqual(tool.run(sock, out=lines.append), 1)
				self.assertTrue(any("FAIL object toolhead" in line and "axis_maximum" in line for line in lines))
			finally:
				server.stop()
		self.assertEqual(tool.run(sock, out=lambda line: None), 1, "no socket: failure, not a crash")


if __name__ == "__main__":
	unittest.main()
