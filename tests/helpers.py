import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dwinlcd.app import App  # noqa: E402
from dwinlcd.config import Config  # noqa: E402
from dwinlcd.mock import FakeClient, FakePrinter, MockSerial  # noqa: E402

logging.getLogger("dwinlcd").setLevel(logging.CRITICAL)


class Harness:
	"""App wired to a FakePrinter and a virtual screen; events are processed synchronously."""

	def __init__(self, cfg=None, printer=None, display=True):
		self.cfg = cfg or Config(path=None)
		self.printer = printer or FakePrinter()
		self.port = MockSerial(present=display)
		self.client = None

		def client_factory(path, on_event):
			self.client = FakeClient(self.printer, on_event)
			return self.client

		self.app = App(self.cfg, client_factory=client_factory, serial_factory=lambda: self.port,
			input_factory=False)
		self.app.start()
		self.pump()

	@property
	def screen(self):
		return self.port.screen

	@property
	def page(self):
		return self.app.ui.page

	def pump(self):
		self.app.process_events()
		self.app.ui.tick()
		self.app._flush_lcd()

	def rotate(self, steps, fast=False):
		if not fast:
			self.app._last_rotate = -1e9  # slow turn: no acceleration, one step per detent
		self.app.events.put(("rotate", steps))
		self.pump()

	def press(self):
		self.app.events.put(("press",))
		self.pump()

	def select(self, label):
		"""Move the list cursor to the row whose label starts with `label`."""
		page = self.page
		labels = [item.label for item in page.items]
		for index, text in enumerate(labels):
			if text.startswith(label):
				self.rotate(index - page.sel)
				return
		raise AssertionError("%r not in menu %s" % (label, labels))

	def open(self, *labels):
		for label in labels:
			self.select(label)
			self.press()

	def confirm(self, yes=True):
		self.rotate(-1 if yes else 1)
		self.press()

	def home(self):
		self.printer.handle("printer.gcode.script", {"script": "G28"})
		self.pump()

	def scripts_since(self, count):
		return self.printer.scripts[count:]
