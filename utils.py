import os
import sys
import re
import subprocess
from pathlib import Path
from typing import Dict

from mutagen.mp3 import MP3
from mutagen.id3 import ID3
from mutagen.flac import FLAC
from mutagen.mp4 import MP4
from rich.console import Console
from rich.panel import Panel

# Shared Rich Console for the entire application
console = Console()

IS_LINUX = sys.platform.startswith('linux')

def sanitize_filename(name: str, max_length: int = 150) -> str:
    """Remove invalid characters and strip trailing spaces/dots."""
    name = re.sub(r'[\\/*?:"<>|]', '', name)
    name = re.sub(r'\s+', ' ', name)
    return name[:max_length].strip('. ')

def check_ffmpeg() -> bool:
    """Check if ffmpeg is available (required by yt-dlp)."""
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True, timeout=3)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        console.print("[bold red]❌ FFmpeg is required but not found.[/bold red] Install it:\n"
              "   [cyan]Linux:[/cyan] sudo apt install ffmpeg\n"
              "   [cyan]macOS:[/cyan] brew install ffmpeg\n"
              "   [cyan]Windows:[/cyan] https://www.gyan.dev/ffmpeg/builds/")
        return False

def check_linux_requirements() -> bool:
    """Check if playerctl is available for recording (Linux only)."""
    if not IS_LINUX:
        console.print("[bold yellow]⚠️  WARNING:[/bold yellow] Spotify recording requires Linux with playerctl.")
        return False
    try:
        subprocess.run(["which", "playerctl"], capture_output=True, check=True)
        return True
    except subprocess.CalledProcessError:
        console.print("[bold yellow]⚠️  WARNING:[/bold yellow] 'playerctl' not found. Install it (e.g., sudo apt install playerctl).")
        return False

def generate_m3u(playlist_name: str, output_dir: Path, original_order_file: Path, final_paths: Dict[str, Path]) -> None:
    """Generate an .m3u playlist maintaining the original order of the search file."""
    if not final_paths:
        return
        
    m3u_path = output_dir / f"{sanitize_filename(playlist_name)}.m3u"
    console.print(f"\n[bold cyan]--- Generating Playlist: {m3u_path.name} ---[/bold cyan]")

    try:
        with open(original_order_file, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
    except Exception as e:
        console.print(f"[bold red]❌ Could not read order file:[/bold red] {e}")
        return

    playlist_entries = []
    for line in lines:
        if "|" in line:
            song_name = line.split("|", 1)[0].strip()
            if song_name in final_paths:
                try:
                    rel_path = final_paths[song_name].relative_to(output_dir)
                    playlist_entries.append(str(rel_path))
                except ValueError:
                    playlist_entries.append(str(final_paths[song_name]))

    if playlist_entries:
        with open(m3u_path, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            for entry in playlist_entries:
                f.write(f"{entry}\n")
        console.print(f"[bold green]✓ Playlist saved successfully with {len(playlist_entries)} tracks.[/bold green]")
    else:
        console.print("[bold yellow]⚠️ No tracks were found to add to the playlist.[/bold yellow]")

def remove_duplicates(directory: Path) -> None:
    """Scans a directory for audio files and removes duplicates based on Title, Artist, and Lyrics."""
    console.print(Panel(f"[bold yellow]Scanning '{directory}' for duplicates[/bold yellow]", expand=False))
    song_groups = {}
    
    with console.status("[cyan]Reading files and extracting metadata...[/cyan]"):
        for ext_pattern in ("*.mp3", "*.flac", "*.m4a"):
            for filepath in directory.rglob(ext_pattern):
                try:
                    ext = filepath.suffix.lower()
                    title, artist, has_lyrics = "", "unknown", False
                    
                    if ext == '.mp3':
                        audio = MP3(str(filepath), ID3=ID3)
                        tit2 = audio.tags.getall('TIT2') if audio.tags else []
                        title = tit2[0].text[0].lower().strip() if tit2 else filepath.stem.lower().strip()
                        tpe1 = audio.tags.getall('TPE1') if audio.tags else []
                        artist = tpe1[0].text[0].lower().strip() if tpe1 else "unknown"
                        has_lyrics = bool(audio.tags.getall('USLT') if audio.tags else [])
                        
                    elif ext == '.flac':
                        audio = FLAC(str(filepath))
                        title = audio.get('title', [filepath.stem])[0].lower().strip()
                        artist = audio.get('artist', ['unknown'])[0].lower().strip()
                        has_lyrics = 'lyrics' in audio
                        
                    elif ext == '.m4a':
                        audio = MP4(str(filepath))
                        title = audio.get('\xa9nam', [filepath.stem])[0].lower().strip()
                        artist = audio.get('\xa9ART', ['unknown'])[0].lower().strip()
                        has_lyrics = '\xa9lyr' in audio

                    file_size = filepath.stat().st_size
                    key = (title, artist)
                    
                    if key not in song_groups:
                        song_groups[key] = []
                        
                    song_groups[key].append({
                        'path': filepath,
                        'has_lyrics': has_lyrics,
                        'size': file_size
                    })
                except Exception:
                    pass

    removed_count = 0
    with console.status("[cyan]Analyzing and removing duplicates...[/cyan]"):
        for (title, artist), files in song_groups.items():
            if len(files) > 1:
                files.sort(key=lambda x: (x['has_lyrics'], x['size']), reverse=True)
                best_file = files[0]
                duplicates = files[1:]
                
                for dup in duplicates:
                    console.print(f"[red]🗑️ Removing:[/red] {dup['path'].name}")
                    console.print(f"   [dim](Keeping: {best_file['path'].name})[/dim]")
                    try:
                        dup['path'].unlink()
                        lrc_path = dup['path'].with_suffix('.lrc')
                        if lrc_path.exists():
                            lrc_path.unlink()
                        removed_count += 1
                    except Exception as e:
                        console.print(f"[bold red]❌ Failed to delete {dup['path'].name}:[/bold red] {e}")

    console.print(f"\n[bold green]✓ Deduplication complete. Removed {removed_count} duplicate files.[/bold green]\n")