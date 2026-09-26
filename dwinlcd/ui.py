# Screen pages for the Ender 3 V2 DWIN display.
#
# Layout, icon numbers and the stock label bitmaps come from Marlin's DWIN UI (via
# odwdinc/DWIN_T5UIC1_LCD, GPL-3.0). The UI is a stack of pages that only the app's main
# thread touches; pages append frames to the display buffer and the app flushes them.
# List menus draw text labels, so they do not depend on the label picture in the display.

import functools
import logging
import math
import os
import textwrap

from . import dwin as D
from . import printer as P
from .moonraker import error_message

log = logging.getLogger(__name__)

GCODE_TIMEOUT = 600.0

# Layout (Marlin dwin.cpp)
TROWS = 6  # rows in a list menu, Back included
MLINE = 53  # menu line height
LBLX = 60  # menu item label x
VALUE_X = 200  # value field: 7 characters, right aligned, up to x=256
VALUE_CHARS = 7
MENU_CHR_W = 8
STATUS_Y = 360
POPUP_CHARS = 28
SCREEN_CHARS = 32


def MBASE(line):
	return 49 + MLINE * line


# Icon library 9 in the display's flash (stock Creality DWIN_SET)
ICON = 0x09
ICON_LOGO = 0
ICON_Print_0 = 1
ICON_Print_1 = 2
ICON_Prepare_0 = 3
ICON_Prepare_1 = 4
ICON_Control_0 = 5
ICON_Control_1 = 6
ICON_HotendTemp = 9
ICON_BedTemp = 10
ICON_Speed = 11
ICON_Zoffset = 12
ICON_Back = 13
ICON_File = 14
ICON_PrintTime = 15
ICON_RemainTime = 16
ICON_Setup_0 = 17
ICON_Setup_1 = 18
ICON_Pause_0 = 19
ICON_Pause_1 = 20
ICON_Continue_0 = 21
ICON_Continue_1 = 22
ICON_Stop_0 = 23
ICON_Stop_1 = 24
ICON_Bar = 25
ICON_More = 26
ICON_Axis = 27
ICON_CloseMotor = 28
ICON_Homing = 29
ICON_SetHome = 30
ICON_PLAPreheat = 31
ICON_ABSPreheat = 32
ICON_Cool = 33
ICON_MoveX = 35
ICON_MoveY = 36
ICON_MoveZ = 37
ICON_Extruder = 38
ICON_Temperature = 40
ICON_Motion = 41
ICON_WriteEEPROM = 42
ICON_ResumeEEPROM = 44
ICON_Info = 45
ICON_SetEndTemp = 46
ICON_SetBedTemp = 47
ICON_FanSpeed = 48
ICON_SetPLAPreheat = 49
ICON_SetABSPreheat = 50
ICON_MaxSpeed = 51
ICON_MaxAccelerated = 52
ICON_MaxJerk = 53
ICON_Step = 54
ICON_PrintSize = 55
ICON_Version = 56
ICON_Contact = 57
ICON_StepE = 74
ICON_SetZOffset = 76
ICON_BLTouch = 78
ICON_Cancel_E = 87
ICON_Confirm_E = 89
ICON_Info_0 = 90
ICON_Info_1 = 91

# Label bitmaps in the display's English picture (JPG 1, cached in virtual area 1).
# name -> source rectangle; used by the main menu and the print screen.
LABELS = {
	"Home": (0, 2, 39, 12),
	"Print": (1, 423, 31, 435), "Print*": (1, 451, 31, 463),
	"Prepare": (33, 423, 82, 438), "Prepare*": (33, 451, 82, 466),
	"Control": (85, 423, 132, 434), "Control*": (85, 451, 132, 463),
	"Info": (132, 423, 159, 435), "Info*": (132, 451, 159, 466),
	"Tune": (0, 438, 32, 448), "Tune*": (0, 466, 34, 476),
	"Pause": (177, 423, 215, 433), "Pause*": (177, 451, 216, 462),
	"Resume": (1, 424, 31, 434), "Resume*": (1, 452, 32, 464),
	"Stop": (218, 423, 247, 436), "Stop*": (218, 452, 249, 466),
	"Print time": (0, 44, 96, 58),
	"Remain": (98, 44, 152, 58),
}
LABEL_NAMES = {rect: name for name, rect in LABELS.items()}  # "*" = selected variant

# (icon normal, icon selected, x, y, width, height, label, label x, label y)
MAIN_BUTTONS = (
	(ICON_Print_0, ICON_Print_1, 17, 130, 109, 99, "Print", 57, 201),
	(ICON_Prepare_0, ICON_Prepare_1, 145, 130, 109, 99, "Prepare", 175, 201),
	(ICON_Control_0, ICON_Control_1, 17, 246, 109, 99, "Control", 48, 318),
	(ICON_Info_0, ICON_Info_1, 145, 246, 109, 99, "Info", 186, 318),
)
PRINT_BUTTONS = {
	"tune": (ICON_Setup_0, ICON_Setup_1, 8, 252, 79, 99, "Tune", 31, 325),
	"pause": (ICON_Pause_0, ICON_Pause_1, 96, 252, 79, 99, "Pause", 116, 325),
	"resume": (ICON_Continue_0, ICON_Continue_1, 96, 252, 79, 99, "Resume", 121, 325),
	"stop": (ICON_Stop_0, ICON_Stop_1, 184, 252, 79, 99, "Stop", 209, 325),
}


def wrap(text, width, max_lines):
	lines = []
	for paragraph in str(text).splitlines():
		lines.extend(textwrap.wrap(paragraph, width) or [""])
	while lines and not lines[0]:
		lines.pop(0)
	return lines[:max_lines]


def fmt(value, pattern="{:.0f}"):
	return "---" if value is None else pattern.format(value)


def hhmm(seconds):
	if seconds is None:
		return "--:--"
	seconds = max(0, int(seconds))
	return "%02d:%02d" % (seconds // 3600, seconds % 3600 // 60)


def temps(current, target):
	return "%3s/%-3s" % (fmt(current), fmt(target))


# ------------------------------------------------------------------------ UI

class UI:
	def __init__(self, app):
		self.app = app
		self.lcd = D.NullLCD()
		self.stack = []
		self.generation = 0  # bumped by reset(); callbacks from older screens are dropped
		self._needs_redraw = False
		self._status = None  # strings drawn in the status area; None = not drawn

	@property
	def online(self):
		return not isinstance(self.lcd, D.NullLCD)

	@property
	def page(self):
		return self.stack[-1] if self.stack else None

	def attach(self, lcd):
		self.lcd = lcd
		self._status = None
		self.redraw()

	def detach(self):
		self.lcd = D.NullLCD()
		self._status = None

	# ------------------------------------------------------------ stack

	def reset(self, *pages):
		self.generation += 1
		self.stack = list(pages)
		self.redraw()

	def push(self, page):
		self.stack.append(page)
		self.redraw()

	def pop(self, redraw=True):
		if len(self.stack) > 1:
			self.stack.pop()
		if redraw:
			self.redraw()
		else:
			self._needs_redraw = True

	def remove(self, page, redraw=True):
		if page not in self.stack:
			return
		if self.page is page:
			self.pop(redraw)
		else:
			self.stack.remove(page)

	def ensure_drawn(self):
		if self._needs_redraw:
			self.redraw()

	def _base_page(self):
		for page in reversed(self.stack):
			if not isinstance(page, Popup):
				return page
		return self.page

	def redraw(self):
		self._needs_redraw = False
		page = self.page
		if page is None or not self.online:
			return
		base = self._base_page()
		if base.full_screen:
			self.lcd.Frame_Clear(D.Color_Bg_Black)
			self._status = None
		else:
			self.clear_main()
		page.draw()
		if base.status_area:
			self.draw_status(force=self._status is None)

	def tick(self):
		page = self.page
		if page is None or not self.online:
			return
		page.refresh()
		if self._base_page().status_area:
			self.draw_status()

	def rotate(self, steps, mult=1):
		if self.page is not None:
			self.page.rotate(steps, mult)

	def press(self):
		if self.page is not None:
			self.page.press()

	# ---------------------------------------------------------- drawing

	def clear_title(self):
		self.lcd.Draw_Rectangle(1, D.Color_Bg_Blue, 0, 0, D.DWIN_WIDTH, 30)

	def clear_menu(self):
		self.lcd.Draw_Rectangle(1, D.Color_Bg_Black, 0, 31, D.DWIN_WIDTH, STATUS_Y)

	def clear_main(self):
		self.clear_title()
		self.clear_menu()

	def draw_title(self, text):
		self.lcd.Draw_String(False, False, D.DWIN_FONT_HEAD, D.Color_White, D.Color_Bg_Blue, 14, 4, text, limit=25)

	def draw_status(self, force=False):
		lcd = self.lcd
		state = self.app.state
		if force or self._status is None:
			lcd.Draw_Rectangle(1, D.Color_Bg_Black, 0, STATUS_Y, D.DWIN_WIDTH, D.DWIN_HEIGHT - 1)
			lcd.ICON_Show(ICON, ICON_HotendTemp, 13, 381)
			lcd.ICON_Show(ICON, ICON_Speed, 13, 429)
			lcd.ICON_Show(ICON, ICON_Zoffset, 158, 428)
			self._status = {}
		ready = state.ready
		fields = {
			"hotend": (33, 382, temps(state.hotend_temp, state.hotend_target) if ready else "---/---"),
			"speed": (53, 429, fmt(state.speed_factor, "{:3.0f}%") if ready else "---%"),
			"z": (178, 429, fmt(state.z_gcode_offset, "{:+.3f}") if ready else "  --- "),
		}
		if state.has_bed:
			if "bed_icon" not in self._status:
				lcd.ICON_Show(ICON, ICON_BedTemp, 158, 381)
				self._status["bed_icon"] = True
			fields["bed"] = (178, 382, temps(state.bed_temp, state.bed_target) if ready else "---/---")
		for key, (x, y, text) in fields.items():
			if self._status.get(key) != text:
				self._status[key] = text
				lcd.Draw_String(False, True, D.DWIN_FONT_STAT, D.Color_White, D.Color_Bg_Black, x, y, text)

	# -------------------------------------------------------- dialogs

	def message(self, title, text="", on_close=None):
		lines = wrap(text, POPUP_CHARS, 7) if isinstance(text, str) else list(text)
		self.push(Message(self, title, lines, on_close))

	def refuse(self, reason):
		self.message("Not available", reason)

	def confirm(self, title, lines, on_yes, on_no=None):
		self.push(Confirm(self, title, lines, on_yes, on_no))

	# -------------------------------------------------------- commands

	def request(self, method, params=None, busy=None, done=None, timeout=30.0):
		"""Send a Moonraker request; errors are shown in a popup (if still relevant)."""
		generation = self.generation
		busy_page = None
		if busy:
			title, lines, icon = busy
			busy_page = Busy(self, title, lines, icon)
			self.push(busy_page)

		def finished(result, error):
			if busy_page is not None:
				self.remove(busy_page, redraw=error is None)
			if generation != self.generation:
				if error:
					log.warning("%s failed: %s", method, error_message(error))
				return
			if error:
				log.warning("%s failed: %s", method, error_message(error))
				self.message("Command failed", error_message(error))
			elif done is not None:
				done()
			self.ensure_drawn()

		log.info("request: %s %s", method, params or "")
		self.app.rpc(method, params, finished, timeout)

	def gcode(self, script, busy=None, done=None):
		log.info("gcode: %s", script.replace("\n", " | "))
		self.request("printer.gcode.script", {"script": script}, busy, done, GCODE_TIMEOUT)

	def command(self, builder, *args, busy=None, done=None):
		"""Build G-code with a printer.* builder; refusals are shown instead of sent."""
		try:
			script = builder(self.app.state, *args)
		except P.CommandRefused as exc:
			log.info("refused: %s", exc)
			self.refuse(str(exc))
			return
		if script:
			self.gcode(script, busy, done)

	def jog(self, axis, target):
		"""Refresh the toolhead position, then move (the clamp needs a fresh position)."""
		generation = self.generation

		def queried(error):
			if generation != self.generation:
				return
			if error:
				self.message("Move failed", error_message(error))
				return
			self.command(P.jog, axis, target, self.app.cfg,
				busy=("Moving " + axis.upper(), ["Please wait."], None),
				done=self.app.query_position)

		self.app.query_position(queried)


# --------------------------------------------------------------------- pages

class Page:
	status_area = True  # bottom area with temperatures
	full_screen = False  # page uses the whole screen

	def __init__(self, ui):
		self.ui = ui
		self.app = ui.app

	@property
	def lcd(self):
		return self.ui.lcd

	@property
	def state(self):
		return self.app.state

	def draw(self):
		pass

	def refresh(self):
		pass

	def rotate(self, steps, mult=1):
		pass

	def press(self):
		pass


class Popup(Page):
	"""Modal box in the menu area (Marlin's Draw_Popup_Bkgd_60)."""

	def __init__(self, ui, title, lines=(), icon=None):
		super().__init__(ui)
		self.title = title
		self.lines = list(lines)
		self.icon = icon

	def _center(self, y, text, color=D.Popup_Text_Color):
		text = str(text)[:POPUP_CHARS]
		if text:
			x = (D.DWIN_WIDTH - len(text) * MENU_CHR_W) // 2
			self.lcd.Draw_String(False, True, D.font8x16, color, D.Color_Bg_Window, x, y, text)

	def draw_box(self, max_lines=7):
		self.lcd.Draw_Rectangle(1, D.Color_Bg_Window, 14, 60, 258, 330)
		if self.icon is not None:
			self.lcd.ICON_Show(ICON, self.icon, 101, 105)
			y = 220
			max_lines = min(max_lines, 2)
		else:
			y = 80
		self._center(y, self.title, D.Color_White)
		for index, line in enumerate(self.lines[:max_lines]):
			self._center(y + 30 + index * 22, line)


class Confirm(Popup):
	"""Yes/No popup. Cancel is preselected; turn left (counter-clockwise) for Confirm."""

	def __init__(self, ui, title, lines, on_yes, on_no=None):
		super().__init__(ui, title, lines)
		self.on_yes = on_yes
		self.on_no = on_no
		self.yes = False

	def draw(self):
		self.draw_box(max_lines=7)
		self.lcd.ICON_Show(ICON, ICON_Confirm_E, 26, 280)
		self.lcd.ICON_Show(ICON, ICON_Cancel_E, 146, 280)
		self._highlight()

	def _highlight(self):
		if self.yes:
			c1, c2 = D.Select_Color, D.Color_Bg_Window
		else:
			c1, c2 = D.Color_Bg_Window, D.Select_Color
		lcd = self.lcd
		lcd.Draw_Rectangle(0, c1, 25, 279, 126, 318)
		lcd.Draw_Rectangle(0, c1, 24, 278, 127, 319)
		lcd.Draw_Rectangle(0, c2, 145, 279, 246, 318)
		lcd.Draw_Rectangle(0, c2, 144, 278, 247, 319)

	def rotate(self, steps, mult=1):
		yes = steps < 0
		if yes != self.yes:
			self.yes = yes
			self._highlight()

	def press(self):
		self.ui.pop(redraw=False)
		callback = self.on_yes if self.yes else self.on_no
		log.info("confirm %r: %s", self.title, "yes" if self.yes else "no")
		if callback is not None:
			callback()
		self.ui.ensure_drawn()


class Message(Popup):
	def __init__(self, ui, title, lines=(), on_close=None):
		super().__init__(ui, title, lines)
		self.on_close = on_close

	def draw(self):
		self.draw_box()
		self.lcd.ICON_Show(ICON, ICON_Confirm_E, 86, 280)
		self.lcd.Draw_Rectangle(0, D.Select_Color, 85, 279, 186, 318)
		self.lcd.Draw_Rectangle(0, D.Select_Color, 84, 278, 187, 319)

	def press(self):
		self.ui.pop(redraw=False)
		if self.on_close is not None:
			self.on_close()
		self.ui.ensure_drawn()


class Busy(Popup):
	"""Shown while a command runs; closed when Moonraker answers. Input is ignored."""

	def __init__(self, ui, title, lines=None, icon=None):
		super().__init__(ui, title, lines or ["Please wait until done."], icon)

	def draw(self):
		self.draw_box()


class StatusScreen(Page):
	"""Full screen shown while Moonraker or Klipper is not ready."""

	status_area = False
	full_screen = True

	def __init__(self, ui):
		super().__init__(ui)
		self._shown = None

	def content(self):
		app = self.app
		state = self.state
		if not app.moonraker_connected:
			return ("Moonraker", ["Waiting for Moonraker...", ""] + wrap(app.cfg.moonraker_socket, SCREEN_CHARS, 3), False)
		klippy = state.klippy_state
		if klippy == "ready":
			return ("Klipper", ["Connecting to Klipper..."], False)
		if klippy == "startup":
			return ("Klipper", ["Klipper is starting..."], False)
		if klippy in ("shutdown", "error"):
			title = "Klipper shutdown" if klippy == "shutdown" else "Klipper error"
			return (title, wrap(state.state_message or klippy, SCREEN_CHARS, 15), True)
		return ("Klipper", ["Klipper is not connected", "to Moonraker.", "", "Waiting for Klipper..."], False)

	def draw(self):
		self._shown = title, lines, can_restart = self.content()
		lcd = self.lcd
		self.ui.clear_title()
		self.ui.draw_title(title)
		for index, line in enumerate(lines[:15]):
			lcd.Draw_String(False, False, D.font8x16, D.Color_White, D.Color_Bg_Black, 8, 45 + index * 22, line,
				limit=SCREEN_CHARS)
		if can_restart:
			lcd.Draw_Rectangle(1, D.Color_Bg_Window, 14, 400, 258, 462)
			for index, line in enumerate(("Press the knob to run", "FIRMWARE_RESTART")):
				x = (D.DWIN_WIDTH - len(line) * MENU_CHR_W) // 2
				lcd.Draw_String(False, True, D.font8x16, D.Color_White, D.Color_Bg_Window, x, 410 + index * 24, line)

	def refresh(self):
		if self.content() != self._shown:
			self.ui.redraw()

	def press(self):
		if not self.content()[2]:
			return
		self.ui.confirm("Run FIRMWARE_RESTART?",
			["Restarts Klipper and the", "printer mainboard (MCU).", "Fix the cause first."],
			lambda: self.ui.request("printer.firmware_restart"))


class MainMenu(Page):
	def __init__(self, ui):
		super().__init__(ui)
		self.sel = 0

	def draw(self):
		self.ui.draw_title(self.app.title)
		self.lcd.ICON_Show(ICON, ICON_LOGO, 71, 52)
		for index in range(len(MAIN_BUTTONS)):
			self._button(index)

	def _button(self, index):
		icon0, icon1, x, y, width, height, label, lx, ly = MAIN_BUTTONS[index]
		selected = index == self.sel
		lcd = self.lcd
		lcd.ICON_Show(ICON, icon1 if selected else icon0, x, y)
		if selected:
			lcd.Draw_Rectangle(0, D.Color_White, x, y, x + width, y + height)
		lcd.Frame_AreaCopy(1, *LABELS[label + "*" if selected else label], lx, ly)

	def rotate(self, steps, mult=1):
		new = max(0, min(len(MAIN_BUTTONS) - 1, self.sel + steps))
		if new != self.sel:
			old, self.sel = self.sel, new
			self._button(old)
			self._button(new)

	def press(self):
		ui = self.ui
		page = (FileMenu, PrepareMenu, ControlMenu, InfoPage)[self.sel](ui)
		ui.push(page)


class PrintPage(Page):
	"""Printing / paused screen (Marlin's Goto_PrintProcess) and the "complete" view."""

	def __init__(self, ui, done=False):
		super().__init__(ui)
		self.sel = 0
		self.done = done
		self._shown = {}

	def draw(self):
		self._shown = {}
		lcd = self.lcd
		self._draw_title()
		lcd.Frame_AreaCopy(1, *LABELS["Print time"], 41, 188)
		lcd.Frame_AreaCopy(1, *LABELS["Remain"], 176, 188)
		lcd.ICON_Show(ICON, ICON_PrintTime, 17, 193)
		lcd.ICON_Show(ICON, ICON_RemainTime, 150, 191)
		self._draw_name()
		self._draw_progress()
		self._draw_times()
		if self.done:
			lcd.Draw_Rectangle(1, D.Color_Bg_Black, 0, 250, D.DWIN_WIDTH - 1, STATUS_Y)
			lcd.ICON_Show(ICON, ICON_Confirm_E, 86, 283)
			lcd.Draw_Rectangle(0, D.Select_Color, 85, 282, 186, 321)
		else:
			self._draw_buttons()

	def _draw_title(self):
		if self.done:
			text = "Print complete"
		else:
			text = "Paused" if self.state.is_paused else "Printing"
		if self._shown.get("title") != text:
			self._shown["title"] = text
			self.ui.clear_title()
			self.ui.draw_title(text)

	def _draw_name(self):
		name = os.path.basename(self.state.filename)[:SCREEN_CHARS + 1] or "(no file)"
		if self._shown.get("name") == name:
			return
		self._shown["name"] = name
		self.lcd.Draw_Rectangle(1, D.Color_Bg_Black, 0, 55, D.DWIN_WIDTH - 1, 80)
		x = max(0, (D.DWIN_WIDTH - len(name) * MENU_CHR_W) // 2)
		self.lcd.Draw_String(False, False, D.font8x16, D.Color_White, D.Color_Bg_Black, x, 60, name)

	def _draw_progress(self):
		percent = 100 if self.done else int(max(0.0, min(1.0, self.state.progress or 0.0)) * 100)
		if self._shown.get("percent") == percent:
			return
		self._shown["percent"] = percent
		lcd = self.lcd
		lcd.ICON_Show(ICON, ICON_Bar, 15, 93)
		lcd.Draw_Rectangle(1, D.BarFill_Color, 16 + percent * 240 // 100, 93, 256, 113)
		lcd.Draw_String(False, True, D.font8x16, D.Percent_Color, D.Color_Bg_Black, 113, 133, ("%d%%" % percent).rjust(4))

	def _draw_times(self):
		state = self.state
		elapsed = hhmm(state.total_duration)
		if self.done:
			remain = "00:00"
		else:
			remain = hhmm(state.remaining_time(self.app.file_estimate(state.filename)))
		for key, x, text in (("elapsed", 42, elapsed), ("remain", 176, remain)):
			if self._shown.get(key) != text:
				self._shown[key] = text
				self.lcd.Draw_String(False, True, D.font8x16, D.Color_White, D.Color_Bg_Black, x, 212, text.ljust(6))

	def _button(self, key, selected):
		icon0, icon1, x, y, width, height, label, lx, ly = PRINT_BUTTONS[key]
		lcd = self.lcd
		lcd.ICON_Show(ICON, icon1 if selected else icon0, x, y)
		if selected:
			lcd.Draw_Rectangle(0, D.Color_White, x, y, x + width, y + height)
		lcd.Frame_AreaCopy(1, *LABELS[label + "*" if selected else label], lx, ly)

	def _draw_buttons(self):
		paused = self.state.is_paused
		self._shown["paused"] = paused
		self._button("tune", self.sel == 0)
		self._button("resume" if paused else "pause", self.sel == 1)
		self._button("stop", self.sel == 2)

	def refresh(self):
		self._draw_title()
		self._draw_name()
		self._draw_progress()
		self._draw_times()
		if not self.done and self._shown.get("paused") != self.state.is_paused:
			self._draw_buttons()

	def rotate(self, steps, mult=1):
		if self.done:
			return
		new = max(0, min(2, self.sel + steps))
		if new != self.sel:
			self.sel = new
			self._draw_buttons()

	def press(self):
		ui = self.ui
		state = self.state
		if self.done:
			if len(ui.stack) > 1:
				ui.pop()
			else:
				ui.reset(MainMenu(ui))
			return
		if self.sel == 0:
			ui.push(TuneMenu(ui))
		elif self.sel == 1 and state.is_paused:
			reason = P.check_can_resume(state)
			if reason:
				return ui.refuse(reason)
			ui.confirm("Resume print?", ["The toolhead moves back", "to the print."],
				lambda: ui.request("printer.print.resume"))
		elif self.sel == 1:
			reason = P.check_can_pause(state)
			if reason:
				return ui.refuse(reason)
			ui.confirm("Pause print?", ["Runs the PAUSE macro."], lambda: ui.request("printer.print.pause"))
		else:
			reason = P.check_can_cancel(state)
			if reason:
				return ui.refuse(reason)
			ui.confirm("Cancel print?", ["Runs CANCEL_PRINT.", "This cannot be undone."],
				lambda: ui.request("printer.print.cancel"))


class InfoPage(Page):
	def draw(self):
		ui = self.ui
		lcd = self.lcd
		state = self.state
		ui.draw_title("Info")
		lcd.ICON_Show(ICON, ICON_Back, 26, MBASE(0) - 3)
		lcd.Draw_String(False, False, D.font8x16, D.Color_White, D.Color_Bg_Black, LBLX, MBASE(0) - 1, "Back")
		lcd.Draw_Rectangle(1, D.Rectangle_Color, 0, MBASE(0) - 18, 14, MBASE(1) - 20)
		lcd.Draw_Line(D.Line_Color, 16, MBASE(0) + 33, 256, MBASE(0) + 34)
		size = "---"
		limits = [state.axis_limits(axis) for axis in "xyz"]
		if None not in limits:
			size = "%gx%gx%g mm" % tuple(round(hi) for _lo, hi in limits)
		rows = (
			(ICON_PrintSize, "Build volume", size),
			(ICON_Version, "Klipper", state.software_version or "---"),
			(ICON_Version, "Moonraker", state.moonraker_version or "---"),
			(ICON_Contact, "Host", state.hostname or "---"),
		)
		for index, (icon, label, value) in enumerate(rows):
			y = 100 + index * 62
			lcd.ICON_Show(ICON, icon, 26, y)
			lcd.Draw_String(False, False, D.font8x16, D.Popup_Text_Color, D.Color_Bg_Black, LBLX, y, label)
			lcd.Draw_String(False, False, D.font8x16, D.Color_White, D.Color_Bg_Black, LBLX, y + 20, value, limit=26)
			lcd.Draw_Line(D.Line_Color, 16, y + 46, 256, y + 47)

	def press(self):
		self.ui.pop()


# ----------------------------------------------------------------- list menus

class Editor:
	"""Inline number editor: turn to change, press to apply."""

	def __init__(self, get, apply, lo, hi, step=1.0, pattern="{:.0f}", accel=True):
		self.get = get
		self.apply = apply
		self.lo = lo
		self.hi = hi
		self.step = step
		self.pattern = pattern
		self.accel = accel
		self.decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0

	def bounds(self):
		lo = self.lo() if callable(self.lo) else self.lo
		hi = self.hi() if callable(self.hi) else self.hi
		return lo, hi

	def text(self, value):
		return fmt(value, self.pattern)


class Item:
	def __init__(self, label, icon=None, action=None, submenu=None, edit=None, value=None, check=None):
		self.label = label
		self.icon = icon
		self.action = action
		self.submenu = submenu
		self.edit = edit
		self.value = value
		self.check = check

	@property
	def has_value(self):
		return self.edit is not None or self.value is not None

	def value_text(self):
		if self.edit is not None:
			return self.edit.text(self.edit.get())
		if self.value is not None:
			return self.value()
		return ""

	def blocked(self):
		return self.check() if self.check is not None else None


class ListMenu(Page):
	title = ""

	def __init__(self, ui):
		super().__init__(ui)
		self.sel = 0
		self.top = 0
		self.editing = None  # [item, value]
		self._items = None
		self._shown = {}  # row -> value text on screen

	def build(self):
		return []

	def title_text(self):
		return self.title

	@property
	def items(self):
		if self._items is None:
			self._items = [Item("Back", ICON_Back, action=self.back)] + list(self.build())
		return self._items

	def back(self):
		self.ui.pop()

	def rebuild(self):
		self._items = None
		self.editing = None
		self.sel = min(self.sel, len(self.items) - 1)
		self.top = max(0, min(self.top, self.sel))
		if self.ui.page is self:
			self.ui.redraw()

	def draw(self):
		self.ui.draw_title(self.title_text())
		self._draw_rows()

	def _draw_rows(self):
		self._shown = {}
		items = self.items
		for row in range(TROWS):
			index = self.top + row
			if index >= len(items):
				break
			self._draw_row(row, items[index])
		self._cursor(self.sel - self.top, True)

	def _draw_row(self, row, item):
		lcd = self.lcd
		y = MBASE(row)
		if item.icon is not None:
			lcd.ICON_Show(ICON, item.icon, 26, y - 3)
		color = D.Color_Gray if item.blocked() else D.Color_White
		limit = 17 if item.has_value else (20 if item.submenu else 24)
		lcd.Draw_String(False, False, D.font8x16, color, D.Color_Bg_Black, LBLX, y - 1, item.label, limit=limit)
		if item.submenu is not None:
			lcd.ICON_Show(ICON, ICON_More, 226, y - 3)
		if item.has_value:
			self._draw_value(row, item)
		lcd.Draw_Line(D.Line_Color, 16, y + 33, 256, y + 34)

	def _draw_value(self, row, item, text=None, editing=False):
		if text is None:
			text = item.value_text()
		self._shown[row] = text
		background = D.Select_Color if editing else D.Color_Bg_Black
		self.lcd.Draw_String(False, True, D.font8x16, D.Color_White, background, VALUE_X, MBASE(row),
			text[-VALUE_CHARS:].rjust(VALUE_CHARS))

	def _cursor(self, row, on):
		color = D.Rectangle_Color if on else D.Color_Bg_Black
		self.lcd.Draw_Rectangle(1, color, 0, MBASE(row) - 18, 14, MBASE(row + 1) - 20)

	def refresh(self):
		items = self.items
		for row in range(TROWS):
			index = self.top + row
			if index >= len(items):
				break
			item = items[index]
			if not item.has_value or (self.editing is not None and self.editing[0] is item):
				continue
			text = item.value_text()
			if self._shown.get(row) != text:
				self._draw_value(row, item, text)

	def rotate(self, steps, mult=1):
		if self.editing is not None:
			self._edit_step(steps, mult)
			return
		new = max(0, min(len(self.items) - 1, self.sel + steps))
		if new == self.sel:
			return
		old_row = self.sel - self.top
		self.sel = new
		if new < self.top:
			self.top = new
		elif new >= self.top + TROWS:
			self.top = new - TROWS + 1
		else:
			self._cursor(old_row, False)
			self._cursor(new - self.top, True)
			return
		self.ui.clear_menu()
		self._draw_rows()

	def press(self):
		if self.editing is not None:
			self._edit_finish()
			return
		item = self.items[self.sel]
		reason = item.blocked()
		if reason:
			self.ui.refuse(reason)
		elif item.submenu is not None:
			self.ui.push(item.submenu())
		elif item.edit is not None:
			self._edit_start(item)
		elif item.action is not None:
			item.action()

	def _edit_start(self, item):
		value = item.edit.get()
		lo, hi = item.edit.bounds()
		if value is None or lo is None or hi is None:
			self.ui.refuse("Value not available yet")
			return
		self.editing = [item, float(value)]
		self._draw_value(self.sel - self.top, item, item.edit.text(value), editing=True)

	def _edit_step(self, steps, mult):
		item, value = self.editing
		edit = item.edit
		lo, hi = edit.bounds()
		if lo is None or hi is None:
			return
		step = edit.step * (mult if edit.accel else 1)
		value = round(min(hi, max(lo, value + steps * step)), edit.decimals)
		self.editing[1] = value
		self._draw_value(self.sel - self.top, item, edit.text(value), editing=True)

	def _edit_finish(self):
		item, value = self.editing
		self.editing = None
		self._draw_value(self.sel - self.top, item, item.edit.text(value))
		item.edit.apply(value)


class FileMenu(ListMenu):
	title = "Print file"

	def __init__(self, ui):
		super().__init__(ui)
		self.files = None
		self.error = None
		self.app.list_files(self._loaded)

	def _loaded(self, files, error):
		self.files = files or []
		self.error = error
		self.rebuild()

	def build(self):
		if self.files is None:
			return [Item("Loading...", ICON_File)]
		if self.error:
			return [Item("Error: " + self.error, ICON_File)]
		if not self.files:
			return [Item("No G-code files", ICON_File)]
		idle = functools.partial(P.check_idle, self.state)
		return [Item(os.path.splitext(os.path.basename(path))[0], ICON_File,
			action=functools.partial(self._pick, path), check=idle) for path in self.files]

	def _pick(self, path):
		ui = self.ui
		ui.confirm("Start print?", wrap(os.path.basename(path), POPUP_CHARS, 3) + ["", "The printer will heat", "and move."],
			lambda: ui.request("printer.print.start", {"filename": path}))


class PrepareMenu(ListMenu):
	title = "Prepare"

	def build(self):
		ui = self.ui
		state = self.state
		presets = self.app.cfg.presets
		idle = functools.partial(P.check_idle, state)
		not_printing = functools.partial(P.check_not_printing, state)
		return [
			Item("Move", ICON_Axis, submenu=lambda: MoveMenu(ui), check=idle),
			Item("Disable steppers", ICON_CloseMotor, action=lambda: ui.command(P.disable_steppers), check=idle),
			Item("Auto home", ICON_Homing, action=self._home, check=idle),
			Item("Z offset", ICON_SetHome, submenu=lambda: ZOffsetMenu(ui), check=not_printing),
			Item("Preheat " + presets[0].name, ICON_PLAPreheat,
				action=lambda: ui.command(P.preheat, presets[0]), check=not_printing),
			Item("Preheat " + presets[1].name, ICON_ABSPreheat,
				action=lambda: ui.command(P.preheat, presets[1]), check=not_printing),
			Item("Cooldown", ICON_Cool, action=lambda: ui.command(P.cooldown), check=not_printing),
		]

	def _home(self):
		ui = self.ui
		ui.confirm("Home all axes?", ["The toolhead will move.", "Keep the bed clear."],
			lambda: ui.command(P.home, busy=("Homing", ["Please wait until done."], ICON_BLTouch)))


class MoveMenu(ListMenu):
	title = "Move"
	Z_CONFIRM_BELOW = 2.0  # mm: ask before jogging the nozzle this close to the bed

	def __init__(self, ui):
		super().__init__(ui)
		self.app.query_position()

	def build(self):
		state = self.state
		homed = functools.partial(P.check_homed, state)
		items = []
		for axis, icon, step in (("x", ICON_MoveX, 1.0), ("y", ICON_MoveY, 1.0), ("z", ICON_MoveZ, 0.1)):
			items.append(Item("Move " + axis.upper(), icon, check=homed, edit=Editor(
				functools.partial(state.position, axis), functools.partial(self._jog, axis),
				functools.partial(self._lo, axis), functools.partial(self._hi, axis),
				step=step, pattern="{:.1f}")))
		items.append(Item("Extrude mm", ICON_Extruder, check=self._check_extrude, edit=Editor(
			lambda: 0.0, self._extrude, -P.MAX_EXTRUDE, P.MAX_EXTRUDE, step=1.0, pattern="{:+.0f}")))
		return items

	def _lo(self, axis):
		limits = self.state.axis_limits(axis)
		if limits is None:
			return None
		return max(limits[0], P.Z_JOG_FLOOR) if axis == "z" else limits[0]

	def _hi(self, axis):
		limits = self.state.axis_limits(axis)
		return None if limits is None else limits[1]

	def _jog(self, axis, target):
		ui = self.ui
		current = self.state.position(axis)
		if axis == "z" and target < self.Z_CONFIRM_BELOW and (current is None or target < current):
			ui.confirm("Move Z to %.1f mm?" % target, ["The nozzle will be", "close to the bed."],
				lambda: ui.jog(axis, target))
		else:
			ui.jog(axis, target)

	def _check_extrude(self):
		reason = P.check_idle(self.state)
		if reason:
			return reason
		if not self.state.can_extrude:
			return "Heat the nozzle to %d C first" % self.state.min_extrude_temp
		return None

	def _extrude(self, amount):
		self.ui.command(P.extrude, amount, self.app.cfg, busy=("Extruding", ["Please wait."], None))


class ZOffsetMenu(ListMenu):
	title = "Z offset"

	def build(self):
		state = self.state
		limit = self.app.cfg.z_offset_limit
		return [
			Item("Live adjust", ICON_SetZOffset, check=functools.partial(P.check_homed, state), edit=Editor(
				lambda: state.z_gcode_offset, self._adjust, -limit, limit, step=0.01, pattern="{:+.3f}",
				accel=False)),
			Item("Probe z_offset", ICON_Zoffset, value=lambda: fmt(state.probe_z_offset, "{:.3f}")),
			Item("Save to config", ICON_WriteEEPROM, action=self._save, check=self._check_save),
		]

	def _adjust(self, new):
		ui = self.ui
		current = self.state.z_gcode_offset or 0.0
		delta = round(new - current, 3)
		if abs(delta) < 0.0005:
			return
		ui.confirm("Z offset %+.3f mm?" % new,
			["Change %+.3f mm:" % delta, "the nozzle moves %s now." % ("up" if delta > 0 else "down"),
				"Not saved until", "Save to config."],
			lambda: ui.command(P.z_offset_adjust, new, self.app.cfg))

	def _check_save(self):
		state = self.state
		reason = P.check_idle(state)
		if reason:
			return reason
		if state.probe_z_offset is None:
			return "No probe z_offset in the config"
		if abs(state.z_gcode_offset or 0.0) < 0.0005:
			return "Adjust the live offset first"
		return None

	def _save(self):
		# SAVE_CONFIG also writes any other pending change, so look before asking.
		self.app.query({"configfile": ["save_config_pending_items"]}, self._confirm_save)

	def _confirm_save(self, error):
		ui = self.ui
		if ui.page is not self:
			return
		state = self.state
		if error:
			ui.message("Save failed", error_message(error))
			return
		new_offset = P.new_probe_offset(state)
		if new_offset is None:
			ui.refuse("Probe z_offset unknown")
			return
		pending = state.get("configfile", "save_config_pending_items", {}) or {}
		others = sorted(section for section in pending if section not in P.PROBE_SECTIONS)
		lines = ["z_offset %.3f -> %.3f" % (state.probe_z_offset, new_offset),
			"Z_OFFSET_APPLY_PROBE and", "SAVE_CONFIG: Klipper restarts."]
		if others:
			lines += ["Also saves pending:"] + wrap(", ".join(others), POPUP_CHARS, 2)
		ui.confirm("Save Z offset?", lines,
			lambda: ui.command(P.z_offset_save, busy=("Saving config", ["Klipper will restart."], None)))


class ControlMenu(ListMenu):
	title = "Control"

	def build(self):
		ui = self.ui
		return [
			Item("Temperature", ICON_Temperature, submenu=lambda: TemperatureMenu(ui)),
			Item("Motion", ICON_Motion, submenu=lambda: MotionMenu(ui)),
			Item("Info", ICON_Info, submenu=lambda: InfoPage(ui)),
		]


class TemperatureMenu(ListMenu):
	title = "Temperature"

	def build(self):
		ui = self.ui
		state = self.state
		presets = self.app.cfg.presets
		not_printing = functools.partial(P.check_not_printing, state)
		items = [Item("Nozzle target", ICON_SetEndTemp, check=not_printing, edit=Editor(
			lambda: state.hotend_target, lambda value: ui.command(P.set_hotend, value), 0,
			lambda: state.hotend_max_target))]
		if state.has_bed:
			items.append(Item("Bed target", ICON_SetBedTemp, check=not_printing, edit=Editor(
				lambda: state.bed_target, lambda value: ui.command(P.set_bed, value), 0,
				lambda: state.bed_max_target)))
		items.append(Item("Fan speed", ICON_FanSpeed, check=not_printing, edit=Editor(
			lambda: state.fan_percent, lambda value: ui.command(P.set_fan, value), 0, 100, pattern="{:.0f}%")))
		for index, icon in enumerate((ICON_SetPLAPreheat, ICON_SetABSPreheat)):
			items.append(Item(presets[index].name + " preheat", icon,
				submenu=functools.partial(PresetMenu, ui, index)))
		return items


class PresetMenu(ListMenu):
	def __init__(self, ui, index):
		super().__init__(ui)
		self.index = index

	@property
	def preset(self):
		return self.app.cfg.presets[self.index]

	def title_text(self):
		return self.preset.name + " preheat"

	def build(self):
		state = self.state
		items = [Item("Nozzle", ICON_SetEndTemp, edit=Editor(
			lambda: self.preset.hotend_temp, self._set_hotend, 0, lambda: state.hotend_max_target))]
		if state.has_bed:
			items.append(Item("Bed", ICON_SetBedTemp, edit=Editor(
				lambda: self.preset.bed_temp, self._set_bed, 0, lambda: state.bed_max_target)))
		items.append(Item("Save", ICON_WriteEEPROM, action=self.app.save_presets))
		return items

	def _set_hotend(self, value):
		self.preset.hotend_temp = int(value)

	def _set_bed(self, value):
		self.preset.bed_temp = int(value)


class MotionMenu(ListMenu):
	"""Klipper velocity limits (SET_VELOCITY_LIMIT). Not saved; printer.cfg is the maximum."""

	title = "Motion"

	def build(self):
		ui = self.ui
		state = self.state
		idle = functools.partial(P.check_idle, state)

		def editor(field, cap_key, keyword, lo, step, pattern, hi=None):
			return Editor(
				functools.partial(state.number, "toolhead", field),
				lambda value: ui.command(functools.partial(P.velocity_limits, **{keyword: value})),
				lo, hi if hi is not None else functools.partial(state.config_limit, cap_key),
				step=step, pattern=pattern)

		items = [
			Item("Max velocity", ICON_MaxSpeed, check=idle,
				edit=editor("max_velocity", "max_velocity", "velocity", 10, 5, "{:.0f}")),
			Item("Max accel", ICON_MaxAccelerated, check=idle,
				edit=editor("max_accel", "max_accel", "accel", 100, 100, "{:.0f}")),
			Item("Corner velocity", ICON_MaxJerk, check=idle,
				edit=editor("square_corner_velocity", "square_corner_velocity", "scv", 0, 0.5, "{:.1f}")),
		]
		if state.number("toolhead", "minimum_cruise_ratio") is not None:
			items.append(Item("Min cruise ratio", ICON_Step, check=idle,
				edit=editor("minimum_cruise_ratio", None, "mcr", 0, 0.05, "{:.2f}", hi=0.99)))
		items.append(Item("Restore printer.cfg", ICON_ResumeEEPROM, check=idle,
			action=lambda: ui.command(P.restore_velocity_limits)))
		return items


class TuneMenu(ListMenu):
	"""While printing: speed, flow and temperatures only."""

	title = "Tune"

	def build(self):
		ui = self.ui
		state = self.state
		items = [
			Item("Print speed", ICON_Speed, edit=Editor(
				lambda: state.speed_factor, lambda value: ui.command(P.set_speed_factor, value),
				P.SPEED_RANGE[0], P.SPEED_RANGE[1], pattern="{:.0f}%")),
			Item("Flow", ICON_StepE, edit=Editor(
				lambda: state.flow_factor, lambda value: ui.command(P.set_flow, value),
				P.FLOW_RANGE[0], P.FLOW_RANGE[1], pattern="{:.0f}%")),
			Item("Nozzle temp", ICON_HotendTemp, edit=Editor(
				lambda: state.hotend_target, lambda value: ui.command(P.set_hotend, value),
				self._nozzle_min, lambda: state.hotend_max_target)),
		]
		if state.has_bed:
			items.append(Item("Bed temp", ICON_BedTemp, edit=Editor(
				lambda: state.bed_target, lambda value: ui.command(P.set_bed, value),
				0, lambda: state.bed_max_target)))
		return items

	def _nozzle_min(self):
		state = self.state
		hi = state.hotend_max_target
		return None if hi is None else min(hi, state.min_extrude_temp)
