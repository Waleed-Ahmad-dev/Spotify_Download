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

New in v3
---------
• --manual: interactively type song names / artists at the terminal
            (great for adding a few new tracks without re-recording a playlist)
• --no-prompt: skip the startup "do you want to add songs manually?" question
• --cookies-browser: pass your browser's cookies to yt-dlp to bypass
            age-restricted YouTube videos (chrome / firefox / edge / etc.)
• --cookies-file: alternative — point to a Netscape cookies.txt file
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
from utils    import console, check_ffmpeg, remove_duplicates, generate_m3u
from recorder import record_spotify
from youtube  import search_youtube, download_songs
from metadata import process_metadata


# ─────────────────────────────────────────────────────────────────────────────
# Manual song entry
# ─────────────────────────────────────────────────────────────────────────────

def manual_entry(output_file: Path, append: bool = False) -> int:
    """
    Interactively ask the user to type song names and optional artists.

    Each entry is written to *output_file* in the standard
    ``Song Title - Artist Name`` format (same as recorder.py output).

    Parameters
    ----------
    output_file : Path   Where to write / append the song list.
    append      : bool   If True and file exists, new songs are appended.

    Returns the number of songs added in this session.
    """
    mode = "a" if append and output_file.exists() else "w"
    existing_count = 0
    if mode == "a" and output_file.exists():
        existing_count = sum(
            1 for ln in output_file.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        )

    console.print()
    console.print(
        "[bold cyan]━━━  Manual Song Entry[/bold cyan]\n"
        "\nType one song per line.  Two formats accepted:\n\n"
        "  [green]Song Title - Artist Name[/green]   ← recommended (combined)\n"
        "  [green]Song Title[/green]                 ← artist will be asked separately\n\n"
        "Press [bold]Enter[/bold] on an empty line  or  type "
        "[bold]done[/bold] / [bold]q[/bold] to finish.\n"
    )

    songs: list[str] = []

    while True:
        try:
            raw = input("  ➜  ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]Entry stopped.[/yellow]")
            break

        if not raw or raw.lower() in ("done", "q", "quit", "exit"):
            break

        # If no dash separator, optionally ask for the artist
        if " - " not in raw:
            try:
                artist_raw = input(
                    f"     Artist for '{raw}' (press Enter to skip): "
                ).strip()
            except (EOFError, KeyboardInterrupt):
                artist_raw = ""
            if artist_raw:
                raw = f"{raw} - {artist_raw}"

        songs.append(raw)
        console.print(f"     [green]✓[/green] Added: [bold]{raw}[/bold]")

    if songs:
        with open(output_file, mode, encoding="utf-8") as fh:
            # Ensure there's a newline before appended content
            if mode == "a" and existing_count:
                fh.write("\n")
            fh.write("\n".join(songs))
            fh.write("\n")
        suffix = f" (+ {existing_count} existing)" if existing_count else ""
        console.print(
            f"\n[bold green]✓ {len(songs)} song(s) saved to "
            f"'{output_file}'{suffix}.[/bold green]\n"
        )
    else:
        console.print("[yellow]⚠ No songs entered.[/yellow]\n")

    return len(songs)


def _ask_manual_at_startup() -> bool:
    """Quick yes/no prompt shown at startup when --manual wasn't passed."""
    try:
        answer = input(
            "➜  Add songs manually before downloading? [y/N]: "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Spotify Playlist → Audio Downloader + Tagger",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Formats
-------
  opus   Transparent lossy — ~40-50%% smaller than FLAC, no audible difference
           at 256 kbps.  Ideal for large libraries.  (default)
  flac   Lossless / studio quality / largest files
  m4a    AAC — modern, widely compatible
  mp3    Legacy — maximum device compatibility

Age-gated videos
----------------
  Some YouTube videos require login.  Pass your browser name and yt-dlp
  will borrow the already-logged-in session from it automatically:
    --cookies-browser chrome
    --cookies-browser firefox
  No passwords are ever stored or transmitted.

Examples
--------
  # Type a few songs interactively, then search + download as Opus
  python main.py --manual --search --download --format opus

  # Full pipeline; startup prompt asks whether to add songs manually
  python main.py --all

  # Full pipeline, skip the manual-entry prompt entirely
  python main.py --all --no-prompt

  # Bypass age-restricted videos using Chrome cookies
  python main.py --all --cookies-browser chrome

  # Download only, 320 kbps MP3, retry failed tags
  python main.py --download --format mp3 --quality 320 --retry-tag

  # Remove duplicate files in a directory
  python main.py --dedupe ./songs
""",
    )

    # ── Mode flags ───────────────────────────────────────────────────────────
    parser.add_argument("--record",   action="store_true",
                        help="Record Spotify playlist to songs.txt via playerctl")
    parser.add_argument("--search",   action="store_true",
                        help="Search YouTube for songs listed in songs.txt")
    parser.add_argument("--download", action="store_true",
                        help="Download found songs & apply metadata/lyrics")
    parser.add_argument("--all",      action="store_true",
                        help="Run the complete pipeline (record→search→download)")

    # ── Manual entry ─────────────────────────────────────────────────────────
    parser.add_argument(
        "--manual", action="store_true",
        help=(
            "Manually type song names / artists at the terminal instead of "
            "(or in addition to) recording a Spotify playlist.  "
            "Appends to --input file when it already exists."
        ),
    )
    parser.add_argument(
        "--no-prompt", action="store_true",
        help="Skip the interactive 'add songs manually?' startup prompt.",
    )

    # ── File / directory options ─────────────────────────────────────────────
    parser.add_argument("--input",      default="songs.txt",
                        help="Input song list file (default: songs.txt)")
    parser.add_argument("--found",      default="found.txt",
                        help="Output file for found YouTube URLs (default: found.txt)")
    parser.add_argument("--notfound",   default="not_found.txt",
                        help="Output file for songs not found (default: not_found.txt)")
    parser.add_argument("--output-dir", default="songs",
                        help="Directory to save audio files (default: songs)")

    # ── Quality / format ─────────────────────────────────────────────────────
    parser.add_argument("--workers", type=int, default=5,
                        help="Number of parallel search threads (default: 5)")
    parser.add_argument("--quality", choices=["128", "192", "256", "320"], default="192",
                        help="Audio bitrate for lossy formats in kbps (default: 192)")
    parser.add_argument("--format",  choices=["mp3", "flac", "m4a", "opus"],
                        help="Target audio format (omit to show interactive menu)")

    # ── Optional pipeline features ───────────────────────────────────────────
    parser.add_argument("--organize",  action="store_true",
                        help="Move downloaded files into Artist sub-folders")
    parser.add_argument("--resume",    action="store_true",
                        help="Skip the search step if found.txt already exists")
    parser.add_argument("--normalize", action="store_true",
                        help="Normalize audio volume to -14 LUFS (Spotify standard)")
    parser.add_argument("--playlist",  type=str, metavar="NAME",
                        help="Generate an .m3u playlist file with this name")
    parser.add_argument("--dedupe",    type=str, metavar="DIR",
                        help="Scan DIR for duplicate audio files, remove them, and exit")

    # ── Metadata / lyrics extras ─────────────────────────────────────────────
    parser.add_argument("--retry-tag", action="store_true",
                        help="Run a second metadata/lyrics pass for incompletely tagged files")
    parser.add_argument("--whisper",   action="store_true",
                        help="Use OpenAI Whisper to transcribe lyrics for songs that have none "
                             "(requires: pip install openai-whisper  and  ffmpeg on PATH)")
    parser.add_argument("--whisper-model", default="small",
                        choices=["tiny", "base", "small", "medium", "large"],
                        help="Whisper model size — larger = more accurate but slower (default: small)")
    parser.add_argument("--whisper-lang", default=None, metavar="LANG",
                        help="Force Whisper language code, e.g. 'hi' (Hindi), 'ur' (Urdu), "
                             "'en' (English).  Default: auto-detect.")

    # ── Age-gate / cookies ────────────────────────────────────────────────────
    parser.add_argument(
        "--cookies-browser",
        default=None,
        metavar="BROWSER",
        choices=["chrome", "chromium", "firefox", "edge", "safari",
                 "brave", "opera", "vivaldi"],
        help=(
            "Borrow cookies from this browser to unlock age-restricted YouTube videos.  "
            "You must already be logged into YouTube in that browser.  "
            "Choices: chrome · chromium · firefox · edge · safari · brave · opera · vivaldi"
        ),
    )
    parser.add_argument(
        "--cookies-file",
        default=None,
        metavar="PATH",
        help=(
            "Path to a Netscape-format cookies.txt file (alternative to --cookies-browser).  "
            "Export one with a browser extension such as 'Get cookies.txt LOCALLY'."
        ),
    )

    args = parser.parse_args()

    # ── Standalone deduplicate ────────────────────────────────────────────────
    if args.dedupe:
        dedupe_dir = Path(args.dedupe)
        if not dedupe_dir.exists() or not dedupe_dir.is_dir():
            console.print(f"[bold red]❌ Directory '{dedupe_dir}' not found.[/bold red]")
            sys.exit(1)
        remove_duplicates(dedupe_dir)
        sys.exit(0)

    if not any([args.record, args.search, args.download, args.all, args.manual]):
        parser.print_help()
        sys.exit(0)

    # ── Resolve paths ─────────────────────────────────────────────────────────
    input_file    = Path(args.input)
    found_file    = Path(args.found)
    notfound_file = Path(args.notfound)
    out_dir       = Path(args.output_dir)
    should_normalize = args.normalize or args.all

    # ── Build cookie config dict (forwarded to youtube module) ────────────────
    cookie_cfg: dict = {}
    if args.cookies_browser:
        cookie_cfg["cookiesfrombrowser"] = (args.cookies_browser,)
        console.print(
            f"\n[dim]🍪  Cookies: [bold]{args.cookies_browser}[/bold] browser session "
            "(age-restricted videos will be unlocked)[/dim]"
        )
    elif args.cookies_file:
        cf = Path(args.cookies_file)
        if not cf.exists():
            console.print(f"[bold red]❌ Cookies file not found: {cf}[/bold red]")
            sys.exit(1)
        cookie_cfg["cookiefile"] = str(cf)
        console.print(f"\n[dim]🍪  Cookies file: {cf}[/dim]")

    # ── Manual entry stage ────────────────────────────────────────────────────
    if args.manual:
        # Explicit flag: always run, append if songs.txt already exists
        added = manual_entry(input_file, append=input_file.exists())
        if added == 0 and not input_file.exists():
            console.print(
                "[bold red]❌ No songs entered and no existing songs.txt. Exiting.[/bold red]"
            )
            sys.exit(0)

    elif not args.no_prompt and not args.record and (
        args.download or args.all or args.search
    ):
        # No explicit flag: show a quick yes/no at startup
        if _ask_manual_at_startup():
            manual_entry(input_file, append=input_file.exists())

    # ── Interactive format selection ──────────────────────────────────────────
    if (args.download or args.all) and not args.format:
        if questionary:
            format_choice = questionary.select(
                "Choose your preferred audio format:",
                choices=[
                    questionary.Choice(
                        "Opus   (Transparent / ~40-50% smaller than FLAC, no audible loss)",
                        value="opus",
                    ),
                    questionary.Choice(
                        "FLAC   (Lossless / Studio Quality / largest files)",
                        value="flac",
                    ),
                    questionary.Choice(
                        "M4A / AAC  (Modern / Highly Efficient)",
                        value="m4a",
                    ),
                    questionary.Choice(
                        "MP3   (Legacy / Maximum Compatibility)",
                        value="mp3",
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

    # ── Pipeline stages ───────────────────────────────────────────────────────
    if args.record or args.all:
        record_spotify(input_file)

    if args.search or args.all:
        if args.resume and found_file.exists():
            console.print(
                f"[bold green]✓ '{found_file}' exists – skipping search (--resume).[/bold green]"
            )
        else:
            search_youtube(
                input_file, found_file, notfound_file,
                max_workers=args.workers,
            )

    if args.download or args.all:
        if not check_ffmpeg():
            sys.exit(1)

        if not found_file.exists() or found_file.stat().st_size == 0:
            console.print(
                "[bold red]❌ No URLs found. Run --search first or remove --resume.[/bold red]"
            )
            sys.exit(1)

        # ── Initial download ──────────────────────────────────────────────────
        console.print("\n[bold cyan]━━━  Downloading tracks[/bold cyan]")
        downloaded = download_songs(
            found_file, out_dir,
            format_ext=args.format,
            quality=args.quality,
            max_workers=2,
            normalize=should_normalize,
            cookie_cfg=cookie_cfg,
        )

        # ── Outer retry for tracks that failed all internal strategies ─────────
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
                    f"\n[bold yellow]⚠️  Retrying {len(failed_lines)} "
                    "failed download(s)…[/bold yellow]"
                )
                from youtube import download_track
                for ln in failed_lines:
                    fp = download_track(
                        ln, out_dir,
                        format_ext=args.format,
                        quality=args.quality,
                        normalize=should_normalize,
                        cookie_cfg=cookie_cfg,
                    )
                    song_name = ln.split("|")[0].strip()
                    if fp:
                        downloaded.append((song_name, fp))
                        console.print(f"  [green]✓ Retry OK:[/green] {song_name}")
                    else:
                        console.print(f"  [red]✗ Still failed:[/red] {song_name}")

        if downloaded:
            # ── Metadata + lyrics ─────────────────────────────────────────────
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

            # ── Summary table ─────────────────────────────────────────────────
            console.print()
            summary = Table(
                title="🎉 Run Summary",
                show_header=True,
                header_style="bold magenta",
            )
            summary.add_column("Task",   style="cyan")
            summary.add_column("Result", justify="right", style="green")

            summary.add_row("Downloaded Tracks", str(len(downloaded)))
            summary.add_row("Format Selected",   args.format.upper())
            summary.add_row(
                "Quality",
                f"{args.quality} kbps" if args.format != "flac" else "Lossless",
            )
            summary.add_row("Metadata Tagged", str(len(final_paths)))

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
            if cookie_cfg:
                src = args.cookies_browser or (
                    Path(args.cookies_file).name if args.cookies_file else "file"
                )
                summary.add_row("Cookie Source", src)

            console.print(summary)
            console.print(
                "\n[bold green]All tasks completed successfully! "
                "Enjoy your music.[/bold green]\n"
            )

        else:
            console.print("[bold red]❌ No songs were downloaded.[/bold red]")

    # ── Manual-only mode ──────────────────────────────────────────────────────
    elif args.manual and not any([args.search, args.download, args.all, args.record]):
        console.print(
            "[bold green]✓ Songs saved.[/bold green]  "
            "Run [cyan]python main.py --search --download[/cyan] when ready."
        )


if __name__ == "__main__":
    main()