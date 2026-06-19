<#
  build_windows.ps1
  Build a self-contained Windows .exe for the Spotify -> Audio Downloader.

  Usage:
      powershell -ExecutionPolicy Bypass -File build_windows.ps1
  (or right-click the file -> "Run with PowerShell")

  Output: dist\spotify-downloader\spotify-downloader.exe  (+ bundled deps).
  This is the Windows counterpart of build_deb.sh.
#>

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

Write-Host "==> Spotify Downloader - Windows build" -ForegroundColor Cyan

# ---- 1. Locate Python (prefer the 'py' launcher) ---------------------------
if (Get-Command py -ErrorAction SilentlyContinue) {
    $python = 'py'
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $python = 'python'
} else {
    Write-Error "Python not found on PATH. Install Python 3.8+ from python.org first."
}
Write-Host "Using Python launcher: $python"

# ---- 2. Warn if ffmpeg/ffprobe aren't on PATH ------------------------------
# The spec bundles them from PATH into vendor/. Without them the .exe will
# require ffmpeg to be installed on the target machine instead.
foreach ($tool in 'ffmpeg', 'ffprobe') {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Write-Warning "$tool not on PATH - it won't be bundled. Install: winget install Gyan.FFmpeg"
    }
}

# ---- 3. Install runtime deps + PyInstaller ---------------------------------
Write-Host "==> Installing dependencies..." -ForegroundColor Cyan
& $python -m pip install --upgrade pip
& $python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { Write-Error "pip install -r requirements.txt failed." }
& $python -m pip install pyinstaller
if ($LASTEXITCODE -ne 0) { Write-Error "pip install pyinstaller failed." }

# ---- 4. Clean previous build -----------------------------------------------
foreach ($d in 'build', 'dist') {
    if (Test-Path $d) { Remove-Item -Recurse -Force $d }
}

# ---- 5. Build ---------------------------------------------------------------
Write-Host "==> Running PyInstaller..." -ForegroundColor Cyan
& $python -m PyInstaller --noconfirm spotify_downloader.spec
if ($LASTEXITCODE -ne 0) { Write-Error "PyInstaller build failed." }

# ---- 6. Report --------------------------------------------------------------
$exe = Join-Path $PSScriptRoot 'dist\spotify-downloader\spotify-downloader.exe'
if (Test-Path $exe) {
    Write-Host ""
    Write-Host "SUCCESS! Built: $exe" -ForegroundColor Green
    Write-Host "Distribute the entire 'dist\spotify-downloader\' folder."
} else {
    Write-Error "Build finished but $exe was not found - check the PyInstaller output above."
}
