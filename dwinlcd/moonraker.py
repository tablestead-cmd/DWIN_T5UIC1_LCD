# JSON-RPC client for Moonraker's unix socket (~/printer_data/comms/moonraker.sock).
#
# Moonraker serves the same JSON-RPC 2.0 API on this socket as on its websocket. Each
# message is UTF-8 JSON terminated by an ETX (0x03) byte; local socket connections need
# no API key. A reader thread owns the connection and reconnects with backoff, so the
# display can start before Moonraker and survives Moonraker restarts.
#
# Responses, notifications and connect/disconnect are all delivered through
# on_event(kind, payload); the app queues them and runs callbacks on its own thread.

import json
import logging
import select
import socket
import threading
import time

log = logging.getLogger(__name__)

ETX = b"\x03"
MAX_MESSAGE = 8 * 1024 * 1024
RECONNECT_DELAYS = (1, 2, 3, 5, 10)


def make_error(message, code=-1):
	return {"code": code, "message": message}


def error_message(error):
	"""User-facing text for a JSON-RPC error object."""
	if isinstance(error, dict):
		text = str(error.get("message", error))
	else:
		text = str(error)
	text = text.strip()
	if text.startswith("!!"):
		text = text[2:].strip()
	return text or "unknown error"


class MoonrakerClient:
	def __init__(self, path, on_event):
		self.path = path
		self._on_event = on_event
		self._sock = None
		self._send_lock = threading.Lock()
		self._lock = threading.Lock()
		self._pending = {}
		self._next_id = 1
		self._stop = threading.Event()
		self._thread = None

	@property
	def connected(self):
		return self._sock is not None

	def start(self):
		if self._thread is None:
			self._stop.clear()
			self._thread = threading.Thread(target=self._run, name="moonraker", daemon=True)
			self._thread.start()

	def stop(self):
		self._stop.set()
		sock = self._sock
		if sock is not None:
			try:
				sock.shutdown(socket.SHUT_RDWR)
			except OSError:
				pass
		if self._thread is not None:
			self._thread.join(timeout=3)
			self._thread = None

	def call(self, method, params=None, callback=None, timeout=30.0):
		"""Send a request; callback(result, error) is delivered via on_event("response")."""
		with self._lock:
			request_id = self._next_id
			self._next_id += 1
		message = {"jsonrpc": "2.0", "method": method, "id": request_id}
		if params is not None:
			message["params"] = params
		data = json.dumps(message, separators=(",", ":")).encode("utf-8") + ETX
		sock = self._sock
		if sock is None:
			self._respond(method, callback, None, make_error("Moonraker is not connected"))
			return request_id
		with self._lock:
			self._pending[request_id] = (callback, time.monotonic() + timeout, method)
		try:
			with self._send_lock:
				sock.sendall(data)
		except OSError as exc:
			with self._lock:
				self._pending.pop(request_id, None)
			self._respond(method, callback, None, make_error("send failed: %s" % exc))
		return request_id

	# ------------------------------------------------------------- internals

	def _respond(self, method, callback, result, error):
		if error is not None and callback is None:
			log.warning("Moonraker: %s failed: %s", method, error_message(error))
		if callback is not None:
			self._on_event("response", (callback, result, error))

	def _run(self):
		attempt = 0
		last_logged = 0.0
		while not self._stop.is_set():
			sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
			sock.settimeout(5.0)
			try:
				sock.connect(self.path)
			except OSError as exc:
				sock.close()
				delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
				now = time.monotonic()
				if attempt == 0 or now - last_logged >= 60:
					log.warning("Moonraker: cannot connect to %s (%s); retrying every %d s",
						self.path, exc.strerror or exc, delay)
					last_logged = now
				attempt += 1
				self._stop.wait(delay)
				continue
			log.info("Moonraker: connected to %s", self.path)
			attempt = 0
			self._sock = sock
			self._on_event("connected", None)
			reason = "closed by Moonraker"
			try:
				self._read_loop(sock)
			except (OSError, ValueError) as exc:
				reason = str(exc) or exc.__class__.__name__
			except Exception as exc:  # a bug must not end the connection thread for good
				log.exception("Moonraker: unexpected error")
				reason = "internal error: %s" % exc
			finally:
				self._sock = None
				try:
					sock.close()
				except OSError:
					pass
				self._fail_pending("Moonraker connection lost")
			if self._stop.is_set():
				break
			log.warning("Moonraker: connection lost (%s); reconnecting", reason)
			self._on_event("disconnected", reason)
			self._stop.wait(1.0)

	def _read_loop(self, sock):
		buffer = bytearray()
		while not self._stop.is_set():
			readable, _, _ = select.select([sock], [], [], 1.0)
			if readable:
				chunk = sock.recv(65536)
				if not chunk:
					raise ConnectionError("closed by Moonraker")
				buffer += chunk
				while True:
					end = buffer.find(ETX)
					if end < 0:
						break
					raw = bytes(buffer[:end])
					del buffer[:end + 1]
					if raw.strip():
						self._dispatch(raw)
				if len(buffer) > MAX_MESSAGE:
					raise ValueError("message larger than %d bytes" % MAX_MESSAGE)
			self._expire()

	def _dispatch(self, raw):
		try:
			message = json.loads(raw)
		except ValueError:
			log.warning("Moonraker: ignoring malformed message (%d bytes)", len(raw))
			return
		if not isinstance(message, dict):
			return
		if "id" in message and ("result" in message or "error" in message):
			request_id = message["id"]
			if not isinstance(request_id, (int, str)):
				return
			with self._lock:
				entry = self._pending.pop(request_id, None)
			if entry is None:
				log.debug("Moonraker: response for unknown id %r", message.get("id"))
				return
			callback, _deadline, method = entry
			error = message.get("error")
			if error is not None and not isinstance(error, dict):
				error = make_error(str(error))
			self._respond(method, callback, message.get("result"), error)
		elif "method" in message:
			self._on_event("notification", (message["method"], message.get("params")))

	def _expire(self):
		now = time.monotonic()
		expired = []
		with self._lock:
			for request_id, (callback, deadline, method) in list(self._pending.items()):
				if now >= deadline:
					expired.append((callback, method))
					del self._pending[request_id]
		for callback, method in expired:
			self._respond(method, callback, None, make_error("no reply from Moonraker (timeout)"))

	def _fail_pending(self, reason):
		with self._lock:
			pending = list(self._pending.values())
			self._pending.clear()
		for callback, _deadline, method in pending:
			self._respond(method, callback, None, make_error(reason))
