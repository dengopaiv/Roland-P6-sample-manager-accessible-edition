# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build for the accessible edition.

One self-contained .exe with Python inside, matching how this app has always
been distributed. Build it with:

    pyinstaller --clean --noconfirm PyP6.spec

or run build_windows_exe.ps1, which checks the prerequisites first.

The interesting part is what has to be collected by hand. PyInstaller finds
plain Python imports on its own; it cannot see files that a package opens at
runtime, and this build depends on several:

  * accessible_output2 ships the screen reader controller DLLs as package
    *data*, and loads them by path at runtime. Without them the app falls
    back to the system voice and stops talking to NVDA and JAWS directly.
  * tkinterdnd2 ships a native Tcl extension, not a Python module, so
    dropping files onto pads breaks without its data files.
  * soundfile carries libsndfile as a bundled binary.
"""

import os

from PyInstaller.utils.hooks import collect_all, collect_data_files

APP_NAME = "PyP6-Roland-P6-Sample-Manager_4_2_3"
ENTRY_SCRIPT = "PyP6-Roland-P6-Sample-Manager_4_2_3.py"

datas = []
binaries = []
hiddenimports = [
    # The accessibility layer is imported by name from the entry script, so
    # PyInstaller does find it - named here as well so a future refactor
    # that imports it lazily cannot silently drop it from the build.
    "pyp6_accessibility",
    # Speech fallbacks. Both are reached through a try/except, which
    # PyInstaller's static analysis treats as optional and skips.
    "comtypes",
    "comtypes.client",
    "comtypes.gen",
    "win32com.client",
    "pythoncom",
    "pywintypes",
    # pydub reaches for audioop, which is the audioop-lts backport on
    # Python 3.13 and later.
    "audioop",
]

for package in ("accessible_output2", "platform_utils", "libloader",
                "soundfile", "sounddevice", "tkinterdnd2"):
    try:
        package_datas, package_binaries, package_hidden = collect_all(package)
    except Exception as exc:  # a missing optional package is not fatal
        print("PyP6.spec: could not collect %s (%s) - continuing without it"
              % (package, exc))
        continue
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

# The screen reader controller DLL, also placed at the top of the bundle.
# accessible_output2 finds its own copy under accessible_output2/lib, but the
# app's built-in fallback backend looks next to the executable (sys._MEIPASS)
# so that speech still reaches NVDA even if accessible_output2 is unavailable
# or fails to import in the frozen build.
try:
    import accessible_output2

    _lib_dir = os.path.join(os.path.dirname(accessible_output2.__file__), "lib")
    for _dll in ("nvdaControllerClient64.dll", "nvdaControllerClient32.dll"):
        _path = os.path.join(_lib_dir, _dll)
        if os.path.exists(_path):
            datas.append((_path, "."))
except Exception as exc:
    print("PyP6.spec: no accessible_output2 DLLs bundled (%s)" % exc)

datas += [("pyp6logo.ico", ".")]

# ffmpeg / ffprobe, if this machine has them. The entry script looks for
# them at the top of the bundle before falling back to PATH, so shipping
# them here is what makes MP3 loading and rate/pitch/mono conversion work on
# a machine with no ffmpeg installed - which is the whole point of a
# single-file build. A vendor/ folder next to this spec wins over PATH so a
# release build can pin a known-good copy.
import shutil

_missing_ffmpeg = []
for _tool in ("ffmpeg", "ffprobe"):
    _exe_name = _tool + (".exe" if os.name == "nt" else "")
    _vendored = os.path.join("vendor", _exe_name)
    _found = _vendored if os.path.exists(_vendored) else shutil.which(_tool)
    if _found:
        binaries.append((_found, "."))
        print("PyP6.spec: bundling %s from %s" % (_tool, _found))
    else:
        _missing_ffmpeg.append(_tool)

if _missing_ffmpeg:
    print("PyP6.spec: WARNING - %s not found, so this build will NOT support "
          "MP3 files or sample rate/pitch/mono conversion. Install ffmpeg and "
          "put it on PATH, or drop the executables in ./vendor/, and rebuild."
          % " and ".join(_missing_ffmpeg))


a = Analysis(
    [ENTRY_SCRIPT],
    pathex=[os.path.abspath(".")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Nothing in the app plots anything, and matplotlib alone would add
        # tens of megabytes to a download people are already asked to trust.
        "matplotlib",
        "pytest",
        "IPython",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX is off deliberately: it rewrites DLLs, and the ones this build
    # depends on are exactly the fragile kind - screen reader controller
    # clients, libsndfile, the Tcl extension.
    upx=False,
    runtime_tmpdir=None,
    # No console window. Every subprocess this app starts is already created
    # with CREATE_NO_WINDOW (see the top of the entry script), so a windowed
    # build does not flash a console per ffmpeg call.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="pyp6logo.ico",
)
