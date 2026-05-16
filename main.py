#!/usr/bin/env python3
"""
Spotify-to-Audio Converter (CLI Edition)
Modular architecture: orchestrates recording, downloading, and tagging.

New in v2
---------
• Opus format option (perceptually transparent, ~40-50% smaller than FLAC)
• --whisper / --whisper-model: transcribe audio for missing lyrics
• --retry-tag: extra metadata retry pass after the first tagging sweep
• Download retry: failed tracks are re-attempted once after the initial pass
"""

import sys
import argparse
from pathlib import Path

# ── Dependency check before local imports ─────────────────────────────────────
try:
    import yt_dlp
    import mutagen
    from rich.table import Table
except ImportError:
    print("❌ Missing dependencies. Run: pip install yt-dlp mutagen rich questionary")
    sys.exit(1)

try:
    import questionary
except ImportError:
    questionary = None

# ── Local module imports ──────────────────────────────────────────────────────
from utils     import console, check_ffmpeg, remove_duplicates, generate_m3u
from recorder  import record_spotify
from youtube   import search_youtube, download_songs
from metadata  import process_metadata


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Spotify Playlist → Audio Downloader + Tagger",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Formats
-------
  flac   Lossless (largest files, studio quality)
  opus   Transparent lossy — ~40-50%% smaller than FLAC, no audible difference
           at 256 kbps. Ideal for large libraries.
  m4a    AAC — widely compatible, efficient
  mp3    Legacy — maximum compatibility

Examples
--------
  # Full pipeline, Opus, with Whisper fallback for lyrics
  python main.py --all --format opus --whisper

  # Download only, 320 kbps MP3, retry failed tags
  python main.py --download --format mp3 --quality 320 --retry-tag

  # Remove duplicate files in a directory
  python main.py --dedupe ./songs
""",
    )

    # ── Mode flags ───────────────────────────────────────────────────────────
    parser.add_argument("--record",   action="store_true", help="Record Spotify playlist to songs.txt")
    parser.add_argument("--search",   action="store_true", help="Search YouTube for songs in songs.txt")
    parser.add_argument("--download", action="store_true", help="Download found songs & apply metadata")
    parser.add_argument("--all",      action="store_true", help="Run the complete pipeline")

    # ── File / directory options ─────────────────────────────────────────────
    parser.add_argument("--input",      default="songs.txt",     help="Input song list (default: songs.txt)")
    parser.add_argument("--found",      default="found.txt",     help="Output file for found URLs (default: found.txt)")
    parser.add_argument("--notfound",   default="not_found.txt", help="Output for missing songs (default: not_found.txt)")
    parser.add_argument("--output-dir", default="songs",         help="Directory for audio files (default: songs)")

    # ── Quality / format ─────────────────────────────────────────────────────
    parser.add_argument("--workers",  type=int, default=5,
                        help="Search thread count (default: 5)")
    parser.add_argument("--quality",  choices=["128", "192", "256", "320"], default="192",
                        help="Bitrate for lossy formats (default: 192)")
    parser.add_argument("--format",   choices=["mp3", "flac", "m4a", "opus"],
                        help="Target audio format (omit to show menu)")

    # ── Optional pipeline features ───────────────────────────────────────────
    parser.add_argument("--organize",  action="store_true",
                        help="Organize files into Artist sub-folders")
    parser.add_argument("--resume",    action="store_true",
                        help="Skip search if found.txt already exists")
    parser.add_argument("--normalize", action="store_true",
                        help="Normalize audio to -14 LUFS (Spotify loudness)")
    parser.add_argument("--playlist",  type=str, metavar="NAME",
                        help="Generate an .m3u playlist with this name")
    parser.add_argument("--dedupe",    type=str, metavar="DIR",
                        help="Remove duplicate files in DIR and exit")

    # ── Metadata / lyrics extras ─────────────────────────────────────────────
    parser.add_argument("--retry-tag", action="store_true",
                        help="Run a second metadata pass for incompletely tagged files")
    parser.add_argument("--whisper",   action="store_true",
                        help="Use OpenAI Whisper to generate lyrics for songs "
                             "that have none (requires: pip install openai-whisper)")
    parser.add_argument("--whisper-model", default="small",
                        choices=["tiny", "base", "small", "medium", "large"],
                        help="Whisper model size (default: small). "
                             "Larger = more accurate but slower & needs more RAM.")
    parser.add_argument("--whisper-lang", default=None, metavar="LANG",
                        help="Force Whisper language code, e.g. 'hi' (Hindi), "
                             "'ur' (Urdu), 'en' (English). Default: auto-detect.")

    args = parser.parse_args()

    # ── Standalone deduplicate ────────────────────────────────────────────────
    if args.dedupe:
        dedupe_dir = Path(args.dedupe)
        if not dedupe_dir.exists() or not dedupe_dir.is_dir():
            console.print(f"[bold red]❌ Directory '{dedupe_dir}' not found.[/bold red]")
            sys.exit(1)
        remove_duplicates(dedupe_dir)
        sys.exit(0)

    if not any([args.record, args.search, args.download, args.all]):
        parser.print_help()
        sys.exit(0)

    # ── Interactive format selection ──────────────────────────────────────────
    if (args.download or args.all) and not args.format:
        if questionary:
            format_choice = questionary.select(
                "Choose your preferred audio format:",
                choices=[
                    questionary.Choice(
                        "Opus   (Transparent / ~40-50% smaller than FLAC, no audible loss)",
                        value="opus"
                    ),
                    questionary.Choice(
                        "FLAC   (Lossless / Studio Quality / largest files)",
                        value="flac"
                    ),
                    questionary.Choice(
                        "M4A / AAC  (Modern / Highly Efficient)",
                        value="m4a"
                    ),
                    questionary.Choice(
                        "MP3   (Legacy / Maximum Compatibility)",
                        value="mp3"
                    ),
                ],
                default="opus",
            ).ask()

            if not format_choice:
                console.print("[yellow]Operation cancelled.[/yellow]")
                sys.exit(0)
            args.format = format_choice
        else:
            console.print(
                "[yellow]⚠️  'questionary' not found – defaulting to Opus. "
                "Install it for the interactive menu: pip install questionary[/yellow]"
            )
            args.format = "opus"

    # ── Resolve paths ─────────────────────────────────────────────────────────
    input_file    = Path(args.input)
    found_file    = Path(args.found)
    notfound_file = Path(args.notfound)
    out_dir       = Path(args.output_dir)
    should_normalize = args.normalize or args.all

    # ── Pipeline stages ───────────────────────────────────────────────────────
    if args.record or args.all:
        record_spotify(input_file)

    if args.search or args.all:
        if args.resume and found_file.exists():
            console.print(
                f"[bold green]✓ '{found_file}' exists – skipping search (--resume).[/bold green]"
            )
        else:
            search_youtube(input_file, found_file, notfound_file, max_workers=args.workers)

    if args.download or args.all:
        if not check_ffmpeg():
            sys.exit(1)

        if not found_file.exists() or found_file.stat().st_size == 0:
            console.print(
                "[bold red]❌ No URLs found. Run --search first or remove --resume.[/bold red]"
            )
            sys.exit(1)

        # ── Initial download ─────────────────────────────────────────────────
        console.print("\n[bold cyan]━━━  Downloading tracks[/bold cyan]")
        downloaded = download_songs(
            found_file, out_dir,
            format_ext=args.format,
            quality=args.quality,
            max_workers=2,
            normalize=should_normalize,
        )

        # ── Outer retry for tracks that failed all internal strategies ────────
        if downloaded is not None:
            downloaded_names = {name for name, _ in downloaded}
            with open(found_file, "r", encoding="utf-8") as fh:
                all_lines = [ln.strip() for ln in fh if ln.strip() and "|" in ln]
            failed_lines = [
                ln for ln in all_lines
                if ln.split("|")[0].strip() not in downloaded_names
            ]
            if failed_lines:
                console.print(
                    f"\n[bold yellow]⚠️  Retrying {len(failed_lines)} failed download(s)…[/bold yellow]"
                )
                from youtube import download_track
                from pathlib import Path as _Path
                for ln in failed_lines:
                    fp = download_track(
                        ln, out_dir,
                        format_ext=args.format,
                        quality=args.quality,
                        normalize=should_normalize,
                    )
                    song_name = ln.split("|")[0].strip()
                    if fp:
                        downloaded.append((song_name, fp))
                        console.print(f"  [green]✓ Retry OK:[/green] {song_name}")
                    else:
                        console.print(f"  [red]✗ Still failed:[/red] {song_name}")

        if downloaded:
            # ── Metadata + lyrics ────────────────────────────────────────────
            console.print("\n[bold cyan]━━━  Tagging tracks[/bold cyan]")
            final_paths = process_metadata(
                downloaded,
                organize=args.organize,
                output_dir=out_dir,
                retry_failed=args.retry_tag or args.all,
                use_whisper=args.whisper,
                whisper_model=args.whisper_model,
                whisper_language=args.whisper_lang,
            )

            if args.playlist:
                generate_m3u(args.playlist, out_dir, found_file, final_paths)

            # ── Summary table ────────────────────────────────────────────────
            console.print()
            summary = Table(
                title="🎉 Run Summary",
                show_header=True,
                header_style="bold magenta",
            )
            summary.add_column("Task",   style="cyan")
            summary.add_column("Result", justify="right", style="green")

            summary.add_row("Downloaded Tracks",  str(len(downloaded)))
            summary.add_row("Format Selected",    args.format.upper())
            summary.add_row("Quality",            f"{args.quality} kbps"
                                                  if args.format != "flac" else "Lossless")
            summary.add_row("Metadata Tagged",    str(len(final_paths)))

            if should_normalize:
                summary.add_row("Audio Normalized",    "Yes (-14 LUFS)")
            if args.organize:
                summary.add_row("Folder Organization", "By Artist")
            if args.playlist:
                summary.add_row("Playlist Generated",  f"{args.playlist}.m3u")
            if args.whisper:
                summary.add_row("Whisper Lyrics",      f"Model: {args.whisper_model}")
            if args.retry_tag or args.all:
                summary.add_row("Tag Retry Pass",      "Enabled")

            console.print(summary)
            console.print(
                "\n[bold green]All tasks completed successfully! Enjoy your music.[/bold green]\n"
            )

        else:
            console.print("[bold red]❌ No songs were downloaded.[/bold red]")


if __name__ == "__main__":
    main()