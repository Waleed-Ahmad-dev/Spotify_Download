# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller build spec for the Spotify -> Audio Downloader (Windows).

Build:
    pyinstaller spotify_downloader.spec
or just run  build_windows.ps1  (installs deps + PyInstaller, then runs this).

Produces a self-contained one-dir app under  dist/spotify-downloader/ :
    spotify-downloader.exe
    vendor/ffmpeg.exe, vendor/ffprobe.exe   (resolved from PATH at build time)
    ...all Python + WinRT dependencies...

Notes:
  • One-dir (COLLECT) is used instead of one-file: far more reliable for the
    WinRT compiled .pyd modules and the bundled ffmpeg binaries.
  • Whisper/torch and the other optional libs are intentionally excluded to keep
    the artifact small; they remain available in source/pip installs.
"""

import shutil
from PyInstaller.utils.hooks import collect_all

# ── Bundle ffmpeg + ffprobe (from PATH) into vendor/ ─────────────────────────
# utils.get_ffmpeg()/get_ffprobe() look inside a vendor/ dir next to the exe.
binaries = []
for _tool in ("ffmpeg", "ffprobe"):
    _path = shutil.which(_tool)
    if _path:
        binaries.append((_path, "vendor"))
        print(f"[spec] bundling {_tool}: {_path}")
    else:
        print(f"[spec] WARNING: {_tool} not on PATH — NOT bundled. "
              f"The .exe will then require {_tool} to be installed on the host.")

# ── Collect the WinRT bindings (compiled modules + metadata) ─────────────────
datas = []
hiddenimports = [
    "convert_to_opus", "recorder", "youtube", "metadata",
    "utils", "transliterate_lyrics",
]
for _pkg in (
    "winrt.runtime",
    "winrt.windows.foundation",
    "winrt.windows.foundation.collections",
    "winrt.windows.media",
    "winrt.windows.media.control",
):
    try:
        d, b, h = collect_all(_pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as exc:                       # noqa: BLE001
        print(f"[spec] collect_all({_pkg!r}) skipped: {exc}")


a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Keep the bundle lean — these are source/pip-only optional features.
    excludes=["torch", "whisper", "lyricsgenius", "indic_transliteration",
              "tkinter", "numpy"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="spotify-downloader",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # this is a terminal TUI app
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="spotify-downloader",
)
