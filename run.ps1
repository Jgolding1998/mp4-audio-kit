# Creates .venv on first run (exact pinned, hash-checked packages), then starts the app.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    python -I -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create .venv. Install Python 3.11 or newer, then retry." }
}
$requirementsHash = (Get-FileHash -LiteralPath "requirements.txt" -Algorithm SHA256).Hash.ToLowerInvariant()
$installedHash = if (Test-Path ".venv\.installed") { [string]::Concat('', (Get-Content -LiteralPath ".venv\.installed" -Raw)).Trim() } else { "" }
if ($installedHash -ne $requirementsHash) {
    .\.venv\Scripts\python.exe -I -m pip --isolated --disable-pip-version-check --require-virtualenv install --index-url https://pypi.org/simple --require-hashes --only-binary=:all: -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "Package install failed. Run this script again to retry." }
    [System.IO.File]::WriteAllText((Join-Path $PSScriptRoot ".venv\.installed"), $requirementsHash)
}
.\.venv\Scripts\python.exe -m mp4_audio_kit @args
