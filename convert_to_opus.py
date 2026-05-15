#!/usr/bin/env python3
"""
convert_to_opus.py
Safely converts all FLAC files in a directory tree to Opus 128k.
- Detects and deletes corrupt/incomplete .opus files (from interrupted conversions)
- Skips FLACs that already have a valid .opus counterpart
- Handles ALL filenames correctly regardless of special characters
- Shows a progress bar and final summary
"""

import subprocess
import sys
from pathlib import Path

# ── Tunables ──────────────────────────────────────────────────────────────────
SONGS_DIR   = Path("songs")   # change if your folder is elsewhere
BITRATE     = "128k"
MIN_SIZE_B  = 32_768          # opus files smaller than 32 KB are almost certainly corrupt
# ─────────────────────────────────────────────────────────────────────────────


def is_valid_opus(path: Path) -> bool:
    """Return True if the .opus file is non-empty and ffprobe can read it."""
    if not path.exists() or path.stat().st_size < MIN_SIZE_B:
        return False
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True
    )
    try:
        duration = float(result.stdout.strip())
        return duration > 1.0          # must be at least 1 second long
    except (ValueError, TypeError):
        return False


def convert(flac: Path) -> bool:
    """Convert a single FLAC to Opus beside it. Returns True on success."""
    opus = flac.with_suffix(".opus")
    cmd = [
        "ffmpeg", "-i", str(flac),
        "-c:a", "libopus",
        "-b:a", BITRATE,
        "-vbr", "on",
        "-compression_level", "10",
        "-map_metadata", "0",
        "-vn",               # drop cover art (opus container can't carry MJPEG)
        "-y",                # overwrite if somehow exists
        str(opus)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0 and is_valid_opus(opus)


def main():
    if not SONGS_DIR.exists():
        print(f"❌  Directory '{SONGS_DIR}' not found. Run this from your project root.")
        sys.exit(1)

    all_flac  = sorted(SONGS_DIR.rglob("*.flac"))
    all_opus  = sorted(SONGS_DIR.rglob("*.opus"))

    print(f"\n🔍  Scanning '{SONGS_DIR}' …")
    print(f"    Found {len(all_flac)} FLAC files")
    print(f"    Found {len(all_opus)} Opus files\n")

    # ── Step 1: find and delete corrupt / incomplete opus files ──────────────
    corrupt: list[Path] = []
    for opus in all_opus:
        if not is_valid_opus(opus):
            corrupt.append(opus)

    if corrupt:
        print(f"⚠️   Found {len(corrupt)} corrupt/incomplete Opus file(s) — deleting them:\n")
        for f in corrupt:
            print(f"    🗑️  {f}")
            f.unlink()
        print()
    else:
        print("✅  No corrupt Opus files found.\n")

    # ── Step 2: convert every FLAC that doesn't have a valid opus twin ───────
    to_convert: list[Path] = []
    already_done: list[Path] = []

    for flac in all_flac:
        opus = flac.with_suffix(".opus")
        if is_valid_opus(opus):
            already_done.append(flac)
        else:
            to_convert.append(flac)

    print(f"📊  Conversion plan:")
    print(f"    Already converted (skipping): {len(already_done)}")
    print(f"    Need conversion:              {len(to_convert)}\n")

    if not to_convert:
        print("🎉  Nothing to do — all FLACs already have valid Opus counterparts.")
        return

    succeeded, failed = [], []

    for idx, flac in enumerate(to_convert, 1):
        label = f"[{idx}/{len(to_convert)}]"
        print(f"{label}  Converting: {flac.name}", end="  … ", flush=True)
        if convert(flac):
            print("✅ done")
            flac.unlink()          # remove the original FLAC only after success
            succeeded.append(flac)
        else:
            print("❌ FAILED (original FLAC kept)")
            failed.append(flac)

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "─" * 60)
    print(f"🎉  Conversion complete!")
    print(f"    Converted:  {len(succeeded)} files")
    print(f"    Skipped:    {len(already_done)} (already done)")
    print(f"    Failed:     {len(failed)} (originals preserved)")
    if failed:
        print("\n  Failed files:")
        for f in failed:
            print(f"    • {f}")
    print()


if __name__ == "__main__":
    main()
