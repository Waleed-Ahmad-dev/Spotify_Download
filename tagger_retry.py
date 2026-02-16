import os
import re
import json
import requests
import time  # Added for delays
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC, USLT, TDRC, TCON

# ==========================================
# CONFIGURATION
# ==========================================
MUSIC_DIRECTORY = '/home/shadow-scripter/Music'

# ==========================================
# HELPER FUNCTIONS
# ==========================================

def clean_filename(filename):
    """Cleans filename for better search results."""
    name = os.path.splitext(filename)[0]
    name = re.sub(r'^\d+[\.\-\s]*', '', name) 
    name = re.sub(r'[\[\(](official|video|lyrics|hq|hd|4k|remastered).*?[\]\)]', '', name, flags=re.IGNORECASE)
    return name.strip()

def search_itunes(query):
    """Searches iTunes for metadata and cover art."""
    try:
        url = "https://itunes.apple.com/search"
        params = {"term": query, "media": "music", "entity": "song", "limit": 1}
        # Increased timeout to 20s to prevent read errors
        response = requests.get(url, params=params, timeout=20)
        data = response.json()
        if data['resultCount'] > 0:
            return data['results'][0]
    except Exception:
        pass
    return None

def get_synced_lyrics(artist, title, duration_sec):
    """
    Searches LrcLib for synced lyrics.
    """
    try:
        url = "https://lrclib.net/api/search"
        params = {
            "artist_name": artist,
            "track_name": title,
        }
        # Increased timeout to 20s
        response = requests.get(url, params=params, timeout=20)
        data = response.json()
        
        for item in data:
            if item.get('syncedLyrics'):
                return item['syncedLyrics'], item['plainLyrics']
                
    except Exception as e:
        print(f"  [!] Lyrics API Error: {e}")
    return None, None

def save_lrc_file(mp3_path, synced_lyrics):
    """Saves a .lrc file next to the mp3."""
    if not synced_lyrics:
        return False
    
    lrc_path = os.path.splitext(mp3_path)[0] + ".lrc"
    try:
        with open(lrc_path, "w", encoding="utf-8") as f:
            f.write(synced_lyrics)
        return True
    except Exception:
        return False

def embed_metadata(file_path, track_data, plain_lyrics=None):
    """Embeds metadata with ID3v2.3 compatibility."""
    try:
        audio = MP3(file_path, ID3=ID3)
        try:
            audio.add_tags()
        except Exception:
            pass

        audio.tags.add(TIT2(encoding=3, text=track_data['trackName']))
        audio.tags.add(TPE1(encoding=3, text=track_data['artistName']))
        audio.tags.add(TALB(encoding=3, text=track_data['collectionName']))
        audio.tags.add(TCON(encoding=3, text=track_data['primaryGenreName']))
        
        if 'releaseDate' in track_data:
            audio.tags.add(TDRC(encoding=3, text=track_data['releaseDate'][:4]))

        if plain_lyrics:
            audio.tags.add(USLT(encoding=3, lang='eng', desc='desc', text=plain_lyrics))

        artwork_url = track_data['artworkUrl100'].replace('100x100', '600x600')
        img_data = requests.get(artwork_url, timeout=20).content
        
        audio.tags.add(APIC(
            encoding=3,
            mime='image/jpeg',
            type=3, 
            desc='Cover',
            data=img_data
        ))

        audio.save(v2_version=3)
        return True
    except Exception as e:
        print(f"  [!] Save Error: {e}")
        return False

# ==========================================
# MAIN
# ==========================================

def main():
    print(f"--- Tagger Retry Mode ---")
    print(f"Target: {MUSIC_DIRECTORY}")
    print("Skipping files that already have .lrc lyrics...\n")

    files_processed = 0

    for root, dirs, files in os.walk(MUSIC_DIRECTORY):
        for file in files:
            if file.lower().endswith('.mp3'):
                file_path = os.path.join(root, file)
                lrc_path = os.path.splitext(file_path)[0] + ".lrc"
                
                # CHECK: If .lrc file exists, we assume it's done. SKIP IT.
                if os.path.exists(lrc_path):
                    continue

                search_query = clean_filename(file)
                print(f"Retrying: {file}")

                # 1. Get Metadata
                track = search_itunes(search_query)
                
                if track:
                    print(f"  > Match: {track['trackName']}")
                    
                    # 2. Get Lyrics
                    duration_sec = track['trackTimeMillis'] / 1000
                    synced_lyrics, plain_lyrics = get_synced_lyrics(
                        track['artistName'], 
                        track['trackName'], 
                        duration_sec
                    )

                    # 3. Save .lrc file
                    if synced_lyrics:
                        save_lrc_file(file_path, synced_lyrics)
                        print("  > [SUCCESS] Synced Lyrics fetched.")
                    else:
                        print("  > [FAILED] Still no lyrics found.")

                    # 4. Embed Tags (Just in case they weren't updated correctly before)
                    embed_metadata(file_path, track, plain_lyrics)
                    
                    files_processed += 1
                else:
                    print("  > Metadata not found.")
                
                print("-" * 30)
                
                # SLEEP: Pause for 2 seconds to respect API limits and prevent timeouts
                time.sleep(2)

    print(f"\nRetry pass completed! Processed {files_processed} files.")

if __name__ == "__main__":
    main()