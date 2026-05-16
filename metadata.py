#!/usr/bin/env python3
"""
metadata.py
===========
Fetches, embeds, and retries metadata + lyrics for downloaded audio files.

Sources tried in order
----------------------
Metadata  : iTunes  →  MusicBrainz  →  filename fallback
Lyrics    : LRCLIB  →  Genius (opt.) →  Whisper transcription (opt.)
            Hindi/Urdu lyrics are auto-transliterated to Hinglish.

Supported audio formats: .mp3  .flac  .m4a  .opus
"""

import os
import re
import sys
import time
import json
import urllib.request
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional

from mutagen.mp3  import MP3
from mutagen.id3  import ID3, TIT2, TPE1, TALB, TDRC, TCON, TRCK, USLT, APIC
from mutagen.flac import FLAC, Picture
from mutagen.mp4  import MP4, MP4Cover
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

from utils import console, sanitize_filename
from transliterate_lyrics import transliterate_if_needed, detect_script

# ── Optional: Genius lyrics via lyricsgenius ──────────────────────────────────
_GENIUS_TOKEN: str = os.environ.get("GENIUS_API_KEY", "")
try:
    import lyricsgenius as _lyricsgenius_mod
    _GENIUS_LIB = True
except ImportError:
    _GENIUS_LIB = False

# ── Optional: Whisper audio transcription ─────────────────────────────────────
try:
    import whisper as _whisper_mod
    _WHISPER_LIB = True
except ImportError:
    _WHISPER_LIB = False

_WHISPER_MODEL_CACHE: Any = None   # lazily loaded

# ── Shared HTTP headers ───────────────────────────────────────────────────────
_UA = "SpotifyDownloaderCLI/2.0 (open-source; github.com)"
_HEADERS = {"User-Agent": _UA}


# ─────────────────────────────────────────────────────────────────────────────
# Tag-status tracker
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TagStatus:
    has_title:  bool = False
    has_artist: bool = False
    has_album:  bool = False
    has_cover:  bool = False
    has_lyrics: bool = False

    @property
    def core_complete(self) -> bool:
        """True when the three truly essential fields are present."""
        return self.has_title and self.has_artist and self.has_cover

    @property
    def fully_complete(self) -> bool:
        return self.core_complete and self.has_lyrics


# ─────────────────────────────────────────────────────────────────────────────
# 1.  Metadata sources
# ─────────────────────────────────────────────────────────────────────────────

def search_itunes(query: str) -> Optional[Dict[str, Any]]:
    """Search the iTunes Search API.  Returns the first result dict or None."""
    clean = (
        query
        .replace(" - ", " ")
        .replace("official audio", "")
        .replace("official video", "")
        .replace("lyrics", "")
        .strip()
    )
    url = (
        "https://itunes.apple.com/search"
        f"?term={urllib.parse.quote(clean)}&media=music&limit=1"
    )
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode())
        if data.get("resultCount", 0) > 0:
            return data["results"][0]
    except Exception:
        pass
    return None


def search_musicbrainz(artist: str, title: str) -> Optional[Dict[str, Any]]:
    """
    Search MusicBrainz for a recording.
    Returns a dict in the same shape as search_itunes() plus
    ``mb_release_id`` for Cover Art Archive lookup.
    Enforces ≥1 s delay to respect MB rate limit.
    """
    query = f'recording:"{title}" AND artist:"{artist}"'
    url = (
        "https://musicbrainz.org/ws/2/recording/"
        f"?query={urllib.parse.quote(query)}&fmt=json&limit=5"
    )
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        recordings = data.get("recordings", [])
        if not recordings:
            return None
        best = max(recordings, key=lambda r: int(r.get("score", 0)))
        artist_credits = best.get("artist-credit", [])
        artist_name = (
            artist_credits[0].get("artist", {}).get("name", artist)
            if artist_credits else artist
        )
        releases = best.get("releases", [])
        release  = releases[0] if releases else {}
        return {
            "trackName":        best.get("title", title),
            "artistName":       artist_name,
            "collectionName":   release.get("title", ""),
            "releaseDate":      (release.get("date") or "")[:4],
            "trackNumber":      best.get("position"),
            "primaryGenreName": "",
            "artworkUrl100":    "",           # filled in by cover-art lookup below
            "mb_release_id":    release.get("id", ""),
        }
    except Exception:
        return None
    finally:
        time.sleep(1.1)   # MB hard rate limit


def get_cover_art_mb(release_id: str) -> Optional[bytes]:
    """Fetch 500-px front cover from the Cover Art Archive."""
    if not release_id:
        return None
    url = f"https://coverartarchive.org/release/{release_id}/front-500"
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read()
    except Exception:
        return None


def _metadata_from_filename(song_name: str) -> Dict[str, Any]:
    """
    Last-resort: extract title / artist from the song name string.
    Expected format (from songs.txt / recorder): 'Title - Artist'
    """
    parts = song_name.split(" - ", 1)
    title  = parts[0].strip() if parts else song_name
    artist = parts[1].strip() if len(parts) > 1 else "Unknown Artist"
    return {
        "trackName":        title,
        "artistName":       artist,
        "collectionName":   "",
        "releaseDate":      "",
        "trackNumber":      None,
        "primaryGenreName": "",
        "artworkUrl100":    "",
        "mb_release_id":    "",
    }


# ─────────────────────────────────────────────────────────────────────────────
# 2.  Lyrics sources
# ─────────────────────────────────────────────────────────────────────────────

def get_synced_lyrics(
    artist: str, track: str, duration_sec: float
) -> Tuple[Optional[str], Optional[str]]:
    """Fetch synced (.lrc) and plain lyrics from LRCLIB."""
    try:
        url = (
            "https://lrclib.net/api/get"
            f"?artist_name={urllib.parse.quote(artist)}"
            f"&track_name={urllib.parse.quote(track)}"
            f"&duration={int(duration_sec)}"
        )
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode())
        return data.get("syncedLyrics"), data.get("plainLyrics")
    except Exception:
        return None, None


def _search_lrclib_loose(artist: str, track: str) -> Optional[str]:
    """LRCLIB fuzzy search (no duration required)."""
    try:
        url = (
            "https://lrclib.net/api/search"
            f"?artist_name={urllib.parse.quote(artist)}"
            f"&track_name={urllib.parse.quote(track)}"
        )
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=12) as resp:
            results = json.loads(resp.read().decode())
        if results:
            return results[0].get("plainLyrics")
    except Exception:
        pass
    return None


def get_lyrics_genius(artist: str, title: str) -> Optional[str]:
    """
    Fetch lyrics from Genius (requires ``GENIUS_API_KEY`` env variable
    and ``pip install lyricsgenius``).  Returns None when unavailable.
    """
    if not _GENIUS_TOKEN or not _GENIUS_LIB:
        return None
    try:
        genius = _lyricsgenius_mod.Genius(
            _GENIUS_TOKEN, verbose=False, timeout=15,
            remove_section_headers=True, skip_non_songs=True
        )
        song = genius.search_song(title, artist, get_full_info=False)
        if song and song.lyrics:
            # Strip Genius footer ("123Embed")
            raw = re.sub(r'\d+Embed$', '', song.lyrics).strip()
            return raw
    except Exception:
        pass
    return None


def save_lrc_file(audio_path: Path, synced_lyrics: str) -> None:
    """Write synced lyrics as a .lrc sidecar file next to the audio."""
    lrc_path = audio_path.with_suffix(".lrc")
    with open(lrc_path, "w", encoding="utf-8") as fh:
        fh.write(synced_lyrics)


# ─────────────────────────────────────────────────────────────────────────────
# 3.  Whisper audio transcription (optional, last-resort lyrics)
# ─────────────────────────────────────────────────────────────────────────────

def transcribe_audio(
    file_path: Path,
    model_size: str = "small",
    language: Optional[str] = None,
) -> Optional[str]:
    """
    Transcribe *file_path* using OpenAI Whisper and return raw text.
    Requires ``pip install openai-whisper``.
    ``language`` can be ``'hi'`` (Hindi), ``'ur'`` (Urdu), ``None`` (auto).
    """
    global _WHISPER_MODEL_CACHE
    if not _WHISPER_LIB:
        console.print(
            "[yellow]  ⚠ Whisper not installed. "
            "Run: pip install openai-whisper[/yellow]"
        )
        return None
    try:
        if _WHISPER_MODEL_CACHE is None:
            console.print(
                f"  [cyan]⬇[/cyan] Loading Whisper model '{model_size}' "
                "(first run may take a moment)…"
            )
            _WHISPER_MODEL_CACHE = _whisper_mod.load_model(model_size)
        result = _WHISPER_MODEL_CACHE.transcribe(
            str(file_path),
            language=language,
            task="transcribe",
            fp16=False,
            verbose=False,
        )
        text = result.get("text", "").strip()
        if text:
            console.print("  [cyan]>[/cyan] [green]Whisper transcription done.[/green]")
        return text or None
    except Exception as exc:
        console.print(f"  [yellow]⚠ Whisper error: {exc}[/yellow]")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 4.  Cover-art download helper
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_artwork(track_info: Dict[str, Any]) -> Optional[bytes]:
    """Download the best available artwork from iTunes or MB Cover Art Archive."""
    # iTunes artwork (replace 100×100 thumbnail with 600×600)
    art_url = track_info.get("artworkUrl100", "")
    if art_url:
        try:
            big_url = art_url.replace("100x100bb", "600x600bb")
            with urllib.request.urlopen(
                urllib.request.Request(big_url, headers=_HEADERS), timeout=12
            ) as resp:
                return resp.read()
        except Exception:
            pass

    # Pre-fetched bytes (e.g. from MusicBrainz cover art archive)
    pre = track_info.get("_cover_bytes")
    if pre:
        return pre

    # MusicBrainz Cover Art Archive
    release_id = track_info.get("mb_release_id", "")
    if release_id:
        data = get_cover_art_mb(release_id)
        if data:
            return data

    return None


# ─────────────────────────────────────────────────────────────────────────────
# 5.  Core tagging  (embed_metadata)
# ─────────────────────────────────────────────────────────────────────────────

def _apply_hinglish(lyrics: Optional[str]) -> Optional[str]:
    """Transliterate Hindi/Urdu lyrics to Hinglish; leave Latin untouched."""
    if not lyrics:
        return lyrics
    script = detect_script(lyrics)
    if script in ('hindi', 'urdu'):
        console.print(
            f"  [cyan]>[/cyan] [magenta]Auto-transliterating {script} lyrics "
            "to Hinglish.[/magenta]"
        )
        return transliterate_if_needed(lyrics)
    return lyrics


def embed_metadata(
    file_path: Path,
    track_info: Dict[str, Any],
    plain_lyrics: Optional[str],
) -> TagStatus:
    """
    Write tags + cover art + lyrics into the audio file.
    Supports .mp3  .flac  .m4a  .opus
    Returns a TagStatus indicating what was successfully embedded.
    """
    status = TagStatus()

    if not file_path.exists():
        console.print(f"[bold red]  ✗ File not found:[/bold red] {file_path}")
        return status
    if file_path.stat().st_size == 0:
        console.print(f"[bold red]  ✗ File is empty:[/bold red] {file_path}")
        return status

    ext = file_path.suffix.lower()

    art_bytes = _fetch_artwork(track_info)
    lyrics    = _apply_hinglish(plain_lyrics)

    title    = track_info.get("trackName",        "") or ""
    artist   = track_info.get("artistName",       "") or ""
    album    = track_info.get("collectionName",   "") or ""
    year     = str(track_info.get("releaseDate",  "") or "")[:4]
    genre    = track_info.get("primaryGenreName", "") or ""
    tracknum = track_info.get("trackNumber")

    try:
        # ── MP3 ──────────────────────────────────────────────────────────────
        if ext == ".mp3":
            audio = MP3(str(file_path), ID3=ID3)
            if audio.tags is None:
                audio.add_tags()
            tags = audio.tags
            if title:
                tags.add(TIT2(encoding=3, text=title));  status.has_title  = True
            if artist:
                tags.add(TPE1(encoding=3, text=artist)); status.has_artist = True
            if album:
                tags.add(TALB(encoding=3, text=album));  status.has_album  = True
            if year:
                tags.add(TDRC(encoding=3, text=year))
            if genre:
                tags.add(TCON(encoding=3, text=genre))
            if tracknum:
                tags.add(TRCK(encoding=3, text=str(tracknum)))
            if art_bytes:
                tags.add(APIC(encoding=3, mime="image/jpeg",
                              type=3, desc="Cover", data=art_bytes))
                status.has_cover = True
            if lyrics:
                tags.add(USLT(encoding=3, lang="eng", desc="", text=lyrics))
                status.has_lyrics = True
            audio.save()

        # ── FLAC ─────────────────────────────────────────────────────────────
        elif ext == ".flac":
            audio = FLAC(str(file_path))
            if title:  audio["title"]  = title;  status.has_title  = True
            if artist: audio["artist"] = artist; status.has_artist = True
            if album:  audio["album"]  = album;  status.has_album  = True
            if year:   audio["date"]   = year
            if genre:  audio["genre"]  = genre
            if tracknum:
                audio["tracknumber"] = str(tracknum)
            if lyrics:
                audio["lyrics"] = lyrics
                status.has_lyrics = True
            if art_bytes:
                pic = Picture()
                pic.type = 3
                pic.mime = "image/jpeg"
                pic.desc = "Cover"
                pic.data = art_bytes
                audio.add_picture(pic)
                status.has_cover = True
            audio.save()

        # ── M4A / AAC ─────────────────────────────────────────────────────────
        elif ext == ".m4a":
            audio = MP4(str(file_path))
            if title:  audio["\xa9nam"] = title;  status.has_title  = True
            if artist: audio["\xa9ART"] = artist; status.has_artist = True
            if album:  audio["\xa9alb"] = album;  status.has_album  = True
            if year:   audio["\xa9day"] = year
            if genre:  audio["\xa9gen"] = genre
            if tracknum:
                audio["trkn"] = [(int(tracknum), 0)]
            if lyrics:
                audio["\xa9lyr"] = lyrics
                status.has_lyrics = True
            if art_bytes:
                audio["covr"] = [MP4Cover(art_bytes,
                                          imageformat=MP4Cover.FORMAT_JPEG)]
                status.has_cover = True
            audio.save()

        # ── Opus (OGG container) ──────────────────────────────────────────────
        elif ext == ".opus":
            from mutagen.oggopus import OggOpus
            audio = OggOpus(str(file_path))
            # Opus uses Vorbis comments (same as FLAC)
            if title:  audio["title"]  = [title];  status.has_title  = True
            if artist: audio["artist"] = [artist]; status.has_artist = True
            if album:  audio["album"]  = [album];  status.has_album  = True
            if year:   audio["date"]   = [year]
            if genre:  audio["genre"]  = [genre]
            if tracknum:
                audio["tracknumber"] = [str(tracknum)]
            if lyrics:
                audio["lyrics"] = [lyrics]
                status.has_lyrics = True
            # Opus cover art: base64-encoded PICTURE block (FLAC/Vorbis standard)
            if art_bytes:
                import base64, struct
                # Build METADATA_BLOCK_PICTURE as per Vorbis spec
                pic_type     = (3).to_bytes(4, 'big')
                mime         = b"image/jpeg"
                mime_len     = len(mime).to_bytes(4, 'big')
                desc         = b""
                desc_len     = len(desc).to_bytes(4, 'big')
                width = height = depth = colors = (0).to_bytes(4, 'big')
                data_len     = len(art_bytes).to_bytes(4, 'big')
                block = (pic_type + mime_len + mime + desc_len + desc
                         + width + height + depth + colors
                         + data_len + art_bytes)
                audio["metadata_block_picture"] = [
                    base64.b64encode(block).decode("ascii")
                ]
                status.has_cover = True
            audio.save()

        else:
            console.print(f"[yellow]  ⚠ Unsupported format for tagging: {ext}[/yellow]")

    except Exception as exc:
        console.print(
            f"[bold red]  ✗ Tagging error ({file_path.name}):[/bold red] {exc}"
        )

    return status


# ─────────────────────────────────────────────────────────────────────────────
# 6.  Organized folder layout
# ─────────────────────────────────────────────────────────────────────────────

def organize_files(
    file_path: Path, track_info: Dict[str, Any], output_dir: Path
) -> Path:
    """Move audio + .lrc sidecar into Artist sub-folder."""
    artist     = sanitize_filename(track_info.get("artistName", "Unknown Artist"))
    new_folder = output_dir / artist
    new_folder.mkdir(parents=True, exist_ok=True)
    new_path   = new_folder / file_path.name
    if new_path != file_path and file_path.exists():
        file_path.rename(new_path)
        lrc = file_path.with_suffix(".lrc")
        if lrc.exists():
            lrc.rename(new_folder / lrc.name)
    return new_path


# ─────────────────────────────────────────────────────────────────────────────
# 7.  Multi-source metadata + lyrics resolver
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_metadata_and_lyrics(
    song_name: str,
    file_path:  Path,
    use_genius: bool = True,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]:
    """
    Try every available metadata and lyrics source for *song_name*.

    Returns (track_info, synced_lyrics, plain_lyrics).
    track_info is never None (falls back to filename extraction).
    """
    synced_lyrics: Optional[str] = None
    plain_lyrics:  Optional[str] = None
    track: Optional[Dict[str, Any]] = None

    # ── Strategy A: iTunes (primary) ─────────────────────────────────────────
    track = search_itunes(song_name)

    # ── Strategy B: iTunes with simplified query ──────────────────────────────
    if track is None:
        parts = song_name.split(" - ", 1)
        if len(parts) == 2:
            for variant in [
                f"{parts[0]} {parts[1]}",
                parts[0],
                parts[1],
            ]:
                track = search_itunes(variant)
                if track:
                    break

    # ── Strategy C: MusicBrainz ───────────────────────────────────────────────
    if track is None:
        parts = song_name.split(" - ", 1)
        # songs.txt format is "Title - Artist"
        if len(parts) == 2:
            title_guess  = parts[0].strip()
            artist_guess = parts[1].strip()
            # Try both orderings
            for t, a in [(title_guess, artist_guess), (artist_guess, title_guess)]:
                mb = search_musicbrainz(a, t)
                if mb:
                    track = mb
                    # Pre-fetch MB cover art so embed_metadata can use it
                    release_id = mb.get("mb_release_id", "")
                    if release_id:
                        cover_bytes = get_cover_art_mb(release_id)
                        if cover_bytes:
                            track["_cover_bytes"] = cover_bytes
                    break
        else:
            mb = search_musicbrainz("", song_name)
            if mb:
                track = mb

    # ── Fallback: extract from filename ──────────────────────────────────────
    if track is None:
        track = _metadata_from_filename(song_name)

    # ── Lyrics: LRCLIB (with duration) ───────────────────────────────────────
    artist = track.get("artistName", "")
    title  = track.get("trackName",  "")
    try:
        from mutagen import File as MutagenFile
        mf = MutagenFile(str(file_path))
        duration_sec = mf.info.length if mf and mf.info else 0.0
    except Exception:
        duration_sec = 0.0

    if artist and title:
        synced_lyrics, plain_lyrics = get_synced_lyrics(artist, title, duration_sec)

    # ── Lyrics: LRCLIB loose search (no duration) ─────────────────────────────
    if not plain_lyrics and artist and title:
        plain_lyrics = _search_lrclib_loose(artist, title)

    # ── Lyrics: Genius ────────────────────────────────────────────────────────
    if not plain_lyrics and use_genius and artist and title:
        plain_lyrics = get_lyrics_genius(artist, title)

    return track, synced_lyrics, plain_lyrics


# ─────────────────────────────────────────────────────────────────────────────
# 8.  Main orchestrator
# ─────────────────────────────────────────────────────────────────────────────

def process_metadata(
    downloaded_files:  List[Tuple[str, Path]],
    organize:          bool = False,
    output_dir:        Optional[Path] = None,
    retry_failed:      bool = True,
    use_whisper:       bool = False,
    whisper_model:     str  = "small",
    whisper_language:  Optional[str] = None,
) -> Dict[str, Path]:
    """
    Fetch metadata + lyrics, embed tags, and return {song_name: final_path}.

    Passes
    ------
    1. All files: try iTunes → MusicBrainz → LRCLIB → Genius
    2. Files still missing core tags: retry with alternate query forms
    3. Files still missing lyrics only: Whisper transcription (if enabled)
    """
    console.print()
    final_paths:   Dict[str, Path]   = {}
    tag_statuses:  Dict[str, TagStatus] = {}

    # ── Pass 1 ────────────────────────────────────────────────────────────────
    console.print(
        "[bold cyan]━━━  Pass 1: Metadata + Lyrics (iTunes / MusicBrainz / LRCLIB)[/bold cyan]"
    )
    with Progress(
        SpinnerColumn(spinner_name="arc"),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(style="yellow"),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(
            "[yellow]Tagging files…", total=len(downloaded_files)
        )
        for song_name, file_path in downloaded_files:
            progress.console.print(f"[bold]Tagging:[/bold] {song_name}")
            current_path = file_path

            if not file_path.exists() or file_path.stat().st_size == 0:
                progress.console.print(
                    "  [red]✗[/red] [yellow]File missing – skipping.[/yellow]"
                )
                final_paths[song_name]  = current_path
                tag_statuses[song_name] = TagStatus()
                progress.advance(task)
                continue

            track, synced, plain = _resolve_metadata_and_lyrics(song_name, file_path)

            if synced:
                save_lrc_file(current_path, synced)
                progress.console.print("  [cyan]>[/cyan] [green]Synced .lrc saved.[/green]")

            status = embed_metadata(current_path, track, plain)
            tag_statuses[song_name] = status

            _log_tag_status(progress.console.print, status)

            if organize and output_dir and track:
                current_path = organize_files(current_path, track, output_dir)
                progress.console.print(
                    f"  [cyan]>[/cyan] [green]Moved → {current_path.parent.name}/[/green]"
                )

            final_paths[song_name] = current_path
            progress.advance(task)
            time.sleep(0.35)   # polite API pacing

    # ── Pass 2: retry files missing core metadata ─────────────────────────────
    if retry_failed:
        need_retry = [
            (name, final_paths[name])
            for name, st in tag_statuses.items()
            if not st.core_complete
        ]
        if need_retry:
            console.print(
                f"\n[bold cyan]━━━  Pass 2: Retry ({len(need_retry)} files missing core tags)[/bold cyan]"
            )
            for song_name, file_path in need_retry:
                if not file_path.exists():
                    continue
                console.print(f"  [yellow]↻[/yellow] Retrying: {song_name}")

                # Flip "Title - Artist" → "Artist Title" and search again
                parts = song_name.split(" - ", 1)
                alt_queries = []
                if len(parts) == 2:
                    alt_queries = [
                        f"{parts[1].strip()} {parts[0].strip()}",
                        parts[0].strip(),
                        parts[1].strip(),
                    ]
                else:
                    alt_queries = [song_name]

                track = None
                for q in alt_queries:
                    track = search_itunes(q)
                    if track:
                        break
                    time.sleep(0.5)

                if track is None and len(parts) == 2:
                    track = search_musicbrainz(parts[1].strip(), parts[0].strip())

                if track is None:
                    track = _metadata_from_filename(song_name)

                artist = track.get("artistName", "")
                title  = track.get("trackName",  "")
                _, plain = get_synced_lyrics(artist, title, 0)
                plain = plain or _search_lrclib_loose(artist, title)
                plain = plain or get_lyrics_genius(artist, title)

                status = embed_metadata(file_path, track, plain)
                tag_statuses[song_name] = status
                _log_tag_status(console.print, status)
                time.sleep(0.5)

    # ── Pass 3: Whisper for files still missing lyrics ────────────────────────
    if use_whisper:
        need_whisper = [
            (name, final_paths[name])
            for name, st in tag_statuses.items()
            if not st.has_lyrics
        ]
        if need_whisper:
            console.print(
                f"\n[bold cyan]━━━  Pass 3: Whisper transcription "
                f"({len(need_whisper)} files without lyrics)[/bold cyan]"
            )
            for song_name, file_path in need_whisper:
                if not file_path.exists():
                    continue
                console.print(f"  [magenta]🎙[/magenta]  Transcribing: {song_name}")
                lyrics = transcribe_audio(
                    file_path,
                    model_size=whisper_model,
                    language=whisper_language,
                )
                if lyrics:
                    lyrics = _apply_hinglish(lyrics)
                    # Embed lyrics only (re-open tags and add USLT/lyrics field)
                    _embed_lyrics_only(file_path, lyrics)
                    tag_statuses[song_name].has_lyrics = True
                else:
                    console.print("  [yellow]⚠ No transcription produced.[/yellow]")

    # ── Summary ───────────────────────────────────────────────────────────────
    total    = len(downloaded_files)
    complete = sum(1 for st in tag_statuses.values() if st.core_complete)
    lyric_ct = sum(1 for st in tag_statuses.values() if st.has_lyrics)
    console.print(
        f"\n[bold green]✓ Tagging done:[/bold green] "
        f"{complete}/{total} fully tagged  |  "
        f"{lyric_ct}/{total} with lyrics\n"
    )

    return final_paths


# ─────────────────────────────────────────────────────────────────────────────
# 9.  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _log_tag_status(print_fn, status: TagStatus) -> None:
    parts = []
    if status.has_title:   parts.append("title")
    if status.has_artist:  parts.append("artist")
    if status.has_album:   parts.append("album")
    if status.has_cover:   parts.append("cover")
    if status.has_lyrics:  parts.append("lyrics")
    if parts:
        print_fn(f"  [cyan]>[/cyan] [green]Tagged:[/green] {', '.join(parts)}")
    else:
        print_fn("  [yellow]⚠ No metadata found from any source.[/yellow]")


def _embed_lyrics_only(file_path: Path, lyrics: str) -> None:
    """Add/overwrite only the lyrics tag in an existing file."""
    ext = file_path.suffix.lower()
    try:
        if ext == ".mp3":
            audio = MP3(str(file_path), ID3=ID3)
            if audio.tags is None:
                audio.add_tags()
            audio.tags.add(USLT(encoding=3, lang="eng", desc="", text=lyrics))
            audio.save()
        elif ext == ".flac":
            audio = FLAC(str(file_path))
            audio["lyrics"] = lyrics
            audio.save()
        elif ext == ".m4a":
            audio = MP4(str(file_path))
            audio["\xa9lyr"] = lyrics
            audio.save()
        elif ext == ".opus":
            from mutagen.oggopus import OggOpus
            audio = OggOpus(str(file_path))
            audio["lyrics"] = [lyrics]
            audio.save()
    except Exception as exc:
        console.print(f"  [yellow]⚠ Could not embed lyrics: {exc}[/yellow]")