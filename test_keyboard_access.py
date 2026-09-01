"""Drives the app from the keyboard and checks what it says.

The audit script proves every control *has* a name. This one proves the
keyboard actually reaches and operates them: Tab really moves, Enter really
presses, arrows really change a value, and the shortcuts really answer.

Run with:  python test_keyboard_access.py
"""

import importlib.util
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

spec = importlib.util.spec_from_file_location(
    "pyp6app", os.path.join(HERE, "PyP6-Roland-P6-Sample-Manager_3_0_0.py"))
app_module = importlib.util.module_from_spec(spec)
sys.modules["pyp6app"] = app_module
spec.loader.exec_module(app_module)

tk = app_module.tk
a11y = app_module.a11y

# Capture the announcements instead of speaking them.
spoken = []
_real_speak = a11y.speak


def capture(text, interrupt=True, dedupe=True):
    if text and a11y.enabled:
        spoken.append(" ".join(str(text).split()))


a11y.speak = capture

root = tk.Tk()
app_module._verify_ui_font(root)
a11y.install(root)
app_module.install_dialog_support(root, a11y)
app = app_module.P6ManagerApp(root)
root.update_idletasks()
root.update()

failures = []
checks = 0


def check(condition, description, detail=""):
    global checks
    checks += 1
    if condition:
        print("  ok    %s" % description)
    else:
        print("  FAIL  %s   %s" % (description, detail))
        failures.append(description)


def press(widget, sequence):
    """Sends a real key event to `widget` and lets Tk settle."""
    widget.focus_set()
    root.update()
    del spoken[:]
    widget.event_generate(sequence)
    root.update()
    root.update_idletasks()


def settle(ms=200):
    """Runs the event loop for a while.

    prepare_dialog sets a dialog's initial focus from an after() callback, so
    a plain update() returns before the dialog has decided where the keyboard
    is - and key events go to the focus widget, not to the window.
    """
    done = []
    root.after(ms, lambda: done.append(True))
    while not done:
        root.update()


def last_said():
    return spoken[-1] if spoken else ""


def all_said():
    return " | ".join(spoken)


print("\n--- Tab traversal ---")
app.bank_menu.focus_set()
root.update()
del spoken[:]
app.bank_menu.event_generate("<Tab>")
root.update()
moved_to = root.focus_get()
check(moved_to is not app.bank_menu, "Tab moves focus off the bank selector",
      "still on %s" % moved_to)
check(bool(spoken), "the control Tab landed on announces itself", all_said())

print("\n--- the bank selector behaves like a combo box ---")
before = app.current_bank.get()
press(app.bank_menu, "<Down>")
after = app.current_bank.get()
check(after != before, "Down changes the bank", "%s -> %s" % (before, after))
check("2 of 8" in last_said(), "the new value is announced with its position",
      last_said())
press(app.bank_menu, "<Home>")
check(app.current_bank.get() == "A", "Home jumps to the first bank",
      app.current_bank.get())
press(app.bank_menu, "<End>")
check(app.current_bank.get() == "H", "End jumps to the last bank",
      app.current_bank.get())
app.switch_bank("A")
app.current_bank.set("A")

print("\n--- a canvas button responds to the keyboard ---")
pressed = []
probe = app_module.RoundedButton(app.top_bar, text="Probe",
                                 command=lambda: pressed.append(True))
probe.pack(side="left")
root.update()
press(probe, "<Return>")
check(pressed, "Return presses the button")
del pressed[:]
press(probe, "<space>")
check(pressed, "Space presses the button")
del spoken[:]
probe.focus_set()
root.update()
check(probe._has_focus, "the focus ring is drawn while focused")
probe.config_state("disabled")
root.update()
check(not app_module.is_focusable(probe),
      "a disabled button leaves the Tab order")
probe.destroy()

print("\n--- pad shortcuts ---")
root.focus_set()
del spoken[:]
root.event_generate("<Alt-Key-3>")
root.update()
focused = root.focus_get()
inside_pad_3 = str(focused).startswith(str(app.pad_widgets[3].panel))
check(inside_pad_3, "Alt+3 puts focus inside pad 3", str(focused))
check("Pad 3" in all_said(), "the jump announces which pad", all_said())

print("\n--- spoken summaries ---")
for key, expected, what in (("<F2>", "Bank", "F2 reports the bank and storage"),
                            ("<F3>", "Pad 1", "F3 reports all six pads"),
                            ("<F8>", "Warning", "F8 reports the warnings")):
    del spoken[:]
    root.event_generate(key)
    root.update()
    check(expected in all_said(), what, all_said()[:90])

print("\n--- speech control ---")
del spoken[:]
root.event_generate("<Control-Shift-A>")
root.update()
check(bool(spoken), "Ctrl+Shift+A repeats the last announcement", all_said()[:70])
root.event_generate("<Control-Shift-W>")
root.update()
check("Pad" in all_said() or "PyP6" in all_said(),
      "Ctrl+Shift+W says where the focus is", all_said()[:90])

print("\n--- region cycling ---")
seen_regions = []
for _ in range(5):
    del spoken[:]
    root.event_generate("<F6>")
    root.update()
    if spoken:
        seen_regions.append(spoken[0].split(".")[0])
check(len(set(seen_regions)) >= 4, "F6 cycles through the window's areas",
      ", ".join(seen_regions))

print("\n--- the waveform can be walked ---")
import numpy as np

tone = np.concatenate([np.zeros(22050, dtype=np.float32),
                       (0.8 * np.sin(np.linspace(0, 400, 22050))).astype(np.float32)])
app.show_playback_waveform(tone, 22050, "test tone")
root.update()
canvas = app.main_wave_canvas
press(canvas, "<Home>")
check("silence" in last_said(), "the start of the tone reads as silence",
      last_said())
press(canvas, "<End>")
check("percent" in last_said() and "silence" not in last_said(),
      "the end reads as a level", last_said())
before_frac = app._wave_cursor_frac
press(canvas, "<Left>")
check(app._wave_cursor_frac < before_frac, "Left moves the review cursor back",
      "%.2f -> %.2f" % (before_frac, app._wave_cursor_frac))
check("2.0 seconds" in a11y.describe(canvas),
      "the waveform describes its length in words", a11y.describe(canvas)[:90])

print("\n--- dialogs open and close from the keyboard ---")
del spoken[:]
root.event_generate("<F1>")
root.update()
helps = [w for w in root.winfo_children()
         if isinstance(w, tk.Toplevel) and w.title() == "Keyboard shortcuts"]
check(len(helps) == 1, "F1 opens the shortcut list")
if helps:
    window = helps[0]
    settle()
    check(getattr(window, "_a11y_escape_bound", False),
          "the shortcut list closes on Escape")
    focused = window.focus_get()
    check(focused is not None and focused is not window,
          "the shortcut list starts with focus on a real control", str(focused))
    (focused or window).event_generate("<Escape>")
    root.update()
    check(not window.winfo_exists(), "Escape actually closed it")

print("\n--- a check box announces its new state ---")
pad = app.pad_widgets[1]
del spoken[:]
pad.mono_cb.focus_set()
root.update()
pad.mono_cb.event_generate("<space>")
root.update()
root.update_idletasks()
check("check" in all_said().lower(), "the mono check box says checked or not",
      all_said()[:70])

print()
print("=" * 60)
print("%d checks, %d failures" % (checks, len(failures)))
for line in failures:
    print("  -", line)
print("=" * 60)

a11y.speak = _real_speak
root.destroy()
sys.exit(1 if failures else 0)
