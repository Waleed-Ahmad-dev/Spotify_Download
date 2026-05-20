#!/usr/bin/env python3
"""
youtube.py
YouTube search + download engine.

Strategy (v5)
-------------
Download and conversion are two completely separate steps:

  Step 1 – yt-dlp downloads the BEST AVAILABLE audio stream in whatever
            container the video offers (webm/m4a/ogg/mp4 – anything).
            No codec is specified, so "Requested format is not available"
            can never occur.

  Step 2 – ffmpeg converts the raw download to the user's chosen format
            (opus / flac / mp3 / m4a) and, optionally, normalises loudness.

Key fix (v5): yt-dlp defaults ({}) now run FIRST because they have the
widest format support.  Specific player_client overrides are fallbacks only.
When a format error occurs the engine also cycles through a format-string
ladder before giving up, so almost any publicly-available video succeeds.
"""

import sys
import time
import random
import subprocess
import concurrent.futures
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional

import yt_dlp
from rich.progress import (
    Progress, SpinnerColumn, TextColumn, BarColumn,
    TaskProgressColumn, TimeRemainingColumn,
)

from utils import console, sanitize_filename

# ── Client strategies ─────────────────────────────────────────────────────────
# IMPORTANT: {} (yt-dlp defaults) is FIRST — it has the widest format support.
# Specific player_client overrides are kept as fallbacks, primarily for
# age-restricted videos.  Moving {} to the end was the original bug.
_DOWNLOAD_CLIENT_STRATEGIES: List[Dict[str, Any]] = [
    {},                                                                          # yt-dlp defaults (best coverage)
    {"player_client": ["ios"],                       "skip": ["translated_subs"]},
    {"player_client": ["android_music"],             "skip": ["translated_subs"]},
    {"player_client": ["android_music", "android"],  "skip": ["translated_subs"]},
    {"player_client": ["tv_embedded"],               "skip": ["translated_subs"]},
    {"player_client": ["mweb"],                      "skip": ["translated_subs"]},
    {"player_client": ["android", "tv_embedded", "web"], "skip": ["translated_subs"]},
]

_SEARCH_EXTRACTOR_ARGS: Dict[str, Any] = {
    "youtube": {
        "player_client": ["ios", "web"],
        "skip": ["translated_subs"],
    }
}

# Format-string ladder — tried in order when a format error occurs.
# Each string is more permissive than the previous one.
_FORMAT_STRINGS: List[str] = [
    "bestaudio/best",
    "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio[ext=ogg]/bestaudio/best",
    "ba[ext=webm]/ba[ext=m4a]/ba[ext=ogg]/ba/b",
    "bestvideo[acodec!=none]+bestaudio/bestvideo[acodec!=none]/best",
    "best",                                                                      # absolute last resort
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

_RATE_LIMIT_ERRORS = frozenset([
    "429",
    "too many requests",
    "http error 429",
])


def _is_age_gate_error(msg: str) -> bool:
    low = msg.lower()
    return any(k in low for k in _AGE_GATE_ERRORS)

def _is_format_error(msg: str) -> bool:
    low = msg.lower()
    return any(k in low for k in _FORMAT_ERRORS)

def _is_rate_limit_error(msg: str) -> bool:
    low = msg.lower()
    return any(k in low for k in _RATE_LIMIT_ERRORS)


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
    """
    Build yt-dlp options for a raw audio download.
    No postprocessor / codec is specified — we just want the audio bytes
    in whatever container yt-dlp can grab.
    """
    opts: Dict[str, Any] = {
        "format":      fmt,
        "outtmpl":     str(output_stem) + ".%(ext)s",
        "quiet":       True,
        "no_warnings": True,
        "noprogress":  True,
        "age_limit":   99,
        # Keep the raw file; ffmpeg converts it in step 2
        "postprocessors":      [],
        "postprocessor_hooks": [pp_hook],
    }
    if client_args:
        opts["extractor_args"] = {"youtube": client_args}
    opts.update(cookie_cfg)
    return opts


def _find_raw_download(output_folder: Path, safe_name: str) -> Optional[Path]:
    """
    Find whatever file yt-dlp actually wrote (any audio extension, any name
    that starts with our safe_name prefix).
    """
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
    Make one yt-dlp download attempt with given client args and format string.
    Returns (path_or_None, error_str_or_None).
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
        if found:
            return found, None
        return None, "download completed but file not found"

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
    Try every (client_strategy × format_string) combination until yt-dlp
    successfully downloads the raw audio stream.
    Returns the Path of the downloaded file, or None.

    The outer loop is client strategies; the inner loop is format strings.
    On a *format* error we immediately try the next format string (same
    client).  On an *age-gate* error we jump straight to the cookie+tv_embedded
    emergency path.  On a *rate-limit* error we sleep and move to the next
    client strategy.
    """
    safe_name   = sanitize_filename(song_name)
    output_stem = output_folder / safe_name

    # If already downloaded (raw or final), skip straight away
    existing = _find_raw_download(output_folder, safe_name)
    if existing:
        return existing

    strategies = list(_DOWNLOAD_CLIENT_STRATEGIES)
    if cookie_cfg:
        # Prepend an extra tv_embedded+cookies attempt for age-gated videos
        strategies = [
            {"player_client": ["tv_embedded"], "skip": ["translated_subs"]},
            *strategies,
        ]

    age_gate_emergency_used = False

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

            last_error = err or last_error

            if err is None:
                break  # succeeded at the path-finding step but still no file?

            if _is_format_error(err):
                # Try the next, more-permissive format string
                continue

            if _is_age_gate_error(err):
                break  # format cycling won't help; handle below

            if _is_rate_limit_error(err):
                break  # format cycling won't help; handle below

            # Any other error: no point trying more formats
            break

        # ── Post-inner-loop: classify and act on the last error ───────────────
        if last_error is None:
            continue  # no error recorded — file was found above

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
            else:
                if attempt_idx == 0:
                    console.print(
                        f"  [red]✗ Age-restricted:[/red] {song_name}\n"
                        "    [dim]To unlock: choose 'Use browser cookies' "
                        "in the wizard.[/dim]"
                    )
            continue

        if _is_format_error(last_error):
            # All format strings exhausted for this client — try next client
            continue

        if _is_rate_limit_error(last_error):
            wait = random.uniform(8, 15)
            console.print(f"  [yellow]⚠ Rate-limited[/yellow] — waiting {wait:.0f}s…")
            time.sleep(wait)
            continue

        # Unknown error: log on the last strategy only
        if attempt_idx == len(strategies) - 1:
            console.print(
                f"  [bold red]✗ Download failed for '{song_name}':[/bold red] {last_error}"
            )

    console.print(
        f"  [bold red]✗ All strategies exhausted for '{song_name}'.[/bold red] "
        "The video may be geo-blocked or unavailable in your region."
    )
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Step 2 helper – ffmpeg conversion
# ─────────────────────────────────────────────────────────────────────────────

# ffmpeg codec names for each output format
_CODEC_MAP = {
    "opus": "libopus",
    "mp3":  "libmp3lame",
    "flac": "flac",
    "m4a":  "aac",
}

# Container format passed to ffmpeg -f (for formats where ext ≠ container name)
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
    """
    Convert *raw_file* → *out_path* using ffmpeg.

    For Opus: VBR with the user-chosen bitrate target.
    For MP3 / M4A: CBR at the chosen bitrate.
    For FLAC: lossless, no bitrate needed.
    Normalisation (loudnorm) is applied as an audio filter when requested.
    Returns True on success.
    """
    codec = _CODEC_MAP.get(format_ext, format_ext)

    # Build audio filter chain
    filters: list[str] = []
    if normalize:
        filters.append("loudnorm=I=-14:LRA=11:TP=-1.0")

    cmd: list[str] = ["ffmpeg", "-y", "-i", str(raw_file)]

    # Audio codec
    cmd += ["-c:a", codec]

    # Bitrate / quality flags (not needed for lossless FLAC)
    if format_ext == "opus":
        cmd += ["-b:a", f"{quality}k", "-vbr", "on", "-compression_level", "10"]
    elif format_ext in ("mp3", "m4a"):
        cmd += ["-b:a", f"{quality}k"]
    # flac: no bitrate flag needed

    # Audio filter
    if filters:
        cmd += ["-af", ",".join(filters)]

    # Drop video / cover art streams (avoids container mismatch errors)
    cmd += ["-vn"]

    # Copy all metadata tags
    cmd += ["-map_metadata", "0"]

    # Output container (some formats need explicit -f)
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
    Download one track from a ``song_name | url`` line and convert it to
    the requested format.

    Returns the Path of the final audio file on success, None on failure.
    Exported so main.py can use it in the outer retry loop.
    """
    if cookie_cfg is None:
        cookie_cfg = {}
    if "|" not in line:
        return None

    song_name, url = [p.strip() for p in line.split("|", 1)]
    safe_name      = sanitize_filename(song_name)
    final_path     = output_folder / f"{safe_name}.{format_ext}"

    # Already fully converted — nothing to do
    if final_path.exists() and final_path.stat().st_size > 0:
        return final_path

    # ── Step 1: download raw audio ────────────────────────────────────────────
    raw_file = _raw_download(url, song_name, output_folder, cookie_cfg)
    if raw_file is None:
        return None

    # If yt-dlp already produced the exact format we want (e.g. it fetched an
    # .opus stream and we want opus), just rename it and skip ffmpeg.
    if raw_file.suffix.lower().lstrip(".") == format_ext and not normalize:
        raw_file.rename(final_path)
        return final_path

    # ── Step 2: ffmpeg conversion ─────────────────────────────────────────────
    ok = _convert_to_format(raw_file, final_path, format_ext, quality, normalize)

    if ok:
        # Remove the raw intermediate file now that conversion succeeded
        try:
            if raw_file != final_path:
                raw_file.unlink()
        except Exception:
            pass
        return final_path
    else:
        console.print(
            f"  [bold red]✗ ffmpeg conversion failed for '{song_name}'[/bold red]"
        )
        # Keep the raw file so the user still has something
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
        "quiet":        True,
        "no_warnings":  True,
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
                err = str(exc)
                if _is_rate_limit_error(err):
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
    """Search YouTube for every song in *input_file* using a thread pool."""
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
    """Download and convert all songs listed in *input_file* in parallel."""
    if cookie_cfg is None:
        cookie_cfg = {}
    if not input_file.exists():
        return []

    output_folder.mkdir(parents=True, exist_ok=True)

    with open(input_file, "r", encoding="utf-8") as fh:
        lines = [ln.strip() for ln in fh if ln.strip()]

    console.print()
    hints = []
    if normalize:    hints.append("loudnorm -14 LUFS")
    if format_ext == "opus": hints.append("Opus VBR — transparent, ~40-50% smaller than FLAC")
    if cookie_cfg:   hints.append("browser cookies active")
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