import concurrent.futures
import yt_dlp
import os
import time
import random

# --- CONFIGURATION ---
INPUT_FILE = "my_playlist_songs.txt"
MAX_WORKERS = 3   # Low number to ensure accuracy and prevent blocking

def find_url(song_name):
    """
    Finds the YouTube URL by fully processing the first result.
    NO 'extract_flat' means it's slower but 100% accurate.
    """
    ydl_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'default_search': 'ytsearch1', # Search and download metadata for result #1
        'noplaylist': True,
    }
    
    # Retry logic
    for attempt in range(3):
        try:
            # Random sleep to mimic human behavior
            time.sleep(random.uniform(1.0, 3.0))
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # This now actually grabs the full video data, ensuring it exists
                info = ydl.extract_info(song_name, download=False)
                
                if 'entries' in info:
                    # Search returned a list (even if length 1)
                    video = info['entries'][0]
                else:
                    # Sometimes standard search returns the video directly
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

def main():
    if not os.path.exists(INPUT_FILE):
        print(f"Error: Could not find '{INPUT_FILE}'.")
        return

    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        songs = [line.strip() for line in f if line.strip()]

    print(f"Loaded {len(songs)} songs.")
    print(f"Starting Robust Search ({MAX_WORKERS} threads)...")
    print("Note: This will be slower but accurate. (~5-10 mins for 170 songs)")

    found_list = []
    not_found_list = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_song = {executor.submit(find_url, song): song for song in songs}
        
        for i, future in enumerate(concurrent.futures.as_completed(future_to_song)):
            result = future.result()
            progress = f"[{i+1}/{len(songs)}]"
            
            if result['found']:
                # Save as: Song Name | URL
                found_list.append(f"{result['song']} | {result['url']}")
                print(f"{progress} \033[92mFOUND\033[0m: {result['song']}")
            else:
                not_found_list.append(result['song'])
                print(f"{progress} \033[91mFAILED\033[0m: {result['song']} ({result.get('error')})")

    print("\nWriting files...")
    with open("found_songs.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(found_list))
        
    with open("not_found.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(not_found_list))

    print("Done.")

if __name__ == "__main__":
    main()