"""
recorder.py
Capture the currently-playing Spotify track from the *local desktop client*
(no Spotify account or Web API required).

Two backends, selected automatically by platform:
  • Linux   → playerctl, reading the MPRIS/D-Bus interface.
  • Windows → GSMTC (Global System Media Transport Controls) via the `winsdk`
              package — the same "now playing" session Windows shows in its
              media flyout.  Reads the Spotify app directly.

Both expose the same two primitives — get_current_song() and next_song() — so
the record_spotify() loop below is fully platform-agnostic.
"""

import time
import sys
import subprocess
from pathlib import Path
from typing import Optional

from rich.panel import Panel
from utils import console, check_recorder_requirements, IS_WINDOWS


# ─────────────────────────────────────────────────────────────────────────────
# Linux backend  (playerctl / MPRIS)
# ─────────────────────────────────────────────────────────────────────────────

def _linux_get_current_song() -> Optional[str]:
    try:
        title = subprocess.check_output(
            ["playerctl", "--player=spotify", "metadata", "title"], text=True
        ).strip()
        artist = subprocess.check_output(
            ["playerctl", "--player=spotify", "metadata", "artist"], text=True
        ).strip()
        if not title:
            return None
        return f"{title} - {artist}" if artist else title
    except subprocess.CalledProcessError:
        return None


def _linux_next_song() -> None:
    subprocess.run(
        ["playerctl", "--player=spotify", "next"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Windows backend  (GSMTC / winsdk)
# ─────────────────────────────────────────────────────────────────────────────

_WIN_LOOP = None   # reused asyncio event loop for winsdk coroutines


def _win_run(coro):
    """Run a winsdk coroutine on a persistent, current event loop."""
    global _WIN_LOOP
    import asyncio
    if _WIN_LOOP is None or _WIN_LOOP.is_closed():
        _WIN_LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_WIN_LOOP)
    return _WIN_LOOP.run_until_complete(coro)


async def _win_spotify_session():
    """Return the Spotify GSMTC session (preferred), else the current session."""
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as MediaManager,
    )
    mgr = await MediaManager.request_async()
    for s in mgr.get_sessions():
        aumid = (s.source_app_user_model_id or "").lower()
        if "spotify" in aumid:          # matches Spotify.exe and the MS-Store AUMID
            return s
    return mgr.get_current_session()


async def _win_current_song_async() -> Optional[str]:
    s = await _win_spotify_session()
    if s is None:
        return None
    props = await s.try_get_media_properties_async()
    title = (props.title or "").strip()
    artist = (props.artist or "").strip()
    if not title:
        return None
    return f"{title} - {artist}" if artist else title


async def _win_next_async() -> None:
    s = await _win_spotify_session()
    if s is not None:
        try:
            await s.try_skip_next_async()
        except Exception:
            pass


def _win_get_current_song() -> Optional[str]:
    try:
        return _win_run(_win_current_song_async())
    except Exception:
        return None


def _win_next_song() -> None:
    try:
        _win_run(_win_next_async())
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Platform dispatch
# ─────────────────────────────────────────────────────────────────────────────

def get_current_song() -> Optional[str]:
    """Currently playing Spotify track as 'title - artist', or None."""
    if IS_WINDOWS:
        return _win_get_current_song()
    return _linux_get_current_song()


def next_song() -> None:
    """Skip to the next track in Spotify."""
    if IS_WINDOWS:
        _win_next_song()
    else:
        _linux_next_song()


# ─────────────────────────────────────────────────────────────────────────────
# Recorder loop (platform-agnostic)
# ─────────────────────────────────────────────────────────────────────────────

def record_spotify(output_file: Path) -> None:
    """Record a Spotify playlist by detecting song changes via the active client."""
    if not check_recorder_requirements():
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
