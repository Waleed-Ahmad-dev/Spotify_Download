import time
import sys
import subprocess
from pathlib import Path
from typing import Optional

from rich.panel import Panel
from utils import console, check_linux_requirements


def get_current_song() -> Optional[str]:
    """Return the currently playing Spotify track as 'title - artist', or None."""
    try:
        title = subprocess.check_output(
            ["playerctl", "--player=spotify", "metadata", "title"], text=True
        ).strip()
        artist = subprocess.check_output(
            ["playerctl", "--player=spotify", "metadata", "artist"], text=True
        ).strip()
        if not title:
            return None
        return f"{title} - {artist}"
    except subprocess.CalledProcessError:
        return None


def next_song() -> None:
    """Skip to the next track in Spotify."""
    subprocess.run(
        ["playerctl", "--player=spotify", "next"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def record_spotify(output_file: Path) -> None:
    """Record a Spotify playlist by detecting song changes via playerctl."""
    if not check_linux_requirements():
        console.print("[bold red]❌ Cannot proceed with recording on this platform.[/bold red]")
        sys.exit(1)

    console.print(
        Panel(
            "[bold green]Spotify Playlist Recorder[/bold green]\n"
            "Play your playlist in Spotify, then press Enter to begin.",
            expand=False,
        )
    )
    input()

    recorded_songs: list[str] = []
    seen_songs: set[str] = set()

    try:
        with console.status(
            "[bold cyan]Listening to Spotify… (Press Ctrl+C to stop)[/bold cyan]",
            spinner="bouncingBar",
        ):
            while True:
                current_song = get_current_song()
                if not current_song:
                    time.sleep(2)
                    continue

                # A repeated song means the playlist looped around
                if current_song in seen_songs and recorded_songs:
                    console.print(
                        f"\n[bold green]✓ Loop detected![/bold green] "
                        f"Recorded {len(recorded_songs)} songs."
                    )
                    break

                recorded_songs.append(current_song)
                seen_songs.add(current_song)
                console.print(
                    f"[cyan][{len(recorded_songs)}][/cyan] "
                    f"[green]Recorded:[/green] {current_song}"
                )

                next_song()

                # Wait up to 15 s for the track to actually change
                start = time.time()
                while time.time() - start < 15:
                    time.sleep(0.8)
                    if get_current_song() != current_song:
                        break
                else:
                    console.print(
                        "[bold yellow]⚠️  Timeout waiting for next song – "
                        "playlist may have ended.[/bold yellow]"
                    )
                    break

    except KeyboardInterrupt:
        console.print("\n[bold yellow]⚠️  Recording stopped by user.[/bold yellow]")

    if recorded_songs:
        with open(output_file, "w", encoding="utf-8") as fh:
            fh.write("\n".join(recorded_songs))
        console.print(
            f"\n[bold green]✓ Saved {len(recorded_songs)} songs to '{output_file}'.[/bold green]"
        )
    else:
        console.print("[bold yellow]⚠️  No songs were recorded.[/bold yellow]")