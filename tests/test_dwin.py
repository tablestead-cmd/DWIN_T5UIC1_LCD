import unittest

import helpers  # noqa: F401
from dwinlcd import dwin as D
from dwinlcd.mock import MockSerial


class RecordingPort:
	def __init__(self, reply=b""):
		self.written = []
		self._rx = bytearray(reply)

	@property
	def in_waiting(self):
		return len(self._rx)

	def write(self, data):
		self.written.append(bytes(data))

	def read(self, size=1):
		data = bytes(self._rx[:size])
		del self._rx[:size]
		return data

	def reset_input_buffer(self):
		pass


class FrameTest(unittest.TestCase):
	def frames(self, draw):
		port = RecordingPort()
		lcd = D.T5UIC1_LCD(port)
		draw(lcd)
		self.assertEqual(port.written, [], "nothing is written before flush()")
		lcd.flush()
		return b"".join(port.written)

	def test_frames_match_marlin_encoding(self):
		tail = "CC 33 C3 3C"
		cases = [
			(lambda lcd: lcd.Frame_SetDir(1), "AA 34 5A A5 01 " + tail),
			(lambda lcd: lcd.UpdateLCD(), "AA 3D " + tail),
			(lambda lcd: lcd.Frame_Clear(D.Color_Bg_Black), "AA 01 08 41 " + tail),
			(lambda lcd: lcd.JPG_ShowAndCache(0), "AA 22 00 00 " + tail),
			(lambda lcd: lcd.JPG_CacheTo1(1), "AA 25 01 01 " + tail),
			(lambda lcd: lcd.ICON_Show(9, 13, 26, 46), "AA 23 00 1A 00 2E 89 0D " + tail),
			(lambda lcd: lcd.Draw_Rectangle(1, 0x1125, 0, 0, 272, 30),
				"AA 05 01 11 25 00 00 00 00 01 10 00 1E " + tail),
			(lambda lcd: lcd.Draw_String(False, True, D.font8x16, 0xFFFF, 0x0841, 60, 48, "Back"),
				"AA 11 41 FF FF 08 41 00 3C 00 30 42 61 63 6B " + tail),
			(lambda lcd: lcd.Frame_AreaCopy(1, 1, 423, 31, 435, 57, 201),
				"AA 27 81 00 01 01 A7 00 1F 01 B3 00 39 00 C9 " + tail),
			(lambda lcd: lcd.Backlight_SetLuminance(0x05), "AA 30 1F " + tail),
		]
		for draw, expected in cases:
			self.assertEqual(self.frames(draw).hex(" ").upper(), expected)

	def test_values_are_clamped_not_crashing(self):
		data = self.frames(lambda lcd: (
			lcd.Draw_IntValue(True, True, 0, D.font8x16, 0xFFFF, 0, 3, 216, 49, -5),
			lcd.Draw_FloatValue(True, True, 0, D.font8x16, 0xFFFF, 0, 3, 1, 216, 49, -12.5),
			lcd.Draw_Rectangle(1, 0, -10, 500, 99999, 30),
			lcd.ICON_Show(9, 1, 400, 900),
		))
		self.assertTrue(data.startswith(b"\xAA\x14"))

	def test_non_ascii_and_limit(self):
		data = self.frames(lambda lcd: lcd.Draw_String(False, False, D.font8x16, 0, 0, 0, 0, "Bénchy\n", limit=4))
		self.assertIn(b"B?nc" + D.TAIL, data)
		self.assertEqual(self.frames(lambda lcd: lcd.Draw_String(False, False, 1, 0, 0, 0, 0, "")), b"")

	def test_handshake(self):
		self.assertTrue(D.T5UIC1_LCD(RecordingPort(b"\x00\xAA\x00OK\xCC\x33\xC3\x3C")).handshake(0.2))
		self.assertFalse(D.T5UIC1_LCD(RecordingPort(b"\xAA\x00NO")).handshake(0.1))
		self.assertTrue(D.T5UIC1_LCD(MockSerial()).handshake(0.2))
		self.assertFalse(D.T5UIC1_LCD(MockSerial(present=False)).handshake(0.05))

	def test_handshake_flushes_pending_frames_first(self):
		port = RecordingPort(b"\xAA\x00OK")
		lcd = D.T5UIC1_LCD(port)
		lcd.UpdateLCD()
		lcd.handshake(0.1)
		self.assertEqual(port.written[0], b"\xAA\x3D" + D.TAIL)
		self.assertEqual(port.written[1], b"\xAA\x00" + D.TAIL)

	def test_null_lcd(self):
		lcd = D.NullLCD()
		self.assertIsNone(lcd.Draw_String(False, False, 1, 0, 0, 0, 0, "x"))
		self.assertEqual(lcd.pending, 0)


if __name__ == "__main__":
	unittest.main()
