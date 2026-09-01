"""Accessibility audit: build the real UI, walk the keyboard order of the main
window and of every dialog, and report what a screen reader would say at each
stop. Flags anything reachable that would announce nothing.

Run with:  python accessibility_audit.py [--dialogs]
It drives the app itself and closes when finished - nothing to click.
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

# Silent: the point is to read the announcements, not to sit through 200.
a11y.enabled = False

root = tk.Tk()
app_module._verify_ui_font(root)
a11y.install(root)
app_module.install_dialog_support(root, a11y)
app = app_module.P6ManagerApp(root)
root.update_idletasks()
root.update()

problems = []


def audit(window, title):
    """Prints the keyboard order of `window` and returns its unnamed stops."""
    def walk(widget, out):
        out.append(widget)
        for child in widget.winfo_children():
            walk(child, out)
        return out

    everything = walk(window, [])
    stops = [w for w in everything if app_module.is_focusable(w)]
    print()
    print("=" * 72)
    print("%s  -  %d widgets, %d keyboard stops" % (title, len(everything), len(stops)))
    print("=" * 72)

    unnamed = []
    last_group = None
    for i, widget in enumerate(stops, 1):
        described = a11y.describe(widget)
        group = a11y._group_name_for(widget)
        prefix = ""
        if group and group != last_group:
            prefix = group + ". "
        last_group = group
        if not a11y.name_of(widget):
            unnamed.append(widget)
            described = described or "<<SILENT>>"
        print("%3d. %-12s %s%s" % (i, widget.winfo_class(), prefix, described))
    if unnamed:
        for widget in unnamed:
            problems.append("%s: unnamed %s (%s)"
                            % (title, widget.winfo_class(), str(widget)))
    return stops


print("speech backend:", a11y.backend_name())
audit(root, "MAIN WINDOW")

def make_test_wav():
    """A short real WAV on disk.

    Three of the dialogs refuse to open without a loaded sample, and those
    three are among the biggest in the app - auditing everything except them
    would be auditing the easy half.
    """
    import tempfile
    import wave
    import math
    import struct

    path = os.path.join(tempfile.gettempdir(), "pyp6_audit_tone.wav")
    if not os.path.exists(path):
        with wave.open(path, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(44100)
            frames = b"".join(
                struct.pack("<h", int(12000 * math.sin(i * 0.05)))
                for i in range(44100))
            handle.writeframes(frames)
    return path


if "--dialogs" in sys.argv:
    # Every dialog that can be built without a device attached. Each is
    # constructed, audited, and destroyed.
    home = os.path.expanduser("~")
    tone = make_test_wav()
    app.pad_widgets[1].set_file(tone, display_name="audit tone")
    root.update()
    cases = [
        ("Folder picker", lambda: app_module.FolderPickerDialog(root, initial_dir=home)),
        ("Save file", lambda: app_module.FileSaveDialog(root, initial_dir=home)),
        ("Clear banks", lambda: app_module.ClearBanksDialog(root, app)),
        ("Copy banks", lambda: app_module.CopyBanksDialog(root, app)),
        ("About", lambda: app_module.AboutDialog(root)),
        ("Settings", lambda: app_module.SettingsDialog(root, app)),
        ("Import bank", lambda: app_module.ImportBankDialog(root, app)),
        ("Save preset", lambda: app_module.PresetSaveDialog(root, app, initial_dir=home)),
        ("Load preset", lambda: app_module.PresetLoadDialog(root, app, initial_dir=home)),
        ("Audio browser", lambda: app_module.AudioPreviewDialog(root, initial_dir=home)),
        ("Waveform creator", lambda: app_module.WaveformCreatorDialog(root, app)),
        ("Message box", lambda: app_module._DarkMessageDialog(
            root, "Test", "A short message.", kind="info", buttons="yesno")),
        ("Text prompt", lambda: app_module._DarkTextPromptDialog(
            root, "Test", "Type a name:", "preset")),
        ("Keyboard help", lambda: app.show_keyboard_help()),
        ("Chop", lambda: app_module.ChopDialog(root, initial_dir=home)),
        ("Sample editor", lambda: app_module.PadWaveformViewDialog(
            root, app, 1, tone, "audit tone")),
        ("Synth", lambda: app_module.SynthDialog(root, app, "A", 1)),
    ]
    for title, build in cases:
        try:
            window = build()
        except Exception as exc:
            problems.append("%s: could not be built (%s: %s)"
                            % (title, type(exc).__name__, exc))
            print("\n!! %s could not be built: %s: %s" % (title, type(exc).__name__, exc))
            continue
        try:
            window.update_idletasks()
            window.update()
            audit(window, "DIALOG: " + title)
            if not getattr(window, "_a11y_escape_bound", False):
                problems.append("%s: Escape does not close it" % title)
        finally:
            try:
                window.grab_release()
            except tk.TclError:
                pass
            try:
                window.destroy()
            except tk.TclError:
                pass
            root.update()

print()
print("=" * 72)
if problems:
    print("%d PROBLEMS" % len(problems))
    for line in problems:
        print("  -", line)
else:
    print("OK - every keyboard stop has a name, every dialog closes on Escape")
print("=" * 72)

root.destroy()
sys.exit(1 if problems else 0)
