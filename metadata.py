import time
import json
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional

from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, TCON, TRCK, USLT, APIC
from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4, MP4Cover
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

from utils import console, sanitize_filename

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
    """Embed tags, cover art, and plain lyrics dynamically based on format."""
    ext = file_path.suffix.lower()
    art_bytes = None
    art_url = track_info.get('artworkUrl100', '').replace('100x100bb', '600x600bb')
    
    if art_url:
        try:
            with urllib.request.urlopen(art_url) as response:
                art_bytes = response.read()
        except Exception:
            pass

    try:
        if ext == '.mp3':
            audio = MP3(str(file_path), ID3=ID3)
            if audio.tags is None: audio.add_tags()
            
            audio.tags.add(TIT2(encoding=3, text=track_info.get('trackName', '')))
            audio.tags.add(TPE1(encoding=3, text=track_info.get('artistName', '')))
            if track_info.get('collectionName'): audio.tags.add(TALB(encoding=3, text=track_info.get('collectionName')))
            if track_info.get('releaseDate'): audio.tags.add(TDRC(encoding=3, text=track_info.get('releaseDate')[:4]))
            if track_info.get('primaryGenreName'): audio.tags.add(TCON(encoding=3, text=track_info.get('primaryGenreName')))
            if track_info.get('trackNumber'): audio.tags.add(TRCK(encoding=3, text=str(track_info.get('trackNumber'))))
            if art_bytes: audio.tags.add(APIC(encoding=3, mime='image/jpeg', type=3, desc='Cover', data=art_bytes))
            if plain_lyrics: audio.tags.add(USLT(encoding=3, lang='eng', desc='', text=plain_lyrics))
            audio.save()

        elif ext == '.flac':
            audio = FLAC(str(file_path))
            audio['title'] = track_info.get('trackName', '')
            audio['artist'] = track_info.get('artistName', '')
            if track_info.get('collectionName'): audio['album'] = track_info.get('collectionName')
            if track_info.get('releaseDate'): audio['date'] = str(track_info.get('releaseDate'))[:4]
            if track_info.get('primaryGenreName'): audio['genre'] = track_info.get('primaryGenreName')
            if track_info.get('trackNumber'): audio['tracknumber'] = str(track_info.get('trackNumber'))
            if plain_lyrics: audio['lyrics'] = plain_lyrics
            if art_bytes:
                pic = Picture()
                pic.type = 3
                pic.mime = "image/jpeg"
                pic.desc = "Cover"
                pic.data = art_bytes
                audio.add_picture(pic)
            audio.save()

        elif ext == '.m4a':
            audio = MP4(str(file_path))
            audio['\xa9nam'] = track_info.get('trackName', '')
            audio['\xa9ART'] = track_info.get('artistName', '')
            if track_info.get('collectionName'): audio['\xa9alb'] = track_info.get('collectionName')
            if track_info.get('releaseDate'): audio['\xa9day'] = str(track_info.get('releaseDate'))[:4]
            if track_info.get('primaryGenreName'): audio['\xa9gen'] = track_info.get('primaryGenreName')
            if track_info.get('trackNumber'): audio['trkn'] = [(track_info.get('trackNumber'), 0)]
            if plain_lyrics: audio['\xa9lyr'] = plain_lyrics
            if art_bytes: audio['covr'] = [MP4Cover(art_bytes, imageformat=MP4Cover.FORMAT_JPEG)]
            audio.save()

    except Exception as e:
        console.print(f"[bold red]Error tagging {file_path}:[/bold red] {e}")

def organize_files(file_path: Path, track_info: Dict[str, Any], output_dir: Path) -> Path:
    """Move audio and its .lrc file into an Artist folder."""
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
                progress.console.print("  [cyan]>[/cyan] [green]Metadata & Cover embedded.[/green]")

                if organize and output_dir:
                    current_path = organize_files(current_path, track, output_dir)
                    progress.console.print(f"  [cyan]>[/cyan] [green]Moved to {current_path.parent.name}/[/green]")
            else:
                progress.console.print("  [cyan]>[/cyan] [yellow]Metadata not found.[/yellow]")
                
            final_paths[song_name] = current_path
            progress.advance(task)
            time.sleep(0.5)
        
    return final_paths