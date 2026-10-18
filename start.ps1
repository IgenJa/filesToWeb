$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Find-Python {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        return @{ Exe = "py"; Prefix = @("-3") }
    }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python -and $python.Source -notmatch "WindowsApps") {
        return @{ Exe = "python"; Prefix = @() }
    }
    Write-Host "Python 3.10 or newer is required."
    Write-Host "Install it from https://www.python.org/downloads/ and include the py launcher."
    exit 1
}

$found = Find-Python
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "Creating a virtual environment..."
    & $found.Exe @($found.Prefix + @("-m", "venv", ".venv"))
}

Write-Host "Installing dependencies..."
& $venvPython -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host ""
    Write-Host "FFmpeg was not found. Photos will still optimize. Videos need FFmpeg:"
    Write-Host "  winget install Gyan.FFmpeg"
    Write-Host "Close this window after installing it, then start the app again."
    Write-Host ""
}

$env:OPEN_BROWSER = "1"
& $venvPython -m app
exit $LASTEXITCODE
