$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name "Station Max Studio v2.1.3" `
    --paths "src" `
    --add-data "src/assets;assets" `
    --collect-all PIL `
    --collect-all customtkinter `
    --collect-all imageio_ffmpeg `
    "src/station_max_studio.py"

if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Built: $projectRoot\dist\Station Max Studio v2.1.3.exe"
