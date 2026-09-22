<#
Builds the accessible edition into one self-contained Windows .exe.

    powershell -ExecutionPolicy Bypass -File build_windows_exe.ps1

Checks the prerequisites first, because the two failure modes that matter
here are silent ones: a missing accessible_output2 produces an .exe that
runs but never speaks, and a missing tkinterdnd2 produces one that runs but
cannot accept dropped files. Both look like a working build.
#>

$ErrorActionPreference = "Stop"
# Anything written to stderr by a probe must not abort the script: importing
# pydub prints a RuntimeWarning about ffmpeg, which is a note, not a failure.
# The exit code is what decides here.
$PSNativeCommandUseErrorActionPreference = $false
Set-Location -Path $PSScriptRoot

function Invoke-Python {
    <#
      Runs python and hands back its exit code and output.

      Wrapped because Windows PowerShell 5.1 turns anything a native command
      writes to stderr into a terminating error while $ErrorActionPreference
      is "Stop" - and both a failed probe import and pydub's ffmpeg notice
      write to stderr as a matter of course. The exit code is the answer.
    #>
    param([string[]]$Arguments)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & python @Arguments 2>&1
        return [pscustomobject]@{ Code = $LASTEXITCODE; Output = $output }
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Test-PythonImport([string]$Module) {
    return (Invoke-Python @("-c", "import $Module")).Code -eq 0
}

$Entry = "PyP6-Roland-P6-Sample-Manager_4_2_3.py"
$Name  = "PyP6-Roland-P6-Sample-Manager_4_2_3"

Write-Host "=== PyP6 accessible edition - Windows build ===" -ForegroundColor Cyan

# --- prerequisites --------------------------------------------------------

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { throw "python was not found on PATH." }
Write-Host ("python : " + (& python --version 2>&1))

# pip name -> importable module name; the two differ often enough that
# guessing one from the other is how a probe ends up testing nothing.
# audioop-lts is checked separately: it is only needed on 3.13 and later,
# where the stdlib audioop that pydub uses was removed.
$required = [ordered]@{
    "pyinstaller" = "PyInstaller"
    "numpy"       = "numpy"
    "sounddevice" = "sounddevice"
    "soundfile"   = "soundfile"
    "pydub"       = "pydub"
}
# Not required, but each one silently removes a feature if it is absent.
$recommended = @{
    "accessible_output2" = "speech through NVDA/JAWS - without it the app falls back to the system voice"
    "tkinterdnd2"        = "dragging files from Explorer onto a pad"
    "comtypes"           = "the SAPI5 fallback voice"
}

$missing = @()
foreach ($package in $required.Keys) {
    if (-not (Test-PythonImport $required[$package])) { $missing += $package }
}
$pyMinor = (Invoke-Python @("-c", "import sys; print(sys.version_info[1])")).Output
if ([int]$pyMinor -ge 13 -and -not (Test-PythonImport "audioop")) {
    $missing += "audioop-lts"
}
if ($missing.Count -gt 0) {
    Write-Host "Missing required packages: $($missing -join ', ')" -ForegroundColor Yellow
    Write-Host "Install them with:  pip install $($missing -join ' ')" -ForegroundColor Yellow
    throw "Cannot build until the required packages are installed."
}

foreach ($module in $recommended.Keys) {
    if (-not (Test-PythonImport $module)) {
        Write-Host ("WARNING: " + $module + " is not installed - the build will work but lose: " +
                    $recommended[$module]) -ForegroundColor Yellow
    }
}

# ffmpeg is not a Python package, so it needs its own check. The spec bundles
# it into the .exe when it can find it; without it the resulting build has no
# MP3 support and no rate/pitch/mono conversion, and nothing in the running
# app says why beyond a warning dialog.
$ffmpegFound = $true
foreach ($tool in @("ffmpeg", "ffprobe")) {
    $vendored = Join-Path "vendor" ($tool + ".exe")
    if (-not (Test-Path $vendored) -and -not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        $ffmpegFound = $false
        Write-Host ("WARNING: " + $tool + " was not found on PATH or in .\vendor\") -ForegroundColor Yellow
    }
}
if (-not $ffmpegFound) {
    Write-Host ("         The .exe will still build and run, but MP3 files and " +
                "rate/pitch/mono conversion will not work in it.") -ForegroundColor Yellow
    Write-Host ("         Fix: install ffmpeg (winget install Gyan.FFmpeg) or copy " +
                "ffmpeg.exe and ffprobe.exe into .\vendor\, then rebuild.") -ForegroundColor Yellow
}

# --- the checks that would otherwise ship broken ---------------------------

Write-Host "`nRunning the accessibility audit..." -ForegroundColor Cyan
$audit = Invoke-Python @("accessibility_audit.py", "--dialogs")
$audit.Output | Select-Object -Last 4
if ($audit.Code -ne 0) {
    throw "The accessibility audit found unnamed controls - fix those before shipping."
}

Write-Host "`nRunning the keyboard tests..." -ForegroundColor Cyan
$keys = Invoke-Python @("test_keyboard_access.py")
$keys.Output | Select-Object -Last 4
if ($keys.Code -ne 0) {
    throw "The keyboard tests failed - fix those before shipping."
}

# --- build ----------------------------------------------------------------

Write-Host "`nBuilding (this takes a few minutes)..." -ForegroundColor Cyan
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
$build = Invoke-Python @("-m", "PyInstaller", "--clean", "--noconfirm", "PyP6.spec")
if ($build.Code -ne 0) {
    $build.Output | Select-Object -Last 30
    throw "PyInstaller failed."
}

$exe = Join-Path "dist" ($Name + ".exe")
if (-not (Test-Path $exe)) { throw "PyInstaller reported success but $exe is not there." }

$sizeMb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host "`nBuilt $exe ($sizeMb MB)" -ForegroundColor Green
Write-Host "Python and every dependency are inside it - nothing to install on the target machine."
Write-Host "`nBefore publishing, start it once and check that:"
Write-Host "  - it speaks its name and version on launch"
Write-Host "  - Tab moves between controls and each one is announced"
Write-Host "  - F1 opens the shortcut list"
