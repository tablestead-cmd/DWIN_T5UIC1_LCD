# Serial protocol driver for the DWIN T5UIC1 display (Creality Ender 3 V2 stock screen).
#
# Derived from DWIN_Screen.py in odwdinc/DWIN_T5UIC1_LCD, which is a Python port of
# Marlin's dwin_lcd.cpp. Licensed under the GNU GPL v3 (see LICENSE).
#
# Every drawing call appends one frame (0xAA, command, payload..., CC 33 C3 3C) to an
# output buffer. Nothing is written to the port until flush(), so a whole screen update
# goes out in one write. handshake() is the only call that reads from the port.

import logging
import time

log = logging.getLogger(__name__)

FHONE = 0xAA
TAIL = b"\xCC\x33\xC3\x3C"
HANDSHAKE_REPLY = b"\xAA\x00OK"

DWIN_WIDTH = 272
DWIN_HEIGHT = 480

# Font sizes: 0x00=6*12 0x01=8*16 0x02=10*20 0x03=12*24 0x04=14*28
#             0x05=16*32 0x06=20*40 0x07=24*48 0x08=28*56 0x09=32*64
font6x12 = 0x00
font8x16 = 0x01
font10x20 = 0x02
font12x24 = 0x03
font14x28 = 0x04
font16x32 = 0x05
font20x40 = 0x06
font24x48 = 0x07
font28x56 = 0x08
font32x64 = 0x09

# Colours (RGB565)
Color_White = 0xFFFF
Color_Yellow = 0xFF0F
Color_Gray = 0x8410  # disabled menu items
Color_Bg_Window = 0x31E8  # Popup background color
Color_Bg_Blue = 0x1125  # Dark blue background color
Color_Bg_Black = 0x0841  # Black background color
Color_Bg_Red = 0xF00F  # Red background color
Popup_Text_Color = 0xD6BA  # Popup font background color
Line_Color = 0x3A6A  # Split line color
Rectangle_Color = 0xEE2F  # Blue square cursor color
Percent_Color = 0xFE29  # Percentage color
BarFill_Color = 0x10E4  # Fill color of progress bar
Select_Color = 0x33BB  # Selected color

DWIN_FONT_MENU = font8x16
DWIN_FONT_STAT = font10x20
DWIN_FONT_HEAD = font10x20


def _u8(value):
	return max(0, min(0xFF, int(value)))


def _u16(value):
	return max(0, min(0xFFFF, int(value)))


def _word(value):
	return _u16(value).to_bytes(2, "big")


def _text(string, limit=None):
	"""The display fonts are ASCII only; anything else becomes '?'."""
	data = str(string).encode("ascii", "replace")
	data = bytes(c if 0x20 <= c < 0x7F else 0x3F for c in data)
	if limit is not None:
		data = data[:max(0, int(limit))]
	return data


class T5UIC1_LCD:
	"""Frame builder for the T5UIC1. `port` is a pyserial Serial or a compatible mock."""

	DWIN_WIDTH = DWIN_WIDTH
	DWIN_HEIGHT = DWIN_HEIGHT

	def __init__(self, port):
		self.port = port
		self._buf = bytearray()
		self.bytes_sent = 0

	# ---------------------------------------------------------------- transport

	def _send(self, cmd, payload=b""):
		self._buf.append(FHONE)
		self._buf.append(cmd)
		self._buf += payload
		self._buf += TAIL

	@property
	def pending(self):
		return len(self._buf)

	def flush(self):
		"""Write all buffered frames to the port. Raises the port's I/O errors."""
		if not self._buf:
			return
		data = bytes(self._buf)
		self._buf.clear()
		self.port.write(data)
		self.bytes_sent += len(data)

	def handshake(self, timeout=0.5):
		"""Send the handshake frame; True if the display answers AA 00 'O' 'K'."""
		self.flush()
		reset = getattr(self.port, "reset_input_buffer", None)
		if reset is not None:
			reset()
		self.port.write(bytes((FHONE, 0x00)) + TAIL)
		deadline = time.monotonic() + timeout
		received = bytearray()
		while time.monotonic() < deadline:
			waiting = getattr(self.port, "in_waiting", 0) or 1
			chunk = self.port.read(waiting)
			if chunk:
				received += chunk
				if HANDSHAKE_REPLY in received:
					return True
		if received:
			log.debug("DWIN: unexpected handshake reply %s", received.hex(" "))
		return False

	def init_display(self, brightness=None):
		"""Show the boot picture, set orientation and cache the label picture (JPG 1)."""
		self.JPG_ShowAndCache(0)
		self.Frame_SetDir(1)
		self.JPG_CacheTo1(1)
		if brightness is not None:
			self.Backlight_SetLuminance(brightness)
		self.UpdateLCD()

	# ------------------------------------------------------ system variables

	# Set the backlight luminance (0x1F-0xFF; lower values are raised to 0x1F like Marlin)
	def Backlight_SetLuminance(self, luminance):
		self._send(0x30, bytes((max(_u8(luminance), 0x1F),)))

	# Set screen display direction: 0=0deg, 1=90deg, 2=180deg, 3=270deg
	def Frame_SetDir(self, dir):
		self._send(0x34, bytes((0x5A, 0xA5, _u8(dir))))

	# Refresh the display with everything drawn so far
	def UpdateLCD(self):
		self._send(0x3D)

	# -------------------------------------------------------------- drawing

	# Clear the whole screen with a colour
	def Frame_Clear(self, color):
		self._send(0x01, _word(color))

	# Draw a point: width/height 0x01-0x0F, x/y upper left
	def Draw_Point(self, color, width, height, x, y):
		self._send(0x02, _word(color) + bytes((_u8(width), _u8(height))) + _word(x) + _word(y))

	# Draw a line from (xStart, yStart) to (xEnd, yEnd)
	def Draw_Line(self, color, xStart, yStart, xEnd, yEnd):
		self._send(0x03, _word(color) + _word(xStart) + _word(yStart) + _word(xEnd) + _word(yEnd))

	# Draw a rectangle. mode: 0=frame, 1=fill, 2=XOR fill
	def Draw_Rectangle(self, mode, color, xStart, yStart, xEnd, yEnd):
		self._send(0x05, bytes((_u8(mode),)) + _word(color) + _word(xStart) + _word(yStart)
			+ _word(xEnd) + _word(yEnd))

	# Move a screen area. mode: 0=circle shift, 1=translation; dir: 0=left 1=right 2=up 3=down
	def Frame_AreaMove(self, mode, dir, dis, color, xStart, yStart, xEnd, yEnd):
		self._send(0x09, bytes((((_u8(mode) & 1) << 7) | (_u8(dir) & 0x03),)) + _word(dis) + _word(color)
			+ _word(xStart) + _word(yStart) + _word(xEnd) + _word(yEnd))

	# ---------------------------------------------------------------- text

	#  Draw a string
	#   widthAdjust: True=self-adjust character width; False=no adjustment
	#   bShow: True=display background color; False=don't display background color
	#   size: Font size
	#   color: Character color
	#   bColor: Background color
	#   x/y: Upper-left coordinate of the string
	#   limit: optional maximum number of characters
	def Draw_String(self, widthAdjust, bShow, size, color, bColor, x, y, string, limit=None):
		data = _text(string, limit)
		if not data:
			return
		flags = (0x80 if widthAdjust else 0) | (0x40 if bShow else 0) | (_u8(size) & 0x0F)
		self._send(0x11, bytes((flags,)) + _word(color) + _word(bColor) + _word(x) + _word(y) + data)

	#  Draw a positive integer (iNum digits). Negative values are drawn as 0.
	def Draw_IntValue(self, bShow, zeroFill, zeroMode, size, color, bColor, iNum, x, y, value):
		flags = (0x80 if bShow else 0) | (0x20 if zeroFill else 0) | (0x10 if zeroMode else 0) | (_u8(size) & 0x0F)
		value = max(0, min((1 << 64) - 1, int(value)))
		self._send(0x14, bytes((flags,)) + _word(color) + _word(bColor) + bytes((_u8(iNum), 0))
			+ _word(x) + _word(y) + value.to_bytes(8, "big"))

	#  Draw a fixed point number: value is already multiplied by 10**fNum
	def Draw_FloatValue(self, bShow, zeroFill, zeroMode, size, color, bColor, iNum, fNum, x, y, value):
		flags = (0x80 if bShow else 0) | (0x20 if zeroFill else 0) | (0x10 if zeroMode else 0) | (_u8(size) & 0x0F)
		value = max(0, min(0xFFFFFFFF, int(value)))
		self._send(0x14, bytes((flags,)) + _word(color) + _word(bColor) + bytes((_u8(iNum), _u8(fNum)))
			+ _word(x) + _word(y) + value.to_bytes(4, "big"))

	def Draw_Signed_Float(self, size, bColor, iNum, fNum, x, y, value):
		sign = "-" if value < 0 else " "
		self.Draw_String(False, True, size, Color_White, bColor, x - 6, y, sign)
		self.Draw_FloatValue(True, True, 0, size, Color_White, bColor, iNum, fNum, x, y, abs(value))

	# ------------------------------------------------------------- pictures

	# Draw JPG and cache it in virtual display area #0
	def JPG_ShowAndCache(self, id):
		self._send(0x22, bytes((0x00, _u8(id))))

	#  Draw an icon from an icon library
	def ICON_Show(self, libID, picID, x, y):
		x = min(int(x), DWIN_WIDTH - 1)
		y = min(int(y), DWIN_HEIGHT - 1)
		self._send(0x23, _word(x) + _word(y) + bytes((0x80 | _u8(libID), _u8(picID))))

	# Unzip a JPG picture into virtual display area n
	def JPG_CacheToN(self, n, id):
		self._send(0x25, bytes((_u8(n), _u8(id))))

	def JPG_CacheTo1(self, id):
		self.JPG_CacheToN(1, id)

	#  Copy an area from a virtual display area to the current screen
	#   cacheID: virtual area number
	#   xStart/yStart/xEnd/yEnd: source rectangle
	#   x/y: screen paste point
	def Frame_AreaCopy(self, cacheID, xStart, yStart, xEnd, yEnd, x, y):
		self._send(0x27, bytes((0x80 | _u8(cacheID),)) + _word(xStart) + _word(yStart) + _word(xEnd)
			+ _word(yEnd) + _word(x) + _word(y))

	def Frame_TitleCopy(self, id, x1, y1, x2, y2):
		self.Frame_AreaCopy(id, x1, y1, x2, y2, 14, 8)


class NullLCD:
	"""Stand-in used while no display is connected: every drawing call is a no-op."""

	pending = 0

	def __getattr__(self, name):
		return _noop


def _noop(*args, **kwargs):
	return None
