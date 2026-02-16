import os
import re
import json
import requests
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC, USLT, TDRC, TCON

# ==========================================
# CONFIGURATION
# ==========================================
# Your specific path
MUSIC_DIRECTORY = '/home/shadow-scripter/Music'

# ==========================================
# HELPER FUNCTIONS
# ==========================================

def clean_filename(filename):
    """
    Cleans the filename to create a better search query.
    Removes extensions, numbers, and things in brackets.
    """
    # Remove extension
    name = os.path.splitext(filename)[0]
    # Remove leading numbers (e.g., "01. Song")
    name = re.sub(r'^\d+[\.\-\s]*', '', name)
    # Remove things in brackets/parentheses like (Official Audio) or [HQ]
    name = re.sub(r'[\[\(].*?[\]\)]', '', name)
    # Remove extra whitespace
    return name.strip()

def search_itunes(query):
    """
    Searches the Apple iTunes API (No Key Required).
    Returns a dictionary with track details if found.
    """
    try:
        url = "https://itunes.apple.com/search"
        params = {
            "term": query,
            "media": "music",
            "entity": "song",
            "limit": 1
        }
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        
        if data['resultCount'] > 0:
            return data['results'][0]
    except Exception as e:
        print(f"  [!] Error connecting to iTunes: {e}")
    return None

def get_lyrics_ovh(artist, title):
    """
    Fetches lyrics from the public lyrics.ovh API.
    """
    try:
        url = f"https://api.lyrics.ovh/v1/{artist}/{title}"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            return data.get('lyrics', None)
    except Exception:
        pass
    return None

def embed_metadata(file_path, track_data, lyrics=None):
    """
    Writes the metadata to the MP3 file using Mutagen.
    """
    try:
        audio = MP3(file_path, ID3=ID3)
        
        # Initialize tags if they don't exist
        try:
            audio.add_tags()
        except Exception:
            pass

        # 1. Text Tags
        audio.tags.add(TIT2(encoding=3, text=track_data['trackName']))
        audio.tags.add(TPE1(encoding=3, text=track_data['artistName']))
        audio.tags.add(TALB(encoding=3, text=track_data['collectionName']))
        audio.tags.add(TCON(encoding=3, text=track_data['primaryGenreName']))
        
        # 2. Date (Year)
        release_date = track_data.get('releaseDate', '')
        if release_date:
            audio.tags.add(TDRC(encoding=3, text=release_date[:4]))

        # 3. Lyrics (if found)
        if lyrics:
            audio.tags.add(USLT(encoding=3, lang='eng', desc='desc', text=lyrics))

        # 4. Cover Art (High Res)
        # iTunes gives 100x100 by default, we hack the URL to get 600x600
        artwork_url = track_data['artworkUrl100'].replace('100x100', '600x600')
        img_data = requests.get(artwork_url).content
        
        audio.tags.add(APIC(
            encoding=3,
            mime='image/jpeg',
            type=3, # 3 is for front cover
            desc='Cover',
            data=img_data
        ))

        audio.save()
        return True
    except Exception as e:
        print(f"  [!] Failed to save tags: {e}")
        return False

# ==========================================
# MAIN LOGIC
# ==========================================

def main():
    success_log = []
    failed_log = []

    if not os.path.exists(MUSIC_DIRECTORY):
        print(f"Error: Directory not found: {MUSIC_DIRECTORY}")
        return

    print(f"--- Starting Tagger in {MUSIC_DIRECTORY} ---")

    for root, dirs, files in os.walk(MUSIC_DIRECTORY):
        for file in files:
            if file.lower().endswith('.mp3'):
                file_path = os.path.join(root, file)
                search_query = clean_filename(file)
                
                print(f"\nProcessing: {file}")
                print(f"  > Search Query: '{search_query}'")

                # 1. Find Metadata (iTunes)
                track = search_itunes(search_query)
                
                if track:
                    print(f"  > Match: '{track['trackName']}' by '{track['artistName']}'")
                    
                    # 2. Find Lyrics (OVH)
                    print("  > Fetching lyrics...")
                    lyrics = get_lyrics_ovh(track['artistName'], track['trackName'])
                    
                    # 3. Save to File
                    if embed_metadata(file_path, track, lyrics):
                        print("  > [SUCCESS] Tags & Cover Art written.")
                        success_log.append({
                            "original_file": file,
                            "new_title": track['trackName'],
                            "artist": track['artistName'],
                            "lyrics_found": bool(lyrics)
                        })
                    else:
                        print("  > [ERROR] Could not write to file.")
                        failed_log.append(file)
                else:
                    print("  > [NOT FOUND] No results in database.")
                    failed_log.append(file)

    # ==========================================
    # FINAL REPORTS
    # ==========================================
    
    # 1. Success Report (JSON with details)
    if success_log:
        with open('tagged_songs_report.json', 'w') as f:
            json.dump(success_log, f, indent=4)
        print(f"\n[Done] Successfully tagged {len(success_log)} files. See 'tagged_songs_report.json'.")

    # 2. Failure Report (Simple List)
    if failed_log:
        with open('failed_files.txt', 'w') as f:
            f.write("The following files could not be found or tagged:\n")
            for item in failed_log:
                f.write(f"{item}\n")
        print(f"[Done] Failed to tag {len(failed_log)} files. See 'failed_files.txt'.")

if __name__ == "__main__":
    main()