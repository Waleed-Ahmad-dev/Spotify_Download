import sys
import time
import random
import concurrent.futures
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional

import yt_dlp
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn

from utils import console, sanitize_filename

def find_url(song_name: str) -> Dict[str, Any]:
    """Search YouTube for the song using explicitly constructed URLs."""
    # Explicitly prefix with ytsearch1: to force yt-dlp to treat it as a search
    query = f"ytsearch1:{song_name} official audio"
    ydl_opts = {
        'extract_flat': True,
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
    }
    for _ in range(3):
        try:
            time.sleep(random.uniform(0.1, 0.5))
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(query, download=False)
                
                # Ensure we actually got search entries back
                if 'entries' in info and info['entries']:
                    video = info['entries'][0]
                    video_id = video.get('id')
                    
                    # Construct the URL cleanly using the video ID
                    if video_id:
                        url = f"https://www.youtube.com/watch?v={video_id}"
                        return {'song': song_name, 'url': url, 'found': True}
                    
                    # Fallback if no ID but a valid web URL exists
                    url = video.get('webpage_url') or video.get('url')
                    if url and str(url).startswith('http'):
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

def download_track(line: str, output_folder: Path, format_ext: str, quality: str = '192', normalize: bool = False) -> Optional[Path]:
    """Download a single track from YouTube as MP3/FLAC/M4A, optionally normalizing volume."""
    if "|" not in line:
        return None
    song_name, url = [p.strip() for p in line.split("|", 1)]

    safe_name = sanitize_filename(song_name)
    output_path = output_folder / safe_name
    expected_file = output_path.with_suffix(f'.{format_ext}')

    if expected_file.exists():
        return expected_file

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': str(output_path) + '.%(ext)s',
        'quiet': True,
        'noprogress': True,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': format_ext,
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

def download_songs(input_file: Path, output_folder: Path, format_ext: str, quality: str = '192', max_workers: int = 2, normalize: bool = False) -> List[Tuple[str, Path]]:
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
        task = progress.add_task(f"[magenta]Downloading {len(lines)} songs ({format_ext.upper()})...", total=len(lines))

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_line = {executor.submit(download_track, line, output_folder, format_ext, quality, normalize): line for line in lines}
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