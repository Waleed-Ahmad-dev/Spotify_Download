import sys
import time
import random
import concurrent.futures
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional

import yt_dlp
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn

from utils import console, sanitize_filename

# ── Client strategies ────────────────────────────────────────────────────────
#
# YouTube's web client now requires a Proof-of-Origin (PO) token for bestaudio
# streams. Without cookies that token is absent and every download fails with
# "Requested format is not available".
#
# The clients below do NOT require a PO token and are tried in order:
#
#   android_music  – YouTube Music app client. Best audio format coverage,
#                    no PO token, no nsig decoding issues.  ← primary
#   android        – Regular Android client. Broad format support, no PO token.
#   tv_embedded    – YouTube TV embed. Works for most videos without auth.
#   (empty)        – yt-dlp built-in defaults. Absolute last resort.
#
# For search (metadata-only, no stream download) we keep ios+web because the
# ios client suppresses nsig/sdk warnings and is perfectly fine for flat
# extraction.
# ────────────────────────────────────────────────────────────────────────────

_SEARCH_EXTRACTOR_ARGS: Dict[str, Any] = {
    "youtube": {
        "player_client": ["ios", "web"],
        "skip": ["translated_subs"],
    }
}

# Tried in order until one succeeds. Each dict is passed as
# extractor_args["youtube"]. Empty dict = let yt-dlp choose.
_DOWNLOAD_CLIENT_STRATEGIES: List[Dict[str, Any]] = [
    {"player_client": ["android_music"],                          "skip": ["translated_subs"]},
    {"player_client": ["android_music", "android"],               "skip": ["translated_subs"]},
    {"player_client": ["tv_embedded"],                            "skip": ["translated_subs"]},
    {"player_client": ["android", "tv_embedded", "web"],          "skip": ["translated_subs"]},
    {},  # yt-dlp defaults – absolute last resort
]

# Simple format selector – no container pinning so every client can match.
# FFmpegExtractAudio converts whatever we get to the requested codec.
_FORMAT = "bestaudio/best"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _build_ydl_opts(
    output_stem: Path,
    format_ext: str,
    quality: str,
    normalize: bool,
    client_args: Dict[str, Any],
    pp_hook,
) -> Dict[str, Any]:
    """Assemble a yt-dlp options dict for one download attempt."""
    opts: Dict[str, Any] = {
        "format": _FORMAT,
        "outtmpl": str(output_stem) + ".%(ext)s",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": format_ext,
                "preferredquality": quality,
            }
        ],
        "postprocessor_hooks": [pp_hook],
    }
    if client_args:
        opts["extractor_args"] = {"youtube": client_args}
    if normalize:
        # Dict form required by yt-dlp; bare list is silently ignored.
        opts["postprocessor_args"] = {
            "ffmpegextractaudio": ["-af", "loudnorm=I=-14:LRA=11:TP=-1.0"]
        }
    return opts


def _locate_output_file(
    output_folder: Path, safe_name: str, format_ext: str
) -> Optional[Path]:
    """Find the file yt-dlp actually wrote, accounting for its own name mangling.

    Two passes only – the "pick newest file" fallback is intentionally omitted
    because concurrent downloads share the same folder and it would return the
    wrong file under load.
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


# ── Single-track downloader with strategy retry ───────────────────────────────

def download_track(
    line: str,
    output_folder: Path,
    format_ext: str,
    quality: str = "192",
    normalize: bool = False,
) -> Optional[Path]:
    """Download one track, escalating through client strategies until one works.

    Returns the Path of the audio file on success, None if all strategies fail.
    """
    if "|" not in line:
        return None

    song_name, url = [p.strip() for p in line.split("|", 1)]
    safe_name = sanitize_filename(song_name)
    output_stem = output_folder / safe_name

    # Skip if already downloaded
    existing = _locate_output_file(output_folder, safe_name, format_ext)
    if existing:
        return existing

    last_error = ""

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

            # Prefer the path captured by the postprocessor hook
            if (
                final_filepath[0]
                and final_filepath[0].exists()
                and final_filepath[0].stat().st_size > 0
            ):
                return final_filepath[0]

            # Fall back to folder scan
            found = _locate_output_file(output_folder, safe_name, format_ext)
            if found:
                return found

            # yt-dlp reported success but we can't find the file – try next strategy
            last_error = "file not found after download"

        except Exception as exc:
            last_error = str(exc)

            if "format is not available" in last_error or "No video formats found" in last_error:
                # This client doesn't expose the needed formats – try next one
                continue

            if "429" in last_error or "Too Many Requests" in last_error:
                # Temporary rate-limit – back off then try next strategy
                time.sleep(random.uniform(8, 15))
                continue

            # Other errors: log only on the final attempt to avoid spam
            if attempt_idx == len(_DOWNLOAD_CLIENT_STRATEGIES) - 1:
                console.print(
                    f"[bold red]  ✗ yt-dlp error for '{song_name}':[/bold red] {exc}"
                )

    return None


# ── YouTube search ─────────────────────────────────────────────────────────────

def find_url(song_name: str) -> Dict[str, Any]:
    """Search YouTube for the best matching audio URL.

    Tries three progressively broader query strings; backs off on 429s.
    """
    queries = [
        f"ytsearch1:{song_name} official audio",
        f"ytsearch1:{song_name} audio",
        f"ytsearch1:{song_name}",
    ]

    opts = {
        "extract_flat": True,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extractor_args": _SEARCH_EXTRACTOR_ARGS,
    }

    for query in queries:
        for attempt in range(2):
            try:
                time.sleep(random.uniform(0.3, 0.9))
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(query, download=False)

                if info and "entries" in info and info["entries"]:
                    video = info["entries"][0]
                    vid_id = video.get("id")
                    if vid_id:
                        return {
                            "song": song_name,
                            "url": f"https://www.youtube.com/watch?v={vid_id}",
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
    input_file: Path,
    output_found: Path,
    output_notfound: Path,
    max_workers: int = 3,
) -> None:
    """Search YouTube for every song in input_file using a thread pool."""
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


# ── Batch downloader ──────────────────────────────────────────────────────────

def download_songs(
    input_file: Path,
    output_folder: Path,
    format_ext: str,
    quality: str = "192",
    max_workers: int = 2,
    normalize: bool = False,
) -> List[Tuple[str, Path]]:
    """Download all songs listed in input_file in parallel."""
    if not input_file.exists():
        return []
    output_folder.mkdir(parents=True, exist_ok=True)

    with open(input_file, "r", encoding="utf-8") as fh:
        lines = [ln.strip() for ln in fh if ln.strip()]

    console.print()
    if normalize:
        console.print("[dim italic]↳ Audio normalization enabled (-14 LUFS)[/dim italic]")

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
                executor.submit(
                    download_track, line, output_folder, format_ext, quality, normalize
                ): line
                for line in lines
            }
            for future in concurrent.futures.as_completed(future_to_line):
                line = future_to_line[future]
                song_name = line.split("|")[0].strip()
                try:
                    filepath = future.result()
                except Exception as exc:
                    progress.console.print(f"[red]❌ Error:[/red] {song_name} — {exc}")
                    filepath = None

                progress.advance(task)

                if filepath:
                    downloaded_files.append((song_name, filepath))
                    progress.console.print(f"[green]✓ Downloaded:[/green] {song_name}")
                else:
                    progress.console.print(f"[red]❌ Failed:[/red] {song_name}")

    return downloaded_files