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


if __name__ == "__main__":
	unittest.main()
