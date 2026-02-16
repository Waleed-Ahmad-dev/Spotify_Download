import subprocess
import time
import sys

def get_current_song():
    """Gets the 'Title - Artist' from the running Spotify app via playerctl."""
    try:
        # Get Title
        title = subprocess.check_output(
            ["playerctl", "--player=spotify", "metadata", "title"], 
            text=True
        ).strip()
        
        # Get Artist
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
    subprocess.run(["playerctl", "--player=spotify", "next"])

def main():
    print("--- Spotify Playlist Recorder (Linux) ---")
    print("1. Open Spotify App.")
    print("2. Start playing your playlist (Shuffle: OFF, Repeat: ON).")
    input("Press Enter when the music is playing to start recording...")

    recorded_songs = []
    seen_songs = set()
    
    print("\nRecording... (Press Ctrl+C to stop manually)")
    
    try:
        while True:
            # 1. Get metadata
            current_song = get_current_song()
            
            if not current_song:
                print("Could not detect Spotify. Is it running?")
                break

            # 2. Check if we've finished the loop
            if current_song in seen_songs:
                print(f"\nLoop detected! Re-encountered: {current_song}")
                print("Playlist recording complete.")
                break
            
            # 3. Save and Print
            recorded_songs.append(current_song)
            seen_songs.add(current_song)
            print(f"[{len(recorded_songs)}] Recorded: {current_song}")
            
            # 4. Press Next
            next_song()
            
            # 5. Small wait to let Spotify load metadata (Adjust if your PC is slow)
            time.sleep(0.5)
            
    except KeyboardInterrupt:
        print("\nRecording stopped by user.")

    # Save to file
    filename = "my_playlist_songs1.txt"
    with open(filename, "w", encoding="utf-8") as f:
        f.write("\n".join(recorded_songs))
        
    print(f"\nSuccess! Saved {len(recorded_songs)} songs to '{filename}'.")

if __name__ == "__main__":
    main()