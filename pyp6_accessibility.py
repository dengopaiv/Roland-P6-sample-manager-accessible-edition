"""Keyboard and screen-reader accessibility layer for the PyP6 Sample Manager.

Why this module exists
----------------------
Tk draws its own widgets. On Windows a Tk window is a single HWND with no
MSAA/UI-Automation tree behind it, so NVDA, JAWS and Narrator see one blank
window no matter how well-formed the widget hierarchy is. On top of that,
most of this app's controls are `tk.Canvas` subclasses (RoundedButton,
RoundedDropdown, RoundedScrollbar) painted with `create_arc` and
`create_text` - even a screen reader that understood Tk would find nothing
there but a rectangle.

The approach that actually works for Tk is *self-voicing*: the app speaks
through the running screen reader's own API, and every control is reachable
from the keyboard. That is what this module provides:

  * a speech backend that talks to NVDA / JAWS / SAPI5 (Windows),
    speech-dispatcher (Linux) or `say` (macOS), off the Tk main thread;
  * accessible metadata (name, role, value, state, help) attached to any
    widget, plus `describe()` to render it the way a screen reader would;
  * one global <FocusIn> hook that announces whatever the keyboard lands on,
    so controls added later are covered automatically;
  * generic dialog handling (announce on open, Escape to close, sane initial
    focus) applied to every Toplevel without touching the dialog classes;
  * global shortcuts: repeat, where-am-I, speech toggle, region cycling.

Everything degrades quietly. No speech backend, no screen reader, a stripped
down Python - the app still runs exactly as before, just silent.
"""

import os
import sys
import threading
import queue
import time

try:
    import tkinter as tk
except ImportError:  # pragma: no cover - Tk is a hard requirement of the app
    tk = None


# --------------------------------------------------------------------------
# Speech backends
# --------------------------------------------------------------------------

class _Backend:
    """Common shape for every speech backend: a name, speak(), and silence()."""

    name = "none"
    available = False

    def speak(self, text, interrupt=True):
        return False

    def braille(self, text):
        return False

    def silence(self):
        return False


class _AccessibleOutput2Backend(_Backend):
    """accessible_output2 - the community standard.

    It ships the NVDA and Dolphin controller DLLs and picks whichever screen
    reader is actually running, falling back to SAPI5. When it is installed
    this is by far the best option, so it is tried first.
    """

    name = "accessible_output2"

    def __init__(self):
        self._out = None
        try:
            from accessible_output2.outputs.auto import Auto
            out = Auto()
            # Auto() constructs even with nothing available, so confirm that
            # one of its outputs actually answered before claiming success.
            if out.get_first_available_output() is not None:
                self._out = out
        except Exception:
            self._out = None
        self.available = self._out is not None
        if self.available:
            try:
                active = self._out.get_first_available_output()
                self.name = "accessible_output2 (%s)" % getattr(
                    active, "name", type(active).__name__)
            except Exception:
                pass

    def speak(self, text, interrupt=True):
        try:
            self._out.speak(text, interrupt=interrupt)
            return True
        except Exception:
            return False

    def braille(self, text):
        try:
            self._out.braille(text)
            return True
        except Exception:
            return False

    def silence(self):
        try:
            self._out.silence()
            return True
        except Exception:
            return False


class _NvdaClientBackend(_Backend):
    """Direct ctypes call into nvdaControllerClient64.dll.

    The fallback for when accessible_output2 is not installed but NVDA is
    running. The DLL is looked for next to the executable first, so a frozen
    build can ship it, then in accessible_output2's lib folder, then on PATH.
    """

    name = "NVDA controller client"

    def __init__(self):
        self._dll = None
        if not sys.platform.startswith("win"):
            return
        import ctypes
        arch = "64" if sys.maxsize > 2 ** 32 else "32"
        dll_name = "nvdaControllerClient%s.dll" % arch
        base = getattr(sys, "_MEIPASS", None) or os.path.dirname(
            os.path.abspath(sys.argv[0] if sys.argv and sys.argv[0] else __file__))
        candidates = [
            os.path.join(base, dll_name),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), dll_name),
        ]
        try:
            import accessible_output2
            candidates.append(os.path.join(
                os.path.dirname(accessible_output2.__file__), "lib", dll_name))
        except Exception:
            pass
        candidates.append(dll_name)  # anywhere on PATH
        for path in candidates:
            try:
                dll = ctypes.windll.LoadLibrary(path)
                if dll.nvdaController_testIfRunning() == 0:
                    self._dll = dll
                    break
            except Exception:
                continue
        self.available = self._dll is not None

    def speak(self, text, interrupt=True):
        try:
            if interrupt:
                self._dll.nvdaController_cancelSpeech()
            self._dll.nvdaController_speakText(str(text))
            return True
        except Exception:
            return False

    def braille(self, text):
        try:
            self._dll.nvdaController_brailleMessage(str(text))
            return True
        except Exception:
            return False

    def silence(self):
        try:
            self._dll.nvdaController_cancelSpeech()
            return True
        except Exception:
            return False


class _Sapi5Backend(_Backend):
    """Windows SAPI5 - always present, used when no screen reader answers.

    Speaks asynchronously (SVSFlagsAsync) so a long announcement never blocks
    the caller, and purges the queue on interrupt so focus moves stay snappy.
    """

    name = "SAPI5"
    _ASYNC = 1
    _PURGE = 2

    def __init__(self):
        self._voice = None
        if not sys.platform.startswith("win"):
            return
        for factory in (self._via_comtypes, self._via_win32com):
            try:
                voice = factory()
            except Exception:
                continue
            if voice is not None:
                self._voice = voice
                break
        self.available = self._voice is not None

    @staticmethod
    def _via_comtypes():
        import comtypes.client
        return comtypes.client.CreateObject("SAPI.SpVoice")

    @staticmethod
    def _via_win32com():
        import win32com.client
        return win32com.client.Dispatch("SAPI.SpVoice")

    def speak(self, text, interrupt=True):
        try:
            flags = self._ASYNC | (self._PURGE if interrupt else 0)
            self._voice.Speak(str(text), flags)
            return True
        except Exception:
            return False

    def silence(self):
        try:
            self._voice.Speak("", self._ASYNC | self._PURGE)
            return True
        except Exception:
            return False


class _SpeechDispatcherBackend(_Backend):
    """Linux: Orca and friends all sit on speech-dispatcher.

    Prefers the python binding; falls back to shelling out to spd-say, which
    ships with the same package and is present on any machine running Orca.
    """

    name = "speech-dispatcher"

    def __init__(self):
        self._client = None
        self._spd_say = None
        if sys.platform.startswith("win") or sys.platform == "darwin":
            return
        try:
            import speechd
            self._client = speechd.SSIPClient("PyP6")
            self._client.set_priority(speechd.Priority.TEXT)
        except Exception:
            self._client = None
        if self._client is None:
            import shutil
            self._spd_say = shutil.which("spd-say") or shutil.which("espeak")
        self.available = self._client is not None or self._spd_say is not None

    def speak(self, text, interrupt=True):
        try:
            if self._client is not None:
                if interrupt:
                    self._client.cancel()
                self._client.speak(str(text))
                return True
            import subprocess
            cmd = [self._spd_say]
            if interrupt and self._spd_say.endswith("spd-say"):
                cmd.append("-C")
            cmd.append(str(text))
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return True
        except Exception:
            return False

    def silence(self):
        try:
            if self._client is not None:
                self._client.cancel()
                return True
        except Exception:
            pass
        return False


class _MacSayBackend(_Backend):
    """macOS: VoiceOver exposes no public speak API, so `say` is the honest option."""

    name = "macOS say"

    def __init__(self):
        self._proc = None
        if sys.platform != "darwin":
            return
        import shutil
        self.available = shutil.which("say") is not None

    def speak(self, text, interrupt=True):
        try:
            import subprocess
            if interrupt:
                self.silence()
            self._proc = subprocess.Popen(["say", str(text)],
                                          stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL)
            return True
        except Exception:
            return False

    def silence(self):
        try:
            if self._proc is not None and self._proc.poll() is None:
                self._proc.terminate()
                return True
        except Exception:
            pass
        return False


def _pick_backend():
    """First backend that reports itself usable, in order of quality."""
    for cls in (_AccessibleOutput2Backend, _NvdaClientBackend,
                _SpeechDispatcherBackend, _MacSayBackend, _Sapi5Backend):
        try:
            backend = cls()
        except Exception:
            continue
        if backend.available:
            return backend
    return _Backend()


# --------------------------------------------------------------------------
# Roles and value readers
# --------------------------------------------------------------------------

# Tk class name -> spoken role. Deliberately the words screen reader users
# already hear from native controls, so the app doesn't invent a vocabulary.
_CLASS_ROLES = {
    "Button": "button",
    "TButton": "button",
    "Checkbutton": "check box",
    "TCheckbutton": "check box",
    "Radiobutton": "radio button",
    "TRadiobutton": "radio button",
    "Entry": "edit",
    "TEntry": "edit",
    "Listbox": "list",
    "Text": "text area",
    "Scale": "slider",
    "TScale": "slider",
    "Treeview": "table",
    "TCombobox": "combo box",
    "Menu": "menu",
    "Menubutton": "menu button",
    "Toplevel": "dialog",
    "Tk": "window",
    "Canvas": "graphic",
    "Frame": "panel",
    "TFrame": "panel",
    "Label": "text",
    "TLabel": "text",
    "Spinbox": "spin box",
}


#: Classes whose -text option is really a caption. Everything else either
#: has no -text at all or, worse, resolves the abbreviation to something
#: else entirely (Entry -> -textvariable).
_HAS_TEXT_OPTION = frozenset([
    "Label", "TLabel", "Button", "TButton", "Checkbutton", "TCheckbutton",
    "Radiobutton", "TRadiobutton", "Menubutton", "TMenubutton",
    "LabelFrame", "TLabelframe", "Message",
])


def _tk_class(widget):
    try:
        return widget.winfo_class()
    except Exception:
        return ""


def _cget(widget, option, default=None):
    try:
        return widget.cget(option)
    except Exception:
        return default


def _var_value(widget, option="variable"):
    """Reads a widget's linked Tk variable without needing the Variable object.

    Checkbutton/Radiobutton only expose the *name* of their variable, so go
    through the interpreter to get its current value.
    """
    name = _cget(widget, option)
    if not name:
        return None
    try:
        return widget.getvar(str(name))
    except Exception:
        return None


def _is_disabled(widget):
    state = _cget(widget, "state")
    if state is not None and str(state) == "disabled":
        return True
    # Our own canvas controls keep their state in a private attribute.
    if str(getattr(widget, "_state", "")) == "disabled":
        return True
    try:
        return "disabled" in widget.state()  # ttk
    except Exception:
        return False


def _listbox_value(widget):
    try:
        total = widget.size()
        sel = widget.curselection()
        if not sel:
            return "no selection", ("%d items" % total if total else "empty")
        idx = int(sel[0])
        return widget.get(idx), "%d of %d" % (idx + 1, total)
    except Exception:
        return None, None


def _treeview_value(widget):
    try:
        focused = widget.focus() or (widget.selection() or [None])[0]
        if not focused:
            return "no selection", None
        item = widget.item(focused)
        parts = [str(item.get("text") or "")]
        parts += [str(v) for v in (item.get("values") or [])]
        text = ", ".join(p for p in parts if p)
        siblings = widget.get_children(widget.parent(focused))
        position = None
        if focused in siblings:
            position = "%d of %d" % (siblings.index(focused) + 1, len(siblings))
        return text, position
    except Exception:
        return None, None


# --------------------------------------------------------------------------
# The accessibility service
# --------------------------------------------------------------------------

class Accessibility:
    """Speech, metadata and keyboard plumbing for one application.

    A single module-level instance (`a11y`) is shared by the app; nothing
    here assumes there is only one, but nothing needs a second either.
    """

    #: Announcements shorter than this are never deduplicated - repeated
    #: single words ("A", "B") are normal when arrowing through a list.
    _DEDUPE_MIN_LEN = 4
    _DEDUPE_WINDOW_S = 0.35

    def __init__(self):
        self.enabled = True
        self.verbosity = "normal"      # "normal" | "brief"
        self.speak_help = True         # read the tooltip text on focus
        self.echo_keys = True          # speak characters as they are typed
        #: Caption -> spoken name, for controls captioned with a symbol.
        #: Mapping the *caption* rather than pinning a fixed name matters for
        #: the ones that toggle: a play button's glyph flips to a stop square
        #: to show its state, and going through this table means the
        #: announcement flips with it instead of lying.
        self.caption_names = {}
        self.backend = _Backend()
        self.root = None
        self.app = None
        self._queue = queue.Queue()
        self._thread = None
        self._history = []
        self._last_text = ""
        self._last_time = 0.0
        self._regions = []             # [(name, widget-or-callable)]
        self._region_index = -1
        self._started = False
        self._keyboard_activation_at = 0.0
        self._last_group = None

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        """Picks a backend and starts the speech worker. Safe to call twice."""
        if self._started:
            return self
        self._started = True
        self.backend = _pick_backend()
        self._thread = threading.Thread(target=self._worker, name="pyp6-speech",
                                        daemon=True)
        self._thread.start()
        return self

    @property
    def speech_available(self):
        return bool(self.backend.available)

    def backend_name(self):
        return self.backend.name if self.backend.available else "none"

    def _worker(self):
        """Serializes speech off the Tk thread.

        SAPI and the NVDA client are both fast, but `say`/`spd-say` fork a
        process and comtypes can block on first use; doing any of that inside
        a <FocusIn> handler would stutter the UI on every Tab press.
        """
        while True:
            try:
                item = self._queue.get()
            except Exception:
                return
            if item is None:
                return
            text, interrupt = item
            try:
                self.backend.speak(text, interrupt=interrupt)
                self.backend.braille(text)
            except Exception:
                pass

    # -- speaking ----------------------------------------------------------

    def speak(self, text, interrupt=True, dedupe=True):
        """Says `text` now, interrupting whatever is being spoken by default."""
        if not text:
            return
        text = " ".join(str(text).split())
        if not self.enabled:
            return
        now = time.time()
        if (dedupe and text == self._last_text
                and len(text) >= self._DEDUPE_MIN_LEN
                and now - self._last_time < self._DEDUPE_WINDOW_S):
            return
        self._last_text, self._last_time = text, now
        self._history.append(text)
        del self._history[:-50]
        if not self._started:
            self.start()
        self._queue.put((text, interrupt))

    def announce(self, text, interrupt=False):
        """Status-style message that should not cut off what is being said."""
        self.speak(text, interrupt=interrupt)

    def silence(self):
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass
        except Exception:
            pass
        try:
            self.backend.silence()
        except Exception:
            pass

    def repeat_last(self):
        if self._history:
            self.speak(self._history[-1], dedupe=False)
        else:
            self.speak("Nothing to repeat")

    def set_enabled(self, enabled):
        enabled = bool(enabled)
        if enabled == self.enabled:
            return self.enabled
        if enabled:
            self.enabled = True
            self.speak("Speech on", dedupe=False)
        else:
            # Queue the confirmation first, then switch off - the worker has
            # already taken the item, so it still gets spoken.
            self.speak("Speech off", dedupe=False)
            self.enabled = False
        return self.enabled

    def toggle_enabled(self):
        return self.set_enabled(not self.enabled)

    # -- input modality ----------------------------------------------------
    #
    # Several controls open a popup tk.Menu. A drawn Tk menu is fine with a
    # mouse and unreadable with a screen reader, so those call sites need to
    # know which one triggered them - and their `command=` callbacks take no
    # arguments, so the answer cannot be passed down. Recording the moment a
    # key activated something, and asking whether that was just now, keeps
    # the signatures untouched.

    #: How long a key press counts as "this action came from the keyboard".
    #: Long enough to survive the callback hop, far too short to catch the
    #: next, unrelated mouse click.
    _KEYBOARD_ACTIVATION_WINDOW_S = 1.0

    def note_keyboard_activation(self):
        self._keyboard_activation_at = time.time()

    def activated_by_keyboard(self):
        return (time.time() - self._keyboard_activation_at
                < self._KEYBOARD_ACTIVATION_WINDOW_S)

    # -- metadata ----------------------------------------------------------

    def label(self, widget, name=None, role=None, help=None, value=None,
              position=None, skip=None):
        """Attaches accessible metadata to `widget` and returns the widget.

        Every argument is optional so callers fill in only what the automatic
        detection gets wrong. `name`, `value` and `position` may be callables,
        evaluated fresh at announce time.
        """
        if widget is None:
            return widget
        try:
            if name is not None:
                widget._a11y_name = name
            if role is not None:
                widget._a11y_role = role
            if help is not None:
                widget._a11y_help = help
            if value is not None:
                widget._a11y_value = value
            if position is not None:
                widget._a11y_position = position
            if skip is not None:
                widget._a11y_skip = bool(skip)
        except Exception:
            pass
        return widget

    def skip(self, widget):
        """Marks a widget as decorative: focus lands on it silently."""
        return self.label(widget, skip=True)

    def group(self, container, name):
        """Names a container so its contents announce which one they are in.

        The six pads hold identical controls - six Load buttons, six sample
        rate selectors - and "Load, button" six times running says nothing
        about which pad is about to be loaded. Naming the container makes the
        first stop inside it read "Pad 3. Load, button", then stay quiet
        until the keyboard crosses into a different pad, which is how a
        screen reader treats a landmark or a fieldset.
        """
        try:
            container._a11y_group = name
        except Exception:
            pass
        return container

    def _group_name_for(self, widget):
        """Walks up to the nearest named container. Nearest, not outermost:
        a group inside a group should announce the specific one."""
        node = widget
        for _ in range(24):  # deep enough for this UI, and cannot loop forever
            if node is None:
                return None
            name = getattr(node, "_a11y_group", None)
            if name:
                return self._resolve(name, node)
            try:
                node = node.master
            except Exception:
                return None
        return None

    @staticmethod
    def _resolve(value, widget):
        """Values may be plain or callable; callables win freshness."""
        if callable(value):
            try:
                return value()
            except TypeError:
                try:
                    return value(widget)
                except Exception:
                    return None
            except Exception:
                return None
        return value

    def name_of(self, widget):
        """Best available accessible name, in order of trustworthiness."""
        explicit = self._resolve(getattr(widget, "_a11y_name", None), widget)
        if explicit:
            return str(explicit)
        # Our canvas controls carry their caption as .text
        text = getattr(widget, "text", None)
        if not (isinstance(text, str) and text.strip()):
            # Only ask widgets that really have a -text option. Tcl resolves
            # unique option prefixes, so cget("text") on an Entry quietly
            # answers with its -textvariable *name* - which is how controls
            # ended up introducing themselves as "PY_VAR39".
            text = (_cget(widget, "text")
                    if _tk_class(widget) in _HAS_TEXT_OPTION else None)
        if isinstance(text, str) and text.strip():
            text = text.strip()
            return self.caption_names.get(text, text)
        return ""

    def role_of(self, widget):
        explicit = self._resolve(getattr(widget, "_a11y_role", None), widget)
        if explicit:
            return str(explicit)
        return _CLASS_ROLES.get(_tk_class(widget), "")

    def value_of(self, widget):
        """Current value plus an optional position ("3 of 8")."""
        explicit = self._resolve(getattr(widget, "_a11y_value", None), widget)
        position = self._resolve(getattr(widget, "_a11y_position", None), widget)
        position = str(position) if position else None
        if explicit is not None:
            return str(explicit), position

        cls = _tk_class(widget)
        if cls in ("Checkbutton", "TCheckbutton"):
            raw = _var_value(widget)
            on_value = _cget(widget, "onvalue", 1)
            checked = str(raw) == str(on_value) if raw is not None else False
            return ("checked" if checked else "not checked"), position
        if cls in ("Radiobutton", "TRadiobutton"):
            raw = _var_value(widget)
            picked = raw is not None and str(raw) == str(_cget(widget, "value", ""))
            return ("selected" if picked else "not selected"), position
        if cls in ("Entry", "TEntry", "Spinbox"):
            try:
                content = widget.get()
            except Exception:
                content = ""
            if str(_cget(widget, "show", "")):
                content = "hidden" if content else ""
            return (content or "blank"), position
        if cls == "Listbox":
            value, pos = _listbox_value(widget)
            return value, (position or pos)
        if cls == "Treeview":
            value, pos = _treeview_value(widget)
            return value, (position or pos)
        if cls in ("Scale", "TScale"):
            try:
                return str(widget.get()), position
            except Exception:
                return None, position
        if cls == "Text":
            return None, position
        # RoundedDropdown and anything else holding a Tk variable
        var = getattr(widget, "variable", None)
        if var is not None and hasattr(var, "get"):
            try:
                return str(var.get()), position
            except Exception:
                pass
        return None, position

    def help_of(self, widget):
        explicit = self._resolve(getattr(widget, "_a11y_help", None), widget)
        if explicit:
            return str(explicit)
        tip = getattr(widget, "_pyp6_tooltip", None)
        text = getattr(tip, "text", None)
        return str(text) if text else ""

    def describe(self, widget, with_help=False, with_role=True):
        """Renders a widget the way a screen reader announces a native one:
        name, role, value, position, state."""
        if widget is None:
            return ""
        parts = []
        name = self.name_of(widget)
        if name:
            parts.append(name)
        role = self.role_of(widget) if with_role else ""
        if role and role not in ("text", "panel"):
            parts.append(role)
        value, position = self.value_of(widget)
        if value:
            parts.append(str(value))
        if position:
            parts.append(str(position))
        if _is_disabled(widget):
            parts.append("unavailable")
        text = ", ".join(p for p in parts if p)
        if with_help:
            help_text = self.help_of(widget)
            if help_text:
                text = "%s. %s" % (text, self._first_sentences(help_text))
        return text

    @staticmethod
    def _first_sentences(text, limit=2):
        """Tooltips here are paragraphs; a screen reader wants the gist."""
        cleaned = " ".join(str(text).split())
        out = []
        for chunk in cleaned.replace("! ", ". ").replace("? ", ". ").split(". "):
            chunk = chunk.strip()
            if not chunk:
                continue
            out.append(chunk)
            if len(out) >= limit:
                break
        result = ". ".join(out)
        if not result:
            return ""
        return result if result.endswith(".") else result + "."

    # -- focus reporting ---------------------------------------------------

    def announce_focus(self, widget, interrupt=True):
        if widget is None or getattr(widget, "_a11y_skip", False):
            return
        with_help = self.speak_help and self.verbosity != "brief"
        text = self.describe(widget, with_help=with_help)
        if not text:
            return
        group = self._group_name_for(widget)
        if group and group != self._last_group:
            text = "%s. %s" % (group, text)
        self._last_group = group
        self.speak(text, interrupt=interrupt)

    def where_am_i(self):
        """Ctrl+Shift+W: the focused control plus the window it lives in."""
        widget = self._focused()
        if widget is None:
            self.speak("Nothing focused", dedupe=False)
            return
        window = widget.winfo_toplevel()
        title = ""
        try:
            title = window.title()
        except Exception:
            pass
        bits = [title, self._region_name_for(widget),
                self._group_name_for(widget),
                self.describe(widget, with_help=True)]
        self.speak(". ".join(b for b in bits if b), dedupe=False)

    def _focused(self):
        try:
            return self.root.focus_get()
        except Exception:
            return None

    # -- regions (F6) ------------------------------------------------------

    def add_region(self, name, target):
        """Registers a named area for F6 cycling.

        `target` is a widget or a callable returning one, resolved on every
        press so regions that get rebuilt (the pad grid) stay correct.
        """
        self._regions.append((name, target))

    def clear_regions(self):
        self._regions = []
        self._region_index = -1

    def _region_widget(self, target):
        try:
            widget = target() if callable(target) else target
            if widget is not None and widget.winfo_exists():
                return widget
        except Exception:
            return None
        return None

    def _region_name_for(self, widget):
        """Which registered region contains `widget` - the most specific one."""
        try:
            path = str(widget)
        except Exception:
            return ""
        best_name, best_len = "", -1
        for name, target in self._regions:
            region = self._region_widget(target)
            if region is None:
                continue
            region_path = str(region)
            if path == region_path or path.startswith(region_path + "."):
                if len(region_path) > best_len:
                    best_name, best_len = name, len(region_path)
        return best_name

    def cycle_region(self, step=1):
        if not self._regions:
            return "break"
        count = len(self._regions)
        for offset in range(1, count + 1):
            index = (self._region_index + step * offset) % count
            name, target = self._regions[index]
            widget = self._region_widget(target)
            if widget is None:
                continue
            target_widget = first_focusable(widget)
            if target_widget is None:
                continue
            self._region_index = index
            try:
                target_widget.focus_set()
            except Exception:
                continue
            self.speak("%s. %s" % (name, self.describe(target_widget)),
                       dedupe=False)
            return "break"
        return "break"

    # -- installation ------------------------------------------------------

    def install(self, root, app=None):
        """Wires the global hooks into a Tk root. Call once, after the UI exists."""
        self.root = root
        self.app = app
        self.start()

        root.bind_all("<FocusIn>", self._on_focus_in, add="+")
        root.bind_all("<F6>", lambda e: self.cycle_region(1), add="+")
        root.bind_all("<Shift-F6>", lambda e: self.cycle_region(-1), add="+")
        root.bind_all("<Control-Shift-A>", lambda e: self._key(self.repeat_last), add="+")
        root.bind_all("<Control-Shift-W>", lambda e: self._key(self.where_am_i), add="+")
        root.bind_all("<Control-Shift-S>", lambda e: self._key(self.toggle_enabled), add="+")
        # Ctrl on its own silences speech - the convention every screen
        # reader user already has in their fingers.
        root.bind_all("<Control_L>", lambda e: self.silence(), add="+")
        root.bind_all("<Control_R>", lambda e: self.silence(), add="+")
        # A Text widget swallows Tab; hand the traversal back so no control
        # can trap the keyboard.
        root.bind_class("Text", "<Tab>", _focus_next_event, add="+")
        root.bind_class("Text", "<Shift-Tab>", _focus_prev_event, add="+")
        root.bind_class("Text", "<ISO_Left_Tab>", _focus_prev_event, add="+")

        # Class-level, not per widget: <FocusIn> covers arriving at a
        # control, but everything below is movement *inside* one, which
        # fires no focus event at all. Binding by class means every list,
        # entry and check box in the app is covered, including the ones in
        # dialogs that do not exist yet when this runs.
        root.bind_class("Listbox", "<<ListboxSelect>>", self._on_list_select, add="+")
        root.bind_class("Treeview", "<<TreeviewSelect>>", self._on_list_select, add="+")
        root.bind_class("Entry", "<KeyPress>", self._on_entry_key_press, add="+")
        root.bind_class("Entry", "<KeyRelease>", self._on_entry_key_release, add="+")
        for cls in ("Checkbutton", "Radiobutton"):
            root.bind_class(cls, "<space>", self._on_toggle, add="+")
            root.bind_class(cls, "<ButtonRelease-1>", self._on_toggle, add="+")
        return self

    # -- movement inside a control ----------------------------------------

    def _on_list_select(self, event):
        """Arrowing through a list. Speaks the row only - repeating the
        list's own name on every keypress is noise, not information."""
        widget = getattr(event, "widget", None)
        if widget is None or getattr(widget, "_a11y_skip", False):
            return
        value, position = self.value_of(widget)
        if value:
            self.speak(", ".join(p for p in (str(value), position) if p),
                       dedupe=False)

    def _on_toggle(self, event):
        """Space or a click on a check box. The variable has not changed yet
        when this fires, so read it back once Tk has finished the toggle."""
        widget = getattr(event, "widget", None)
        if widget is None or getattr(widget, "_a11y_skip", False):
            return

        def report():
            value, _position = self.value_of(widget)
            name = self.name_of(widget)
            self.speak(", ".join(p for p in (name, str(value or "")) if p),
                       dedupe=False)

        try:
            widget.after_idle(report)
        except Exception:
            pass

    #: Keys that move the caret rather than change the text.
    _CARET_KEYS = frozenset(["Left", "Right", "Home", "End", "Up", "Down",
                             "Prior", "Next"])

    def _on_entry_key_press(self, event):
        """Remembers the character Backspace/Delete is about to remove.

        Announcing "deleted x" needs the text as it was; by KeyRelease it is
        already gone, so the character has to be captured on the way in.
        """
        widget = getattr(event, "widget", None)
        if widget is None or event.keysym not in ("BackSpace", "Delete"):
            return
        try:
            index = widget.index("insert")
            content = widget.get()
        except Exception:
            return
        if event.keysym == "BackSpace":
            widget._a11y_pending_delete = content[index - 1:index] if index > 0 else ""
        else:
            widget._a11y_pending_delete = content[index:index + 1]

    def _on_entry_key_release(self, event):
        """Character echo while typing.

        A screen reader normally echoes what you type by watching the caret
        of a native edit control. There is no such control here, so nothing
        is echoed and typing into a field is silent - the app has to say it.
        """
        widget = getattr(event, "widget", None)
        if widget is None or not self.echo_keys:
            return
        if getattr(widget, "_a11y_skip", False):
            return
        # Never echo a masked field, whatever it is masking.
        if str(_cget(widget, "show", "")):
            return
        keysym = event.keysym
        if keysym in ("BackSpace", "Delete"):
            removed = getattr(widget, "_a11y_pending_delete", "")
            self.speak(self._spoken_char(removed) if removed else "delete",
                       dedupe=False)
            return
        if keysym in ("Home", "End", "Up", "Down", "Prior", "Next"):
            try:
                self.speak(widget.get() or "blank", dedupe=False)
            except Exception:
                pass
            return
        if keysym in ("Left", "Right"):
            self.speak(self._char_at_caret(widget, keysym), dedupe=False)
            return
        if keysym in ("Return", "KP_Enter", "Tab", "ISO_Left_Tab", "Escape"):
            return  # the resulting focus move or action speaks for itself
        char = getattr(event, "char", "")
        if char and char.isprintable():
            self.speak(self._spoken_char(char), dedupe=False)

    @staticmethod
    def _spoken_char(char):
        """Space and friends have to be named, or they are simply silence."""
        return {" ": "space", "\t": "tab", "\n": "newline"}.get(char, char)

    def _char_at_caret(self, widget, keysym):
        """The character the caret just moved onto, the way a screen reader
        reads arrow movement through text."""
        try:
            index = widget.index("insert")
            content = widget.get()
        except Exception:
            return ""
        if keysym == "Left":
            char = content[index:index + 1]
        else:
            char = content[index - 1:index] if index > 0 else ""
        if not char:
            return "start" if keysym == "Left" else "end"
        return self._spoken_char(char)

    def _key(self, fn):
        try:
            fn()
        except Exception:
            pass
        return "break"

    def _on_focus_in(self, event):
        widget = getattr(event, "widget", None)
        if widget is None:
            return
        # A Toplevel gaining focus is the window manager, not a control;
        # dialog openings are announced separately on <Map>.
        if _tk_class(widget) in ("Toplevel", "Tk"):
            return
        self.announce_focus(widget)


# --------------------------------------------------------------------------
# Focus helpers
# --------------------------------------------------------------------------

def _focus_next_event(event):
    try:
        event.widget.tk_focusNext().focus_set()
    except Exception:
        pass
    return "break"


def _focus_prev_event(event):
    try:
        event.widget.tk_focusPrev().focus_set()
    except Exception:
        pass
    return "break"


#: Widget classes Tk lets the keyboard reach without being asked to.
_FOCUSABLE_BY_DEFAULT = frozenset([
    "Entry", "TEntry", "Listbox", "Text", "Treeview", "Spinbox", "TCombobox",
    "Checkbutton", "TCheckbutton", "Radiobutton", "TRadiobutton",
    "Button", "TButton", "Scale", "TScale", "Menubutton", "TMenubutton",
])


def is_focusable(widget):
    """True if Tab traversal would stop on this widget."""
    try:
        if not widget.winfo_exists() or not widget.winfo_ismapped():
            return False
        takefocus = _cget(widget, "takefocus")
        # An explicit -takefocus is checked before -state, and Tk agrees:
        # "disabled" on a Text means read-only, not unreachable, and a
        # read-only box of warnings still has to be reachable to be read.
        # Controls that genuinely go dead (a greyed-out button) set
        # takefocus to 0 themselves, so nothing slips through here.
        if takefocus not in ("", None):
            return str(takefocus) in ("1", "true", "yes")
        if _is_disabled(widget):
            return False
        # Tk's own rule for an empty -takefocus is "does this widget or its
        # class bind a key event?". Spelled out as a class list here rather
        # than inspecting bindtags, because the question worth answering is
        # which widgets a *user* can operate, and the bindtag answer is yes
        # for decorative canvases that merely listen for <Enter>.
        return _tk_class(widget) in _FOCUSABLE_BY_DEFAULT
    except Exception:
        return False


def first_focusable(container):
    """Depth-first search for the first control the keyboard can land on."""
    if container is None:
        return None
    if is_focusable(container):
        return container
    try:
        children = container.winfo_children()
    except Exception:
        return None
    for child in children:
        found = first_focusable(child)
        if found is not None:
            return found
    return None


def focusable_within(container, out=None):
    """Every focusable descendant, in Tab order."""
    out = [] if out is None else out
    if container is None:
        return out
    if is_focusable(container):
        out.append(container)
    try:
        children = container.winfo_children()
    except Exception:
        return out
    for child in children:
        focusable_within(child, out)
    return out


# --------------------------------------------------------------------------
# Generic dialog support
# --------------------------------------------------------------------------

def install_dialog_support(root, service):
    """Makes every Toplevel behave for a keyboard user, without editing them.

    Binding <Map> at the `all` level catches dialogs this module has never
    heard of - including any added later - so the guarantees below hold app
    wide: the dialog announces itself, Escape closes it, and focus starts on
    a real control instead of nowhere.
    """

    def on_map(event):
        widget = getattr(event, "widget", None)
        if widget is None or _tk_class(widget) != "Toplevel":
            return
        if getattr(widget, "_a11y_dialog_ready", False):
            return
        widget._a11y_dialog_ready = True
        prepare_dialog(widget, service)

    root.bind_all("<Map>", on_map, add="+")


def prepare_dialog(window, service, announce=True, initial_focus=None):
    """Escape-to-close, sane initial focus, and an opening announcement."""
    try:
        title = window.title()
    except Exception:
        title = ""

    if not getattr(window, "_a11y_escape_bound", False):
        window._a11y_escape_bound = True

        def on_escape(_event=None):
            # Prefer the dialog's own cancel path so it can clean up
            # (release grabs, stop playback, restore state).
            for attr in ("on_cancel", "_on_cancel", "cancel", "_on_close", "close"):
                fn = getattr(window, attr, None)
                if callable(fn):
                    try:
                        fn()
                        return "break"
                    except Exception:
                        break
            try:
                window.destroy()
            except Exception:
                pass
            return "break"

        window.bind("<Escape>", on_escape, add="+")

    def settle():
        target = initial_focus
        if target is None:
            try:
                current = window.focus_get()
            except Exception:
                current = None
            if current is not None and current is not window and is_focusable(current):
                target = current
            else:
                target = first_focusable(window)
        if target is not None:
            try:
                target.focus_set()
            except Exception:
                target = None
        if not announce:
            return
        bits = ["%s dialog" % title if title else "Dialog"]
        # A message box's whole point is the message, so that is read in
        # full. Anything else only contributes the gist of its help text -
        # a dialog that recites three paragraphs before naming its first
        # control is worse than one that says nothing.
        message = getattr(window, "_a11y_message", None)
        if message:
            bits.append(str(message))
        else:
            hint = service.help_of(window)
            if hint:
                bits.append(service._first_sentences(hint))
        if target is not None:
            bits.append(service.describe(target))
        service.speak(". ".join(b for b in bits if b), dedupe=False)

    # Deferred: the dialog's own grab_set/focus juggling has to finish first,
    # or the announcement describes a control that is about to lose focus.
    try:
        window.after(60, settle)
    except Exception:
        settle()


# --------------------------------------------------------------------------
# Module-level singleton
# --------------------------------------------------------------------------

a11y = Accessibility()


def speak(text, interrupt=True):
    a11y.speak(text, interrupt=interrupt)


def announce(text):
    a11y.announce(text)


def label(widget, **kwargs):
    return a11y.label(widget, **kwargs)
