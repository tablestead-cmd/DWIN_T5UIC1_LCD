# Rotary encoder (knob) and push button input for the Ender 3 V2 display.
#
# QuadratureDecoder is plain Python so it can be unit tested without hardware.
# GpioInput wires it to gpiozero (lgpio pin factory on Debian 13 / MainsailOS 3).
# Callbacks run on gpiozero's/lgpio's thread; they only hand events to the app's queue.

import logging
import threading
import time

log = logging.getLogger(__name__)

MIN_PRESS_INTERVAL = 0.1  # s: presses closer together than this are contact bounce

# state = (A << 1) | B using the raw pin levels. One detent of the Ender 3 V2 knob is a
# full Gray-code cycle (Marlin uses ENCODER_PULSES_PER_STEP 4 for this screen).
# "B changes before A" is +1 (clockwise); this matches both Marlin's DWIN encoder code
# and the original encoder.py with the README wiring (A=GPIO19, B=GPIO26).
# Inverting both levels (pull-up vs pull-down) does not change the direction.
_TRANSITIONS = {
	(0, 1): 1, (1, 3): 1, (3, 2): 1, (2, 0): 1,
	(0, 2): -1, (2, 3): -1, (3, 1): -1, (1, 0): -1,
}


class QuadratureDecoder:
	"""Turns A/B pin states into +1/-1 detent steps.

	Every valid transition adds +1/-1 to an accumulator; a detent is reported once it
	reaches `pulses_per_step`. Contact bounce produces +1/-1 pairs that cancel out and
	impossible jumps (both pins changed) are ignored. When the knob has been still for
	`resync_after` seconds, the current position is taken as a detent, so a missed
	edge cannot shift the phase for long.
	"""

	def __init__(self, pulses_per_step=4, reverse=False, resync_after=0.5):
		self.pulses_per_step = max(1, int(pulses_per_step))
		self.reverse = bool(reverse)
		self.resync_after = resync_after
		self._state = None
		self._rest = None
		self._acc = 0
		self._last = None
		self._lock = threading.Lock()

	def reset(self, state, now=None):
		with self._lock:
			self._state = self._rest = state
			self._acc = 0
			self._last = now

	def update(self, state, now):
		"""Feed the new (A << 1) | B state; returns +1, -1 or 0."""
		with self._lock:
			previous = self._state
			if previous is None:
				self._state = self._rest = state
				self._last = now
				return 0
			if state == previous:
				return 0
			if self._last is not None and now - self._last >= self.resync_after:
				self._rest = previous
				self._acc = 0
			self._last = now
			self._state = state
			self._acc += _TRANSITIONS.get((previous, state), 0)
			step = 0
			if self._acc >= self.pulses_per_step:
				step = 1
			elif self._acc <= -self.pulses_per_step:
				step = -1
			elif state == self._rest and 2 * abs(self._acc) >= self.pulses_per_step:
				# Back at the detent with an edge or two lost on the way.
				step = 1 if self._acc > 0 else -1
			if step or state == self._rest:
				self._acc = 0
			return -step if self.reverse else step


class GpioInput:
	"""Knob (A/B) and button through gpiozero.

	on_rotate(steps) is called with +1 (clockwise) or -1; on_press() on each press.
	Both are called from the GPIO callback thread.
	"""

	def __init__(self, cfg, on_rotate, on_press, pin_factory=None):
		from gpiozero import Button

		self.cfg = cfg
		self._on_rotate = on_rotate
		self._on_press = on_press
		self.decoder = QuadratureDecoder(cfg.pulses_per_step, cfg.reverse)
		button_bounce = cfg.button_debounce_ms / 1000.0 or None
		if cfg.pull_up:
			self.button = Button(cfg.pin_button, pull_up=True, bounce_time=button_bounce,
				pin_factory=pin_factory)
		else:
			# External pull-up (e.g. on a level shifter): pressed still means "low".
			self.button = Button(cfg.pin_button, pull_up=None, active_state=False,
				bounce_time=button_bounce, pin_factory=pin_factory)
		self._last_press = float("-inf")
		self.button.when_pressed = self._pressed
		self.factory = self.button.pin_factory
		self.pin_a = self.factory.pin(cfg.pin_a)
		self.pin_b = self.factory.pin(cfg.pin_b)
		for pin in (self.pin_a, self.pin_b):
			pin.function = "input"
			pin.pull = "up" if cfg.pull_up else "floating"
			if cfg.encoder_debounce_ms:
				pin.bounce = cfg.encoder_debounce_ms / 1000.0
			pin.edges = "both"
		self._a = 1 if self.pin_a.state else 0
		self._b = 1 if self.pin_b.state else 0
		self.decoder.reset((self._a << 1) | self._b, time.monotonic())
		# gpiozero keeps only weak references to these bound methods; self must stay alive.
		self.pin_a.when_changed = self._a_changed
		self.pin_b.when_changed = self._b_changed
		log.info("input: pin factory %s; knob A=GPIO%d B=GPIO%d button=GPIO%d pull_up=%s reverse=%s",
			type(self.factory).__name__, cfg.pin_a, cfg.pin_b, cfg.pin_button, cfg.pull_up, cfg.reverse)

	def _a_changed(self, ticks, state):
		if state in (0, 1):
			self._a = state
			self._feed(ticks)

	def _b_changed(self, ticks, state):
		if state in (0, 1):
			self._b = state
			self._feed(ticks)

	def _feed(self, ticks):
		step = self.decoder.update((self._a << 1) | self._b, ticks if ticks is not None else time.monotonic())
		if step:
			self._on_rotate(step)

	def _pressed(self):
		# Independent of lgpio's debounce: a bouncing contact never gives two presses.
		now = time.monotonic()
		if now - self._last_press < max(MIN_PRESS_INTERVAL, self.cfg.button_debounce_ms / 1000.0):
			return
		self._last_press = now
		self._on_press()

	def close(self):
		for pin in (self.pin_a, self.pin_b):
			try:
				pin.when_changed = None
				pin.close()
			except Exception as exc:  # closing must never raise during shutdown
				log.debug("input: closing %r failed: %s", pin, exc)
		try:
			self.button.close()
		except Exception as exc:
			log.debug("input: closing button failed: %s", exc)
