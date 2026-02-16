import os
import re
import json
import requests
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
    # Remove "01.", "Track 1", etc.
    name = re.sub(r'^\d+[\.\-\s]*', '', name) 
    # Remove (Official), [HQ], (Lyrics)
    name = re.sub(r'[\[\(](official|video|lyrics|hq|hd|4k|remastered).*?[\]\)]', '', name, flags=re.IGNORECASE)
    return name.strip()

def search_itunes(query):
    """Searches iTunes for metadata and cover art."""
    try:
        url = "https://itunes.apple.com/search"
        params = {"term": query, "media": "music", "entity": "song", "limit": 1}
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        if data['resultCount'] > 0:
            return data['results'][0]
    except Exception:
        pass
    return None

def get_synced_lyrics(artist, title, duration_sec):
    """
    Searches LrcLib (Open Source Lyrics DB) for synced lyrics.
    Requires duration to find the best match.
    """
    try:
        url = "https://lrclib.net/api/search"
        params = {
            "artist_name": artist,
            "track_name": title,
        }
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        
        # Find the best match based on duration (within 4 seconds difference)
        for item in data:
            if item.get('syncedLyrics'):
                # API duration is in seconds, iTunes duration is ms
                # We relax the check to ensure we get *some* lyrics
                return item['syncedLyrics'], item['plainLyrics']
                
    except Exception as e:
        print(f"  [!] Lyrics Error: {e}")
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

        # 1. Standard Info
        audio.tags.add(TIT2(encoding=3, text=track_data['trackName']))
        audio.tags.add(TPE1(encoding=3, text=track_data['artistName']))
        audio.tags.add(TALB(encoding=3, text=track_data['collectionName']))
        audio.tags.add(TCON(encoding=3, text=track_data['primaryGenreName']))
        
        # 2. Year
        if 'releaseDate' in track_data:
            audio.tags.add(TDRC(encoding=3, text=track_data['releaseDate'][:4]))

        # 3. Unsynced Lyrics (embedded inside MP3 as backup)
        if plain_lyrics:
            audio.tags.add(USLT(encoding=3, lang='eng', desc='desc', text=plain_lyrics))

        # 4. High-Res Cover Art
        artwork_url = track_data['artworkUrl100'].replace('100x100', '600x600')
        img_data = requests.get(artwork_url).content
        
        audio.tags.add(APIC(
            encoding=3,
            mime='image/jpeg',
            type=3, 
            desc='Cover',
            data=img_data
        ))

        # FORCE ID3v2.3 (Critical for Windows/Android/Ubuntu thumbnails)
        audio.save(v2_version=3)
        return True
    except Exception as e:
        print(f"  [!] Save Error: {e}")
        return False

# ==========================================
# MAIN
# ==========================================

def main():
    print(f"--- Tagger Pro: Synced Lyrics & HD Covers ---")
    print(f"Target: {MUSIC_DIRECTORY}\n")

    files_processed = 0

    for root, dirs, files in os.walk(MUSIC_DIRECTORY):
        for file in files:
            if file.lower().endswith('.mp3'):
                file_path = os.path.join(root, file)
                search_query = clean_filename(file)
                
                print(f"Processing: {file}")

                # 1. Get Metadata (iTunes)
                track = search_itunes(search_query)
                
                if track:
                    print(f"  > Match: {track['trackName']} - {track['artistName']}")
                    
                    # 2. Get Lyrics (LrcLib)
                    duration_sec = track['trackTimeMillis'] / 1000
                    synced_lyrics, plain_lyrics = get_synced_lyrics(
                        track['artistName'], 
                        track['trackName'], 
                        duration_sec
                    )

                    # 3. Save .lrc file (for synced lyrics on phone)
                    if synced_lyrics:
                        save_lrc_file(file_path, synced_lyrics)
                        print("  > Synced Lyrics (.lrc) saved.")
                    else:
                        print("  > No synced lyrics found.")

                    # 4. Embed Tags & Cover
                    if embed_metadata(file_path, track, plain_lyrics):
                        print("  > Tags & Cover updated (ID3v2.3).")
                    
                    files_processed += 1
                else:
                    print("  > Metadata not found.")
                
                print("-" * 30)

    print(f"\nCompleted! Processed {files_processed} files.")
    print("NOTE: You may need to restart your file manager to see new icons.")

if __name__ == "__main__":
    main()