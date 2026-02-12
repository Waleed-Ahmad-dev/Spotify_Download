#!/usr/bin/env python3
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import threading
import queue
import time
import os
import main  # Import the existing logic

class SpotifyDownloaderApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Spotify Downloader GUI")
        self.root.geometry("600x500")

        # Style
        self.style = ttk.Style()
        self.style.theme_use('clam')

        # Tabs
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(expand=True, fill='both', padx=10, pady=10)

        self.record_frame = ttk.Frame(self.notebook)
        self.search_frame = ttk.Frame(self.notebook)
        self.download_frame = ttk.Frame(self.notebook)

        self.notebook.add(self.record_frame, text="1. Record")
        self.notebook.add(self.search_frame, text="2. Search")
        self.notebook.add(self.download_frame, text="3. Download")

        # Initialize Tabs
        self.init_record_tab()
        self.init_search_tab()
        self.init_download_tab()

        # Shared Data
        self.recorded_songs = []
        self.is_recording = False
        self.log_queue = queue.Queue()

        # Start periodic GUI updates for thread safety
        self.process_log_queue()

    def process_log_queue(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                # Handle log messages if we have a global log area, 
                # or verify which tab sent it. For now each tab handles its own printing mostly.
                if isinstance(msg, dict):
                    if msg['type'] == 'search_progress':
                        self.search_progress_var.set(msg['value'])
                    elif msg['type'] == 'download_progress':
                        self.download_progress_var.set(msg['value'])
        except queue.Empty:
            pass
        self.root.after(100, self.process_log_queue)

    # ==========================
    # RECORD TAB
    # ==========================
    def init_record_tab(self):
        frame = self.record_frame
        
        # Instructions
        lbl = ttk.Label(frame, text="Record from Spotify (Linux Only)", font=('Helvetica', 12, 'bold'))
        lbl.pack(pady=10)
        
        info = ttk.Label(frame, text="1. Open Spotify and play your playlist.\n2. Ensure Shuffle is OFF and Repeat is ON.\n3. Click 'Start Recording'.", justify='center')
        info.pack(pady=5)

        # Controls
        self.btn_record = ttk.Button(frame, text="Start Recording", command=self.toggle_recording)
        self.btn_record.pack(pady=10)

        self.btn_save_record = ttk.Button(frame, text="Save to File", command=self.save_recorded_songs, state='disabled')
        self.btn_save_record.pack(pady=5)

        # List of recorded songs
        self.record_listbox = tk.Listbox(frame, width=50, height=15)
        self.record_listbox.pack(pady=10, padx=10, expand=True, fill='both')

        self.val_record_status = tk.StringVar(value="Ready")
        self.lbl_record_status = ttk.Label(frame, textvariable=self.val_record_status, foreground="blue")
        self.lbl_record_status.pack(pady=5)

    def toggle_recording(self):
        if not self.is_recording:
            # check requirements
            if not main.check_linux_requirements():
                messagebox.showerror("Error", "Linux with playerctl required!")
                return
            
            self.is_recording = True
            self.btn_record.config(text="Stop Recording")
            self.val_record_status.set("Recording... (Press Stop to finish)")
            self.btn_save_record.config(state='disabled')
            
            # Start recording loop
            self.recording_loop()
        else:
            self.is_recording = False
            self.btn_record.config(text="Start Recording")
            self.val_record_status.set(f"Stopped. {len(self.recorded_songs)} songs recorded.")
            if self.recorded_songs:
                self.btn_save_record.config(state='normal')

    def recording_loop(self):
        if not self.is_recording:
            return

        current_song = main.get_current_song()
        if current_song:
            if current_song not in self.recorded_songs:
                self.recorded_songs.append(current_song)
                self.record_listbox.insert(tk.END, current_song)
                self.record_listbox.yview(tk.END)
                # Skip to next song
                main.next_song()
                # Wait a bit longer for Spotify to change tracks
                self.root.after(2000, self.recording_loop) # Check again in 2s
            else:
                # Duplicate found immediately or loop might be handled here locally
                 # But real duplicate check is "if current_song in self.recorded_songs"
                 # If we see the same song again, it might simply be that next_song() hasn't finished yet.
                 # So we shouldn't stop immediately unless we see the FIRST song again (loop complete)
                 
                 # Logic from CLI:
                 # if current_song in seen_songs: loop detected.
                 
                 # Here we are simpler: We just skip and check again.
                 # User manually stops or we can detect loop if we want.
                 
                 # Let's add a small check for loop if it's the very first song
                 if len(self.recorded_songs) > 1 and current_song == self.recorded_songs[0]:
                     self.toggle_recording() # auto stop
                     messagebox.showinfo("Done", "Playlist loop detected. Recording stopped.")
                     return

                 self.root.after(1000, self.recording_loop)
        else:
             # creating a retry if spotify is paused or not found
             self.root.after(2000, self.recording_loop)

    def save_recorded_songs(self):
        filepath = filedialog.asksaveasfilename(defaultextension=".txt", initialfile="my_playlist_songs.txt")
        if filepath:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write("\n".join(self.recorded_songs))
            messagebox.showinfo("Saved", f"Saved {len(self.recorded_songs)} songs to {filepath}")


    # ==========================
    # SEARCH TAB
    # ==========================
    def init_search_tab(self):
        frame = self.search_frame
        
        # File Selection
        frame_files = ttk.LabelFrame(frame, text="File Selection")
        frame_files.pack(fill='x', padx=10, pady=10)

        # Input
        ttk.Label(frame_files, text="Input File (Songs List):").grid(row=0, column=0, padx=5, pady=5, sticky='w')
        self.entry_search_input = ttk.Entry(frame_files, width=40)
        self.entry_search_input.grid(row=0, column=1, padx=5, pady=5)
        self.entry_search_input.insert(0, "my_playlist_songs.txt")
        ttk.Button(frame_files, text="Browse", command=lambda: self.browse_file(self.entry_search_input)).grid(row=0, column=2, padx=5, pady=5)

        # Output Found
        ttk.Label(frame_files, text="Output (Found):").grid(row=1, column=0, padx=5, pady=5, sticky='w')
        self.entry_search_found = ttk.Entry(frame_files, width=40)
        self.entry_search_found.grid(row=1, column=1, padx=5, pady=5)
        self.entry_search_found.insert(0, "found_songs.txt")

        # Output NotFound
        ttk.Label(frame_files, text="Output (Not Found):").grid(row=2, column=0, padx=5, pady=5, sticky='w')
        self.entry_search_notfound = ttk.Entry(frame_files, width=40)
        self.entry_search_notfound.grid(row=2, column=1, padx=5, pady=5)
        self.entry_search_notfound.insert(0, "not_found.txt")

        # Start Button
        self.btn_search = ttk.Button(frame, text="Start Search", command=self.start_search)
        self.btn_search.pack(pady=10)

        # Progress
        self.search_progress_var = tk.StringVar(value="Ready to search")
        ttk.Label(frame, textvariable=self.search_progress_var).pack(pady=5)
        
        self.search_progress_bar = ttk.Progressbar(frame, mode='determinate')
        self.search_progress_bar.pack(fill='x', padx=10, pady=5)
        
        # Log area
        self.search_log = tk.Text(frame, height=10, width=60)
        self.search_log.pack(padx=10, pady=10)

    def browse_file(self, entry_widget):
        filename = filedialog.askopenfilename()
        if filename:
            entry_widget.delete(0, tk.END)
            entry_widget.insert(0, filename)

    def start_search(self):
        input_file = self.entry_search_input.get()
        output_found = self.entry_search_found.get()
        output_notfound = self.entry_search_notfound.get()

        if not os.path.exists(input_file):
            messagebox.showerror("Error", f"Input file not found: {input_file}")
            return

        self.btn_search.config(state='disabled')
        self.search_log.delete(1.0, tk.END)
        self.search_progress_var.set("Searching...")

        # Run in thread
        threading.Thread(target=self.run_search_thread, args=(input_file, output_found, output_notfound), daemon=True).start()

    def run_search_thread(self, input_file, output_found, output_notfound):
        try:
            with open(input_file, "r", encoding="utf-8") as f:
                songs = [line.strip() for line in f if line.strip()]
            
            total_songs = len(songs)
            if total_songs == 0:
                self.log_queue.put({'type': 'search_progress', 'value': "No songs in file."})
                self.root.after(0, lambda: self.btn_search.config(state='normal'))
                return

            import concurrent.futures
            found_list = []
            not_found_list = []
            
            processed = 0

            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                future_to_song = {executor.submit(main.find_url, song): song for song in songs}
                
                for future in concurrent.futures.as_completed(future_to_song):
                    result = future.result()
                    processed += 1
                    
                    # Update Progress
                    pct = (processed / total_songs) * 100
                    self.root.after(0, lambda v=pct: self.search_progress_bar.config(value=v))
                    
                    msg = ""
                    if result['found']:
                        found_list.append(f"{result['song']} | {result['url']}")
                        msg = f"[FOUND] {result['song']}\n"
                    else:
                        not_found_list.append(result['song'])
                        msg = f"[FAILED] {result['song']} ({result.get('error')})\n"
                    
                    self.root.after(0, lambda m=msg: self.search_log.insert(tk.END, m))
                    self.root.after(0, lambda m=msg: self.search_log.see(tk.END))

            # Save results
            with open(output_found, "w", encoding="utf-8") as f:
                f.write("\n".join(found_list))
            with open(output_notfound, "w", encoding="utf-8") as f:
                f.write("\n".join(not_found_list))

            self.log_queue.put({'type': 'search_progress', 'value': f"Completed! Found: {len(found_list)}, Missed: {len(not_found_list)}"})
            
        except Exception as e:
            self.log_queue.put({'type': 'search_progress', 'value': f"Error: {e}"})
        finally:
             self.root.after(0, lambda: self.btn_search.config(state='normal'))

    # ==========================
    # DOWNLOAD TAB
    # ==========================
    def init_download_tab(self):
        frame = self.download_frame
        
        # File Selection
        frame_files = ttk.LabelFrame(frame, text="File Selection")
        frame_files.pack(fill='x', padx=10, pady=10)

        # Input
        ttk.Label(frame_files, text="Input File (Found Songs):").grid(row=0, column=0, padx=5, pady=5, sticky='w')
        self.entry_dl_input = ttk.Entry(frame_files, width=40)
        self.entry_dl_input.grid(row=0, column=1, padx=5, pady=5)
        self.entry_dl_input.insert(0, "found_songs.txt")
        ttk.Button(frame_files, text="Browse", command=lambda: self.browse_file(self.entry_dl_input)).grid(row=0, column=2, padx=5, pady=5)

        # Output Folder
        ttk.Label(frame_files, text="Output Folder:").grid(row=1, column=0, padx=5, pady=5, sticky='w')
        self.entry_dl_output = ttk.Entry(frame_files, width=40)
        self.entry_dl_output.grid(row=1, column=1, padx=5, pady=5)
        self.entry_dl_output.insert(0, "songs")
        ttk.Button(frame_files, text="Browse", command=self.browse_folder).grid(row=1, column=2, padx=5, pady=5)

        # Start Button
        self.btn_download = ttk.Button(frame, text="Start Download", command=self.start_download)
        self.btn_download.pack(pady=10)

        # Progress
        self.download_progress_var = tk.StringVar(value="Ready to download")
        ttk.Label(frame, textvariable=self.download_progress_var).pack(pady=5)
        
        self.dl_progress_bar = ttk.Progressbar(frame, mode='determinate')
        self.dl_progress_bar.pack(fill='x', padx=10, pady=5)
        
        # Log area
        self.dl_log = tk.Text(frame, height=10, width=60)
        self.dl_log.pack(padx=10, pady=10)

    def browse_folder(self):
        dirname = filedialog.askdirectory()
        if dirname:
            self.entry_dl_output.delete(0, tk.END)
            self.entry_dl_output.insert(0, dirname)

    def start_download(self):
        input_file = self.entry_dl_input.get()
        output_folder = self.entry_dl_output.get()

        if not os.path.exists(input_file):
            messagebox.showerror("Error", f"Input file not found: {input_file}")
            return
        
        if not os.path.exists(output_folder):
            try:
                os.makedirs(output_folder)
            except OSError:
                messagebox.showerror("Error", f"Could not create folder: {output_folder}")
                return

        self.btn_download.config(state='disabled')
        self.dl_log.delete(1.0, tk.END)
        self.download_progress_var.set("Downloading...")

        # Run in thread
        threading.Thread(target=self.run_download_thread, args=(input_file, output_folder), daemon=True).start()

    def run_download_thread(self, input_file, output_folder):
        try:
            with open(input_file, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f if line.strip()]
            
            total = len(lines)
            success_count = 0
            
            for i, line in enumerate(lines):
                # Update progress
                pct = ((i) / total) * 100
                self.root.after(0, lambda v=pct: self.dl_progress_bar.config(value=v))
                
                msg = f"Downloading [{i+1}/{total}]: {line.split('|')[0]}...\n"
                self.root.after(0, lambda m=msg: self.dl_log.insert(tk.END, m))
                self.root.after(0, lambda: self.dl_log.see(tk.END)) # auto scroll

                # Actual download
                if main.download_track(line, output_folder):
                    success_count += 1
                    self.root.after(0, lambda: self.dl_log.insert(tk.END, "   -> Success\n"))
                else:
                    self.root.after(0, lambda: self.dl_log.insert(tk.END, "   -> Failed\n"))

            pct = 100
            self.root.after(0, lambda v=pct: self.dl_progress_bar.config(value=v))
            self.log_queue.put({'type': 'download_progress', 'value': f"Done! {success_count}/{total} downloaded."})

        except Exception as e:
            self.log_queue.put({'type': 'download_progress', 'value': f"Error: {e}"})
        finally:
            self.root.after(0, lambda: self.btn_download.config(state='normal'))

if __name__ == "__main__":
    if not os.path.exists("songs"):
        os.makedirs("songs")
        
    root = tk.Tk()
    app = SpotifyDownloaderApp(root)
    root.mainloop()
