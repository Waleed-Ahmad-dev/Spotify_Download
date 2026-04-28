#!/usr/bin/env python3
"""
Spotify-to-MP3 Converter (CLI Edition)
Records Spotify playlists, finds YouTube matches, downloads MP3s, tags metadata + lyrics,
features a smart duplicate remover, applies audio volume normalization, and generates M3U playlists.
Now with a beautiful Rich Terminal UI.
"""

import os
import sys
import time
import argparse
import subprocess
import concurrent.futures
import random
import urllib.request
import urllib.parse
import json
import re
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any

# --- Dependencies Check ---
try:
    import yt_dlp
except ImportError:
    print("❌ yt-dlp not installed. Install with: pip install yt-dlp")
    sys.exit(1)

try:
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, TCON, TRCK, USLT, APIC
except ImportError:
    print("❌ mutagen not installed. Install with: pip install mutagen")
    sys.exit(1)

try:
    from rich.console import Console
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn
    from rich.panel import Panel
    from rich.table import Table
except ImportError:
    print("❌ 'rich' not installed. Install with: pip install rich")
    sys.exit(1)

# Initialize Rich Console
console = Console()

# --- Platform & Utilities ---
IS_LINUX = sys.platform.startswith('linux')

def sanitize_filename(name: str, max_length: int = 150) -> str:
    """Remove invalid characters and strip trailing spaces/dots (which break Windows paths)."""
    name = re.sub(r'[\\/*?:"<>|]', '', name)
    name = re.sub(r'\s+', ' ', name)
    return name[:max_length].strip('. ')

def check_ffmpeg() -> bool:
    """Check if ffmpeg is available (required by yt-dlp for mp3 conversion)."""
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

# --- 2. RECORDING (Spotify via playerctl) ---
def get_current_song() -> Optional[str]:
    """Get current Spotify song as 'title - artist'."""
    try:
        title = subprocess.check_output(["playerctl", "--player=spotify", "metadata", "title"], text=True).strip()
        artist = subprocess.check_output(["playerctl", "--player=spotify", "metadata", "artist"], text=True).strip()
        if not title:
            return None
        return f"{title} - {artist}"
    except subprocess.CalledProcessError:
        return None

def next_song() -> None:
    """Skip to next song in Spotify."""
    subprocess.run(["playerctl", "--player=spotify", "next"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def record_spotify(output_file: Path) -> None:
    """Record Spotify playlist by detecting song changes."""
    if not check_linux_requirements():
        console.print("[bold red]❌ Cannot proceed with recording on this platform.[/bold red]")
        sys.exit(1)

    console.print(Panel("[bold green]Spotify Playlist Recorder[/bold green]\nPlay your music in Spotify, then press Enter to begin.", expand=False))
    input()

    recorded_songs = []
    seen_songs = set()
    
    try:
        with console.status("[bold cyan]Listening to Spotify... (Press Ctrl+C to stop)[/bold cyan]", spinner="bouncingBar") as status:
            while True:
                current_song = get_current_song()
                if not current_song:
                    time.sleep(2)
                    continue

                if current_song in seen_songs and len(recorded_songs) > 0:
                    console.print(f"\n[bold green]✓ Loop detected![/bold green] Recorded {len(recorded_songs)} songs.")
                    break

                recorded_songs.append(current_song)
                seen_songs.add(current_song)
                console.print(f"[cyan][{len(recorded_songs)}][/cyan] [green]Recorded:[/green] {current_song}")

                next_song()

                start = time.time()
                while time.time() - start < 15:
                    time.sleep(0.8)
                    if get_current_song() != current_song:
                        break
                else:
                    console.print("[bold yellow]⚠️  Timeout waiting for next song – playlist may have ended.[/bold yellow]")
                    break
    except KeyboardInterrupt:
        console.print("\n[bold yellow]⚠️  Recording stopped by user.[/bold yellow]")

    if recorded_songs:
        with open(output_file, "w", encoding="utf-8") as f:
            f.write("\n".join(recorded_songs))
        console.print(f"\n[bold green]✓ Saved {len(recorded_songs)} songs to '{output_file}'.[/bold green]")

# --- 3. SEARCHING (YouTube) ---
def find_url(song_name: str) -> Dict[str, Any]:
    """Search YouTube for the song using optimized flat extraction."""
    query = f"{song_name} official audio"
    ydl_opts = {
        'extract_flat': True,
        'quiet': True,
        'no_warnings': True,
        'default_search': 'ytsearch1',
        'noplaylist': True,
    }
    for _ in range(3):
        try:
            time.sleep(random.uniform(0.1, 0.5))
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(query, download=False)
                video = info['entries'][0] if 'entries' in info and info['entries'] else info
                url = video.get('webpage_url') or video.get('url')
                if url:
                    return {'song': song_name, 'url': url, 'found': True}
            return {'song': song_name, 'error': 'No results', 'found': False}
        except Exception as e:
            if "429" in str(e):
                time.sleep(10)
    return {'song': song_name, 'error': 'Max retries exceeded', 'found': False}

def search_youtube(input_file: Path, output_found: Path, output_notfound: Path, max_workers: int = 3) -> None:
    """Search for each song in the input file using multiple threads."""
    if not input_file.exists():
        console.print(f"[bold red]❌ Error: '{input_file}' not found.[/bold red]")
        sys.exit(1)

    with open(input_file, "r", encoding="utf-8") as f:
        songs = [line.strip() for line in f if line.strip()]

    found_list, not_found_list = [], []

    console.print()
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(f"[cyan]Searching YouTube for {len(songs)} songs...", total=len(songs))

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_song = {executor.submit(find_url, song): song for song in songs}
            for future in concurrent.futures.as_completed(future_to_song):
                res = future.result()
                progress.advance(task)
                if res['found']:
                    found_list.append(f"{res['song']} | {res['url']}")
                    progress.console.print(f"[green]🟢 FOUND:[/green] {res['song']}")
                else:
                    not_found_list.append(res['song'])
                    progress.console.print(f"[red]🔴 FAILED:[/red] {res['song']}")

    with open(output_found, "w", encoding="utf-8") as f:
        f.write("\n".join(found_list))
    with open(output_notfound, "w", encoding="utf-8") as f:
        f.write("\n".join(not_found_list))

# --- 4. DOWNLOADING (yt-dlp) ---
def download_track(line: str, output_folder: Path, quality: str = '192', normalize: bool = False) -> Optional[Path]:
    """Download a single track from YouTube as MP3, optionally normalizing volume."""
    if "|" not in line:
        return None
    song_name, url = [p.strip() for p in line.split("|", 1)]

    safe_name = sanitize_filename(song_name)
    output_path = output_folder / safe_name
    expected_file = output_path.with_suffix('.mp3')

    if expected_file.exists():
        return expected_file

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': str(output_path) + '.%(ext)s',
        'quiet': True,
        'noprogress': True,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': quality,
        }],
    }
    
    if normalize:
        ydl_opts['postprocessor_args'] = ['-af', 'loudnorm=I=-14:LRA=11:TP=-1.0']

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        
        if expected_file.exists():
            return expected_file
        else:
            return None
    except Exception:
        return None

def download_songs(input_file: Path, output_folder: Path, quality: str = '192', max_workers: int = 2, normalize: bool = False) -> List[Tuple[str, Path]]:
    """Download all found songs using parallel workers."""
    if not input_file.exists():
        return []
    output_folder.mkdir(parents=True, exist_ok=True)

    with open(input_file, "r", encoding="utf-8") as f:
        lines = [l.strip() for l in f if l.strip()]

    console.print()
    if normalize:
        console.print("[dim italic]↳ Audio volume normalization enabled (-14 LUFS)[/dim italic]")
        
    downloaded_files = []

    with Progress(
        SpinnerColumn(spinner_name="dots2"),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(style="magenta"),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(f"[magenta]Downloading {len(lines)} songs...", total=len(lines))

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_line = {executor.submit(download_track, line, output_folder, quality, normalize): line for line in lines}
            for future in concurrent.futures.as_completed(future_to_line):
                filepath = future.result()
                song_name = future_to_line[future].split('|')[0].strip()
                progress.advance(task)
                
                if filepath:
                    downloaded_files.append((song_name, filepath))
                    progress.console.print(f"[green]✓ Downloaded:[/green] {song_name}")
                else:
                    progress.console.print(f"[red]❌ Failed:[/red] {song_name}")

    return downloaded_files

# --- 5. TAGGING, LYRICS & PLAYLISTS ---
def search_itunes(query: str) -> Optional[Dict[str, Any]]:
    """Search iTunes API for track metadata."""
    try:
        clean_query = query.replace('-', ' ')
        url = f"https://itunes.apple.com/search?term={urllib.parse.quote(clean_query)}&media=music&limit=1"
        with urllib.request.urlopen(url) as response:
            data = json.loads(response.read().decode())
            if data['resultCount'] > 0:
                return data['results'][0]
    except Exception:
        pass
    return None

def get_synced_lyrics(artist: str, track: str, duration_sec: float) -> Tuple[Optional[str], Optional[str]]:
    """Fetch synced and plain lyrics from LRCLIB API."""
    try:
        url = f"https://lrclib.net/api/get?artist_name={urllib.parse.quote(artist)}&track_name={urllib.parse.quote(track)}&duration={int(duration_sec)}"
        req = urllib.request.Request(url, headers={'User-Agent': 'SpotifyDownloaderCLI/1.0'})
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode())
            return data.get('syncedLyrics'), data.get('plainLyrics')
    except Exception:
        return None, None

def save_lrc_file(audio_path: Path, synced_lyrics: str) -> None:
    """Save synced lyrics as .lrc file next to the MP3."""
    lrc_path = audio_path.with_suffix('.lrc')
    with open(lrc_path, "w", encoding="utf-8") as f:
        f.write(synced_lyrics)

def embed_metadata(file_path: Path, track_info: Dict[str, Any], plain_lyrics: Optional[str]) -> None:
    """Embed ID3 tags, cover art, and plain lyrics into MP3."""
    try:
        audio = MP3(str(file_path), ID3=ID3)
        if audio.tags is None:
            audio.add_tags()

        audio.tags.add(TIT2(encoding=3, text=track_info.get('trackName', '')))
        audio.tags.add(TPE1(encoding=3, text=track_info.get('artistName', '')))
        
        album = track_info.get('collectionName', '')
        if album:
            audio.tags.add(TALB(encoding=3, text=album))
        year = track_info.get('releaseDate', '')[:4]
        if year:
            audio.tags.add(TDRC(encoding=3, text=year))
        genre = track_info.get('primaryGenreName', '')
        if genre:
            audio.tags.add(TCON(encoding=3, text=genre))
        track_number = track_info.get('trackNumber')
        if track_number:
            audio.tags.add(TRCK(encoding=3, text=str(track_number)))

        art_url = track_info.get('artworkUrl100', '').replace('100x100bb', '600x600bb')
        if art_url:
            try:
                with urllib.request.urlopen(art_url) as response:
                    audio.tags.add(APIC(encoding=3, mime='image/jpeg', type=3, desc='Cover', data=response.read()))
            except Exception:
                pass

        if plain_lyrics:
            audio.tags.add(USLT(encoding=3, lang='eng', desc='', text=plain_lyrics))

        audio.save()
    except Exception as e:
        console.print(f"[bold red]Error tagging {file_path}:[/bold red] {e}")

def organize_files(file_path: Path, track_info: Dict[str, Any], output_dir: Path) -> Path:
    """Move MP3 and its .lrc file into an Artist folder."""
    artist = sanitize_filename(track_info.get('artistName', 'Unknown Artist'))
    new_folder = output_dir / artist
    new_folder.mkdir(parents=True, exist_ok=True)

    new_path = new_folder / file_path.name
    if new_path != file_path:
        file_path.rename(new_path)
        lrc_path = file_path.with_suffix('.lrc')
        if lrc_path.exists():
            lrc_path.rename(new_folder / lrc_path.name)
    return new_path

def process_metadata(downloaded_files: List[Tuple[str, Path]], organize: bool = False, output_dir: Path = None) -> Dict[str, Path]:
    """Fetch metadata, lyrics, embed tags, and return a dictionary mapping song names to their final paths."""
    console.print()
    final_paths = {}
    
    with Progress(
        SpinnerColumn(spinner_name="arc"),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(style="yellow"),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(f"[yellow]Fetching Metadata & Lyrics...", total=len(downloaded_files))

        for song_name, file_path in downloaded_files:
            progress.console.print(f"[bold]Tagging:[/bold] {song_name}")
            track = search_itunes(song_name)
            current_path = file_path
            
            if track:
                duration_sec = track.get('trackTimeMillis', 0) / 1000
                synced_lyrics, plain_lyrics = get_synced_lyrics(track['artistName'], track['trackName'], duration_sec)

                if synced_lyrics:
                    save_lrc_file(current_path, synced_lyrics)
                    progress.console.print("  [cyan]>[/cyan] [green]Synced .lrc lyrics saved.[/green]")

                embed_metadata(current_path, track, plain_lyrics)
                progress.console.print("  [cyan]>[/cyan] [green]ID3 Tags & Cover embedded.[/green]")

                if organize and output_dir:
                    current_path = organize_files(current_path, track, output_dir)
                    progress.console.print(f"  [cyan]>[/cyan] [green]Moved to {current_path.parent.name}/[/green]")
            else:
                progress.console.print("  [cyan]>[/cyan] [yellow]Metadata not found.[/yellow]")
                
            final_paths[song_name] = current_path
            progress.advance(task)
            time.sleep(0.5)
        
    return final_paths

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

# --- 6. DUPLICATE REMOVAL ---
def remove_duplicates(directory: Path) -> None:
    """Scans a directory for MP3s and removes duplicates based on Title, Artist, and Lyrics."""
    console.print(Panel(f"[bold yellow]Scanning '{directory}' for duplicates[/bold yellow]", expand=False))
    song_groups = {}
    
    with console.status("[cyan]Reading files and extracting metadata...[/cyan]"):
        for filepath in directory.rglob("*.mp3"):
            try:
                audio = MP3(str(filepath), ID3=ID3)
                
                tit2 = audio.tags.getall('TIT2') if audio.tags else []
                title = tit2[0].text[0].lower().strip() if tit2 else filepath.stem.lower().strip()
                
                tpe1 = audio.tags.getall('TPE1') if audio.tags else []
                artist = tpe1[0].text[0].lower().strip() if tpe1 else "unknown"
                
                uslt = audio.tags.getall('USLT') if audio.tags else []
                has_lyrics = bool(uslt)
                
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

# --- 7. MAIN PIPELINE ---
def main():
    parser = argparse.ArgumentParser(description="Spotify Playlist to MP3 Downloader + Tagger")
    parser.add_argument('--record', action='store_true', help='Record Spotify playlist to songs.txt')
    parser.add_argument('--search', action='store_true', help='Search YouTube for songs in songs.txt')
    parser.add_argument('--download', action='store_true', help='Download found songs & apply metadata')
    parser.add_argument('--all', action='store_true', help='Run the complete pipeline (Record, Search, Download, Normalize)')

    parser.add_argument('--input', default='songs.txt', help='Input song list file (default: songs.txt)')
    parser.add_argument('--found', default='found.txt', help='Output file for found URLs (default: found.txt)')
    parser.add_argument('--notfound', default='not_found.txt', help='Output file for missing songs (default: not_found.txt)')
    parser.add_argument('--output-dir', default='songs', help='Directory to save MP3s (default: songs)')
    parser.add_argument('--workers', type=int, default=5, help='Number of search threads (default: 5)')
    parser.add_argument('--quality', choices=['128', '192', '320'], default='192', help='MP3 bitrate (default: 192)')
    parser.add_argument('--organize', action='store_true', help='Organize MP3s into Artist folders after tagging')
    parser.add_argument('--resume', action='store_true', help='Skip search if found.txt already exists')
    parser.add_argument('--normalize', action='store_true', help='Normalize audio volume to -14 LUFS (Spotify standard)')
    parser.add_argument('--playlist', type=str, metavar='NAME', help='Generate an .m3u playlist with this name retaining original order')
    parser.add_argument('--dedupe', type=str, metavar='DIR', help='Remove duplicates in a specified directory based on metadata')

    args = parser.parse_args()

    if args.dedupe:
        dedupe_dir = Path(args.dedupe)
        if not dedupe_dir.exists() or not dedupe_dir.is_dir():
            console.print(f"[bold red]❌ Error: Directory '{dedupe_dir}' not found.[/bold red]")
            sys.exit(1)
        remove_duplicates(dedupe_dir)
        sys.exit(0)

    if not any([args.record, args.search, args.download, args.all]):
        parser.print_help()
        sys.exit(0)

    input_file = Path(args.input)
    found_file = Path(args.found)
    notfound_file = Path(args.notfound)
    out_dir = Path(args.output_dir)
    
    should_normalize = args.normalize or args.all

    if args.record or args.all:
        record_spotify(input_file)

    if args.search or args.all:
        if args.resume and found_file.exists():
            console.print(f"[bold green]✓ Found file '{found_file}' exists. Skipping search (--resume).[/bold green]")
        else:
            search_youtube(input_file, found_file, notfound_file, max_workers=args.workers)

    if args.download or args.all:
        if not check_ffmpeg():
            sys.exit(1)

        if not found_file.exists() or found_file.stat().st_size == 0:
            console.print("[bold red]❌ No URLs found. Run --search first or remove --resume.[/bold red]")
            sys.exit(1)

        downloaded = download_songs(found_file, out_dir, quality=args.quality, max_workers=2, normalize=should_normalize)
        if downloaded:
            final_paths = process_metadata(downloaded, organize=args.organize, output_dir=out_dir)
            
            if args.playlist:
                generate_m3u(args.playlist, out_dir, found_file, final_paths)
            
            # Print Final Summary Table
            console.print()
            summary_table = Table(title="🎉 Run Summary", show_header=True, header_style="bold magenta")
            summary_table.add_column("Task", style="cyan")
            summary_table.add_column("Result", justify="right", style="green")
            
            summary_table.add_row("Downloaded Tracks", str(len(downloaded)))
            summary_table.add_row("Metadata Tagged", str(len(final_paths)))
            if should_normalize:
                summary_table.add_row("Audio Normalized", "Yes (-14 LUFS)")
            if args.organize:
                summary_table.add_row("Folder Organization", "Enabled (By Artist)")
            if args.playlist:
                summary_table.add_row("Playlist Generated", f"{args.playlist}.m3u")
                
            console.print(summary_table)
            console.print("\n[bold green]All tasks completed successfully! Enjoy your music.[/bold green]\n")
                
        else:
            console.print("[bold red]❌ No songs were downloaded.[/bold red]")

if __name__ == "__main__":
    main()