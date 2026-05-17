#!/usr/bin/env python3
"""
youtube.py
YouTube search + download engine.

Changes from v2
---------------
• cookie_cfg dict passed through every download function so age-restricted
  videos can be unlocked via browser cookies or a cookies.txt file.
• Age-gate handling: tv_embedded is tried first among strategies because it
  bypasses YouTube's age-verification wall without requiring login.
• New _AGE_GATE_ERRORS set: when yt-dlp raises an age-gate / sign-in error,
  we skip straight to the tv_embedded strategy (or the cookies strategy if
  cookie_cfg was provided) rather than cycling through all clients in order.
• 'age_limit': 99 is added to every yt-dlp options dict so yt-dlp itself
  never self-blocks on content-rating metadata.
• download_track() and download_songs() both accept cookie_cfg: dict.
"""

import sys
import time
import random
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
# tv_embedded is listed FIRST because it is the most effective client for
# bypassing YouTube's age-verification gate without requiring a login session.
# The remaining clients are tried in escalating order of "last resort".
_DOWNLOAD_CLIENT_STRATEGIES: List[Dict[str, Any]] = [
    {"player_client": ["tv_embedded"],                              "skip": ["translated_subs"]},
    {"player_client": ["android_music"],                            "skip": ["translated_subs"]},
    {"player_client": ["android_music", "android"],                 "skip": ["translated_subs"]},
    {"player_client": ["mweb"],                                     "skip": ["translated_subs"]},
    {"player_client": ["android", "tv_embedded", "web"],            "skip": ["translated_subs"]},
    {},   # yt-dlp built-in defaults — absolute last resort
]

_SEARCH_EXTRACTOR_ARGS: Dict[str, Any] = {
    "youtube": {
        "player_client": ["ios", "web"],
        "skip": ["translated_subs"],
    }
}

_FORMAT = "bestaudio/best"

# Error substrings that indicate an age-gate / sign-in block.
# When any of these appear we skip the failing client immediately and, if
# cookie_cfg was supplied, prepend a "cookies + tv_embedded" emergency strategy.
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

# Other transient errors that warrant a retry / client switch
_FORMAT_ERRORS = frozenset([
    "format is not available",
    "no video formats found",
    "requested format is not available",
])

_RATE_LIMIT_ERRORS = frozenset([
    "429",
    "too many requests",
    "http error 429",
])


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_age_gate_error(msg: str) -> bool:
    low = msg.lower()
    return any(k in low for k in _AGE_GATE_ERRORS)


def _is_format_error(msg: str) -> bool:
    low = msg.lower()
    return any(k in low for k in _FORMAT_ERRORS)


def _is_rate_limit_error(msg: str) -> bool:
    low = msg.lower()
    return any(k in low for k in _RATE_LIMIT_ERRORS)


def _build_ydl_opts(
    output_stem:  Path,
    format_ext:   str,
    quality:      str,
    normalize:    bool,
    client_args:  Dict[str, Any],
    pp_hook,
    cookie_cfg:   Dict[str, Any],
) -> Dict[str, Any]:
    """
    Assemble a complete yt-dlp options dict for one download attempt.

    cookie_cfg entries are merged at the top level (yt-dlp expects them there):
      {"cookiesfrombrowser": ("chrome",)}   – or –
      {"cookiefile": "/path/to/cookies.txt"}
    """
    # Opus: codec="opus", quality="0" (best VBR); everything else is standard
    codec   = "opus" if format_ext == "opus" else format_ext
    q_value = "0"    if format_ext == "opus" else quality

    opts: Dict[str, Any] = {
        "format":      _FORMAT,
        "outtmpl":     str(output_stem) + ".%(ext)s",
        "quiet":       True,
        "no_warnings": True,
        "noprogress":  True,
        # Tell yt-dlp never to self-block based on content-rating metadata
        "age_limit":   99,
        "postprocessors": [
            {
                "key":              "FFmpegExtractAudio",
                "preferredcodec":   codec,
                "preferredquality": q_value,
            }
        ],
        "postprocessor_hooks": [pp_hook],
    }

    if client_args:
        opts["extractor_args"] = {"youtube": client_args}

    if normalize:
        opts["postprocessor_args"] = {
            "ffmpegextractaudio": ["-af", "loudnorm=I=-14:LRA=11:TP=-1.0"]
        }

    # Merge cookie settings (cookiesfrombrowser / cookiefile / etc.)
    opts.update(cookie_cfg)

    return opts


def _locate_output_file(
    output_folder: Path,
    safe_name:     str,
    format_ext:    str,
) -> Optional[Path]:
    """
    Find the file yt-dlp actually wrote, handling any name mangling it applies.
    Two passes only — no "newest file" fallback (unsafe under concurrency).
    """
    audio_exts = {
        f".{format_ext}", ".mp3", ".flac", ".m4a",
        ".opus", ".webm", ".ogg", ".m4a",
    }

    # Pass 1 — exact expected path
    exact = output_folder / f"{safe_name}.{format_ext}"
    if exact.exists() and exact.stat().st_size > 0:
        return exact

    # Pass 2 — prefix match (handles yt-dlp truncation / ' (NA)' appends)
    prefix = safe_name[:40].lower()
    for f in output_folder.iterdir():
        if (
            f.suffix.lower() in audio_exts
            and f.stat().st_size > 0
            and f.stem.lower().startswith(prefix)
        ):
            return f

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Single-track downloader  (exported — also used by main.py retry loop)
# ─────────────────────────────────────────────────────────────────────────────

def download_track(
    line:          str,
    output_folder: Path,
    format_ext:    str,
    quality:       str       = "192",
    normalize:     bool      = False,
    cookie_cfg:    Dict[str, Any] = None,
) -> Optional[Path]:
    """
    Download one track from a ``song_name | url`` line, escalating through
    client strategies until one works or all are exhausted.

    Age-gate logic
    --------------
    If a strategy triggers an age-gate error and *cookie_cfg* was provided,
    an emergency "cookies + tv_embedded" attempt is inserted before continuing
    the normal escalation sequence.  If no cookies are provided, the code
    simply moves to the next strategy (tv_embedded is already first in the
    list, so it is tried very early).

    Returns the Path of the audio file on success, None if all fail.
    """
    if cookie_cfg is None:
        cookie_cfg = {}

    if "|" not in line:
        return None

    song_name, url = [p.strip() for p in line.split("|", 1)]
    safe_name      = sanitize_filename(song_name)
    output_stem    = output_folder / safe_name

    # Skip if already downloaded in a previous run
    existing = _locate_output_file(output_folder, safe_name, format_ext)
    if existing:
        return existing

    # Build the list of strategies to try.
    # If the caller supplied cookies, prepend an explicit cookies+tv_embedded
    # strategy so age-gated tracks get the best possible first attempt.
    strategies = list(_DOWNLOAD_CLIENT_STRATEGIES)
    if cookie_cfg:
        strategies = [
            {"player_client": ["tv_embedded"], "skip": ["translated_subs"]},
            *strategies,
        ]

    age_gate_emergency_used = False   # track whether we already injected cookies

    for attempt_idx, client_args in enumerate(strategies):
        final_filepath: List[Optional[Path]] = [None]

        def _pp_hook(d: Dict[str, Any]) -> None:
            if d.get("status") == "finished":
                fp = d.get("filepath") or (d.get("info_dict") or {}).get("filepath")
                if fp:
                    final_filepath[0] = Path(fp)

        opts = _build_ydl_opts(
            output_stem, format_ext, quality, normalize,
            client_args, _pp_hook, cookie_cfg,
        )

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])

            # Prefer path captured by the post-processor hook
            if (
                final_filepath[0]
                and final_filepath[0].exists()
                and final_filepath[0].stat().st_size > 0
            ):
                return final_filepath[0]

            # Fallback: scan output folder
            found = _locate_output_file(output_folder, safe_name, format_ext)
            if found:
                return found

            # yt-dlp reported success but file is missing — try next strategy

        except yt_dlp.utils.DownloadError as exc:
            err = str(exc)

            # ── Age-gate ──────────────────────────────────────────────────────
            if _is_age_gate_error(err):
                if cookie_cfg and not age_gate_emergency_used:
                    # Emergency: inject cookies + tv_embedded right now
                    console.print(
                        f"  [yellow]⚠ Age-restricted:[/yellow] {song_name} — "
                        "retrying with browser cookies + tv_embedded…"
                    )
                    age_gate_emergency_used = True
                    emergency_args = {
                        "player_client": ["tv_embedded"],
                        "skip": ["translated_subs"],
                    }
                    emergency_opts = _build_ydl_opts(
                        output_stem, format_ext, quality, normalize,
                        emergency_args, _pp_hook, cookie_cfg,
                    )
                    try:
                        with yt_dlp.YoutubeDL(emergency_opts) as ydl:
                            ydl.download([url])
                        fp = (
                            final_filepath[0]
                            if (final_filepath[0]
                                and final_filepath[0].exists()
                                and final_filepath[0].stat().st_size > 0)
                            else _locate_output_file(output_folder, safe_name, format_ext)
                        )
                        if fp:
                            return fp
                    except Exception:
                        pass  # fall through to next strategy
                else:
                    # No cookies provided — inform user and skip this video
                    if attempt_idx == 0:   # only warn once
                        console.print(
                            f"  [red]✗ Age-restricted:[/red] {song_name}\n"
                            "    [dim]To unlock: add "
                            "--cookies-browser chrome  (or firefox / edge)[/dim]"
                        )
                continue   # move to next strategy

            # ── Format not available for this client ──────────────────────────
            if _is_format_error(err):
                continue

            # ── Rate-limited ──────────────────────────────────────────────────
            if _is_rate_limit_error(err):
                wait = random.uniform(8, 15)
                console.print(
                    f"  [yellow]⚠ Rate-limited[/yellow] — waiting {wait:.0f}s…"
                )
                time.sleep(wait)
                continue

            # ── Any other error — only log on final attempt ───────────────────
            if attempt_idx == len(strategies) - 1:
                console.print(
                    f"[bold red]  ✗ Download failed for '{song_name}':[/bold red] {exc}"
                )

        except Exception as exc:
            # Unexpected non-yt-dlp error
            if attempt_idx == len(strategies) - 1:
                console.print(
                    f"[bold red]  ✗ Unexpected error for '{song_name}':[/bold red] {exc}"
                )

    return None


# ─────────────────────────────────────────────────────────────────────────────
# YouTube search
# ─────────────────────────────────────────────────────────────────────────────

def find_url(song_name: str) -> Dict[str, Any]:
    """
    Search YouTube for the best matching audio URL.
    Tries three progressively broader query strings; backs off on 429s.
    """
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

                break   # no entries — try next query

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
    """Download all songs listed in *input_file* in parallel."""
    if cookie_cfg is None:
        cookie_cfg = {}

    if not input_file.exists():
        return []
    output_folder.mkdir(parents=True, exist_ok=True)

    with open(input_file, "r", encoding="utf-8") as fh:
        lines = [ln.strip() for ln in fh if ln.strip()]

    console.print()
    if normalize:
        console.print("[dim italic]↳ Audio normalization enabled (-14 LUFS)[/dim italic]")
    if format_ext == "opus":
        console.print(
            "[dim italic]↳ Opus format: VBR best quality "
            "(transparent, ~40-50% smaller than FLAC)[/dim italic]"
        )
    if cookie_cfg:
        console.print(
            "[dim italic]↳ Browser cookies active — age-restricted videos will be attempted[/dim italic]"
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
            f"[magenta]Downloading {len(lines)} song(s) ({format_ext.upper()})…",
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
                    console.print(f"[green]✓ Downloaded:[/green] {song_name}")
                else:
                    console.print(f"[red]❌ Failed:[/red] {song_name}")

    return downloaded_files