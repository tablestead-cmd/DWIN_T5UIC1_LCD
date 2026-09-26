import unittest

import helpers  # noqa: F401  (sets sys.path)
from dwinlcd.config import Config
from dwinlcd.encoder import QuadratureDecoder

# (A << 1) | B sequences for one detent, "B changes first" = clockwise
CW_FROM_HIGH = [2, 0, 1, 3]  # rest with both pins high (pull-ups, contacts open)
CCW_FROM_HIGH = [1, 0, 2, 3]
CW_FROM_LOW = [1, 3, 2, 0]  # rest with both pins low
CCW_FROM_LOW = [2, 3, 1, 0]


class DecoderTest(unittest.TestCase):
	def feed(self, decoder, states, start=0.0, dt=0.005):
		steps = []
		t = start
		for state in states:
			t += dt
			step = decoder.update(state, t)
			if step:
				steps.append(step)
		return steps, t

	def test_one_step_per_detent_both_rest_states(self):
		for rest, cw, ccw in ((3, CW_FROM_HIGH, CCW_FROM_HIGH), (0, CW_FROM_LOW, CCW_FROM_LOW)):
			decoder = QuadratureDecoder()
			decoder.reset(rest, 0.0)
			steps, t = self.feed(decoder, cw * 3)
			self.assertEqual(steps, [1, 1, 1], rest)
			steps, _ = self.feed(decoder, ccw * 2, start=t)
			self.assertEqual(steps, [-1, -1], rest)

	def test_direction_reversal_loses_no_detent(self):
		decoder = QuadratureDecoder()
		decoder.reset(3, 0.0)
		steps, _ = self.feed(decoder, CW_FROM_HIGH + CCW_FROM_HIGH + CW_FROM_HIGH)
		self.assertEqual(steps, [1, -1, 1])

	def test_contact_bounce_is_ignored(self):
		decoder = QuadratureDecoder()
		decoder.reset(3, 0.0)
		bouncy = [2, 3, 2, 3, 2, 0, 2, 0, 1, 0, 1, 3]
		steps, _ = self.feed(decoder, bouncy)
		self.assertEqual(steps, [1])

	def test_wiggle_without_full_detent_gives_nothing(self):
		decoder = QuadratureDecoder()
		decoder.reset(3, 0.0)
		steps, _ = self.feed(decoder, [2, 3, 1, 3, 2, 0, 2, 3])
		self.assertEqual(steps, [])

	def test_missed_edge_still_counts_on_return_to_rest(self):
		decoder = QuadratureDecoder()
		decoder.reset(3, 0.0)
		# 2 -> 1 skips state 0 (both pins changed between two callbacks): +2 at the detent
		steps, _ = self.feed(decoder, [2, 1, 3])
		self.assertEqual(steps, [1])
		steps, _ = self.feed(decoder, [2, 0, 3], start=1.0)  # 0 -> 3 skips state 1
		self.assertEqual(steps, [1])
		steps, _ = self.feed(decoder, [2, 3], start=2.0)  # one edge and back: not a detent
		self.assertEqual(steps, [])

	def test_reverse_option(self):
		decoder = QuadratureDecoder(reverse=True)
		decoder.reset(3, 0.0)
		steps, _ = self.feed(decoder, CW_FROM_HIGH)
		self.assertEqual(steps, [-1])

	def test_half_cycle_encoders(self):
		decoder = QuadratureDecoder(pulses_per_step=2)
		decoder.reset(3, 0.0)
		steps, _ = self.feed(decoder, CW_FROM_HIGH)
		self.assertEqual(steps, [1, 1])

	def test_resync_after_idle(self):
		decoder = QuadratureDecoder()
		decoder.reset(3, 0.0)
		# Half a detent, then the knob rests there for a while (learned as the new rest)
		self.feed(decoder, [2, 0], dt=0.005)
		steps, _ = self.feed(decoder, [1, 3, 2, 0], start=2.0)
		self.assertEqual(steps, [1])


@unittest.skipUnless(__import__("importlib").util.find_spec("gpiozero"), "gpiozero not installed")
class GpioInputTest(unittest.TestCase):
	def setUp(self):
		from gpiozero.pins.mock import MockFactory

		self.factory = MockFactory()
		self.events = []

	def tearDown(self):
		self.factory.close()

	def make(self, **options):
		from dwinlcd.encoder import GpioInput

		cfg = Config(**options)
		return GpioInput(cfg, self.events.append, lambda: self.events.append("press"), pin_factory=self.factory)

	def turn(self, a, b, sequence):
		pins = {"a": a, "b": b}
		for name, level in sequence:
			pin = pins[name]
			pin.drive_high() if level else pin.drive_low()

	def test_rotation_and_press(self):
		device = self.make()
		a, b, button = self.factory.pin(19), self.factory.pin(26), self.factory.pin(13)
		self.assertTrue(a.state and b.state and button.state, "pull-ups enabled")
		cw = [("b", 0), ("a", 0), ("b", 1), ("a", 1)]
		ccw = [("a", 0), ("b", 0), ("a", 1), ("b", 1)]
		self.turn(a, b, cw + cw + ccw)
		button.drive_low()
		button.drive_high()
		self.assertEqual(self.events, [1, 1, -1, "press"])
		device.close()

	def test_custom_pins_and_reverse(self):
		device = self.make(pin_a=5, pin_b=6, pin_button=12, reverse=True)
		a, b = self.factory.pin(5), self.factory.pin(6)
		self.turn(a, b, [("b", 0), ("a", 0), ("b", 1), ("a", 1)])
		self.assertEqual(self.events, [-1])
		device.close()


if __name__ == "__main__":
	unittest.main()
