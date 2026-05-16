#!/usr/bin/env python3
"""
youtube.py
YouTube search + download engine.

Changes from v1
---------------
• download_track() is now a module-level export (main.py uses it for the
  outer retry loop on failed downloads).
• _DOWNLOAD_CLIENT_STRATEGIES: 'mweb' added as an extra strategy between
  tv_embedded and the bare default — catches some edge-case streams.
• Opus format: when format_ext == 'opus', the FFmpegExtractAudio post-
  processor uses the 'opus' codec with quality set to '0' (best VBR).
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
# Tried in order until one succeeds.  Empty dict = yt-dlp built-in defaults.
_DOWNLOAD_CLIENT_STRATEGIES: List[Dict[str, Any]] = [
    {"player_client": ["android_music"],                         "skip": ["translated_subs"]},
    {"player_client": ["android_music", "android"],              "skip": ["translated_subs"]},
    {"player_client": ["tv_embedded"],                           "skip": ["translated_subs"]},
    {"player_client": ["mweb"],                                  "skip": ["translated_subs"]},
    {"player_client": ["android", "tv_embedded", "web"],         "skip": ["translated_subs"]},
    {},   # yt-dlp defaults – absolute last resort
]

_SEARCH_EXTRACTOR_ARGS: Dict[str, Any] = {
    "youtube": {
        "player_client": ["ios", "web"],
        "skip": ["translated_subs"],
    }
}

_FORMAT = "bestaudio/best"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_ydl_opts(
    output_stem:  Path,
    format_ext:   str,
    quality:      str,
    normalize:    bool,
    client_args:  Dict[str, Any],
    pp_hook,
) -> Dict[str, Any]:
    """Assemble yt-dlp options for one download attempt."""
    # Opus needs codec 'opus'; quality '0' means best VBR (libopus default)
    codec   = "opus"   if format_ext == "opus" else format_ext
    q_value = "0"      if format_ext == "opus" else quality

    opts: Dict[str, Any] = {
        "format":       _FORMAT,
        "outtmpl":      str(output_stem) + ".%(ext)s",
        "quiet":        True,
        "no_warnings":  True,
        "noprogress":   True,
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
    return opts


def _locate_output_file(
    output_folder: Path,
    safe_name:     str,
    format_ext:    str,
) -> Optional[Path]:
    """
    Find the file yt-dlp actually wrote, accounting for name mangling.
    Two passes only – no 'newest file' fallback (unsafe under concurrency).
    """
    audio_exts = {f".{format_ext}", ".mp3", ".flac", ".m4a", ".opus", ".webm", ".ogg"}

    # Pass 1 – exact expected path
    exact = output_folder / f"{safe_name}.{format_ext}"
    if exact.exists() and exact.stat().st_size > 0:
        return exact

    # Pass 2 – prefix match (handles yt-dlp truncation / ' (NA)' appends)
    prefix = safe_name[:40].lower()
    for f in output_folder.iterdir():
        if (
            f.suffix.lower() in audio_exts
            and f.stat().st_size > 0
            and f.stem.lower().startswith(prefix)
        ):
            return f

    return None


# ── Single-track downloader ───────────────────────────────────────────────────

def download_track(
    line:          str,
    output_folder: Path,
    format_ext:    str,
    quality:       str  = "192",
    normalize:     bool = False,
) -> Optional[Path]:
    """
    Download one track from the ``song_name | url`` line,
    escalating through client strategies until one works.

    Returns the Path of the audio file on success, None if all fail.
    This function is exported so main.py can use it in its outer retry loop.
    """
    if "|" not in line:
        return None

    song_name, url = [p.strip() for p in line.split("|", 1)]
    safe_name      = sanitize_filename(song_name)
    output_stem    = output_folder / safe_name

    # Skip if already downloaded
    existing = _locate_output_file(output_folder, safe_name, format_ext)
    if existing:
        return existing

    for attempt_idx, client_args in enumerate(_DOWNLOAD_CLIENT_STRATEGIES):
        final_filepath: List[Optional[Path]] = [None]

        def _pp_hook(d: Dict[str, Any]) -> None:
            if d.get("status") == "finished":
                fp = d.get("filepath") or (d.get("info_dict") or {}).get("filepath")
                if fp:
                    final_filepath[0] = Path(fp)

        opts = _build_ydl_opts(
            output_stem, format_ext, quality, normalize, client_args, _pp_hook
        )

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])

            if (
                final_filepath[0]
                and final_filepath[0].exists()
                and final_filepath[0].stat().st_size > 0
            ):
                return final_filepath[0]

            found = _locate_output_file(output_folder, safe_name, format_ext)
            if found:
                return found

        except Exception as exc:
            err = str(exc)

            if "format is not available" in err or "No video formats found" in err:
                continue

            if "429" in err or "Too Many Requests" in err:
                time.sleep(random.uniform(8, 15))
                continue

            if attempt_idx == len(_DOWNLOAD_CLIENT_STRATEGIES) - 1:
                console.print(
                    f"[bold red]  ✗ yt-dlp error for '{song_name}':[/bold red] {exc}"
                )

    return None


# ── YouTube search ─────────────────────────────────────────────────────────────

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

                break  # no entries – try next query

            except Exception as exc:
                err = str(exc)
                if "429" in err or "Too Many Requests" in err:
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
            f"[cyan]Searching YouTube for {len(songs)} songs…", total=len(songs)
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
                    progress.console.print(f"[red]🔴 FAILED:[/red] {res['song']}")

    with open(output_found, "w", encoding="utf-8") as fh:
        fh.write("\n".join(found_list))
    with open(output_notfound, "w", encoding="utf-8") as fh:
        fh.write("\n".join(not_found_list))

    console.print(
        f"\n[bold green]✓ Search complete.[/bold green] "
        f"Found: [green]{len(found_list)}[/green]  "
        f"Missing: [red]{len(not_found_list)}[/red]"
    )


# ── Batch downloader ──────────────────────────────────────────────────────────

def download_songs(
    input_file:    Path,
    output_folder: Path,
    format_ext:    str,
    quality:       str  = "192",
    max_workers:   int  = 2,
    normalize:     bool = False,
) -> List[Tuple[str, Path]]:
    """Download all songs listed in *input_file* in parallel."""
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
            f"[magenta]Downloading {len(lines)} songs ({format_ext.upper()})…",
            total=len(lines),
        )

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_line = {
                executor.submit(
                    download_track, line, output_folder, format_ext, quality, normalize
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