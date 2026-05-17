#!/usr/bin/env python3
"""
main.py  –  Spotify → Audio Downloader
Just run:  python main.py
Everything is asked interactively — no flags required.
(Power users can still pass flags; run  python main.py --help  to see them.)
"""

import sys
import argparse
from pathlib import Path

# ── Dependency check ──────────────────────────────────────────────────────────
try:
    import yt_dlp
    import mutagen
    from rich.console import Console
    from rich.panel   import Panel
    from rich.table   import Table
    from rich         import box
except ImportError:
    print("❌  Missing core dependencies.")
    print("    Run:  pip install yt-dlp mutagen rich questionary")
    sys.exit(1)

try:
    import questionary
    from questionary import Style as QStyle
    _HAS_Q = True
except ImportError:
    questionary = None
    _HAS_Q = False

# ── Local imports ─────────────────────────────────────────────────────────────
from utils    import console, check_ffmpeg, remove_duplicates, generate_m3u
from recorder import record_spotify
from youtube  import search_youtube, download_songs
from metadata import process_metadata

# ── Questionary style (matches Rich cyan/magenta theme) ──────────────────────
_Q_STYLE = QStyle([
    ("qmark",        "fg:#00d7ff bold"),
    ("question",     "bold"),
    ("answer",       "fg:#ff79c6 bold"),
    ("pointer",      "fg:#00d7ff bold"),
    ("highlighted",  "fg:#00d7ff bold"),
    ("selected",     "fg:#ff79c6"),
    ("separator",    "fg:#6272a4"),
    ("instruction",  "fg:#6272a4 italic"),
]) if _HAS_Q else None


# ─────────────────────────────────────────────────────────────────────────────
# Thin wrappers around questionary / plain input  (graceful fallback)
# ─────────────────────────────────────────────────────────────────────────────

def _ask_select(message: str, choices: list, default: str = None) -> str:
    """Single-choice menu. Returns the chosen value string."""
    if _HAS_Q:
        # choices can be strings or {"name": ..., "value": ...} dicts
        result = questionary.select(
            message, choices=choices, default=default, style=_Q_STYLE
        ).ask()
        if result is None:
            _abort()
        return result
    else:
        # Plain-text fallback
        console.print(f"\n[bold]{message}[/bold]")
        items = []
        for i, c in enumerate(choices, 1):
            label = c["name"] if isinstance(c, dict) else c
            value = c["value"] if isinstance(c, dict) else c
            items.append(value)
            console.print(f"  [cyan]{i}[/cyan]. {label}")
        while True:
            raw = input("  Enter number: ").strip()
            if raw.isdigit() and 1 <= int(raw) <= len(items):
                return items[int(raw) - 1]
            console.print("  [red]Invalid choice, try again.[/red]")


def _ask_confirm(message: str, default: bool = False) -> bool:
    """Yes/No question. Returns bool."""
    if _HAS_Q:
        result = questionary.confirm(
            message, default=default, style=_Q_STYLE
        ).ask()
        if result is None:
            _abort()
        return result
    else:
        hint = "[Y/n]" if default else "[y/N]"
        raw = input(f"  {message} {hint}: ").strip().lower()
        if not raw:
            return default
        return raw in ("y", "yes")


def _ask_text(message: str, default: str = "") -> str:
    """Free-text input. Returns stripped string."""
    if _HAS_Q:
        result = questionary.text(
            message,
            default=default,
            style=_Q_STYLE,
        ).ask()
        if result is None:
            _abort()
        return result.strip()
    else:
        hint = f" [{default}]" if default else ""
        raw = input(f"  {message}{hint}: ").strip()
        return raw if raw else default


def _ask_path(message: str, default: str) -> Path:
    """Ask for a file/folder path with a default."""
    return Path(_ask_text(message, default))


def _abort():
    console.print("\n[yellow]Cancelled.[/yellow]")
    sys.exit(0)


# ─────────────────────────────────────────────────────────────────────────────
# Welcome banner
# ─────────────────────────────────────────────────────────────────────────────

def _banner():
    console.print()
    console.print(Panel.fit(
        "[bold cyan]🎵  Spotify → Audio Downloader[/bold cyan]\n"
        "[dim]Opus · FLAC · M4A · MP3  |  Auto-tagging · Lyrics · Hinglish[/dim]",
        border_style="cyan",
        padding=(0, 4),
    ))
    console.print()


# ─────────────────────────────────────────────────────────────────────────────
# Manual song entry (unchanged logic, kept here for self-containment)
# ─────────────────────────────────────────────────────────────────────────────

def _manual_entry(output_file: Path, append: bool = False) -> int:
    mode = "a" if append and output_file.exists() else "w"
    existing = 0
    if mode == "a" and output_file.exists():
        existing = sum(
            1 for ln in output_file.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        )

    console.print()
    console.print(
        "[bold cyan]━━━  Manual Song Entry[/bold cyan]\n"
        "\nType one song per line. Two formats work:\n"
        "  [green]Song Title - Artist Name[/green]   ← best\n"
        "  [green]Song Title[/green]                 ← artist asked separately\n\n"
        "Empty line or [bold]done[/bold] / [bold]q[/bold] to finish.\n"
    )

    songs: list[str] = []
    while True:
        try:
            raw = input("  ➜  ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not raw or raw.lower() in ("done", "q", "quit", "exit"):
            break
        if " - " not in raw:
            try:
                artist = input(f"     Artist for '{raw}' (Enter to skip): ").strip()
            except (EOFError, KeyboardInterrupt):
                artist = ""
            if artist:
                raw = f"{raw} - {artist}"
        songs.append(raw)
        console.print(f"     [green]✓[/green] Added: [bold]{raw}[/bold]")

    if songs:
        with open(output_file, mode, encoding="utf-8") as fh:
            if mode == "a" and existing:
                fh.write("\n")
            fh.write("\n".join(songs) + "\n")
        suffix = f" (+ {existing} existing)" if existing else ""
        console.print(
            f"\n[bold green]✓ {len(songs)} song(s) saved{suffix}.[/bold green]\n"
        )
    else:
        console.print("[yellow]⚠  No songs entered.[/yellow]\n")
    return len(songs)


# ─────────────────────────────────────────────────────────────────────────────
# The wizard  –  called when no CLI flags are passed
# ─────────────────────────────────────────────────────────────────────────────

def _wizard():
    _banner()

    if not _HAS_Q:
        console.print(
            "[yellow]⚠  'questionary' not installed — using plain prompts.\n"
            "   For the full menu experience:  pip install questionary[/yellow]\n"
        )

    # ── Step 1: What do you want to do? ──────────────────────────────────────
    MODE_FULL     = "full"
    MODE_MANUAL   = "manual"
    MODE_SEARCH   = "search"
    MODE_DOWNLOAD = "download"
    MODE_DEDUPE   = "dedupe"
    MODE_CONVERT  = "convert"

    mode = _ask_select(
        "What do you want to do?",
        choices=[
            {"name": "🚀  Full pipeline  (enter / record songs → search → download → tag)", "value": MODE_FULL},
            {"name": "✏️   Enter song names manually, then search & download",               "value": MODE_MANUAL},
            {"name": "🔍  Search YouTube only  (songs.txt already exists)",                  "value": MODE_SEARCH},
            {"name": "⬇️   Download only        (found.txt already exists)",                 "value": MODE_DOWNLOAD},
            {"name": "🗑️   Remove duplicate audio files from a folder",                      "value": MODE_DEDUPE},
            {"name": "🔄  Convert existing FLAC / MP3 files to Opus",                        "value": MODE_CONVERT},
        ],
    )

    # ── Dedupe branch ─────────────────────────────────────────────────────────
    if mode == MODE_DEDUPE:
        target = _ask_path("Folder to scan for duplicates", "songs")
        if not target.exists() or not target.is_dir():
            console.print(f"[bold red]❌  '{target}' not found.[/bold red]")
            sys.exit(1)
        remove_duplicates(target)
        return

    # ── Convert branch ────────────────────────────────────────────────────────
    if mode == MODE_CONVERT:
        import subprocess as _sp
        source_dir = _ask_path("Folder containing audio files", "songs")
        quality = _ask_select(
            "Opus quality preset",
            choices=[
                {"name": "best     (~256 kbps, transparent — recommended)", "value": "best"},
                {"name": "high     (~192 kbps, excellent)",                  "value": "high"},
                {"name": "standard (~128 kbps, mobile / streaming)",         "value": "standard"},
            ],
            default="best",
        )
        keep = _ask_confirm("Keep original files after conversion?", default=False)
        dry  = _ask_confirm("Dry-run first (preview without converting)?", default=False)
        cmd  = [sys.executable, "convert_to_opus.py",
                "--dir", str(source_dir), "--quality", quality]
        if keep: cmd.append("--keep-originals")
        if dry:  cmd.append("--dry-run")
        _sp.run(cmd)
        return

    # ── Common path questions (used by most modes) ────────────────────────────
    songs_file = _ask_path("Song list file", "songs.txt")
    out_dir    = _ask_path("Output folder for audio files", "songs")

    do_record = False
    do_manual = False
    do_search = False
    do_dl     = False

    if mode == MODE_FULL:
        source = _ask_select(
            "How will you provide the song list?",
            choices=[
                {"name": "✏️   Type song names here (manual entry)",                 "value": "manual"},
                {"name": "🎙️  Record from Spotify via playerctl (Linux only)",        "value": "record"},
                {"name": "📄  Use existing songs.txt (already have one)",             "value": "existing"},
            ],
        )
        if source == "manual":
            do_manual = True
        elif source == "record":
            do_record = True
        do_search = True
        do_dl     = True

    elif mode == MODE_MANUAL:
        do_manual = True
        do_search = True
        do_dl     = True

    elif mode == MODE_SEARCH:
        do_search = True

    elif mode == MODE_DOWNLOAD:
        found_file_check = _ask_path("found.txt file path", "found.txt")
        do_dl = True

    # ── Format & quality ─────────────────────────────────────────────────────
    audio_format = "opus"
    quality      = "192"

    if do_dl:
        audio_format = _ask_select(
            "Audio format",
            choices=[
                {"name": "Opus   🏆  Transparent, ~40-50% smaller than FLAC  (recommended)", "value": "opus"},
                {"name": "FLAC      Lossless / studio quality / largest files",               "value": "flac"},
                {"name": "M4A       AAC — modern, widely compatible",                         "value": "m4a"},
                {"name": "MP3       Legacy — maximum device compatibility",                   "value": "mp3"},
            ],
            default="opus",
        )

        if audio_format != "flac":
            quality = _ask_select(
                "Audio bitrate (kbps)",
                choices=[
                    {"name": "256 kbps  — excellent, barely any difference from 320",  "value": "256"},
                    {"name": "192 kbps  — great quality, smaller files  (recommended)", "value": "192"},
                    {"name": "320 kbps  — maximum lossy quality, largest files",        "value": "320"},
                    {"name": "128 kbps  — acceptable, very small files",               "value": "128"},
                ],
                default="192",
            )

    # ── Optional features ─────────────────────────────────────────────────────
    organize  = False
    normalize = False
    playlist  = None
    retry_tag = False
    use_whisper    = False
    whisper_model  = "small"
    whisper_lang   = None
    cookie_cfg: dict = {}
    workers = 5
    resume  = False

    if do_search or do_dl:
        console.print("\n[bold]Optional features[/bold]  (answer each quickly)\n")

    if do_search:
        workers_str = _ask_select(
            "Number of parallel YouTube search threads",
            choices=[
                {"name": "3  — slow network / gentle on YouTube",  "value": "3"},
                {"name": "5  — balanced  (recommended)",           "value": "5"},
                {"name": "10 — fast network",                      "value": "10"},
            ],
            default="5",
        )
        workers = int(workers_str)

        if songs_file.exists():
            resume = _ask_confirm(
                "found.txt already exists — skip search and reuse it?",
                default=False,
            )

    if do_dl:
        organize  = _ask_confirm("Organise files into Artist sub-folders?",    default=False)
        normalize = _ask_confirm("Normalise volume to -14 LUFS (Spotify level)?", default=False)

        want_playlist = _ask_confirm("Generate an .m3u playlist file?", default=False)
        if want_playlist:
            playlist = _ask_text("Playlist name (no extension)", "My Playlist")

        retry_tag = _ask_confirm(
            "Run an extra tagging pass for songs with incomplete metadata?",
            default=True,
        )

        use_whisper = _ask_confirm(
            "Use Whisper AI to transcribe lyrics for songs that have none?\n"
            "  (needs:  pip install openai-whisper)",
            default=False,
        )
        if use_whisper:
            whisper_model = _ask_select(
                "Whisper model size",
                choices=[
                    {"name": "tiny   — fastest, least accurate",              "value": "tiny"},
                    {"name": "base   — fast, decent accuracy",                "value": "base"},
                    {"name": "small  — good balance  (recommended)",          "value": "small"},
                    {"name": "medium — better accuracy, needs ~5 GB RAM",     "value": "medium"},
                    {"name": "large  — best accuracy, needs ~10 GB RAM",      "value": "large"},
                ],
                default="small",
            )
            want_lang = _ask_confirm(
                "Force a specific language for Whisper? (No = auto-detect)", default=False
            )
            if want_lang:
                whisper_lang = _ask_text(
                    "Language code  e.g. hi (Hindi)  ur (Urdu)  en (English)", ""
                ) or None

        want_cookies = _ask_confirm(
            "Some videos are age-restricted and fail to download.\n"
            "  Use browser cookies to unlock them?",
            default=False,
        )
        if want_cookies:
            browser = _ask_select(
                "Which browser are you logged into YouTube with?",
                choices=[
                    {"name": "Chrome",    "value": "chrome"},
                    {"name": "Firefox",   "value": "firefox"},
                    {"name": "Edge",      "value": "edge"},
                    {"name": "Chromium",  "value": "chromium"},
                    {"name": "Brave",     "value": "brave"},
                    {"name": "Safari",    "value": "safari"},
                    {"name": "Opera",     "value": "opera"},
                    {"name": "Vivaldi",   "value": "vivaldi"},
                ],
                default="chrome",
            )
            cookie_cfg = {"cookiesfrombrowser": (browser,)}
            console.print(f"[dim]🍪  Will borrow cookies from {browser}.[/dim]")

    # ── Confirmation summary ──────────────────────────────────────────────────
    console.print()
    summary_tbl = Table(
        title="📋  Run Plan", show_header=True,
        header_style="bold magenta", box=box.ROUNDED,
    )
    summary_tbl.add_column("Setting", style="cyan")
    summary_tbl.add_column("Value",   style="green")

    steps = []
    if do_record: steps.append("Record Spotify playlist")
    if do_manual: steps.append("Manual song entry")
    if do_search: steps.append("Search YouTube")
    if do_dl:     steps.append("Download + Tag")
    summary_tbl.add_row("Steps",        " → ".join(steps))
    summary_tbl.add_row("Song list",    str(songs_file))
    summary_tbl.add_row("Output folder",str(out_dir))

    if do_dl:
        summary_tbl.add_row("Format",      audio_format.upper())
        if audio_format != "flac":
            summary_tbl.add_row("Bitrate",  f"{quality} kbps")
        summary_tbl.add_row("Organize",    "Yes (by Artist)" if organize else "No")
        summary_tbl.add_row("Normalize",   "Yes (-14 LUFS)"  if normalize else "No")
        summary_tbl.add_row("Playlist",    playlist if playlist else "No")
        summary_tbl.add_row("Retry tags",  "Yes"             if retry_tag else "No")
        summary_tbl.add_row("Whisper",     f"Yes ({whisper_model})" if use_whisper else "No")
        summary_tbl.add_row("Cookies",     next(iter(cookie_cfg.values()))[0]
                                           if cookie_cfg else "No")

    console.print(summary_tbl)
    console.print()

    go = _ask_confirm("Everything look right? Start now?", default=True)
    if not go:
        _abort()

    # ── Run the pipeline ──────────────────────────────────────────────────────
    found_file    = songs_file.parent / "found.txt"
    notfound_file = songs_file.parent / "not_found.txt"

    # 1. Record
    if do_record:
        record_spotify(songs_file)

    # 2. Manual entry
    if do_manual:
        added = _manual_entry(songs_file, append=songs_file.exists())
        if added == 0 and not songs_file.exists():
            console.print("[bold red]❌  No songs entered. Exiting.[/bold red]")
            sys.exit(0)

    # 3. Search
    if do_search:
        if resume and found_file.exists():
            console.print(
                f"[bold green]✓ Reusing existing '{found_file}'.[/bold green]"
            )
        else:
            search_youtube(songs_file, found_file, notfound_file, max_workers=workers)

    # 4. Download + tag
    if do_dl:
        if not check_ffmpeg():
            sys.exit(1)

        # In download-only mode the user may have given a different found.txt path
        if mode == MODE_DOWNLOAD:
            found_file = found_file_check  # type: ignore[name-defined]

        if not found_file.exists() or found_file.stat().st_size == 0:
            console.print("[bold red]❌  No URLs to download. Run search first.[/bold red]")
            sys.exit(1)

        console.print("\n[bold cyan]━━━  Downloading tracks[/bold cyan]")
        downloaded = download_songs(
            found_file, out_dir,
            format_ext=audio_format,
            quality=quality,
            max_workers=2,
            normalize=normalize,
            cookie_cfg=cookie_cfg,
        )

        # Outer retry for tracks that failed every internal strategy
        if downloaded is not None:
            done_names = {n for n, _ in downloaded}
            with open(found_file, encoding="utf-8") as fh:
                all_lines = [ln.strip() for ln in fh if ln.strip() and "|" in ln]
            failed_lines = [
                ln for ln in all_lines
                if ln.split("|")[0].strip() not in done_names
            ]
            if failed_lines:
                console.print(
                    f"\n[bold yellow]⚠️  Retrying {len(failed_lines)} failed track(s)…[/bold yellow]"
                )
                from youtube import download_track
                for ln in failed_lines:
                    song_name = ln.split("|")[0].strip()
                    fp = download_track(
                        ln, out_dir,
                        format_ext=audio_format,
                        quality=quality,
                        normalize=normalize,
                        cookie_cfg=cookie_cfg,
                    )
                    if fp:
                        downloaded.append((song_name, fp))
                        console.print(f"  [green]✓ Retry OK:[/green] {song_name}")
                    else:
                        console.print(f"  [red]✗ Still failed:[/red] {song_name}")

        if not downloaded:
            console.print("[bold red]❌  No songs downloaded.[/bold red]")
            return

        # Tag
        console.print("\n[bold cyan]━━━  Tagging tracks[/bold cyan]")
        final_paths = process_metadata(
            downloaded,
            organize=organize,
            output_dir=out_dir,
            retry_failed=retry_tag,
            use_whisper=use_whisper,
            whisper_model=whisper_model,
            whisper_language=whisper_lang,
        )

        if playlist:
            generate_m3u(playlist, out_dir, found_file, final_paths)

        # Final summary
        console.print()
        done_tbl = Table(
            title="🎉  Finished!", show_header=True,
            header_style="bold magenta", box=box.ROUNDED,
        )
        done_tbl.add_column("Task",   style="cyan")
        done_tbl.add_column("Result", justify="right", style="green")
        done_tbl.add_row("Downloaded",    str(len(downloaded)))
        done_tbl.add_row("Format",        audio_format.upper())
        done_tbl.add_row("Quality",       "Lossless" if audio_format == "flac" else f"{quality} kbps")
        done_tbl.add_row("Tagged",        str(len(final_paths)))
        if normalize:    done_tbl.add_row("Normalised", "Yes (-14 LUFS)")
        if organize:     done_tbl.add_row("Organised",  "By Artist")
        if playlist:     done_tbl.add_row("Playlist",   f"{playlist}.m3u")
        if use_whisper:  done_tbl.add_row("Whisper",    f"Model: {whisper_model}")
        if cookie_cfg:   done_tbl.add_row("Cookies",    next(iter(cookie_cfg.values()))[0])
        console.print(done_tbl)
        console.print("\n[bold green]All done! Enjoy your music. 🎶[/bold green]\n")


# ─────────────────────────────────────────────────────────────────────────────
# Legacy CLI  (for scripting / power users — python main.py --all etc.)
# ─────────────────────────────────────────────────────────────────────────────

def _cli():
    """Thin argparse wrapper so existing scripts keep working."""
    parser = argparse.ArgumentParser(
        description="Spotify → Audio Downloader  (run without flags for interactive mode)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Tip: just run  python main.py  for the guided interactive wizard.\n\n"
            "Flag reference\n"
            "--------------\n"
            "  --all              Full pipeline\n"
            "  --record           Record Spotify playlist\n"
            "  --manual           Type song names interactively\n"
            "  --search           Search YouTube\n"
            "  --download         Download + tag\n"
            "  --format           opus / flac / m4a / mp3\n"
            "  --quality          128 / 192 / 256 / 320\n"
            "  --organize         Sort into Artist folders\n"
            "  --normalize        -14 LUFS loudness\n"
            "  --retry-tag        Second tagging pass\n"
            "  --whisper          Whisper lyrics fallback\n"
            "  --whisper-model    tiny/base/small/medium/large\n"
            "  --whisper-lang     hi / ur / en / …\n"
            "  --cookies-browser  chrome / firefox / edge / …\n"
            "  --cookies-file     /path/to/cookies.txt\n"
            "  --playlist NAME    Generate .m3u\n"
            "  --dedupe DIR       Remove duplicate files\n"
            "  --resume           Reuse existing found.txt\n"
            "  --no-prompt        Skip startup manual-entry question\n"
            "  --workers N        Search thread count\n"
        ),
    )
    parser.add_argument("--record",          action="store_true")
    parser.add_argument("--search",          action="store_true")
    parser.add_argument("--download",        action="store_true")
    parser.add_argument("--all",             action="store_true")
    parser.add_argument("--manual",          action="store_true")
    parser.add_argument("--no-prompt",       action="store_true")
    parser.add_argument("--input",           default="songs.txt")
    parser.add_argument("--found",           default="found.txt")
    parser.add_argument("--notfound",        default="not_found.txt")
    parser.add_argument("--output-dir",      default="songs")
    parser.add_argument("--workers",         type=int, default=5)
    parser.add_argument("--quality",         choices=["128","192","256","320"], default="192")
    parser.add_argument("--format",          choices=["mp3","flac","m4a","opus"])
    parser.add_argument("--organize",        action="store_true")
    parser.add_argument("--resume",          action="store_true")
    parser.add_argument("--normalize",       action="store_true")
    parser.add_argument("--playlist",        type=str, metavar="NAME")
    parser.add_argument("--dedupe",          type=str, metavar="DIR")
    parser.add_argument("--retry-tag",       action="store_true")
    parser.add_argument("--whisper",         action="store_true")
    parser.add_argument("--whisper-model",   default="small",
                        choices=["tiny","base","small","medium","large"])
    parser.add_argument("--whisper-lang",    default=None, metavar="LANG")
    parser.add_argument("--cookies-browser", default=None, metavar="BROWSER",
                        choices=["chrome","chromium","firefox","edge",
                                 "safari","brave","opera","vivaldi"])
    parser.add_argument("--cookies-file",    default=None, metavar="PATH")
    args = parser.parse_args()

    # ── Dedupe ────────────────────────────────────────────────────────────────
    if args.dedupe:
        d = Path(args.dedupe)
        if not d.exists() or not d.is_dir():
            console.print(f"[red]❌  '{d}' not found.[/red]"); sys.exit(1)
        remove_duplicates(d); sys.exit(0)

    # ── Manual-only ───────────────────────────────────────────────────────────
    if args.manual and not any([args.search, args.download, args.all, args.record]):
        _manual_entry(Path(args.input), append=Path(args.input).exists())
        console.print("[green]✓ Songs saved. Run --search --download when ready.[/green]")
        return

    # ── Format selection ──────────────────────────────────────────────────────
    if (args.download or args.all) and not args.format:
        args.format = _ask_select(
            "Choose audio format",
            choices=[
                {"name": "Opus (transparent, ~40-50% smaller than FLAC)", "value": "opus"},
                {"name": "FLAC (lossless)", "value": "flac"},
                {"name": "M4A / AAC",       "value": "m4a"},
                {"name": "MP3",             "value": "mp3"},
            ],
            default="opus",
        )

    input_file    = Path(args.input)
    found_file    = Path(args.found)
    notfound_file = Path(args.notfound)
    out_dir       = Path(args.output_dir)
    normalize     = args.normalize or args.all

    cookie_cfg: dict = {}
    if args.cookies_browser:
        cookie_cfg["cookiesfrombrowser"] = (args.cookies_browser,)
    elif args.cookies_file:
        cf = Path(args.cookies_file)
        if not cf.exists():
            console.print(f"[red]❌  Cookies file not found: {cf}[/red]"); sys.exit(1)
        cookie_cfg["cookiefile"] = str(cf)

    # ── Manual entry ──────────────────────────────────────────────────────────
    if args.manual:
        _manual_entry(input_file, append=input_file.exists())
    elif not args.no_prompt and not args.record and (args.download or args.all or args.search):
        try:
            if input("➜  Add songs manually before continuing? [y/N]: ").strip().lower() in ("y","yes"):
                _manual_entry(input_file, append=input_file.exists())
        except (EOFError, KeyboardInterrupt):
            pass

    # ── Record ────────────────────────────────────────────────────────────────
    if args.record or args.all:
        record_spotify(input_file)

    # ── Search ────────────────────────────────────────────────────────────────
    if args.search or args.all:
        if args.resume and found_file.exists():
            console.print(f"[green]✓ Reusing '{found_file}'.[/green]")
        else:
            search_youtube(input_file, found_file, notfound_file, max_workers=args.workers)

    # ── Download + tag ────────────────────────────────────────────────────────
    if args.download or args.all:
        if not check_ffmpeg(): sys.exit(1)
        if not found_file.exists() or found_file.stat().st_size == 0:
            console.print("[red]❌  No URLs found. Run --search first.[/red]"); sys.exit(1)

        console.print("\n[bold cyan]━━━  Downloading[/bold cyan]")
        downloaded = download_songs(
            found_file, out_dir,
            format_ext=args.format, quality=args.quality,
            max_workers=2, normalize=normalize, cookie_cfg=cookie_cfg,
        )

        if downloaded is not None:
            done_names = {n for n, _ in downloaded}
            with open(found_file, encoding="utf-8") as fh:
                all_lines = [ln.strip() for ln in fh if ln.strip() and "|" in ln]
            failed = [ln for ln in all_lines if ln.split("|")[0].strip() not in done_names]
            if failed:
                console.print(f"\n[yellow]⚠️  Retrying {len(failed)} failed track(s)…[/yellow]")
                from youtube import download_track
                for ln in failed:
                    sn = ln.split("|")[0].strip()
                    fp = download_track(ln, out_dir, format_ext=args.format,
                                        quality=args.quality, normalize=normalize,
                                        cookie_cfg=cookie_cfg)
                    if fp:
                        downloaded.append((sn, fp))
                        console.print(f"  [green]✓ Retry OK:[/green] {sn}")
                    else:
                        console.print(f"  [red]✗ Still failed:[/red] {sn}")

        if downloaded:
            console.print("\n[bold cyan]━━━  Tagging[/bold cyan]")
            final_paths = process_metadata(
                downloaded, organize=args.organize, output_dir=out_dir,
                retry_failed=args.retry_tag or args.all,
                use_whisper=args.whisper,
                whisper_model=args.whisper_model,
                whisper_language=args.whisper_lang,
            )
            if args.playlist:
                generate_m3u(args.playlist, out_dir, found_file, final_paths)

            t = Table(title="🎉 Done", header_style="bold magenta")
            t.add_column("Task"); t.add_column("Result", justify="right", style="green")
            t.add_row("Downloaded", str(len(downloaded)))
            t.add_row("Format",     args.format.upper())
            t.add_row("Tagged",     str(len(final_paths)))
            console.print(t)
            console.print("\n[bold green]All done! Enjoy your music. 🎶[/bold green]\n")
        else:
            console.print("[red]❌  No songs downloaded.[/red]")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # If the user passed any flags → legacy CLI mode
    # If they just typed  python main.py  → interactive wizard
    if len(sys.argv) > 1:
        _cli()
    else:
        try:
            _wizard()
        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted.[/yellow]")
            sys.exit(0)