#!/usr/bin/env python3
"""
Spotify-to-MP3 Converter (CLI Edition)
Modular architecture: orchestrates recording, downloading, and tagging.
"""

import sys
import argparse
from pathlib import Path

# Dependency Check Block before local imports
try:
    import yt_dlp
    import mutagen
    from rich.table import Table
except ImportError:
    print("❌ Missing dependencies. Please run: pip install yt-dlp mutagen rich questionary")
    sys.exit(1)

try:
    import questionary
except ImportError:
    questionary = None

# Local module imports
from utils import console, check_ffmpeg, remove_duplicates, generate_m3u
from recorder import record_spotify
from youtube import search_youtube, download_songs
from metadata import process_metadata

def main():
    parser = argparse.ArgumentParser(description="Spotify Playlist to MP3 Downloader + Tagger")
    parser.add_argument('--record', action='store_true', help='Record Spotify playlist to songs.txt')
    parser.add_argument('--search', action='store_true', help='Search YouTube for songs in songs.txt')
    parser.add_argument('--download', action='store_true', help='Download found songs & apply metadata')
    parser.add_argument('--all', action='store_true', help='Run the complete pipeline')

    parser.add_argument('--input', default='songs.txt', help='Input song list file (default: songs.txt)')
    parser.add_argument('--found', default='found.txt', help='Output file for found URLs (default: found.txt)')
    parser.add_argument('--notfound', default='not_found.txt', help='Output file for missing songs (default: not_found.txt)')
    parser.add_argument('--output-dir', default='songs', help='Directory to save audio files (default: songs)')
    parser.add_argument('--workers', type=int, default=5, help='Number of search threads (default: 5)')
    parser.add_argument('--quality', choices=['128', '192', '320'], default='192', help='Audio bitrate for lossy formats (default: 192)')
    parser.add_argument('--format', choices=['mp3', 'flac', 'm4a'], help='Target audio format (if omitted, prompts menu)')
    parser.add_argument('--organize', action='store_true', help='Organize files into Artist folders after tagging')
    parser.add_argument('--resume', action='store_true', help='Skip search if found.txt already exists')
    parser.add_argument('--normalize', action='store_true', help='Normalize audio volume to -14 LUFS (Spotify standard)')
    parser.add_argument('--playlist', type=str, metavar='NAME', help='Generate an .m3u playlist with this name retaining original order')
    parser.add_argument('--dedupe', type=str, metavar='DIR', help='Remove duplicates in a specified directory based on metadata')

    args = parser.parse_args()

    # Handle the standalone deduplicate feature first
    if args.dedupe:
        dedupe_dir = Path(args.dedupe)
        if not dedupe_dir.exists() or not dedupe_dir.is_dir():
            console.print(f"[bold red]❌ Error: Directory '{dedupe_dir}' not found.[/bold red]")
            sys.exit(1)
        remove_duplicates(dedupe_dir)
        sys.exit(0)

    if not any([args.record, args.search, args.download, args.all]):
        parser.print_help()
        sys.exit(0)

    # Format Selection UI
    if (args.download or args.all) and not args.format:
        if questionary:
            format_choice = questionary.select(
                "Choose your preferred audio format:",
                choices=[
                    questionary.Choice("FLAC (Lossless / Studio Quality)", value="flac"),
                    questionary.Choice("M4A / AAC (Modern / Highly Efficient)", value="m4a"),
                    questionary.Choice("MP3 (Legacy / Maximum Compatibility)", value="mp3"),
                ],
                default="flac"
            ).ask()
            
            if not format_choice:
                console.print("[yellow]Operation cancelled by user.[/yellow]")
                sys.exit(0)
            args.format = format_choice
        else:
            console.print("[yellow]⚠️ 'questionary' module not found. Defaulting to MP3. Install it for the interactive menu: pip install questionary[/yellow]")
            args.format = "mp3"

    input_file = Path(args.input)
    found_file = Path(args.found)
    notfound_file = Path(args.notfound)
    out_dir = Path(args.output_dir)
    
    should_normalize = args.normalize or args.all

    if args.record or args.all:
        record_spotify(input_file)

    if args.search or args.all:
        if args.resume and found_file.exists():
            console.print(f"[bold green]✓ Found file '{found_file}' exists. Skipping search (--resume).[/bold green]")
        else:
            search_youtube(input_file, found_file, notfound_file, max_workers=args.workers)

    if args.download or args.all:
        if not check_ffmpeg():
            sys.exit(1)

        if not found_file.exists() or found_file.stat().st_size == 0:
            console.print("[bold red]❌ No URLs found. Run --search first or remove --resume.[/bold red]")
            sys.exit(1)

        downloaded = download_songs(found_file, out_dir, format_ext=args.format, quality=args.quality, max_workers=2, normalize=should_normalize)
        if downloaded:
            final_paths = process_metadata(downloaded, organize=args.organize, output_dir=out_dir)
            
            if args.playlist:
                generate_m3u(args.playlist, out_dir, found_file, final_paths)
            
            # Print Final Summary Table
            console.print()
            summary_table = Table(title="🎉 Run Summary", show_header=True, header_style="bold magenta")
            summary_table.add_column("Task", style="cyan")
            summary_table.add_column("Result", justify="right", style="green")
            
            summary_table.add_row("Downloaded Tracks", str(len(downloaded)))
            summary_table.add_row("Format Selected", args.format.upper())
            summary_table.add_row("Metadata Tagged", str(len(final_paths)))
            if should_normalize:
                summary_table.add_row("Audio Normalized", "Yes (-14 LUFS)")
            if args.organize:
                summary_table.add_row("Folder Organization", "Enabled (By Artist)")
            if args.playlist:
                summary_table.add_row("Playlist Generated", f"{args.playlist}.m3u")
                
            console.print(summary_table)
            console.print("\n[bold green]All tasks completed successfully! Enjoy your music.[/bold green]\n")
                
        else:
            console.print("[bold red]❌ No songs were downloaded.[/bold red]")

if __name__ == "__main__":
    main()