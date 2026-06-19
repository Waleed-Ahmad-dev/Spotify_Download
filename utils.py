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
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from mutagen.mp3  import MP3
from mutagen.id3  import ID3
from mutagen.flac import FLAC
from mutagen.mp4  import MP4
from rich.console import Console
from rich.panel   import Panel

# Shared Rich console for the entire application
console = Console()

IS_LINUX = sys.platform.startswith("linux")
IS_WINDOWS = sys.platform.startswith("win")


# ─────────────────────────────────────────────────────────────────────────────
# ffmpeg / ffprobe discovery (cross-platform)
# ─────────────────────────────────────────────────────────────────────────────
# On Windows ffmpeg is rarely on PATH out of the box, and a frozen .exe ships its
# own binaries.  These resolvers locate ffmpeg/ffprobe in this order:
#   1. FFMPEG_LOCATION env override (a directory, or a direct file path)
#   2. PATH (shutil.which)
#   3. a bundled  vendor/  directory beside the script / frozen exe
#   4. (ffmpeg only) the static binary shipped by the optional imageio-ffmpeg pkg

def _exe_name(base: str) -> str:
    """Append '.exe' on Windows."""
    return base + ".exe" if IS_WINDOWS else base


def _vendor_dirs() -> List[Path]:
    """Candidate directories that may hold bundled ffmpeg/ffprobe binaries."""
    dirs: List[Path] = []
    if getattr(sys, "frozen", False):                       # PyInstaller build
        dirs.append(Path(sys.executable).resolve().parent / "vendor")
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            dirs.append(Path(meipass) / "vendor")
    dirs.append(Path(__file__).resolve().parent / "vendor")  # source checkout
    return dirs


def _resolve_binary(base: str, use_imageio: bool = False) -> Optional[str]:
    """Resolve an ffmpeg/ffprobe path, or None if nothing is found."""
    exe = _exe_name(base)

    # 1. Explicit override (directory or full file path)
    override = os.environ.get("FFMPEG_LOCATION", "").strip()
    if override:
        p = Path(override)
        if p.is_file():
            cand = p if p.stem.lower() == base else p.with_name(exe)
            if cand.exists():
                return str(cand)
        elif p.is_dir() and (p / exe).exists():
            return str(p / exe)

    # 2. On PATH
    found = shutil.which(base)
    if found:
        return found

    # 3. Bundled vendor dir (frozen .exe or a manual drop-in beside the source)
    for d in _vendor_dirs():
        cand = d / exe
        if cand.exists():
            return str(cand)

    # 4. imageio-ffmpeg static binary (ffmpeg only — it does not ship ffprobe)
    if use_imageio:
        try:
            import imageio_ffmpeg
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pass

    return None


_FFMPEG_PATH: Optional[str] = None
_FFPROBE_PATH: Optional[str] = None


def get_ffmpeg() -> str:
    """Absolute path to ffmpeg, or the bare name 'ffmpeg' if unresolved."""
    global _FFMPEG_PATH
    if _FFMPEG_PATH is None:
        _FFMPEG_PATH = _resolve_binary("ffmpeg", use_imageio=True) or "ffmpeg"
    return _FFMPEG_PATH


def get_ffprobe() -> str:
    """Absolute path to ffprobe, or the bare name 'ffprobe' if unresolved."""
    global _FFPROBE_PATH
    if _FFPROBE_PATH is None:
        _FFPROBE_PATH = _resolve_binary("ffprobe", use_imageio=False) or "ffprobe"
    return _FFPROBE_PATH


def _prepend_ffmpeg_to_path() -> None:
    """
    Put the resolved ffmpeg directory on PATH so child libraries that invoke
    ffmpeg by name (yt-dlp post-processing, openai-whisper) can find it too.
    """
    ff = get_ffmpeg()
    if os.sep in ff or "/" in ff:           # a real path, not a bare name
        ff_dir = str(Path(ff).resolve().parent)
        if ff_dir and ff_dir not in os.environ.get("PATH", "").split(os.pathsep):
            os.environ["PATH"] = ff_dir + os.pathsep + os.environ.get("PATH", "")


_prepend_ffmpeg_to_path()


# ─────────────────────────────────────────────────────────────────────────────
# Filename sanitisation
# ─────────────────────────────────────────────────────────────────────────────
# Windows reserved device names — illegal as a file's base name (with or without
# an extension), e.g. "NUL.mp3" cannot be created on Windows.
_WIN_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def sanitize_filename(name: str, max_length: int = 150) -> str:
    """Strip filename-illegal characters, normalise whitespace, and avoid the
    Windows reserved device names."""
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = re.sub(r"\s+", " ", name)
    name = name[:max_length].strip(". ")
    if name.split(".", 1)[0].strip().lower() in _WIN_RESERVED:
        name = "_" + name
    return name or "untitled"


def check_ffmpeg() -> bool:
    """Return True if ffmpeg can be located (PATH, override, vendor/, or imageio)."""
    ffmpeg = get_ffmpeg()
    try:
        subprocess.run(
            [ffmpeg, "-version"], capture_output=True, check=True, timeout=10
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError,
            subprocess.TimeoutExpired, OSError):
        console.print(
            "[bold red]❌ FFmpeg is required but not found.[/bold red]\n"
            "Install it:\n"
            "   [cyan]Windows:[/cyan] winget install Gyan.FFmpeg   "
            "[dim](or: pip install imageio-ffmpeg)[/dim]\n"
            "   [cyan]Linux:[/cyan]   sudo apt install ffmpeg\n"
            "   [cyan]macOS:[/cyan]   brew install ffmpeg\n"
            "   [dim]Manual builds: https://www.gyan.dev/ffmpeg/builds/[/dim]"
        )
        return False


def check_recorder_requirements() -> bool:
    """
    Return True if this platform's 'now-playing' recorder backend is available.
      • Linux   → playerctl on PATH (reads the MPRIS/D-Bus interface)
      • Windows → the 'winsdk' package (reads GSMTC — the system media session)
    Reading the local Spotify app needs no Spotify account/API on either OS.
    """
    if IS_LINUX:
        if shutil.which("playerctl"):
            return True
        console.print(
            "[bold yellow]⚠️  WARNING:[/bold yellow] "
            "'playerctl' not found.  Install it: sudo apt install playerctl"
        )
        return False
    if IS_WINDOWS:
        try:
            import winsdk.windows.media.control  # noqa: F401
            return True
        except Exception:
            console.print(
                "[bold yellow]⚠️  WARNING:[/bold yellow] "
                "Windows recording needs the 'winsdk' package.\n"
                "   Install it:  [cyan]pip install winsdk[/cyan]"
            )
            return False
    console.print(
        "[bold yellow]⚠️  WARNING:[/bold yellow] "
        "Auto-recording from the Spotify app isn't supported on this platform.\n"
        "   Use manual song entry or provide a songs.txt instead."
    )
    return False


# Backwards-compatible alias for older imports / scripts.
check_linux_requirements = check_recorder_requirements


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
                playlist_entries.append(rel_path.as_posix())
            except ValueError:
                playlist_entries.append(found_path.as_posix())

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