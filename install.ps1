# muse-cli installer for Windows (PowerShell 5.1+).
#
#   irm https://raw.githubusercontent.com/TheBikramLama/muse-cli/main/install.ps1 | iex
#
# What it does:
#   1. Checks for Python 3.10+ (py -3, python, or python3)
#   2. Downloads muse-cli to $env:USERPROFILE\.muse-cli (git clone, or zip if git is missing)
#   3. Creates a virtualenv and installs dependencies (textual)
#   4. Adds a `muse-cli` shim to the user PATH
#   5. Prints next steps (run `muse-cli setup`)
#
# Re-running updates an existing install. Set $env:MUSE_CLI_DIR to override
# the install location.

$ErrorActionPreference = "Stop"

$RepoUrl = "https://github.com/TheBikramLama/muse-cli.git"
$ZipUrl  = "https://codeload.github.com/TheBikramLama/muse-cli/zip/refs/heads/main"
$InstallDir = if ($env:MUSE_CLI_DIR) { $env:MUSE_CLI_DIR } else { "$env:USERPROFILE\.muse-cli" }

function Say($m) { Write-Host "  $m" }
function Ok($m)  { Write-Host "  ✓ $m" -ForegroundColor Green }
function Die($m) { Write-Host "  ✗ $m" -ForegroundColor Red; exit 1 }

Write-Host "muse-cli installer"
Write-Host "=================="

# 1. Python 3.10+
$Python = $null
foreach ($c in @("py -3", "python", "python3")) {
    try {
        $v = & ([ScriptBlock]::Create($c)) -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if ($v -and ([version]$v) -ge ([version]"3.10")) { $Python = $c; $PyVer = $v; break }
    } catch { }
}
if (-not $Python) { Die "Python 3.10+ not found. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH') and re-run." }
Ok "Python $PyVer ($Python)"

# 2. Fetch the repo
$Git = Get-Command git -ErrorAction SilentlyContinue
if ((Test-Path "$InstallDir\.git")) {
    Say "Updating existing install at $InstallDir ..."
    try { & git -C $InstallDir pull --ff-only -q } catch { Say "(could not fast-forward; keeping current checkout)" }
} elseif (Test-Path "$InstallDir\muse_cli") {
    Say "Existing install found at $InstallDir (no git metadata) — refreshing ..."
    Remove-Item -Recurse -Force $InstallDir
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    if ($Git) { & git clone -q --depth 1 $RepoUrl $InstallDir }
    else {
        $zip = "$env:TEMP\muse-cli.zip"
        Invoke-WebRequest -Uri $ZipUrl -OutFile $zip
        Expand-Archive -Path $zip -DestinationPath "$env:TEMP\muse-cli-tmp" -Force
        Copy-Item "$env:TEMP\muse-cli-tmp\muse-cli-main\*" $InstallDir -Recurse -Force
        Remove-Item -Recurse -Force "$env:TEMP\muse-cli-tmp", $zip
    }
} else {
    Say "Installing to $InstallDir ..."
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    if ($Git) { & git clone -q --depth 1 $RepoUrl $InstallDir }
    else {
        $zip = "$env:TEMP\muse-cli.zip"
        Invoke-WebRequest -Uri $ZipUrl -OutFile $zip
        Expand-Archive -Path $zip -DestinationPath "$env:TEMP\muse-cli-tmp" -Force
        Copy-Item "$env:TEMP\muse-cli-tmp\muse-cli-main\*" $InstallDir -Recurse -Force
        Remove-Item -Recurse -Force "$env:TEMP\muse-cli-tmp", $zip
    }
}
Ok "muse-cli sources ready"

# 3. Virtualenv + deps
$VenvPy = "$InstallDir\.venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    Say "Creating virtualenv (first run only) ..."
    & ([ScriptBlock]::Create($Python)) -m venv "$InstallDir\.venv"
}
& $VenvPy -m pip install -q -r "$InstallDir\requirements.txt"
Ok "dependencies installed"

# 4. Shim on PATH
$ShimDir = "$InstallDir\bin"
New-Item -ItemType Directory -Force -Path $ShimDir | Out-Null
@"
@echo off
"$VenvPy" -m muse_cli %*
"@ | Out-File -FilePath "$ShimDir\muse-cli.cmd" -Encoding ascii

$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($UserPath -notlike "*$ShimDir*") {
    [Environment]::SetEnvironmentVariable("Path", "$UserPath;$ShimDir", "User")
    $env:Path += ";$ShimDir"
    Say "Added $ShimDir to your user PATH (new terminals pick it up)."
}
Ok "launcher installed (muse-cli.cmd)"

Write-Host ""
Write-Host "  Done. Next steps (open a NEW terminal first so PATH applies):"
Write-Host "    1. muse-cli setup     # one-time pairing with the Muse app"
Write-Host "    2. muse-cli           # launch the TUI"
Write-Host "    3. muse-cli doctor    # check the connection any time"
