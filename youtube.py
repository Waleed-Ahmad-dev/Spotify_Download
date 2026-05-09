import sys
import time
import random
import concurrent.futures
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional

import yt_dlp
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn

from utils import console, sanitize_filename

# ── yt-dlp extractor args ───────────────────────────────────────────────────
# Two separate configs because the ios client avoids nsig/sdk warnings during
# metadata-only extraction (search) but does NOT expose the full set of audio
# stream formats, causing "Requested format is not available" on every video
# when used for actual downloads.  The web client has no such restriction.

# Used in find_url (search only – no download, ios is fine here)
_SEARCH_EXTRACTOR_ARGS: Dict[str, Any] = {
    "youtube": {
        "player_client": ["ios", "web"],
        "skip": ["translated_subs"],
    }
}

# Used in download_track – web client exposes all audio formats
_DOWNLOAD_EXTRACTOR_ARGS: Dict[str, Any] = {
    "youtube": {
        "player_client": ["web"],
        "skip": ["translated_subs"],
    }
}


def find_url(song_name: str) -> Dict[str, Any]:
    """Search YouTube for the best matching audio URL.

    FIX #4: tries three progressively broader queries so obscure or
    non-English tracks that return no results for "official audio" still
    get found.

    FIX #7: retry delay is randomised AND we skip to the next query
    variant instead of hammering the same one on 429.
    """
    # Multiple query strategies – most specific first
    search_queries = [
        f"ytsearch1:{song_name} official audio",
        f"ytsearch1:{song_name} audio",
        f"ytsearch1:{song_name}",
    ]

    ydl_opts = {
        "extract_flat": True,
        "quiet": True,
        "no_warnings": True,   # FIX #3: suppress noisy sdk/nsig warnings
        "noplaylist": True,
        "extractor_args": _SEARCH_EXTRACTOR_ARGS,
    }

    for query in search_queries:
        for attempt in range(2):
            try:
                time.sleep(random.uniform(0.3, 0.9))
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(query, download=False)

                if info and "entries" in info and info["entries"]:
                    video = info["entries"][0]
                    video_id = video.get("id")
                    if video_id:
                        url = f"https://www.youtube.com/watch?v={video_id}"
                        return {"song": song_name, "url": url, "found": True}

                    # Fallback: use a pre-built URL from the entry if present
                    url = video.get("webpage_url") or video.get("url", "")
                    if url and str(url).startswith("http"):
                        return {"song": song_name, "url": url, "found": True}

                # No entries returned – try next query immediately
                break

            except Exception as exc:
                err_str = str(exc)
                if "429" in err_str or "Too Many Requests" in err_str:
                    # Back off and try the next query variant
                    time.sleep(random.uniform(10, 20))
                    break   # Move to next query instead of retrying same one
                if attempt == 1:
                    break   # Two strikes on this query; move on

    return {"song": song_name, "error": "No results found", "found": False}


def _locate_output_file(output_folder: Path, safe_name: str, format_ext: str) -> Optional[Path]:
    """Return the actual file on disk after yt-dlp runs.

    FIX #1 / #5: yt-dlp applies its own filename sanitization on top of
    whatever outtmpl we provide, so the path we *computed* in Python may
    not match what yt-dlp actually wrote.  We use three progressively
    wider searches so we always find the file regardless of how yt-dlp
    renamed it.
    """
    # 1. Exact expected path
    exact = output_folder / f"{safe_name}.{format_ext}"
    if exact.exists() and exact.stat().st_size > 0:
        return exact

    # 2. Any file whose stem starts with the safe_name (handles yt-dlp
    #    appending " (NA)" or truncating long names)
    audio_exts = {f".{format_ext}", ".mp3", ".flac", ".m4a", ".opus", ".webm", ".ogg"}
    for candidate in output_folder.iterdir():
        if candidate.suffix.lower() in audio_exts and candidate.stat().st_size > 0:
            if candidate.stem.startswith(safe_name[:40]):   # first 40 chars match
                return candidate

    # 3. Last-resort: pick the newest audio file in the folder (only safe
    #    when we're single-threaded for this folder, but better than None)
    candidates = sorted(
        (f for f in output_folder.iterdir()
         if f.suffix.lower() in audio_exts and f.stat().st_size > 0),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def download_track(
    line: str,
    output_folder: Path,
    format_ext: str,
    quality: str = "192",
    normalize: bool = False,
) -> Optional[Path]:
    """Download a single track from YouTube as MP3/FLAC/M4A.

    FIX #1: uses a postprocessor_hook to capture the real output path
            instead of guessing it from sanitize_filename.
    FIX #2: postprocessor_args now uses the dict form yt-dlp expects.
    FIX #3: extractor_args + no_warnings suppress sdk/nsig warnings.
    FIX #5: falls back to _locate_output_file when the hook fires early
            or the filepath key is absent.
    """
    if "|" not in line:
        return None

    song_name, url = [p.strip() for p in line.split("|", 1)]
    safe_name = sanitize_filename(song_name)
    output_stem = output_folder / safe_name   # no extension yet

    # ── Already downloaded? ──────────────────────────────────────────────
    # FIX #1: check via _locate_output_file, not a hard-coded path guess.
    existing = _locate_output_file(output_folder, safe_name, format_ext)
    if existing:
        return existing

    # ── Capture the real output path via a hook ──────────────────────────
    # FIX #5: postprocessor_hooks give us the filename AFTER FFmpeg has
    # finished renaming/converting the file.
    final_filepath: List[Optional[Path]] = [None]

    def _pp_hook(d: Dict[str, Any]) -> None:
        if d.get("status") == "finished":
            fp = d.get("filepath") or (d.get("info_dict") or {}).get("filepath")
            if fp:
                final_filepath[0] = Path(fp)

    # ── Build yt-dlp options ─────────────────────────────────────────────
    # Explicit fallback chain: prefer opus/webm (highest quality lossless
    # transcode to flac/m4a), then anything audio, then full video+audio.
    # "bestaudio/best" alone fails when the ios player_client is used because
    # that client only serves a restricted subset of streams.
    # We use the web client here (_DOWNLOAD_EXTRACTOR_ARGS) to get the full
    # format list.
    ydl_opts: Dict[str, Any] = {
        "format": (
            "bestaudio[ext=webm]/bestaudio[ext=m4a]"
            "/bestaudio[ext=opus]/bestaudio/best"
        ),
        "outtmpl": str(output_stem) + ".%(ext)s",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "extractor_args": _DOWNLOAD_EXTRACTOR_ARGS,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": format_ext,
                "preferredquality": quality,
            }
        ],
        "postprocessor_hooks": [_pp_hook],   # FIX #5
    }

    # FIX #2: postprocessor_args must be a dict mapping pp-name → arg list,
    # not a bare list.  The key must be lowercase and match yt-dlp's
    # internal name for the postprocessor.
    if normalize:
        ydl_opts["postprocessor_args"] = {
            "ffmpegextractaudio": ["-af", "loudnorm=I=-14:LRA=11:TP=-1.0"]
        }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # Priority 1: path captured by the hook
        if final_filepath[0] and final_filepath[0].exists() and final_filepath[0].stat().st_size > 0:
            return final_filepath[0]

        # Priority 2: scan the folder for what yt-dlp actually wrote
        # FIX #1: this handles yt-dlp's own filename mangling
        return _locate_output_file(output_folder, safe_name, format_ext)

    except Exception as exc:
        console.print(f"[bold red]  ✗ yt-dlp error for '{song_name}':[/bold red] {exc}")
        return None


def search_youtube(
    input_file: Path,
    output_found: Path,
    output_notfound: Path,
    max_workers: int = 3,
) -> None:
    """Search YouTube for every song in *input_file* using a thread pool."""
    if not input_file.exists():
        console.print(f"[bold red]❌ Error: '{input_file}' not found.[/bold red]")
        sys.exit(1)

    with open(input_file, "r", encoding="utf-8") as fh:
        songs = [line.strip() for line in fh if line.strip()]

    found_list: List[str] = []
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
            f"[cyan]Searching YouTube for {len(songs)} songs...", total=len(songs)
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


def download_songs(
    input_file: Path,
    output_folder: Path,
    format_ext: str,
    quality: str = "192",
    max_workers: int = 2,
    normalize: bool = False,
) -> List[Tuple[str, Path]]:
    """Download all found songs using a thread pool."""
    if not input_file.exists():
        return []
    output_folder.mkdir(parents=True, exist_ok=True)

    with open(input_file, "r", encoding="utf-8") as fh:
        lines = [ln.strip() for ln in fh if ln.strip()]

    console.print()
    if normalize:
        console.print("[dim italic]↳ Audio volume normalization enabled (-14 LUFS)[/dim italic]")

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
            f"[magenta]Downloading {len(lines)} songs ({format_ext.upper()})...",
            total=len(lines),
        )

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_line = {
                executor.submit(download_track, line, output_folder, format_ext, quality, normalize): line
                for line in lines
            }
            for future in concurrent.futures.as_completed(future_to_line):
                line = future_to_line[future]
                song_name = line.split("|")[0].strip()
                filepath = future.result()
                progress.advance(task)

                if filepath:
                    downloaded_files.append((song_name, filepath))
                    progress.console.print(f"[green]✓ Downloaded:[/green] {song_name}")
                else:
                    progress.console.print(f"[red]❌ Failed:[/red] {song_name}")

    return downloaded_files