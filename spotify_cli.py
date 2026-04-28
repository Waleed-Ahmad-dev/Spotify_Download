#!/usr/bin/env python3
"""
Spotify-to-MP3 Converter (CLI Edition)
Records Spotify playlists, finds YouTube matches, downloads MP3s, tags metadata + lyrics,
features a smart duplicate remover, applies audio volume normalization, and generates M3U playlists.
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
        print("❌ FFmpeg is required but not found. Install it:\n"
              "   Linux: sudo apt install ffmpeg\n"
              "   macOS: brew install ffmpeg\n"
              "   Windows: https://www.gyan.dev/ffmpeg/builds/")
        return False

def check_linux_requirements() -> bool:
    """Check if playerctl is available for recording (Linux only)."""
    if not IS_LINUX:
        print("⚠️  WARNING: Spotify recording requires Linux with playerctl.")
        return False
    try:
        subprocess.run(["which", "playerctl"], capture_output=True, check=True)
        return True
    except subprocess.CalledProcessError:
        print("⚠️  WARNING: 'playerctl' not found. Install it (e.g., sudo apt install playerctl).")
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
        print("❌ Cannot proceed with recording on this platform.")
        sys.exit(1)

    print("\n--- Spotify Playlist Recorder ---")
    input("Press Enter when music is playing in Spotify to start recording...\n")

    recorded_songs = []
    seen_songs = set()
    print("Recording... (Press Ctrl+C to stop manually)\n")
    try:
        while True:
            current_song = get_current_song()
            if not current_song:
                time.sleep(2)
                continue

            if current_song in seen_songs and len(recorded_songs) > 0:
                print(f"\n✓ Loop detected! Recorded {len(recorded_songs)} songs.")
                break

            recorded_songs.append(current_song)
            seen_songs.add(current_song)
            print(f"[{len(recorded_songs)}] Recorded: {current_song}")

            next_song()

            start = time.time()
            while time.time() - start < 15:
                time.sleep(0.8)
                if get_current_song() != current_song:
                    break
            else:
                print("⚠️  Timeout waiting for next song – playlist may have ended.")
                break
    except KeyboardInterrupt:
        print("\n⚠️  Recording stopped by user.")

    if recorded_songs:
        with open(output_file, "w", encoding="utf-8") as f:
            f.write("\n".join(recorded_songs))
        print(f"\n✓ Saved {len(recorded_songs)} songs to '{output_file}'.")

# --- 3. SEARCHING (YouTube) ---
def find_url(song_name: str) -> Dict[str, Any]:
    """Search YouTube for the song using optimized flat extraction."""
    query = f"{song_name} official audio"
    ydl_opts = {
        'extract_flat': True,  # Massive speedup: skips downloading video manifests
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
        print(f"❌ Error: '{input_file}' not found.")
        sys.exit(1)

    with open(input_file, "r", encoding="utf-8") as f:
        songs = [line.strip() for line in f if line.strip()]

    print(f"\n✓ Searching YouTube for {len(songs)} songs...")
    found_list, not_found_list = [], []

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_song = {executor.submit(find_url, song): song for song in songs}
        for i, future in enumerate(concurrent.futures.as_completed(future_to_song)):
            res = future.result()
            if res['found']:
                found_list.append(f"{res['song']} | {res['url']}")
                print(f"[{i+1}/{len(songs)}] 🟢 FOUND: {res['song']}")
            else:
                not_found_list.append(res['song'])
                print(f"[{i+1}/{len(songs)}] 🔴 FAILED: {res['song']}")

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
            print(f"⚠️ Downloaded, but couldn't locate file at: {expected_file}")
            return None
    except Exception as e:
        print(f"Download error for {song_name}: {e}")
        return None

def download_songs(input_file: Path, output_folder: Path, quality: str = '192', max_workers: int = 2, normalize: bool = False) -> List[Tuple[str, Path]]:
    """Download all found songs using parallel workers."""
    if not input_file.exists():
        return []
    output_folder.mkdir(parents=True, exist_ok=True)

    with open(input_file, "r", encoding="utf-8") as f:
        lines = [l.strip() for l in f if l.strip()]

    print(f"\n✓ Downloading {len(lines)} songs to '{output_folder}' (quality {quality}kbps)...")
    if normalize:
        print("  ↳ Audio volume normalization enabled (-14 LUFS)")
        
    downloaded_files = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_line = {executor.submit(download_track, line, output_folder, quality, normalize): line for line in lines}
        for future in concurrent.futures.as_completed(future_to_line):
            filepath = future.result()
            if filepath:
                song_name = future_to_line[future].split('|')[0].strip()
                downloaded_files.append((song_name, filepath))

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
        print(f"Error tagging {file_path}: {e}")

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
    print("\n✓ Fetching Metadata and Lyrics...")
    final_paths = {}
    
    for song_name, file_path in downloaded_files:
        print(f"Tagging: {song_name}")
        track = search_itunes(song_name)
        current_path = file_path
        
        if track:
            duration_sec = track.get('trackTimeMillis', 0) / 1000
            synced_lyrics, plain_lyrics = get_synced_lyrics(track['artistName'], track['trackName'], duration_sec)

            if synced_lyrics:
                save_lrc_file(current_path, synced_lyrics)
                print("  > [SUCCESS] Synced .lrc lyrics saved.")

            embed_metadata(current_path, track, plain_lyrics)
            print("  > [SUCCESS] ID3 Tags & Cover embedded.")

            if organize and output_dir:
                current_path = organize_files(current_path, track, output_dir)
                print(f"  > [SUCCESS] Moved to {current_path.parent}")
        else:
            print("  > [FAILED] Metadata not found.")
            
        final_paths[song_name] = current_path
        time.sleep(0.5)
        
    return final_paths

def generate_m3u(playlist_name: str, output_dir: Path, original_order_file: Path, final_paths: Dict[str, Path]) -> None:
    """Generate an .m3u playlist maintaining the original order of the search file."""
    if not final_paths:
        return
        
    m3u_path = output_dir / f"{sanitize_filename(playlist_name)}.m3u"
    print(f"\n--- Generating Playlist: {m3u_path.name} ---")

    try:
        with open(original_order_file, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
    except Exception as e:
        print(f"❌ Could not read order file: {e}")
        return

    playlist_entries = []
    for line in lines:
        if "|" in line:
            song_name = line.split("|", 1)[0].strip()
            if song_name in final_paths:
                # Get the path relative to where the .m3u file is saved (output_dir)
                try:
                    rel_path = final_paths[song_name].relative_to(output_dir)
                    playlist_entries.append(str(rel_path))
                except ValueError:
                    # Fallback to absolute path if relative fails for some reason
                    playlist_entries.append(str(final_paths[song_name]))

    if playlist_entries:
        with open(m3u_path, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            for entry in playlist_entries:
                f.write(f"{entry}\n")
        print(f"✓ Playlist saved successfully with {len(playlist_entries)} tracks.")
    else:
        print("⚠️ No tracks were found to add to the playlist.")

# --- 6. DUPLICATE REMOVAL ---
def remove_duplicates(directory: Path) -> None:
    """Scans a directory for MP3s and removes duplicates based on Title, Artist, and Lyrics."""
    print(f"\n--- Scanning '{directory}' for duplicates ---")
    song_groups = {}
    
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
            
        except Exception as e:
            print(f"⚠️ Could not read tags for {filepath.name}: {e}")

    removed_count = 0
    for (title, artist), files in song_groups.items():
        if len(files) > 1:
            files.sort(key=lambda x: (x['has_lyrics'], x['size']), reverse=True)
            best_file = files[0]
            duplicates = files[1:]
            
            for dup in duplicates:
                print(f"🗑️ Removing duplicate: {dup['path'].name}")
                print(f"   (Keeping: {best_file['path'].name})")
                try:
                    dup['path'].unlink()
                    lrc_path = dup['path'].with_suffix('.lrc')
                    if lrc_path.exists():
                        lrc_path.unlink()
                    removed_count += 1
                except Exception as e:
                    print(f"❌ Failed to delete {dup['path'].name}: {e}")

    print(f"\n✓ Deduplication complete. Removed {removed_count} duplicate files.")

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
            print(f"❌ Error: Directory '{dedupe_dir}' not found.")
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
            print(f"✓ Found file '{found_file}' exists. Skipping search (--resume).")
        else:
            search_youtube(input_file, found_file, notfound_file, max_workers=args.workers)

    if args.download or args.all:
        if not check_ffmpeg():
            sys.exit(1)

        if not found_file.exists() or found_file.stat().st_size == 0:
            print("❌ No URLs found. Run --search first or remove --resume.")
            sys.exit(1)

        downloaded = download_songs(found_file, out_dir, quality=args.quality, max_workers=2, normalize=should_normalize)
        if downloaded:
            final_paths = process_metadata(downloaded, organize=args.organize, output_dir=out_dir)
            
            # Generate the playlist if requested
            if args.playlist:
                generate_m3u(args.playlist, out_dir, found_file, final_paths)
                
        else:
            print("❌ No songs were downloaded.")

if __name__ == "__main__":
    main()