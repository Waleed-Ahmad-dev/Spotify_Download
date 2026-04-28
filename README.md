<div align="center">
  <img src="https://img.icons8.com/color/96/000000/spotify--v1.png" alt="Spotify Logo" width="80"/>
  <img src="https://img.icons8.com/color/96/000000/youtube-play.png" alt="YouTube Logo" width="80"/>
  
  <h1>Spotify to MP3 / FLAC / M4A Downloader</h1>

  <p>
    <strong>A highly modular, professional-grade pipeline for archiving Spotify playlists with pristine audio quality, accurate metadata, and precise lyrics.</strong>
  </p>

  <p>
    <a href="https://github.com/Waleed-Ahmad-dev/Spotify_Download/issues"><img src="https://img.shields.io/github/issues/Waleed-Ahmad-dev/Spotify_Download?style=for-the-badge&color=red" alt="Issues"/></a>
    <a href="https://github.com/Waleed-Ahmad-dev/Spotify_Download/network/members"><img src="https://img.shields.io/github/forks/Waleed-Ahmad-dev/Spotify_Download?style=for-the-badge&color=blue" alt="Forks"/></a>
    <a href="https://github.com/Waleed-Ahmad-dev/Spotify_Download/stargazers"><img src="https://img.shields.io/github/stars/Waleed-Ahmad-dev/Spotify_Download?style=for-the-badge&color=gold" alt="Stars"/></a>
    <a href="https://github.com/Waleed-Ahmad-dev/Spotify_Download/blob/main/LICENSE"><img src="https://img.shields.io/github/license/Waleed-Ahmad-dev/Spotify_Download?style=for-the-badge&color=green" alt="License"/></a>
  </p>
</div>

<br />

## Table of Contents

- [Project Overview](#project-overview)
- [Core Features](#core-features)
- [System Architecture & Project Structure](#system-architecture--project-structure)
- [Prerequisites](#prerequisites)
- [Installation Guide](#installation-guide)
- [Command Line Interface (CLI) Usage](#command-line-interface-cli-usage)
  - [Recording Playlists](#recording-playlists)
  - [Searching Tracks](#searching-tracks)
  - [Downloading & Tagging](#downloading--tagging)
  - [All-In-One Execution](#all-in-one-execution)
  - [Library Management (Deduplication)](#library-management-deduplication)
- [Configuration Reference](#configuration-reference)
- [Contribution Guidelines](#contribution-guidelines)
- [Disclaimer & License](#disclaimer--license)

---

## Project Overview

The **Spotify Downloader** is a sophisticated, command-line driven application designed to flawlessly bridge the gap between streaming playlists and local music archiving. Unlike conventional tools that blindly scrape the web, this software implements a multi-stage pipeline: exact-match recording via system audio interfaces, parallelized high-fidelity downloading from YouTube, and meticulous metadata injection.

By integrating seamlessly with APIs like iTunes for track details and LRCLIB for synchronized lyrics, the output is not just a collection of audio files, but a fully structured, locally hosted music library that rivals streaming platforms in organization and metadata depth.

---

## Core Features

- **Precision Spotify Recording (Linux Exclusive):** Integrates with the MPRIS D-Bus interface via `playerctl` to dynamically capture exact track titles and artist names directly from the active Spotify client. Features automated loop detection to gracefully terminate the recording phase.
- **Parallelized Retrieval Engine:** Utilizes Python's `concurrent.futures` to rapidly query YouTube and download audio streams. Automatically resolves rate-limiting and applies fallback mechanisms for maximum reliability.
- **Multi-Format Lossless & Lossy Extraction:** Leverages `yt-dlp` and `FFmpeg` to extract audio directly into standard MP3, highly efficient M4A (AAC), or studio-quality FLAC containers.
- **Industry Standard Audio Normalization:** Features optional integration with FFmpeg's `loudnorm` filter to normalize audio output to -14 LUFS, precisely matching Spotify's standard volume levels across diverse tracks.
- **Comprehensive Metadata Injection:** Dynamically fetches ID3/FLAC/MP4 tags from the iTunes API. Embeds high-resolution cover art (up to 600x600), album details, release dates, and track numbers directly into the audio containers using `mutagen`.
- **Synchronized Lyrics Support:** Interfaces with LRCLIB to retrieve both plain and time-synchronized lyrics, embedding plain lyrics into the audio file and saving synchronized `.lrc` files side-by-side for compatible music players.
- **Intelligent Library Deduplication:** Features a dedicated deduplication engine that parses existing directories, compares tracks based on cryptographic hashes, embedded metadata, and lyrics presence, systematically eliminating inferior duplicates.
- **Rich Terminal User Interface (TUI):** Built with `rich` and `questionary` for an immersive terminal experience, featuring interactive menus, live progress bars with time estimations, and formatted summary tables.

---

## System Architecture & Project Structure

The project strictly adheres to a modular architecture, ensuring separation of concerns and maintainability.

```text
Spotify_Download/
├── main.py
├── recorder.py
├── youtube.py
├── metadata.py
├── utils.py
├── requirements.txt
├── build_deb.sh
├── deb_resources/
│   ├── control
│   └── spotify-downloader.desktop
└── .gitignore
```

### File Level Documentation

- **`main.py`**
  - *Purpose:* The central orchestrator and primary Command Line Interface entry point.
  - *Functionality:* Handles argument parsing using `argparse`, deploys interactive format selection menus via `questionary`, and directs the sequential execution of the recording, searching, downloading, and tagging pipelines. Displays a comprehensive, color-coded summary table upon completion.
- **`recorder.py`**
  - *Purpose:* The system-level Spotify interface module.
  - *Functionality:* Exclusively designed for Linux systems, it utilizes system subprocesses to execute `playerctl` commands. It actively monitors the MPRIS interface to capture the currently playing track and artist. Implements an intelligent loop-detection algorithm to autonomously stop recording when the playlist repeats.
- **`youtube.py`**
  - *Purpose:* The search and high-speed retrieval engine.
  - *Functionality:* Harnesses `yt-dlp` to query YouTube for optimal official audio streams based on the recorded text files. Implements `ThreadPoolExecutor` for concurrent operations, significantly reducing total processing time. Handles stream extraction, format conversion via FFmpeg post-processing, and optional audio normalization parameters.
- **`metadata.py`**
  - *Purpose:* The tag enrichment and file organization subsystem.
  - *Functionality:* Interfaces with the iTunes Search API to resolve pristine track details and the LRCLIB API for localized lyrics. It utilizes the `mutagen` library to safely embed these complex data structures (including binary image data for cover art) directly into the file headers. Additionally, it processes filesystem operations to automatically organize outputs into artist-specific directories.
- **`utils.py`**
  - *Purpose:* A centralized library of shared utility functions and system checks.
  - *Functionality:* Manages the shared `rich.console` environment. Provides critical dependency validation (`check_ffmpeg`, `check_linux_requirements`). Contains the logic for sanitizing filesystem paths, generating `.m3u` playlists that retain original playback order, and hosts the sophisticated metadata-aware deduplication function (`remove_duplicates`).
- **`requirements.txt`**
  - *Purpose:* Environment definition.
  - *Functionality:* Locks down exact Python dependency versions required for stability (e.g., `yt-dlp`, `mutagen`, `rich`, `questionary`).
- **`build_deb.sh` & `deb_resources/`**
  - *Purpose:* Deployment and distribution tooling.
  - *Functionality:* A shell script configured to package the tool into a `.deb` installer for Debian/Ubuntu distributions, utilizing configuration files and standard `.desktop` entries located in `deb_resources` for seamless desktop integration.

---

## Prerequisites

### System Requirements

The application relies on external binaries for media processing and system interaction.

1. **Python 3.8+**
2. **FFmpeg:** Strictly required for audio extraction, format conversion, and LUFS normalization.
3. **Playerctl (Linux Only):** Strictly required if utilizing the `--record` feature to read from Spotify.

**Installation on Debian/Ubuntu:**

```bash
sudo apt update
sudo apt install python3 python3-pip ffmpeg playerctl
```

**Installation on Fedora/RHEL:**

```bash
sudo dnf install python3 python3-pip ffmpeg playerctl
```

**Installation on macOS (Homebrew):**

```bash
brew install python ffmpeg
```

### Python Dependencies

It is highly recommended to isolate the project environment.

```bash
pip install -r requirements.txt
```

---

## Installation Guide

### Option 1: Source Installation (Recommended for All Platforms)

1. Clone the repository to your local machine:

    ```bash
    git clone https://github.com/Waleed-Ahmad-dev/Spotify_Download.git
    cd Spotify_Download
    ```

2. Create and activate a virtual environment:

    ```bash
    python3 -m venv venv
    source venv/bin/activate  # On Windows use: venv\Scripts\activate
    ```

3. Install dependencies:

    ```bash
    pip install -r requirements.txt
    ```

### Option 2: Debian Package Installation (Debian/Ubuntu Only)

If you are using a Debian-based Linux distribution, you can build and install a `.deb` package for system-wide access.

1. Navigate to the repository root.
2. Execute the build script:

    ```bash
    chmod +x build_deb.sh
    ./build_deb.sh
    ```

3. Install the generated package:

    ```bash
    sudo dpkg -i build/spotify-downloader-gui_1.0_all.deb
    sudo apt-get install -f
    ```

---

## Command Line Interface (CLI) Usage

The application is operated entirely through `main.py`. The modular design allows you to run individual stages or the entire pipeline autonomously.

### 1. Recording Playlists (Linux Only)

Capture your currently playing Spotify playlist. Ensure your Spotify client is running, playing the desired playlist, with **Shuffle OFF** and **Repeat ON**.

```bash
python3 main.py --record --input my_playlist.txt
```

### 2. Searching Tracks

Scan YouTube for the most accurate official audio matches based on your recorded text list.

```bash
python3 main.py --search --input my_playlist.txt --found urls.txt --notfound missing.txt --workers 5
```

### 3. Downloading & Tagging

Process the found URLs, extract the audio, embed iTunes metadata, embed lyrics, and organize the files.

```bash
python3 main.py --download --format flac --quality 320 --organize --playlist "MyAwesomeMix"
```

*Note: If `--format` is omitted, an interactive terminal menu will appear prompting you to select your preferred audio format.*

### 4. All-In-One Execution

Execute the entire pipeline seamlessly from recording to final tagging.

```bash
python3 main.py --all --format mp3 --normalize --organize
```

### 5. Library Management (Deduplication)

Clean up an existing music library by removing lower-quality duplicates. The algorithm keeps the version with the highest file size and embedded lyrics.

```bash
python3 main.py --dedupe /path/to/my/music/library
```

---

## Configuration Reference

A complete list of arguments accepted by `main.py`:

| Argument | Type | Default | Description |
| :--- | :---: | :--- | :--- |
| `--record` | Flag | `False` | Initializes the Spotify recording module to capture tracks into a file. |
| `--search` | Flag | `False` | Searches YouTube for tracks listed in the input file. |
| `--download` | Flag | `False` | Downloads audio streams from found URLs and applies metadata tagging. |
| `--all` | Flag | `False` | Runs the Record, Search, and Download pipelines sequentially. |
| `--input` | String | `songs.txt` | Target file for reading track lists or saving recorded tracks. |
| `--found` | String | `found.txt` | Output file for storing successfully matched YouTube URLs. |
| `--notfound` | String | `not_found.txt` | Output file for logging tracks that yielded no search results. |
| `--output-dir` | String | `songs` | The target root directory where audio files will be saved. |
| `--workers` | Integer | `5` | The number of concurrent threads utilized during the search phase. |
| `--quality` | String | `192` | Bitrate for lossy audio formats. Accepted values: `128`, `192`, `320`. |
| `--format` | String | Prompts | Target audio container. Accepted values: `mp3`, `flac`, `m4a`. |
| `--organize` | Flag | `False` | Automatically moves processed files into subdirectories named by Artist. |
| `--resume` | Flag | `False` | Bypasses the YouTube search phase if the found URL file already exists. |
| `--normalize` | Flag | `False` | Applies FFmpeg `-14 LUFS` volume normalization to mirror Spotify audio levels. |
| `--playlist` | String | `None` | Generates an `.m3u` playlist with the specified name preserving record order. |
| `--dedupe` | String | `None` | Path to a directory. Scans and removes duplicate audio files. Overrides all other flags. |

---

## Contribution Guidelines

Contributions are highly encouraged to expand functionality, improve efficiency, or resolve issues. To maintain architectural integrity, please adhere to the following workflow:

1. **Fork the Repository:** Create your own parallel workspace.
2. **Branch Strategically:** Create a dedicated branch for your feature or bug fix (`git checkout -b feature/advanced-caching` or `git checkout -b fix/youtube-rate-limit`).
3. **Strict Typing & PEP 8:** Ensure all Python code includes type hints (`typing` module) and strictly conforms to PEP 8 style guidelines.
4. **Modular Design:** Additions to the core pipeline must be separated into appropriate modules. Avoid monolithic additions to `main.py`.
5. **Commit Conventionally:** Use standardized commit messages (e.g., `feat: implemented asynchronous I/O for API calls`).
6. **Pull Request Review:** Submit a detailed PR explaining the motivation, architectural decisions, and testing validation of your changes.

---

## Disclaimer & License

**Important Disclaimer:** This software is provided strictly for educational and personal archiving purposes. The developers and contributors do not endorse, encourage, or facilitate digital piracy or copyright infringement. Users are solely responsible for ensuring they possess the legal rights to access and create personal backups of the media they process using this tool.

This project is open-source and distributed under the standard GitHub repository parameters. All third-party APIs (iTunes, LRCLIB, YouTube) are accessed in accordance with standard web request paradigms.

**Copyright © 2026 Waleed-Ahmad-dev.**
