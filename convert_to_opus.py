#!/usr/bin/env python3
"""
convert_to_opus.py
==================
Batch-converts FLAC (and optionally MP3 / M4A) files in a directory tree
to Opus VBR — perceptually transparent at 256 kbps and ~40-50% smaller
than FLAC.

Quality presets
---------------
  best     → ~256 kbps VBR  (transparent, default)
  high     → ~192 kbps VBR  (excellent, saves more space)
  standard → ~128 kbps VBR  (good for mobile / streaming)

Usage examples
--------------
  python convert_to_opus.py
  python convert_to_opus.py --quality high
  python convert_to_opus.py --also mp3 m4a
  python convert_to_opus.py --keep-originals
  python convert_to_opus.py --dry-run
  python convert_to_opus.py --dir /path/to/music
"""

import argparse
import subprocess
import sys
from pathlib import Path

# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_SONGS_DIR = Path("songs")
MIN_SIZE_B        = 32_768          # files < 32 KB are almost certainly corrupt

QUALITY_PRESETS = {
    "best":     "256k",             # transparent
    "high":     "192k",             # excellent
    "standard": "128k",             # space-saver
}

SUPPORTED_EXTRA = {"mp3", "m4a", "ogg", "webm"}


# ── Validity check ────────────────────────────────────────────────────────────

def is_valid_opus(path: Path) -> bool:
    """Return True if the .opus file is non-empty and ffprobe can read it."""
    if not path.exists() or path.stat().st_size < MIN_SIZE_B:
        return False
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True, text=True,
    )
    try:
        return float(result.stdout.strip()) > 1.0
    except (ValueError, TypeError):
        return False


# ── Single-file conversion ────────────────────────────────────────────────────

def convert(source: Path, bitrate: str) -> bool:
    """
    Convert *source* to an Opus file beside it using libopus VBR.
    Copies all Vorbis-comment tags; strips embedded cover art (the Opus
    container cannot carry MJPEG streams from FLAC/MP3).
    Returns True on success.
    """
    opus = source.with_suffix(".opus")
    cmd = [
        "ffmpeg", "-i", str(source),
        "-c:a",               "libopus",
        "-b:a",               bitrate,
        "-vbr",               "on",
        "-compression_level", "10",
        "-map_metadata",      "0",
        "-vn",                # drop cover art
        "-y",                 # overwrite if present
        str(opus),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0 and is_valid_opus(opus)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Batch-convert audio files to Opus VBR",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dir", default=str(DEFAULT_SONGS_DIR), metavar="PATH",
        help=f"Root directory to scan (default: {DEFAULT_SONGS_DIR})",
    )
    parser.add_argument(
        "--quality", choices=list(QUALITY_PRESETS), default="best",
        help="VBR quality preset  best=256k · high=192k · standard=128k  (default: best)",
    )
    parser.add_argument(
        "--also", nargs="*", metavar="EXT", default=[],
        help=(
            "Extra source extensions to convert in addition to FLAC. "
            f"Supported: {', '.join(sorted(SUPPORTED_EXTRA))}"
        ),
    )
    parser.add_argument(
        "--keep-originals", action="store_true",
        help="Do NOT delete source files after successful conversion",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be done without converting anything",
    )
    args = parser.parse_args()

    songs_dir = Path(args.dir)
    if not songs_dir.exists():
        print(f"❌  Directory '{songs_dir}' not found.")
        sys.exit(1)

    bitrate = QUALITY_PRESETS[args.quality]

    # Build the set of source extensions to process
    source_exts = {"flac"}
    for ext in args.also:
        ext = ext.lstrip(".").lower()
        if ext in SUPPORTED_EXTRA:
            source_exts.add(ext)
        else:
            print(f"⚠️   Ignoring unknown extension: '{ext}'")

    print(f"\n🔍  Scanning '{songs_dir}' …")
    print(f"    Source formats : {', '.join(sorted(e.upper() for e in source_exts))}")
    print(f"    Opus bitrate   : {bitrate} VBR")
    print(f"    Keep originals : {args.keep_originals}")
    print(f"    Dry-run        : {args.dry_run}")
    print()

    # Collect all source files and existing Opus files
    all_sources: list[Path] = []
    for ext in source_exts:
        all_sources.extend(sorted(songs_dir.rglob(f"*.{ext}")))
    all_opus = sorted(songs_dir.rglob("*.opus"))

    print(f"    Found {len(all_sources)} source file(s)")
    print(f"    Found {len(all_opus)} existing Opus file(s)\n")

    # ── Step 1: find and delete corrupt / incomplete Opus files ──────────────
    corrupt: list[Path] = []
    for opus in all_opus:
        if not is_valid_opus(opus):
            corrupt.append(opus)

    if corrupt:
        print(f"⚠️   Found {len(corrupt)} corrupt/incomplete Opus file(s):\n")
        for f in corrupt:
            print(f"    🗑️  {f}")
            if not args.dry_run:
                f.unlink()
        print()
    else:
        print("✅  No corrupt Opus files found.\n")

    # ── Step 2: build conversion plan ────────────────────────────────────────
    to_convert:   list[Path] = []
    already_done: list[Path] = []

    for src in all_sources:
        if is_valid_opus(src.with_suffix(".opus")):
            already_done.append(src)
        else:
            to_convert.append(src)

    print("📊  Conversion plan:")
    print(f"    Already converted (skip) : {len(already_done)}")
    print(f"    Need conversion          : {len(to_convert)}\n")

    if not to_convert:
        print("🎉  Nothing to do — all source files already have valid Opus counterparts.")
        return

    if args.dry_run:
        print("🔎  Dry-run — files that WOULD be converted:\n")
        for src in to_convert:
            opus_size_estimate = (
                src.stat().st_size * 0.35
                if args.quality == "best"
                else src.stat().st_size * 0.25
            )
            print(
                f"    {src.suffix.upper()[1:]:5s} → Opus  "
                f"{src.stat().st_size / 1_048_576:.1f} MB → "
                f"~{opus_size_estimate / 1_048_576:.1f} MB   {src}"
            )
        print()
        return

    # ── Step 3: convert ───────────────────────────────────────────────────────
    succeeded: list[Path] = []
    failed:    list[Path] = []

    for idx, src in enumerate(to_convert, 1):
        label = f"[{idx}/{len(to_convert)}]"
        src_mb = src.stat().st_size / 1_048_576
        print(
            f"{label}  {src.suffix.upper()[1:]} → Opus  "
            f"({src_mb:.1f} MB)  {src.name}",
            end="  … ", flush=True,
        )
        if convert(src, bitrate):
            opus_path = src.with_suffix(".opus")
            opus_mb   = opus_path.stat().st_size / 1_048_576 if opus_path.exists() else 0
            saving    = (1 - opus_mb / src_mb) * 100 if src_mb else 0
            print(f"✅ done  ({opus_mb:.1f} MB, saved {saving:.0f}%)")
            succeeded.append(src)
            if not args.keep_originals:
                src.unlink()
        else:
            print("❌ FAILED (original kept)")
            failed.append(src)

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "─" * 60)
    print("🎉  Conversion complete!")
    print(f"    Converted  : {len(succeeded)} file(s)  →  Opus {bitrate} VBR")
    print(f"    Skipped    : {len(already_done)} (already converted)")
    print(f"    Failed     : {len(failed)} (originals preserved)")
    if not args.keep_originals and succeeded:
        print(f"    Originals  : deleted ({len(succeeded)} file(s) removed)")
    if failed:
        print("\n  Failed files:")
        for f in failed:
            print(f"    • {f}")
    print()


if __name__ == "__main__":
    main()