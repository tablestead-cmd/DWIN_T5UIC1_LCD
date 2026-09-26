#!/usr/bin/env python3
# Read-only check of every Moonraker method and Klipper field the display relies on.
# Sends no G-code and changes nothing; safe while printing.
#
#   python3 tools/check_moonraker.py [--socket ~/printer_data/comms/moonraker.sock]
#
# Exit status 0 = everything the display needs is there, 1 = something is missing.

import argparse
import json
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dwinlcd.printer import SUBSCRIPTION  # noqa: E402

SETTINGS = {
	"printer": ["max_velocity", "max_accel", "square_corner_velocity", "minimum_cruise_ratio"],
	"extruder": ["max_temp", "min_extrude_temp"],
	"heater_bed": ["max_temp"],
	"bltouch": ["z_offset"],
}
OPTIONAL = {("toolhead", "minimum_cruise_ratio"), ("printer", "minimum_cruise_ratio"),
	("extruder", "min_extrude_temp"), ("pause_resume", "is_paused"), ("fan", "speed")}


class Rpc:
	def __init__(self, path, timeout=15.0):
		self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
		self.sock.settimeout(timeout)
		self.sock.connect(path)
		self.buffer = bytearray()
		self.next_id = 1

	def call(self, method, params=None):
		request_id = self.next_id
		self.next_id += 1
		message = {"jsonrpc": "2.0", "method": method, "id": request_id}
		if params is not None:
			message["params"] = params
		self.sock.sendall(json.dumps(message).encode("utf-8") + b"\x03")
		while True:
			while b"\x03" not in self.buffer:
				chunk = self.sock.recv(65536)
				if not chunk:
					raise ConnectionError("Moonraker closed the connection")
				self.buffer += chunk
			end = self.buffer.index(b"\x03")
			reply = json.loads(bytes(self.buffer[:end]))
			del self.buffer[:end + 1]
			if reply.get("id") == request_id:  # skip notifications
				return reply.get("result"), reply.get("error")

	def close(self):
		self.sock.close()


def run(path, out=print):
	failures = []

	def report(ok, label, detail="", optional=False):
		status = "OK  " if ok else ("WARN" if optional else "FAIL")
		out("%s %s%s" % (status, label, (": " + str(detail)) if detail != "" else ""))
		if not ok and not optional:
			failures.append(label)

	try:
		rpc = Rpc(path)
	except OSError as exc:
		report(False, "connect to " + path, exc)
		return 1
	report(True, "connect to " + path)

	result, error = rpc.call("server.connection.identify", {"client_name": "DWIN_T5UIC1_LCD-check",
		"version": "check", "type": "display", "url": "https://github.com/tablestead-cmd/DWIN_T5UIC1_LCD"})
	report(error is None, "server.connection.identify type=display", error or result, optional=True)

	result, error = rpc.call("server.info")
	report(error is None, "server.info", error or "klippy_state=%s moonraker=%s api=%s" % (
		result.get("klippy_state"), result.get("moonraker_version"), result.get("api_version_string")))
	if error is None and result.get("klippy_state") != "ready":
		report(False, "Klipper is ready", result.get("klippy_state"))
		rpc.close()
		return 1

	result, error = rpc.call("printer.info")
	report(error is None, "printer.info", error or "state=%s version=%s host=%s" % (
		result.get("state"), result.get("software_version"), result.get("hostname")))

	objects = dict(SUBSCRIPTION)
	objects["toolhead"] = list(objects["toolhead"]) + ["position"]
	result, error = rpc.call("printer.objects.query", {"objects": objects})
	report(error is None, "printer.objects.query (display fields)", error or "")
	status = (result or {}).get("status", {})
	for name, fields in objects.items():
		values = status.get(name)
		if values is None:
			report(False, "object %s" % name, "missing", optional=name in ("fan", "pause_resume"))
			continue
		missing = [field for field in fields if field not in values]
		required = [field for field in missing if (name, field) not in OPTIONAL]
		report(not required, "object %s" % name, "missing %s" % missing if missing else "",
			optional=not required and bool(missing))

	result, error = rpc.call("printer.objects.query", {"objects": {"configfile": ["settings",
		"save_config_pending_items"]}})
	settings = ((result or {}).get("status", {}).get("configfile", {}).get("settings")) or {}
	report(error is None and bool(settings), "configfile.settings", error or "%d sections" % len(settings))
	for section, options in SETTINGS.items():
		values = settings.get(section)
		if values is None and section == "bltouch":
			values = settings.get("probe")
		if values is None:
			report(False, "settings [%s]" % section, "missing")
			continue
		for option in options:
			present = option in values
			report(present, "settings [%s] %s" % (section, option), values.get(option, "missing"),
				optional=(section, option) in OPTIONAL)

	result, error = rpc.call("server.files.list", {"root": "gcodes"})
	files = result if isinstance(result, list) else []
	keys = sorted(files[0].keys()) if files else []
	report(error is None, "server.files.list root=gcodes", error or "%d files, keys %s" % (len(files), keys))
	if files:
		report("path" in keys or "filename" in keys, "file entries have a path", keys)
		first = files[0].get("path") or files[0].get("filename")
		result, error = rpc.call("server.files.metadata", {"filename": first})
		report(error is None, "server.files.metadata", error or "estimated_time=%s" % (result or {}).get(
			"estimated_time"), optional=True)
	rpc.close()
	out("")
	out("RESULT: %s" % ("everything the display needs is available" if not failures else
		"missing: " + ", ".join(failures)))
	return 1 if failures else 0


def main():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--socket", default=os.path.expanduser("~/printer_data/comms/moonraker.sock"))
	return run(parser.parse_args().socket)


if __name__ == "__main__":
	sys.exit(main())
