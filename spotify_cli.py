#!/usr/bin/env python3
"""
Spotify-to-MP3 Converter (CLI Edition)
Records Spotify playlists, finds YouTube matches, downloads MP3s, and tags metadata + lyrics.
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

# --- Dependencies Check ---
try:
    import yt_dlp
except ImportError:
    print("❌ yt-dlp not installed. Install with: pip install yt-dlp")
    sys.exit(1)

try:
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, TIT2, TPE1, USLT, APIC
except ImportError:
    print("❌ mutagen not installed. Install with: pip install mutagen")
    sys.exit(1)

# --- 1. CONFIG & PLATFORM ---
IS_LINUX = sys.platform.startswith('linux')

def check_linux_requirements():
    if not IS_LINUX:
        print("⚠️  WARNING: Spotify recording requires Linux with playerctl.")
        return False
    try:
        subprocess.run(["which", "playerctl"], capture_output=True, check=True)
        return True
    except subprocess.CalledProcessError:
        print("⚠️  WARNING: 'playerctl' not found. Install it first (e.g., sudo apt install playerctl).")
        return False

# --- 2. RECORDING (Spotify via playerctl) ---
def get_current_song():
    try:
        title = subprocess.check_output(["playerctl", "--player=spotify", "metadata", "title"], text=True).strip()
        artist = subprocess.check_output(["playerctl", "--player=spotify", "metadata", "artist"], text=True).strip()
        if not title: return None
        return f"{title} - {artist}"
    except subprocess.CalledProcessError:
        return None

def next_song():
    subprocess.run(["playerctl", "--player=spotify", "next"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def record_spotify(output_file):
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
                print("❌ Could not detect Spotify. Is it running and playing?")
                break
            if current_song in seen_songs:
                print(f"\n✓ Loop detected! Playlist recording complete.")
                break
            
            recorded_songs.append(current_song)
            seen_songs.add(current_song)
            print(f"[{len(recorded_songs)}] Recorded: {current_song}")
            
            next_song()
            time.sleep(0.5)
            
    except KeyboardInterrupt:
        print("\n⚠️  Recording stopped by user.")

    if recorded_songs:
        with open(output_file, "w", encoding="utf-8") as f:
            f.write("\n".join(recorded_songs))
        print(f"\n✓ Saved {len(recorded_songs)} songs to '{output_file}'.")

# --- 3. SEARCHING (YouTube) ---
def find_url(song_name):
    ydl_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'default_search': 'ytsearch1',
        'noplaylist': True,
    }
    for _ in range(3):
        try:
            time.sleep(random.uniform(1.0, 3.0))
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(song_name, download=False)
                video = info['entries'][0] if 'entries' in info else info
                url = video.get('webpage_url') or video.get('url')
                if url: return {'song': song_name, 'url': url, 'found': True}
            return {'song': song_name, 'error': 'No results', 'found': False}
        except Exception as e:
            if "429" in str(e): time.sleep(20)
    return {'song': song_name, 'error': 'Max retries exceeded', 'found': False}

def search_youtube(input_file, output_found, output_notfound, max_workers=3):
    if not os.path.exists(input_file):
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

    with open(output_found, "w", encoding="utf-8") as f: f.write("\n".join(found_list))
    with open(output_notfound, "w", encoding="utf-8") as f: f.write("\n".join(not_found_list))

# --- 4. DOWNLOADING (yt-dlp) ---
def download_track(line, output_folder):
    if "|" not in line: return None
    song_name, url = [p.strip() for p in line.split("|", 1)]
    
    # Clean the filename
    safe_name = "".join([c for c in song_name if c.isalnum() or c in (' ', '-', '_', '.', '(', ')')]).strip()[:150]
    output_path = os.path.join(output_folder, safe_name)
    
    if os.path.exists(f"{output_path}.mp3"):
        return output_path + ".mp3"

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': f'{output_path}.%(ext)s',
        'quiet': True,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        return output_path + ".mp3"
    except Exception as e:
        print(f"Download error for {song_name}: {e}")
        return None

def download_songs(input_file, output_folder):
    if not os.path.exists(input_file): return []
    os.makedirs(output_folder, exist_ok=True)
    with open(input_file, "r", encoding="utf-8") as f:
        lines = [l.strip() for l in f if l.strip()]

    print(f"\n✓ Downloading {len(lines)} songs to '{output_folder}'...")
    downloaded_files = []
    for i, line in enumerate(lines):
        print(f"[{i+1}/{len(lines)}] Downloading: {line.split('|')[0]}...")
        filepath = download_track(line, output_folder)
        if filepath:
            downloaded_files.append((line.split('|')[0], filepath))
    return downloaded_files

# --- 5. TAGGING & LYRICS ---
def search_itunes(query):
    try:
        url = f"https://itunes.apple.com/search?term={urllib.parse.quote(query)}&media=music&limit=1"
        with urllib.request.urlopen(url) as response:
            data = json.loads(response.read().decode())
            if data['resultCount'] > 0:
                return data['results'][0]
    except Exception: pass
    return None

def get_synced_lyrics(artist, track, duration_sec):
    try:
        url = f"https://lrclib.net/api/get?artist_name={urllib.parse.quote(artist)}&track_name={urllib.parse.quote(track)}&duration={int(duration_sec)}"
        req = urllib.request.Request(url, headers={'User-Agent': 'SpotifyDownloaderCLI/1.0'})
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode())
            return data.get('syncedLyrics'), data.get('plainLyrics')
    except Exception:
        return None, None

def save_lrc_file(audio_path, synced_lyrics):
    lrc_path = os.path.splitext(audio_path)[0] + ".lrc"
    with open(lrc_path, "w", encoding="utf-8") as f:
        f.write(synced_lyrics)

def embed_metadata(file_path, track_info, plain_lyrics):
    try:
        audio = MP3(file_path, ID3=ID3)
        if audio.tags is None: audio.add_tags()
        
        # Add Title & Artist
        audio.tags.add(TIT2(encoding=3, text=track_info.get('trackName', '')))
        audio.tags.add(TPE1(encoding=3, text=track_info.get('artistName', '')))
        
        # Add High-Res Cover Art
        art_url = track_info.get('artworkUrl100', '').replace('100x100bb', '600x600bb')
        if art_url:
            try:
                with urllib.request.urlopen(art_url) as response:
                    audio.tags.add(APIC(encoding=3, mime='image/jpeg', type=3, desc='Cover', data=response.read()))
            except Exception: pass
            
        # Add Plain Lyrics
        if plain_lyrics:
            audio.tags.add(USLT(encoding=3, lang='eng', desc='', text=plain_lyrics))
            
        audio.save()
    except Exception as e:
        print(f"Error tagging {file_path}: {e}")

def process_metadata(downloaded_files):
    print("\n✓ Fetching Metadata and Lyrics...")
    for song_name, file_path in downloaded_files:
        print(f"Tagging: {song_name}")
        track = search_itunes(song_name)
        if track:
            duration_sec = track.get('trackTimeMillis', 0) / 1000
            synced_lyrics, plain_lyrics = get_synced_lyrics(track['artistName'], track['trackName'], duration_sec)
            
            if synced_lyrics:
                save_lrc_file(file_path, synced_lyrics)
                print("  > [SUCCESS] Synced .lrc lyrics saved.")
            
            embed_metadata(file_path, track, plain_lyrics)
            print("  > [SUCCESS] ID3 Tags & Cover embedded.")
        else:
            print("  > [FAILED] Metadata not found.")
        time.sleep(1) # Be gentle with APIs

# --- 6. MAIN PIPELINE ---
def main():
    parser = argparse.ArgumentParser(description="Spotify Playlist to MP3 Downloader + Tagger")
    parser.add_argument('--record', action='store_true', help='Record Spotify playlist to songs.txt')
    parser.add_argument('--search', action='store_true', help='Search YouTube for songs in songs.txt')
    parser.add_argument('--download', action='store_true', help='Download found songs & apply metadata')
    parser.add_argument('--all', action='store_true', help='Run the complete pipeline')
    
    args = parser.parse_args()
    
    if not any([args.record, args.search, args.download, args.all]):
        parser.print_help()
        sys.exit(0)

    input_list = "songs.txt"
    found_list = "found.txt"
    notfound_list = "not_found.txt"
    out_dir = "songs"

    if args.record or args.all:
        record_spotify(input_list)
        
    if args.search or args.all:
        search_youtube(input_list, found_list, notfound_list)
        
    if args.download or args.all:
        downloaded = download_songs(found_list, out_dir)
        if downloaded:
            process_metadata(downloaded)

if __name__ == "__main__":
    main()