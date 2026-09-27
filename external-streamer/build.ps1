$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$dist = Join-Path $root 'dist'
$work = Join-Path $root 'build'
if (Test-Path $dist) { Remove-Item -Recurse -Force $dist }
if (Test-Path $work) { Remove-Item -Recurse -Force $work }
Push-Location $root
try {
    python -m PyInstaller --noconfirm --clean --onefile --windowed --name external-streamer --distpath $dist --workpath $work --specpath $work src\external_streamer.py
}
finally {
    Pop-Location
}
Copy-Item (Join-Path $dist 'external-streamer.exe') (Join-Path $root 'external-streamer.exe') -Force
Write-Output "Built external-streamer.exe"
