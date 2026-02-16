import os
import yt_dlp

# --- CONFIGURATION ---
INPUT_FILE = "found_songs.txt"
OUTPUT_FOLDER = "songs"

def download_track(line):
    """
    Downloads a single track. Returns True if successful.
    """
    if "|" not in line:
        return False

    parts = line.split("|")
    song_name = parts[0].strip()
    url = parts[1].strip()

    # Sanitize filename: Remove characters that break file systems
    safe_name = "".join([c for c in song_name if c.isalpha() or c.isdigit() or c in (' ', '-', '_', '.', '(', ')')])
    safe_name = safe_name.strip()
    
    # Define full path
    output_path = os.path.join(OUTPUT_FOLDER, safe_name)
    
    # Check if file already exists
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
        print(f"Downloading: {safe_name}...")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        print(f"\033[92m[DONE]\033[0m {safe_name}") # Green text for success
        return True
    except Exception as e:
        print(f"\033[91m[ERROR]\033[0m {safe_name}: {e}") # Red text for error
        return False

def main():
    # 1. Create Folder
    if not os.path.exists(OUTPUT_FOLDER):
        os.makedirs(OUTPUT_FOLDER)
        print(f"Created folder: {OUTPUT_FOLDER}")

    # 2. Read File
    if not os.path.exists(INPUT_FILE):
        print(f"Error: {INPUT_FILE} not found.")
        return

    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]

    total = len(lines)
    print(f"Found {total} songs. Starting sequential download...")
    print("---------------------------------------------------")

    # 3. Loop through songs one by one
    for i, line in enumerate(lines):
        print(f"[{i+1}/{total}] ", end="")
        download_track(line)

    print("\n---------------------------------------------------")
    print("All downloads complete! Check the 'songs' folder.")

if __name__ == "__main__":
    main()