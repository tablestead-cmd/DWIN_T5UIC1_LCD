import os
import queue
import socket
import tempfile
import threading
import time
import unittest

import helpers  # noqa: F401
from dwinlcd.moonraker import ETX, MoonrakerClient, error_message
from dwinlcd.mock import FakePrinter, MockMoonrakerServer


class ClientTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.path = os.path.join(self.tmp.name, "moonraker.sock")
		self.printer = FakePrinter()
		self.server = MockMoonrakerServer(self.printer, self.path)
		self.events = queue.Queue()
		self.client = MoonrakerClient(self.path, lambda kind, payload: self.events.put((kind, payload)))

	def tearDown(self):
		self.client.stop()
		self.server.stop()
		self.tmp.cleanup()

	def wait_for(self, kind, timeout=5.0):
		deadline = time.monotonic() + timeout
		while time.monotonic() < deadline:
			try:
				event = self.events.get(timeout=0.1)
			except queue.Empty:
				continue
			if event[0] == kind:
				return event[1]
		self.fail("no %s event" % kind)

	def call(self, method, params=None, timeout=5.0):
		self.client.call(method, params, lambda result, error: None, timeout)
		callback, result, error = self.wait_for("response")
		return result, error

	def test_starts_before_moonraker_and_connects_later(self):
		self.client.start()
		time.sleep(0.3)
		self.assertFalse(self.client.connected)
		self.server.start()
		self.wait_for("connected", timeout=6)
		result, error = self.call("server.info")
		self.assertIsNone(error)
		self.assertEqual(result["klippy_state"], "ready")

	def test_request_response_and_errors(self):
		self.server.start()
		self.client.start()
		self.wait_for("connected")
		result, error = self.call("printer.objects.query", {"objects": {"toolhead": ["homed_axes"]}})
		self.assertEqual(result["status"], {"toolhead": {"homed_axes": ""}})
		result, error = self.call("printer.gcode.script", {"script": "G1 X10"})
		self.assertIsNone(result)
		self.assertIn("Must home axis first", error_message(error))
		result, error = self.call("no.such.method")
		self.assertEqual(error["code"], -32601)

	def test_notifications_are_delivered(self):
		self.server.start()
		self.client.start()
		self.wait_for("connected")
		self.call("printer.objects.subscribe", {"objects": {"extruder": ["target"]}})
		self.printer.set("extruder", target=200.0, temperature=30.0)
		method, params = self.wait_for("notification")
		self.assertEqual(method, "notify_status_update")
		self.assertEqual(params[0], {"extruder": {"target": 200.0}})

	def test_reconnects_after_moonraker_restart(self):
		self.server.start()
		self.client.start()
		self.wait_for("connected")
		self.server.stop()
		self.wait_for("disconnected")
		result, error = self.call("server.info")
		self.assertIn("not connected", error_message(error))
		self.server.start()
		self.wait_for("connected", timeout=6)
		result, error = self.call("server.info")
		self.assertIsNone(error)

	def test_pending_requests_fail_on_disconnect_and_timeout(self):
		# A server that accepts but never answers
		listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
		listener.bind(self.path)
		listener.listen(1)
		accepted = []
		threading.Thread(target=lambda: accepted.append(listener.accept()[0]), daemon=True).start()
		self.client.start()
		self.wait_for("connected")
		result, error = self.call("server.info", timeout=1.0)
		self.assertIn("timeout", error_message(error))
		self.client.call("server.info", None, lambda result, error: None, 30)
		time.sleep(0.2)
		accepted[0].close()
		callback, result, error = self.wait_for("response")
		self.assertIn("lost", error_message(error))
		listener.close()

	def test_framing_handles_split_and_joined_messages(self):
		listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
		listener.bind(self.path)
		listener.listen(1)
		self.client.start()
		connection, _address = listener.accept()
		self.wait_for("connected")
		connection.sendall(b'{"jsonrpc":"2.0","method":"notify_klippy_ready"}' + ETX + b'{"jsonrpc":"2.0",')
		self.assertEqual(self.wait_for("notification"), ("notify_klippy_ready", None))
		connection.sendall(b'"method":"notify_klippy_shutdown"}' + ETX + b"garbage" + ETX
			+ b'{"id":[1],"result":"ok"}' + ETX + b'[1,2]' + ETX
			+ b'{"jsonrpc":"2.0","method":"notify_klippy_ready"}' + ETX)
		self.assertEqual(self.wait_for("notification"), ("notify_klippy_shutdown", None))
		self.assertEqual(self.wait_for("notification"), ("notify_klippy_ready", None))
		self.assertTrue(self.client.connected, "bad messages do not drop the connection")
		connection.close()
		listener.close()


if __name__ == "__main__":
	unittest.main()
