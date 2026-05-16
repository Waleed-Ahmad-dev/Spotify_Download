#!/usr/bin/env python3
"""
utils.py
Shared utilities: console, filename sanitization, ffmpeg check,
M3U playlist generation, and audio deduplication.

Updated: added .opus support throughout remove_duplicates().
"""

import os
import sys
import re
import subprocess
from pathlib import Path
from typing import Dict

from mutagen.mp3  import MP3
from mutagen.id3  import ID3
from mutagen.flac import FLAC
from mutagen.mp4  import MP4
from rich.console import Console
from rich.panel   import Panel

# Shared Rich console for the entire application
console = Console()

IS_LINUX = sys.platform.startswith("linux")


def sanitize_filename(name: str, max_length: int = 150) -> str:
    """Remove characters that are illegal in filenames and normalise whitespace."""
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = re.sub(r"\s+", " ", name)
    return name[:max_length].strip(". ")


def check_ffmpeg() -> bool:
    """Return True if ffmpeg is available on PATH."""
    try:
        subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, check=True, timeout=5
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        console.print(
            "[bold red]❌ FFmpeg is required but not found.[/bold red]\n"
            "Install it:\n"
            "   [cyan]Linux:[/cyan]   sudo apt install ffmpeg\n"
            "   [cyan]macOS:[/cyan]   brew install ffmpeg\n"
            "   [cyan]Windows:[/cyan] https://www.gyan.dev/ffmpeg/builds/"
        )
        return False


def check_linux_requirements() -> bool:
    """Return True if playerctl is available (Linux recorder requirement)."""
    if not IS_LINUX:
        console.print(
            "[bold yellow]⚠️  WARNING:[/bold yellow] "
            "Spotify recording requires Linux with playerctl."
        )
        return False
    try:
        subprocess.run(["which", "playerctl"], capture_output=True, check=True)
        return True
    except subprocess.CalledProcessError:
        console.print(
            "[bold yellow]⚠️  WARNING:[/bold yellow] "
            "'playerctl' not found.  Install it: sudo apt install playerctl"
        )
        return False


def generate_m3u(
    playlist_name:      str,
    output_dir:         Path,
    original_order_file: Path,
    final_paths:        Dict[str, Path],
) -> None:
    """Write an .m3u playlist preserving the original song order."""
    if not final_paths:
        return

    m3u_path = output_dir / f"{sanitize_filename(playlist_name)}.m3u"
    console.print(f"\n[bold cyan]--- Generating Playlist: {m3u_path.name} ---[/bold cyan]")

    try:
        with open(original_order_file, "r", encoding="utf-8") as fh:
            lines = [ln.strip() for ln in fh if ln.strip()]
    except Exception as exc:
        console.print(f"[bold red]❌ Could not read order file:[/bold red] {exc}")
        return

    sanitised_lookup: Dict[str, Path] = {
        sanitize_filename(k): v for k, v in final_paths.items()
    }

    playlist_entries: list[str] = []
    for line in lines:
        if "|" not in line:
            continue
        song_name = line.split("|", 1)[0].strip()

        found_path = final_paths.get(song_name) or sanitised_lookup.get(
            sanitize_filename(song_name)
        )

        if found_path and found_path.exists():
            try:
                rel_path = found_path.relative_to(output_dir)
                playlist_entries.append(str(rel_path))
            except ValueError:
                playlist_entries.append(str(found_path))

    if playlist_entries:
        with open(m3u_path, "w", encoding="utf-8") as fh:
            fh.write("#EXTM3U\n")
            for entry in playlist_entries:
                fh.write(f"{entry}\n")
        console.print(
            f"[bold green]✓ Playlist saved with {len(playlist_entries)} tracks.[/bold green]"
        )
    else:
        console.print("[bold yellow]⚠ No tracks were added to the playlist.[/bold yellow]")


def _read_opus_tags(filepath: Path) -> tuple[str, str, bool]:
    """Read title, artist, has_lyrics from an Opus file."""
    try:
        from mutagen.oggopus import OggOpus
        audio = OggOpus(str(filepath))
        title      = audio.get("title",  [filepath.stem])[0].lower().strip()
        artist     = audio.get("artist", ["unknown"])[0].lower().strip()
        has_lyrics = "lyrics" in audio
        return title, artist, has_lyrics
    except Exception:
        return filepath.stem.lower().strip(), "unknown", False


def remove_duplicates(directory: Path) -> None:
    """
    Scan *directory* recursively and delete duplicate audio files.

    Deduplication key: (title, artist) from tags.
    When duplicates exist, the file with lyrics AND the largest size wins.
    Supports .mp3  .flac  .m4a  .opus
    """
    console.print(
        Panel(f"[bold yellow]Scanning '{directory}' for duplicates[/bold yellow]", expand=False)
    )
    song_groups: Dict[tuple, list] = {}

    with console.status("[cyan]Reading files and extracting metadata…[/cyan]"):
        # All supported extensions including Opus
        for ext_pattern in ("*.mp3", "*.flac", "*.m4a", "*.opus"):
            for filepath in directory.rglob(ext_pattern):
                try:
                    ext = filepath.suffix.lower()
                    title, artist, has_lyrics = "", "unknown", False

                    if ext == ".mp3":
                        audio = MP3(str(filepath), ID3=ID3)
                        tit2 = audio.tags.getall("TIT2") if audio.tags else []
                        title  = tit2[0].text[0].lower().strip() if tit2 else filepath.stem.lower()
                        tpe1   = audio.tags.getall("TPE1") if audio.tags else []
                        artist = tpe1[0].text[0].lower().strip() if tpe1 else "unknown"
                        has_lyrics = bool(audio.tags.getall("USLT") if audio.tags else [])

                    elif ext == ".flac":
                        audio      = FLAC(str(filepath))
                        title      = audio.get("title",  [filepath.stem])[0].lower().strip()
                        artist     = audio.get("artist", ["unknown"])[0].lower().strip()
                        has_lyrics = "lyrics" in audio

                    elif ext == ".m4a":
                        audio      = MP4(str(filepath))
                        title      = audio.get("\xa9nam", [filepath.stem])[0].lower().strip()
                        artist     = audio.get("\xa9ART", ["unknown"])[0].lower().strip()
                        has_lyrics = "\xa9lyr" in audio

                    elif ext == ".opus":
                        title, artist, has_lyrics = _read_opus_tags(filepath)

                    key = (title, artist)
                    song_groups.setdefault(key, []).append({
                        "path":       filepath,
                        "has_lyrics": has_lyrics,
                        "size":       filepath.stat().st_size,
                    })
                except Exception:
                    pass   # skip unreadable files silently

    removed_count = 0
    with console.status("[cyan]Analysing and removing duplicates…[/cyan]"):
        for (_title, _artist), files in song_groups.items():
            if len(files) <= 1:
                continue
            # Best = has lyrics AND largest size
            files.sort(key=lambda x: (x["has_lyrics"], x["size"]), reverse=True)
            best = files[0]
            for dup in files[1:]:
                console.print(f"[red]🗑️  Removing:[/red] {dup['path'].name}")
                console.print(f"   [dim](Keeping: {best['path'].name})[/dim]")
                try:
                    dup["path"].unlink()
                    lrc = dup["path"].with_suffix(".lrc")
                    if lrc.exists():
                        lrc.unlink()
                    removed_count += 1
                except Exception as exc:
                    console.print(
                        f"[bold red]❌ Failed to delete {dup['path'].name}:[/bold red] {exc}"
                    )

    console.print(
        f"\n[bold green]✓ Deduplication complete. "
        f"Removed {removed_count} duplicate file(s).[/bold green]\n"
    )