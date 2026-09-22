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
    "pyp6app", os.path.join(HERE, "PyP6-Roland-P6-Sample-Manager_4_2_3.py"))
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

print("\n--- the folder tree in the sample browser ---")
# ttk moves a tree with the arrow keys from its FOCUS item, not its
# selection, and does nothing at all while that item is empty. The sidebar
# is only ever selected from code (_sync_folder_tree), so without a focus
# item it looked like it was sitting on the current folder while Up and
# Down were dead - a tree only a mouse could drive.
import shutil
import tempfile

tree_base = tempfile.mkdtemp(prefix="p6tree_")
for _name in ("alpha", "bravo", "charlie"):
    os.makedirs(os.path.join(tree_base, _name, "inner"), exist_ok=True)

browser = app_module.AudioPreviewDialog(
    root, initial_dir=os.path.join(tree_base, "alpha"))
settle(300)
tree = browser.folder_tree
check(tree.focus() == os.path.join(tree_base, "alpha"),
      "the folder tree's keyboard cursor starts on the current folder",
      repr(tree.focus()))

tree.focus_set()
root.update()
press(tree, "<Up>")
check(tree.focus() == tree_base, "Up in the tree reaches the parent",
      repr(tree.focus()))
check(os.path.normpath(browser.current_dir) == os.path.normpath(tree_base),
      "and the file list follows it there", browser.current_dir)
check(bool(spoken), "the row it landed on is spoken", all_said())

# Three rows down from the parent: into the open alpha, back out of it, and
# on to bravo. The tree used to be collapsed and reopened on every one of
# these, which moved the rows out from under the cursor mid-traversal.
for _ in range(3):
    press(tree, "<Down>")
check(tree.focus() == os.path.join(tree_base, "bravo"),
      "Down walks on to the next sibling instead of the tree rebuilding"
      " itself under the cursor", repr(tree.focus()))
check(not tree.item(os.path.join(tree_base, "bravo"), "open"),
      "arrowing onto a folder does not force it open")
press(tree, "<Right>")
check(tree.item(os.path.join(tree_base, "bravo"), "open"),
      "Right opens the branch")

browser.navigate_to(os.path.join(tree_base, "charlie"))
root.update()
check(tree.focus() == os.path.join(tree_base, "charlie"),
      "navigating from elsewhere carries the tree cursor along",
      repr(tree.focus()))

browser.destroy()
root.update()


print("\n--- the sample browser answers Explorer's keys ---")
# The Load button on a pad opens AudioPreviewDialog, and "as close to a
# Windows file dialog as it can be" is the bar it is held to here.
for _n in ("kick.wav", "snare.wav", "shaker.wav"):
    _w = __import__("wave").open(os.path.join(tree_base, "alpha", _n), "wb")
    _w.setnchannels(1); _w.setsampwidth(2); _w.setframerate(44100)
    _w.writeframes(__import__("struct").pack("<4410h", *([0] * 4410)))
    _w.close()

browser = app_module.AudioPreviewDialog(
    root, initial_dir=os.path.join(tree_base, "alpha"))
settle(300)
files = browser.listbox

check(app_module.is_focusable(files) and app_module.is_focusable(browser.folder_tree),
      "ttk lists count as keyboard stops, so the audit can see them at all")
check(root.focus_get() is files, "the dialog opens in the file list, not on a toolbar button",
      str(root.focus_get()))
check(bool(files.focus()) and "no selection" not in all_said(),
      "arriving names a file instead of saying 'no selection'", all_said()[-60:])

# Type-ahead: the only way to cross a folder of hundreds without sight.
files.focus_set()
root.update()
press(files, "<KeyPress-s>")
first = (files.item(files.focus(), "text") or "").strip()
press(files, "<KeyPress-s>")
second = (files.item(files.focus(), "text") or "").strip()
check(first.lower().startswith("s"), "typing a letter jumps to that name", first)
check(second.lower().startswith("s") and second != first,
      "the same letter again steps to the next one", "%s then %s" % (first, second))
press(files, "<KeyPress-q>")
check("no match" in all_said(), "a search that finds nothing says so", all_said()[-40:])

# Alt+Left / Alt+Right / Alt+Up.
browser.navigate_to(tree_base)
root.update()
press(files, "<Alt-Left>")
check(os.path.normpath(browser.current_dir) == os.path.join(tree_base, "alpha"),
      "Alt+Left goes back", browser.current_dir)
press(files, "<Alt-Right>")
check(os.path.normpath(browser.current_dir) == os.path.normpath(tree_base),
      "Alt+Right goes forward", browser.current_dir)

# Arriving is announced once, and says what is there.
browser.navigate_to(tree_base)
root.update()
del spoken[:]
browser.navigate_to(os.path.join(tree_base, "bravo"))
root.update()
root.update_idletasks()
check(len([t for t in spoken if "bravo" in t]) == 1,
      "the folder you land in is named exactly once", all_said())
check(any("item" in t or "empty" in t for t in spoken),
      "and says what is in it", all_said())

# The tree sounds like a tree.
value, _pos = a11y.value_of(browser.folder_tree)
check(a11y.role_of(browser.folder_tree) == "tree" and "level" in value,
      "the sidebar announces as a tree, with the row's level",
      "%s / %s" % (a11y.role_of(browser.folder_tree), value))
del spoken[:]
press(browser.folder_tree, "<Right>")
check("expanded" in all_said(), "Right says the branch expanded", all_said()[:60])

# Sorting, and the preview waveform.
press(files, "<Control-Key-2>")
check(browser._sort_column == "length" and "Sorted by length" in all_said(),
      "Ctrl+2 sorts by length and says so", all_said()[:60])
check(browser.wave_canvas in app_module.focusable_within(browser),
      "the preview waveform is a Tab stop")

# The two lists are two separate searches. One shared buffer meant a letter
# typed in the file list was still in the tree's search a moment later, so
# the tree looked for "sd" and found nothing.
press(files, "<KeyPress-x>")          # leaves "x" in the FILE LIST's buffer
press(browser.folder_tree, "<KeyPress-a>")
check(getattr(browser.folder_tree, "_type_ahead_buf", None) == "a",
      "the tree's type-ahead is not polluted by what was typed in the list",
      repr(getattr(browser.folder_tree, "_type_ahead_buf", None)))
check((getattr(files, "_type_ahead_buf", "") or "").endswith("x"),
      "and the list keeps its own, which the tree's letter did not join",
      repr(getattr(files, "_type_ahead_buf", None)))

# Enter finishes the dialog, which nothing but a double-click used to do.
browser.navigate_to(os.path.join(tree_base, "alpha"))
root.update()
for _iid in files.get_children():
    if "kick" in (files.item(_iid, "text") or ""):
        files.focus(_iid)
        break
files.focus_set()
root.update()
files.event_generate("<Return>")
root.update()
root.update_idletasks()
check(bool(browser.selected_path) and browser.selected_path.endswith("kick.wav"),
      "Enter on a sample chooses it", browser.selected_path)
check(not browser.winfo_exists(), "and closes the dialog")
if browser.winfo_exists():
    browser.destroy()
root.update()


shutil.rmtree(tree_base, ignore_errors=True)

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

print("\n--- the all-banks view (4.2.3) ---")
# F4 is the only way in from the keyboard that does not involve stepping a
# combo box, and the switch replaces 6 pads with 48 - silently, unless the
# toggle says which view it landed in.
del spoken[:]
root.event_generate("<F4>")
root.update()
root.update_idletasks()
check(app._view_mode == "all", "F4 switches to the all-banks view",
      app._view_mode)
check("all banks" in all_said().lower(), "the switch says which view it is",
      all_said()[:70])

cell = app.overview_slots[("C", 4)]
check("Bank C" in cell.accessible_summary() and "pad 4" in cell.accessible_summary(),
      "a compact pad says which bank and pad it is",
      cell.accessible_summary())
check(a11y._group_name_for(cell.load_btn) == cell.accessible_summary(),
      "its buttons inherit that as their group",
      str(a11y._group_name_for(cell.load_btn)))

# The bank selector has nothing to do here, and a greyed control that the
# keyboard still stops on is exactly the dead end this layer rules out.
check(not app_module.is_focusable(app.bank_menu),
      "the greyed bank selector leaves the Tab order")

# Alt+1..6 has to follow the view: the six full-size pads are unpacked
# here, so focusing one would put the keyboard on a widget off screen.
del spoken[:]
app.focus_pad(4)
root.update()
focused = root.focus_get()
active_cell = app.overview_slots[(app.current_bank.get(), 4)]
check(focused is not None and str(focused).startswith(str(active_cell.outer)),
      "Alt+4 lands inside the compact pad, not the hidden full-size one",
      "landed on %s, wanted a child of %s" % (focused, active_cell.outer))

del spoken[:]
app.speak_pad_overview()
check("all banks" in last_said().lower() and " of 6" in last_said(),
      "F3 reports how full each bank is instead of 48 pads",
      last_said()[:70])

del spoken[:]
root.event_generate("<F4>")
root.update()
root.update_idletasks()
check(app._view_mode == "single", "F4 switches back", app._view_mode)

print()
print("=" * 60)
print("%d checks, %d failures" % (checks, len(failures)))
for line in failures:
    print("  -", line)
print("=" * 60)

a11y.speak = _real_speak
root.destroy()
sys.exit(1 if failures else 0)
