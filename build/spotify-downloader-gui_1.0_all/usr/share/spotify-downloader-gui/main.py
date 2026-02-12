#!/usr/bin/env python3
"""
Spotify-to-MP3 Converter
A CLI tool to record Spotify playlists, find YouTube matches, and download MP3s.
⚠️  Spotify recording requires Linux + playerctl (Spotify Premium recommended for skipping)
"""

import subprocess
import time
import sys
import os
import argparse
import concurrent.futures
import random
import platform
from typing import List, Dict, Optional

# ======================
# PLATFORM DETECTION
# ======================
IS_LINUX = sys.platform.startswith('linux')
IS_WINDOWS = sys.platform.startswith('win')
IS_MAC = sys.platform.startswith('darwin')

def check_linux_requirements():
    """Warn users if trying to record on non-Linux platforms."""
    if not IS_LINUX:
        print(f"⚠️  WARNING: Spotify recording requires Linux with playerctl (DBus/Mpris support).")
        print(f"   Current platform: {platform.system()} ({sys.platform})")
        print(f"   Recording functionality will not work properly on this OS.\n")
        return False
    # Check if playerctl exists
    try:
        subprocess.run(["which", "playerctl"], capture_output=True, check=True)
        return True
    except subprocess.CalledProcessError:
        print("⚠️  WARNING: 'playerctl' not found. Install it first:")
        print("   Ubuntu/Debian: sudo apt install playerctl")
        print("   Fedora: sudo dnf install playerctl")
        print("   Arch: sudo pacman -S playerctl\n")
        return False

# ======================
# SPOTIFY RECORDING (record_spotify.py logic)
# ======================
def get_current_song() -> Optional[str]:
    """Gets the 'Title - Artist' from the running Spotify app via playerctl."""
    try:
        title = subprocess.check_output(
            ["playerctl", "--player=spotify", "metadata", "title"], 
            text=True
        ).strip()
        
        artist = subprocess.check_output(
            ["playerctl", "--player=spotify", "metadata", "artist"], 
            text=True
        ).strip()
        
        if not title:
            return None
            
        return f"{title} - {artist}"
    except subprocess.CalledProcessError:
        return None

def next_song():
    """Tells Spotify to skip to the next song."""
    subprocess.run(["playerctl", "--player=spotify", "next"], 
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def record_spotify(output_file: str, shuffle_off_warning: bool = True):
    """Records a full Spotify playlist by auto-skipping through songs."""
    if shuffle_off_warning:
        print("\n⚠️  IMPORTANT: For accurate recording, ensure in Spotify:")
        print("   • Shuffle: OFF")
        print("   • Repeat: ON (playlist mode)")
        print("   • Premium account recommended (Free tier blocks skipping)\n")
    
    if not check_linux_requirements():
        print("❌ Cannot proceed with recording on this platform.\n")
        sys.exit(1)

    print("--- Spotify Playlist Recorder ---")
    print("1. Open Spotify App and start playing your playlist")
    input("2. Press Enter when music is playing to start recording...\n")

    recorded_songs = []
    seen_songs = set()
    
    print("Recording... (Press Ctrl+C to stop manually)\n")
    
    try:
        while True:
            current_song = get_current_song()
            
            if not current_song:
                print("❌ Could not detect Spotify. Is it running and playing?")
                break

            if current_song in seen_songs:
                print(f"\n✓ Loop detected! Re-encountered: {current_song}")
                print("✓ Playlist recording complete.")
                break
            
            recorded_songs.append(current_song)
            seen_songs.add(current_song)
            print(f"[{len(recorded_songs)}] Recorded: {current_song}")
            
            next_song()
            time.sleep(0.5)  # Allow metadata to update
            
    except KeyboardInterrupt:
        print("\n⚠️  Recording stopped by user.")

    if recorded_songs:
        with open(output_file, "w", encoding="utf-8") as f:
            f.write("\n".join(recorded_songs))
        print(f"\n✓ Success! Saved {len(recorded_songs)} songs to '{output_file}'.")
    else:
        print("\n⚠️  No songs were recorded.")

# ======================
# YOUTUBE SEARCH (search_youtube_urls.py logic)
# ======================
def find_url(song_name: str) -> Dict:
    """
    Finds the YouTube URL by fully processing the first result.
    NO 'extract_flat' means it's slower but 100% accurate.
    """
    try:
        import yt_dlp
    except ImportError:
        return {'song': song_name, 'error': 'yt-dlp not installed. Run: pip install yt-dlp', 'found': False}
    
    ydl_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'default_search': 'ytsearch1',
        'noplaylist': True,
    }
    
    for attempt in range(3):
        try:
            time.sleep(random.uniform(1.0, 3.0))
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(song_name, download=False)
                
                if 'entries' in info:
                    video = info['entries'][0]
                else:
                    video = info

                url = video.get('webpage_url') or video.get('url')
                title = video.get('title', 'Unknown Title')
                
                if url:
                    return {'song': song_name, 'url': url, 'found': True, 'title': title}
            
            return {'song': song_name, 'error': 'No results found', 'found': False}
            
        except Exception as e:
            error_msg = str(e)
            if "429" in error_msg:
                print(f"!! Rate Limit (HTTP 429). Sleeping 20s...")
                time.sleep(20)
            elif "Sign in" in error_msg:
                return {'song': song_name, 'error': 'Age restricted/Login required', 'found': False}
            
            if attempt == 2:
                return {'song': song_name, 'error': error_msg, 'found': False}

    return {'song': song_name, 'error': 'Max retries exceeded', 'found': False}

def search_youtube(input_file: str, output_found: str, output_notfound: str, max_workers: int):
    """Searches YouTube for each song and saves results to files."""
    try:
        import yt_dlp
    except ImportError:
        print("❌ yt-dlp not installed. Install with: pip install yt-dlp")
        sys.exit(1)
    
    if not os.path.exists(input_file):
        print(f"❌ Error: Could not find '{input_file}'.")
        sys.exit(1)

    with open(input_file, "r", encoding="utf-8") as f:
        songs = [line.strip() for line in f if line.strip()]

    print(f"✓ Loaded {len(songs)} songs from '{input_file}'")
    print(f"✓ Starting robust YouTube search ({max_workers} threads)...")
    print("   Note: This is intentionally slow for accuracy (~5-10 mins for 170 songs)\n")

    found_list = []
    not_found_list = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_song = {executor.submit(find_url, song): song for song in songs}
        
        for i, future in enumerate(concurrent.futures.as_completed(future_to_song)):
            result = future.result()
            progress = f"[{i+1}/{len(songs)}]"
            
            if result['found']:
                found_list.append(f"{result['song']} | {result['url']}")
                print(f"{progress} \033[92mFOUND\033[0m: {result['song']}")
            else:
                not_found_list.append(result['song'])
                print(f"{progress} \033[91mFAILED\033[0m: {result['song']} ({result.get('error', 'unknown error')})")

    print("\n✓ Writing results...")
    with open(output_found, "w", encoding="utf-8") as f:
        f.write("\n".join(found_list))
        
    with open(output_notfound, "w", encoding="utf-8") as f:
        f.write("\n".join(not_found_list))

    print(f"✓ Saved {len(found_list)} found songs to '{output_found}'")
    print(f"✓ Saved {len(not_found_list)} unfound songs to '{output_notfound}'")

# ======================
# DOWNLOADING (download_songs.py logic)
# ======================
def download_track(line: str, output_folder: str) -> bool:
    """Downloads a single track. Returns True if successful."""
    try:
        import yt_dlp
    except ImportError:
        print("❌ yt-dlp not installed. Install with: pip install yt-dlp")
        return False
    
    if "|" not in line:
        return False

    parts = line.split("|", 1)
    song_name = parts[0].strip()
    url = parts[1].strip()

    # Sanitize filename
    safe_name = "".join([c for c in song_name if c.isalpha() or c.isdigit() or c in (' ', '-', '_', '.', '(', ')')])
    safe_name = safe_name.strip()[:150]  # Limit length to avoid OS issues
    
    output_path = os.path.join(output_folder, safe_name)
    
    # Skip if already exists
    if os.path.exists(f"{output_path}.mp3"):
        print(f"[SKIP] Already exists: {safe_name}")
        return True

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': f'{output_path}.%(ext)s',
        'quiet': True,
        'no_warnings': True,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
    }

    try:
        print(f"Downloading: {safe_name}...", end=" ", flush=True)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        print(f"\033[92m✓\033[0m")
        return True
    except Exception as e:
        print(f"\033[91m✗ ERROR\033[0m: {str(e)[:80]}")
        return False

def download_songs(input_file: str, output_folder: str):
    """Downloads all songs from the found_songs.txt file."""
    try:
        import yt_dlp
    except ImportError:
        print("❌ yt-dlp not installed. Install with: pip install yt-dlp")
        sys.exit(1)
    
    # Check for ffmpeg
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("⚠️  WARNING: ffmpeg not found. Required for MP3 conversion.")
        print("   Install it first:")
        print("   Ubuntu/Debian: sudo apt install ffmpeg")
        print("   macOS: brew install ffmpeg")
        print("   Windows: https://ffmpeg.org/download.html\n")
        cont = input("Continue anyway? (y/n): ").strip().lower()
        if cont != 'y':
            sys.exit(1)

    if not os.path.exists(input_file):
        print(f"❌ Error: '{input_file}' not found.")
        sys.exit(1)

    if not os.path.exists(output_folder):
        os.makedirs(output_folder)
        print(f"✓ Created output folder: {output_folder}")

    with open(input_file, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]

    total = len(lines)
    print(f"✓ Found {total} songs to download")
    print("-" * 50)

    success_count = 0
    for i, line in enumerate(lines):
        print(f"[{i+1}/{total}] ", end="")
        if download_track(line, output_folder):
            success_count += 1

    print("-" * 50)
    print(f"✓ Completed! {success_count}/{total} songs downloaded to '{output_folder}'")

# ======================
# MAIN CLI ENTRY POINT
# ======================
def main():
    parser = argparse.ArgumentParser(
        description="Spotify-to-MP3 Converter: Record playlists → Find YouTube matches → Download MP3s",
        epilog="⚠️  Recording requires Linux + playerctl. Downloading requires yt-dlp + ffmpeg.",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # RECORD command
    record_parser = subparsers.add_parser('record', help='Record Spotify playlist (Linux only)')
    record_parser.add_argument('-o', '--output', default='my_playlist_songs.txt',
                             help='Output file for song list (default: my_playlist_songs.txt)')
    record_parser.add_argument('--no-warnings', action='store_true',
                             help='Skip shuffle/repeat warnings')
    
    # SEARCH command
    search_parser = subparsers.add_parser('search', help='Search YouTube for songs from file')
    search_parser.add_argument('-i', '--input', default='my_playlist_songs.txt',
                             help='Input file with song list (default: my_playlist_songs.txt)')
    search_parser.add_argument('-f', '--found', default='found_songs.txt',
                             help='Output file for found songs (default: found_songs.txt)')
    search_parser.add_argument('-n', '--notfound', default='not_found.txt',
                             help='Output file for unfound songs (default: not_found.txt)')
    search_parser.add_argument('-w', '--workers', type=int, default=3,
                             help='Max concurrent searches (default: 3, lower = more reliable)')
    
    # DOWNLOAD command
    download_parser = subparsers.add_parser('download', help='Download found songs as MP3s')
    download_parser.add_argument('-i', '--input', default='found_songs.txt',
                               help='Input file with song URLs (default: found_songs.txt)')
    download_parser.add_argument('-o', '--output', default='songs',
                               help='Output folder for MP3s (default: songs)')
    
    # ALL-IN-ONE command
    all_parser = subparsers.add_parser('all', help='Run all steps sequentially (Linux only)')
    all_parser.add_argument('-p', '--playlist', default='my_playlist_songs.txt',
                          help='Song list file (default: my_playlist_songs.txt)')
    all_parser.add_argument('-f', '--found', default='found_songs.txt',
                          help='Found songs file (default: found_songs.txt)')
    all_parser.add_argument('-n', '--notfound', default='not_found.txt',
                          help='Not found file (default: not_found.txt)')
    all_parser.add_argument('-d', '--download-folder', default='songs',
                          help='MP3 output folder (default: songs)')
    all_parser.add_argument('-w', '--workers', type=int, default=3,
                          help='Search workers (default: 3)')
    
    # VERSION
    parser.add_argument('-v', '--version', action='version',
                      version='spotify-to-mp3 1.0 (GitHub Edition)')

    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        print("\n💡 Quick start examples:")
        print("   python spotify_to_mp3.py record          # Record from Spotify (Linux)")
        print("   python spotify_to_mp3.py search          # Find YouTube URLs")
        print("   python spotify_to_mp3.py download        # Download MP3s")
        print("   python spotify_to_mp3.py all             # Run all steps (Linux)")
        sys.exit(0)

    # Execute commands
    if args.command == 'record':
        record_spotify(args.output, not args.no_warnings)
    
    elif args.command == 'search':
        search_youtube(args.input, args.found, args.notfound, args.workers)
    
    elif args.command == 'download':
        download_songs(args.input, args.output)
    
    elif args.command == 'all':
        print("=== STEP 1: RECORD SPOTIFY PLAYLIST ===")
        record_spotify(args.playlist, True)
        
        print("\n=== STEP 2: SEARCH YOUTUBE ===")
        search_youtube(args.playlist, args.found, args.notfound, args.workers)
        
        print("\n=== STEP 3: DOWNLOAD MP3s ===")
        download_songs(args.found, args.download_folder)
        
        print("\n✓✓✓ ALL STEPS COMPLETED ✓✓✓")

if __name__ == "__main__":
    main()