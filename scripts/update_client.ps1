# Checks for app updates before launch. Fail-open: always exits 0 unless the user aborts.
# Shipped inside the client release folder alongside update_config.json.

param(
    [switch]$SkipPrompt
)

$ErrorActionPreference = "Stop"

$InstallRoot = $PSScriptRoot
$ConfigPath = Join-Path $InstallRoot "update_config.json"
$VersionPath = Join-Path $InstallRoot "VERSION"
$PythonExe = Join-Path $InstallRoot "python\python.exe"
$BackupRoot = Join-Path $InstallRoot "backup"

function Write-Info([string]$Message) {
    Write-Host $Message
}

function Write-Warn([string]$Message) {
    Write-Host $Message -ForegroundColor Yellow
}

function Get-LocalVersion {
    if (-not (Test-Path $VersionPath)) {
        return "0.0.0"
    }
    return (Get-Content -Path $VersionPath -Raw).Trim()
}

function Compare-SemVer {
    param([string]$Left, [string]$Right)

    $leftParts = $Left.Split(".") | ForEach-Object { [int]$_ }
    $rightParts = $Right.Split(".") | ForEach-Object { [int]$_ }
    $count = [Math]::Max($leftParts.Count, $rightParts.Count)

    for ($i = 0; $i -lt $count; $i++) {
        $lv = if ($i -lt $leftParts.Count) { $leftParts[$i] } else { 0 }
        $rv = if ($i -lt $rightParts.Count) { $rightParts[$i] } else { 0 }
        if ($lv -lt $rv) { return -1 }
        if ($lv -gt $rv) { return 1 }
    }
    return 0
}

function Test-PlaceholderUrl {
    param([string]$Url)
    return ($Url -match "YOUR_HOST" -or [string]::IsNullOrWhiteSpace($Url))
}

function Backup-AppFiles {
    param([string]$BackupDir)

    New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null

    Copy-Item (Join-Path $InstallRoot "app.py") $BackupDir -Force
    Copy-Item (Join-Path $InstallRoot "Groups.csv") $BackupDir -Force
    Copy-Item (Join-Path $InstallRoot "VERSION") $BackupDir -Force
    Copy-Item (Join-Path $InstallRoot "src") (Join-Path $BackupDir "src") -Recurse -Force
}

function Restore-AppFiles {
    param([string]$BackupDir)

    Copy-Item (Join-Path $BackupDir "app.py") $InstallRoot -Force
    Copy-Item (Join-Path $BackupDir "Groups.csv") $InstallRoot -Force
    Copy-Item (Join-Path $BackupDir "VERSION") $InstallRoot -Force

    $srcBackup = Join-Path $BackupDir "src"
    $srcTarget = Join-Path $InstallRoot "src"
    if (Test-Path $srcTarget) {
        Remove-Item $srcTarget -Recurse -Force
    }
    Copy-Item $srcBackup $srcTarget -Recurse -Force
}

function Test-AppImports {
    if (-not (Test-Path $PythonExe)) {
        throw "Embedded python.exe not found: $PythonExe"
    }
    & $PythonExe -c "import streamlit, pandas, openpyxl, xlrd"
    if ($LASTEXITCODE -ne 0) {
        throw "Import verification failed after update."
    }
}

function Apply-AppPatch {
    param([string]$ZipPath)

    $localVersion = Get-LocalVersion
    $backupDir = Join-Path $BackupRoot $localVersion
    Backup-AppFiles -BackupDir $backupDir

    $extractDir = Join-Path ([System.IO.Path]::GetTempPath()) ("atp_update_" + [guid]::NewGuid().ToString())
    New-Item -ItemType Directory -Path $extractDir -Force | Out-Null

    try {
        Expand-Archive -Path $ZipPath -DestinationPath $extractDir -Force

        foreach ($rel in @("app.py", "Groups.csv", "VERSION")) {
            $src = Join-Path $extractDir $rel
            if (-not (Test-Path $src)) {
                throw "Patch zip is missing required file: $rel"
            }
            Copy-Item $src (Join-Path $InstallRoot $rel) -Force
        }

        $srcFolder = Join-Path $extractDir "src"
        if (-not (Test-Path $srcFolder)) {
            throw "Patch zip is missing required folder: src"
        }

        $targetSrc = Join-Path $InstallRoot "src"
        if (Test-Path $targetSrc) {
            Remove-Item $targetSrc -Recurse -Force
        }
        Copy-Item $srcFolder $targetSrc -Recurse -Force

        Test-AppImports
    }
    catch {
        Write-Warn "Update failed: $($_.Exception.Message)"
        Write-Warn "Restoring previous version ($localVersion)..."
        Restore-AppFiles -BackupDir $backupDir
        throw
    }
    finally {
        if (Test-Path $extractDir) {
            Remove-Item $extractDir -Recurse -Force -ErrorAction SilentlyContinue
        }
        if (Test-Path $ZipPath) {
            Remove-Item $ZipPath -Force -ErrorAction SilentlyContinue
        }
    }
}

try {
    if (-not (Test-Path $ConfigPath)) {
        exit 0
    }

    $config = Get-Content -Path $ConfigPath -Raw | ConvertFrom-Json
    $manifestUrl = [string]$config.manifest_url
    if (Test-PlaceholderUrl $manifestUrl) {
        exit 0
    }

    $localVersion = Get-LocalVersion
    Write-Info ""
    Write-Info " Checking for updates (installed: v$localVersion)..."

    try {
        $manifestResponse = Invoke-WebRequest -Uri $manifestUrl -UseBasicParsing -TimeoutSec 15
    }
    catch {
        Write-Warn " Could not reach update server. Starting with current version."
        exit 0
    }

    $manifest = $manifestResponse.Content | ConvertFrom-Json
    $remoteVersion = [string]$manifest.version
    if ([string]::IsNullOrWhiteSpace($remoteVersion)) {
        exit 0
    }

    if ((Compare-SemVer $localVersion $remoteVersion) -ge 0) {
        Write-Info " App is up to date."
        exit 0
    }

    Write-Info ""
    Write-Info " Update available: v$localVersion -> v$remoteVersion"
    if ($manifest.release_notes) {
        Write-Info ""
        Write-Info " $($manifest.release_notes)"
    }

    if ($manifest.requirements_changed -eq $true) {
        Write-Warn ""
        Write-Warn " This release includes dependency changes and needs a full reinstall."
        if ($manifest.full_zip_url) {
            Write-Warn " Download: $($manifest.full_zip_url)"
        }
        Write-Warn " Unzip the full package over this folder, or ask your administrator."
        exit 0
    }

    if (-not $manifest.app_zip_url) {
        Write-Warn " Update manifest is missing app_zip_url."
        exit 0
    }

    if (-not $SkipPrompt) {
        $answer = Read-Host " Install update now? [Y/N]"
        if ($answer -notmatch '^[Yy]') {
            Write-Info " Skipping update."
            exit 0
        }
    }

    $tempZip = Join-Path ([System.IO.Path]::GetTempPath()) ("atp_app_$remoteVersion.zip")
    Write-Info " Downloading update..."
    Invoke-WebRequest -Uri $manifest.app_zip_url -OutFile $tempZip -UseBasicParsing -TimeoutSec 120

    Apply-AppPatch -ZipPath $tempZip
    Write-Info " Update installed successfully (v$remoteVersion)."
    Write-Info ""
}
catch {
    Write-Warn " Update check failed: $($_.Exception.Message)"
    Write-Warn " Starting with current version."
}

exit 0
