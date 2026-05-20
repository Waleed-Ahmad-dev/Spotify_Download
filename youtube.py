#!/usr/bin/env python3
"""
youtube.py
YouTube search + download engine.

Strategy (v6)
-------------
Download and conversion are two completely separate steps:

  Step 1 – yt-dlp downloads the BEST AVAILABLE audio stream in whatever
            container the video offers (webm/m4a/ogg/mp4 – anything).
            No codec is specified, so "Requested format is not available"
            can never occur under normal circumstances.

  Step 2 – ffmpeg converts the raw download to the user's chosen format
            (opus / flac / mp3 / m4a) and, optionally, normalises loudness.

Key fixes (v6)
--------------
• A custom _QuietLogger suppresses yt-dlp's raw stderr spam entirely;
  errors surface only through our own formatted messages.
• When cookies are present, the 'web' player client runs FIRST — it is the
  only client that can derive PO (Proof-of-Origin) tokens from browser
  cookies, which YouTube requires in 2024-2025 for most clients.
• 'mweb' and 'tv_embedded' follow; both are historically exempt from the
  strictest PO-token checks.
• yt-dlp defaults ({}) run next for maximum built-in compatibility.
• A format-string ladder is tried per client before moving on.
• A yt-dlp version warning fires if the installed version is too old.
"""

import sys
import time
import random
import subprocess
import concurrent.futures
from datetime import date
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional

import yt_dlp
from rich.progress import (
    Progress, SpinnerColumn, TextColumn, BarColumn,
    TaskProgressColumn, TimeRemainingColumn,
)

from utils import console, sanitize_filename

# ── yt-dlp version guard ──────────────────────────────────────────────────────
_MIN_YTDLP_DATE = date(2024, 11, 1)   # anything older is likely to break

def _check_ytdlp_version() -> None:
    """Warn if yt-dlp is older than _MIN_YTDLP_DATE."""
    try:
        ver = yt_dlp.version.__version__   # e.g. "2024.12.23"
        parts = ver.split(".")
        if len(parts) >= 3:
            release = date(int(parts[0]), int(parts[1]), int(parts[2]))
            if release < _MIN_YTDLP_DATE:
                console.print(
                    f"[bold yellow]⚠  yt-dlp {ver} is outdated.[/bold yellow] "
                    "Many downloads will fail.\n"
                    "   Run: [cyan]pip install -U yt-dlp[/cyan]\n"
                )
    except Exception:
        pass

_check_ytdlp_version()


# ── Silent logger — suppresses yt-dlp's raw stderr ERROR: lines ──────────────
class _QuietLogger:
    """Redirect all yt-dlp log output to /dev/null."""
    def debug(self, msg: str)   -> None: pass
    def info(self, msg: str)    -> None: pass
    def warning(self, msg: str) -> None: pass
    def error(self, msg: str)   -> None: pass


# ── Client strategies ─────────────────────────────────────────────────────────
# Ordered by reliability in 2025 YouTube:
#   1. web  — needs PO tokens, but derives them from browser cookies
#   2. mweb — mobile web, often exempt from strictest PO-token checks
#   3. tv_embedded — historically exempt from PO tokens
#   4. {}   — yt-dlp auto-selects the best available client
#   5-7.    — specific clients as last resorts
_BASE_STRATEGIES: List[Dict[str, Any]] = [
    {"player_client": ["mweb"],                          "skip": ["translated_subs"]},
    {"player_client": ["tv_embedded"],                   "skip": ["translated_subs"]},
    {},                                                                          # yt-dlp defaults
    {"player_client": ["android_music"],                 "skip": ["translated_subs"]},
    {"player_client": ["android_music", "android"],      "skip": ["translated_subs"]},
    {"player_client": ["android", "tv_embedded", "web"], "skip": ["translated_subs"]},
]

# When cookies ARE present, prepend 'web' (PO tokens flow from cookie session)
_WEB_STRATEGY: Dict[str, Any] = {
    "player_client": ["web"], "skip": ["translated_subs"]
}

_SEARCH_EXTRACTOR_ARGS: Dict[str, Any] = {
    "youtube": {
        "player_client": ["ios", "web"],
        "skip": ["translated_subs"],
    }
}

# Format-string ladder — tried in order on format errors.
# Each entry is progressively more permissive.
_FORMAT_STRINGS: List[str] = [
    "bestaudio/best",
    "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio[ext=ogg]/bestaudio/best",
    "ba[ext=webm]/ba[ext=m4a]/ba/b",
    "bestvideo[acodec!=none]+bestaudio/bestvideo[acodec!=none]/best",
    "best",
]

# Raw audio extensions yt-dlp may produce before our ffmpeg pass
_RAW_AUDIO_EXTS = {".webm", ".m4a", ".ogg", ".opus", ".mp3", ".flac", ".wav", ".aac", ".mp4"}

# ── Error classification ──────────────────────────────────────────────────────
_AGE_GATE_ERRORS = frozenset([
    "sign in to confirm your age",
    "age-restricted",
    "age restricted",
    "inappropriate for some users",
    "this video may be inappropriate",
    "video is age restricted",
    "confirm your age",
    "requires authentication",
    "login required",
    "private video",
    "members-only",
])

_FORMAT_ERRORS = frozenset([
    "format is not available",
    "no video formats found",
    "requested format is not available",
    "no audio formats found",
])

_UNAVAILABLE_ERRORS = frozenset([
    "video unavailable",
    "this video is not available",
    "this video has been removed",
    "copyright",
    "blocked",
])

_RATE_LIMIT_ERRORS = frozenset([
    "429",
    "too many requests",
    "http error 429",
])


def _is_age_gate_error(msg: str)    -> bool: return any(k in msg.lower() for k in _AGE_GATE_ERRORS)
def _is_format_error(msg: str)      -> bool: return any(k in msg.lower() for k in _FORMAT_ERRORS)
def _is_unavailable_error(msg: str) -> bool: return any(k in msg.lower() for k in _UNAVAILABLE_ERRORS)
def _is_rate_limit_error(msg: str)  -> bool: return any(k in msg.lower() for k in _RATE_LIMIT_ERRORS)


# ─────────────────────────────────────────────────────────────────────────────
# Step 1 helpers – yt-dlp download (raw audio, any container)
# ─────────────────────────────────────────────────────────────────────────────

def _build_ydl_opts(
    output_stem:  Path,
    client_args:  Dict[str, Any],
    cookie_cfg:   Dict[str, Any],
    pp_hook,
    fmt:          str = "bestaudio/best",
) -> Dict[str, Any]:
    """Build yt-dlp options for a raw audio download (no codec conversion)."""
    opts: Dict[str, Any] = {
        "format":      fmt,
        "outtmpl":     str(output_stem) + ".%(ext)s",
        # Use our silent logger instead of quiet/no_warnings flags so that
        # yt-dlp never writes directly to stderr.
        "logger":      _QuietLogger(),
        "noprogress":  True,
        "age_limit":   99,
        "postprocessors":      [],
        "postprocessor_hooks": [pp_hook],
    }
    if client_args:
        opts["extractor_args"] = {"youtube": client_args}
    opts.update(cookie_cfg)
    return opts


def _find_raw_download(output_folder: Path, safe_name: str) -> Optional[Path]:
    """Find whatever audio file yt-dlp actually wrote for this track."""
    prefix = safe_name[:50].lower()
    for f in output_folder.iterdir():
        if (
            f.suffix.lower() in _RAW_AUDIO_EXTS
            and f.stat().st_size > 0
            and f.stem.lower().startswith(prefix)
        ):
            return f
    return None


def _attempt_download(
    url:           str,
    output_stem:   Path,
    output_folder: Path,
    safe_name:     str,
    client_args:   Dict[str, Any],
    cookie_cfg:    Dict[str, Any],
    fmt:           str,
) -> Tuple[Optional[Path], Optional[str]]:
    """
    One yt-dlp download attempt.
    Returns (path_or_None, error_message_or_None).
    All yt-dlp output is silenced via _QuietLogger.
    """
    final_path: List[Optional[Path]] = [None]

    def _pp_hook(d: Dict[str, Any]) -> None:
        if d.get("status") == "finished":
            fp = d.get("filepath") or (d.get("info_dict") or {}).get("filepath")
            if fp:
                final_path[0] = Path(fp)

    opts = _build_ydl_opts(output_stem, client_args, cookie_cfg, _pp_hook, fmt)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

        candidate = final_path[0]
        if candidate and candidate.exists() and candidate.stat().st_size > 0:
            return candidate, None
        found = _find_raw_download(output_folder, safe_name)
        return (found, None) if found else (None, "download completed but file not found")

    except yt_dlp.utils.DownloadError as exc:
        return None, str(exc)
    except Exception as exc:
        return None, str(exc)


def _raw_download(
    url:           str,
    song_name:     str,
    output_folder: Path,
    cookie_cfg:    Dict[str, Any],
) -> Optional[Path]:
    """
    Try every (client_strategy × format_string) combination.
    Returns the Path of the raw downloaded file, or None.

    Outer loop : client strategies
    Inner loop : format strings (cycled on format errors only)

    When cookies are present, the 'web' client is prepended — it's the only
    client that can derive PO tokens from browser cookies (needed in 2025 YT).
    """
    safe_name   = sanitize_filename(song_name)
    output_stem = output_folder / safe_name

    existing = _find_raw_download(output_folder, safe_name)
    if existing:
        return existing

    strategies = list(_BASE_STRATEGIES)
    if cookie_cfg:
        strategies = [_WEB_STRATEGY, *strategies]

    age_gate_emergency_used = False
    last_error_global: Optional[str] = None

    for attempt_idx, client_args in enumerate(strategies):
        last_error: Optional[str] = None

        # ── Inner loop: format-string fallbacks ───────────────────────────────
        for fmt in _FORMAT_STRINGS:
            path, err = _attempt_download(
                url, output_stem, output_folder,
                safe_name, client_args, cookie_cfg, fmt,
            )
            if path:
                return path

            last_error = err
            last_error_global = err

            if err is None:
                break

            if _is_format_error(err):
                continue          # try next, more-permissive format string

            break                 # non-format error: format cycling won't help

        # ── Post-inner: classify and react ───────────────────────────────────
        if last_error is None:
            continue

        if _is_unavailable_error(last_error):
            console.print(
                f"  [bold red]✗ Video unavailable:[/bold red] {song_name}\n"
                "    It may be deleted, geo-blocked, or copyright-restricted."
            )
            return None           # no point trying more strategies

        if _is_age_gate_error(last_error):
            if cookie_cfg and not age_gate_emergency_used:
                age_gate_emergency_used = True
                console.print(
                    f"  [yellow]⚠ Age-restricted:[/yellow] {song_name} — "
                    "retrying with cookies + tv_embedded…"
                )
                for fmt in _FORMAT_STRINGS:
                    path, _ = _attempt_download(
                        url, output_stem, output_folder, safe_name,
                        {"player_client": ["tv_embedded"], "skip": ["translated_subs"]},
                        cookie_cfg, fmt,
                    )
                    if path:
                        return path
            elif attempt_idx == 0:
                console.print(
                    f"  [red]✗ Age-restricted:[/red] {song_name}\n"
                    "    [dim]To unlock: choose 'Use browser cookies' in the wizard.[/dim]"
                )
            continue

        if _is_rate_limit_error(last_error):
            wait = random.uniform(8, 15)
            console.print(f"  [yellow]⚠ Rate-limited[/yellow] — waiting {wait:.0f}s…")
            time.sleep(wait)
            continue

        if _is_format_error(last_error):
            continue   # all format strings exhausted; try next client

    # All strategies failed
    if last_error_global and _is_format_error(last_error_global):
        console.print(
            f"  [bold red]✗ All download strategies failed for '{song_name}'.[/bold red]\n"
            "  Possible causes:\n"
            "    • [yellow]Outdated yt-dlp[/yellow] — run: [cyan]pip install -U yt-dlp[/cyan]\n"
            "    • Video is geo-blocked in your region\n"
            "    • YouTube is temporarily rate-limiting your IP"
        )
    elif last_error_global:
        console.print(
            f"  [bold red]✗ Download failed for '{song_name}':[/bold red] {last_error_global}"
        )
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Step 2 helper – ffmpeg conversion
# ─────────────────────────────────────────────────────────────────────────────

_CODEC_MAP = {
    "opus": "libopus",
    "mp3":  "libmp3lame",
    "flac": "flac",
    "m4a":  "aac",
}

_CONTAINER_MAP = {
    "m4a": "mp4",
}


def _convert_to_format(
    raw_file:   Path,
    out_path:   Path,
    format_ext: str,
    quality:    str,
    normalize:  bool,
) -> bool:
    """Convert raw_file → out_path via ffmpeg. Returns True on success."""
    codec = _CODEC_MAP.get(format_ext, format_ext)

    filters: list[str] = []
    if normalize:
        filters.append("loudnorm=I=-14:LRA=11:TP=-1.0")

    cmd: list[str] = ["ffmpeg", "-y", "-i", str(raw_file), "-c:a", codec]

    if format_ext == "opus":
        cmd += ["-b:a", f"{quality}k", "-vbr", "on", "-compression_level", "10"]
    elif format_ext in ("mp3", "m4a"):
        cmd += ["-b:a", f"{quality}k"]

    if filters:
        cmd += ["-af", ",".join(filters)]

    cmd += ["-vn", "-map_metadata", "0"]

    container = _CONTAINER_MAP.get(format_ext)
    if container:
        cmd += ["-f", container]

    cmd.append(str(out_path))

    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0


# ─────────────────────────────────────────────────────────────────────────────
# Public: single-track download + convert
# ─────────────────────────────────────────────────────────────────────────────

def download_track(
    line:          str,
    output_folder: Path,
    format_ext:    str,
    quality:       str            = "192",
    normalize:     bool           = False,
    cookie_cfg:    Dict[str, Any] = None,
) -> Optional[Path]:
    """
    Download one track from a 'song_name | url' line and convert it.
    Returns the final audio Path on success, None on failure.
    """
    if cookie_cfg is None:
        cookie_cfg = {}
    if "|" not in line:
        return None

    song_name, url = [p.strip() for p in line.split("|", 1)]
    safe_name      = sanitize_filename(song_name)
    final_path     = output_folder / f"{safe_name}.{format_ext}"

    if final_path.exists() and final_path.stat().st_size > 0:
        return final_path

    raw_file = _raw_download(url, song_name, output_folder, cookie_cfg)
    if raw_file is None:
        return None

    # If yt-dlp already produced the exact target format, just rename
    if raw_file.suffix.lower().lstrip(".") == format_ext and not normalize:
        raw_file.rename(final_path)
        return final_path

    ok = _convert_to_format(raw_file, final_path, format_ext, quality, normalize)
    if ok:
        try:
            if raw_file != final_path:
                raw_file.unlink()
        except Exception:
            pass
        return final_path
    else:
        console.print(f"  [bold red]✗ ffmpeg conversion failed for '{song_name}'[/bold red]")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# YouTube search
# ─────────────────────────────────────────────────────────────────────────────

def find_url(song_name: str) -> Dict[str, Any]:
    """Search YouTube for the best matching audio URL."""
    queries = [
        f"ytsearch1:{song_name} official audio",
        f"ytsearch1:{song_name} audio",
        f"ytsearch1:{song_name}",
    ]
    opts = {
        "extract_flat": True,
        "logger":       _QuietLogger(),
        "noprogress":   True,
        "noplaylist":   True,
        "age_limit":    99,
        "extractor_args": _SEARCH_EXTRACTOR_ARGS,
    }
    for query in queries:
        for attempt in range(2):
            try:
                time.sleep(random.uniform(0.3, 0.9))
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(query, download=False)
                if info and "entries" in info and info["entries"]:
                    video  = info["entries"][0]
                    vid_id = video.get("id")
                    if vid_id:
                        return {
                            "song":  song_name,
                            "url":   f"https://www.youtube.com/watch?v={vid_id}",
                            "found": True,
                        }
                    url = video.get("webpage_url") or video.get("url", "")
                    if url and str(url).startswith("http"):
                        return {"song": song_name, "url": url, "found": True}
                break
            except Exception as exc:
                if _is_rate_limit_error(str(exc)):
                    time.sleep(random.uniform(10, 20))
                    break
                if attempt == 1:
                    break

    return {"song": song_name, "error": "No results found", "found": False}


def search_youtube(
    input_file:      Path,
    output_found:    Path,
    output_notfound: Path,
    max_workers:     int = 3,
) -> None:
    """Search YouTube for every song in input_file using a thread pool."""
    if not input_file.exists():
        console.print(f"[bold red]❌ Error: '{input_file}' not found.[/bold red]")
        sys.exit(1)

    with open(input_file, "r", encoding="utf-8") as fh:
        songs = [line.strip() for line in fh if line.strip()]

    found_list:     List[str] = []
    not_found_list: List[str] = []
    console.print()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(
            f"[cyan]Searching YouTube for {len(songs)} song(s)…",
            total=len(songs),
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_song = {executor.submit(find_url, song): song for song in songs}
            for future in concurrent.futures.as_completed(future_to_song):
                res = future.result()
                progress.advance(task)
                if res["found"]:
                    found_list.append(f"{res['song']} | {res['url']}")
                    progress.console.print(f"[green]🟢 FOUND:[/green] {res['song']}")
                else:
                    not_found_list.append(res["song"])
                    progress.console.print(f"[red]🔴 NOT FOUND:[/red] {res['song']}")

    with open(output_found, "w", encoding="utf-8") as fh:
        fh.write("\n".join(found_list))
    with open(output_notfound, "w", encoding="utf-8") as fh:
        fh.write("\n".join(not_found_list))

    console.print(
        f"\n[bold green]✓ Search complete.[/bold green] "
        f"Found: [green]{len(found_list)}[/green]  "
        f"Not found: [red]{len(not_found_list)}[/red]"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Batch downloader
# ─────────────────────────────────────────────────────────────────────────────

def download_songs(
    input_file:    Path,
    output_folder: Path,
    format_ext:    str,
    quality:       str            = "192",
    max_workers:   int            = 2,
    normalize:     bool           = False,
    cookie_cfg:    Dict[str, Any] = None,
) -> List[Tuple[str, Path]]:
    """Download and convert all songs listed in input_file in parallel."""
    if cookie_cfg is None:
        cookie_cfg = {}
    if not input_file.exists():
        return []

    output_folder.mkdir(parents=True, exist_ok=True)

    with open(input_file, "r", encoding="utf-8") as fh:
        lines = [ln.strip() for ln in fh if ln.strip()]

    console.print()
    hints = []
    if normalize:              hints.append("loudnorm -14 LUFS")
    if format_ext == "opus":   hints.append("Opus VBR — transparent, ~40-50% smaller than FLAC")
    if cookie_cfg:             hints.append("browser cookies active")
    for h in hints:
        console.print(f"[dim italic]↳ {h}[/dim italic]")

    console.print(
        f"[dim]ℹ  Strategy: download best available stream → "
        f"ffmpeg → {format_ext.upper()}[/dim]\n"
    )

    downloaded_files: List[Tuple[str, Path]] = []

    with Progress(
        SpinnerColumn(spinner_name="dots2"),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(style="magenta"),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(
            f"[magenta]Downloading {len(lines)} song(s) → {format_ext.upper()}…",
            total=len(lines),
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_line = {
                executor.submit(
                    download_track,
                    line, output_folder, format_ext, quality, normalize, cookie_cfg,
                ): line
                for line in lines
            }
            for future in concurrent.futures.as_completed(future_to_line):
                line      = future_to_line[future]
                song_name = line.split("|")[0].strip()
                try:
                    filepath = future.result()
                except Exception as exc:
                    console.print(f"[red]❌ Error:[/red] {song_name} — {exc}")
                    filepath = None

                progress.advance(task)

                if filepath:
                    downloaded_files.append((song_name, filepath))
                    console.print(f"[green]✓[/green] {song_name}")
                else:
                    console.print(f"[red]❌ Failed:[/red] {song_name}")

    return downloaded_files