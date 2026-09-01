"""Frozen-build probe: prove the speech layer still finds a screen reader
once PyInstaller has moved everything into a bundle.

This is the failure that would otherwise ship silently - the app runs, looks
right, and speaks through the system voice (or not at all) because the
controller DLLs never made it in. Built as a console exe so the answer is
readable; the real app reports the same thing in About -> Speech output.

Build and run it with:

    python -m PyInstaller --clean --noconfirm --onefile --console \n        --distpath probe_dist --workpath probe_build --specpath probe_build \n        --collect-all accessible_output2 --collect-all platform_utils \n        --collect-all libloader --hidden-import comtypes.client \n        --hidden-import win32com.client frozen_speech_probe.py
    probe_dist\frozen_speech_probe.exe

It should print a real screen reader, not "none" and not only "SAPI5".
"""

import sys

import pyp6_accessibility as accessibility

print("frozen        :", getattr(sys, "frozen", False))
print("bundle dir    :", getattr(sys, "_MEIPASS", "(not frozen)"))

service = accessibility.a11y
service.start()
print("backend       :", service.backend_name())
print("available     :", service.speech_available)

service.speak("PyP6 speech check. If you can hear this, the bundled build talks.")
print("spoke a test line")
