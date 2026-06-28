# Builds a portable Windows release in release\ATP_Stock_Report\
# Does not modify the dev project - only copies files into release\

param(
    [string]$UpdateBaseUrl = ""
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ReleaseName = "ATP_Stock_Report"
$ReleaseDir  = Join-Path $ProjectRoot "release\$ReleaseName"
$ArtifactsDir = Join-Path $ProjectRoot "release\publish"
$PythonVer   = "3.11.9"
$EmbedZip    = "python-$PythonVer-embed-amd64.zip"
$EmbedUrl    = "https://www.python.org/ftp/python/$PythonVer/$EmbedZip"
$GetPipUrl   = "https://bootstrap.pypa.io/get-pip.py"
$CacheDir    = Join-Path $ProjectRoot "release\.cache"
$VersionFile = Join-Path $ProjectRoot "VERSION"

if (-not (Test-Path $VersionFile)) {
    throw "VERSION file not found at $VersionFile"
}
$AppVersion = (Get-Content -Path $VersionFile -Raw).Trim()
if ([string]::IsNullOrWhiteSpace($AppVersion)) {
    throw "VERSION file is empty"
}

Write-Host "=== ATP Stock Report - Windows Release Build ===" -ForegroundColor Cyan
Write-Host "Project: $ProjectRoot"
Write-Host "Version: $AppVersion"
Write-Host "Output:  $ReleaseDir"
Write-Host ""

# Clean previous release
if (Test-Path $ReleaseDir) {
    Write-Host "Removing previous release folder..."
    Remove-Item $ReleaseDir -Recurse -Force
}

New-Item -ItemType Directory -Path (Join-Path $ProjectRoot "release") -Force | Out-Null
New-Item -ItemType Directory -Path $ReleaseDir -Force | Out-Null
New-Item -ItemType Directory -Path $CacheDir -Force | Out-Null
New-Item -ItemType Directory -Path $ArtifactsDir -Force | Out-Null

# Copy application files (source tree untouched)
Write-Host "Copying application files..."
Copy-Item (Join-Path $ProjectRoot "app.py")     $ReleaseDir
Copy-Item (Join-Path $ProjectRoot "Groups.csv") $ReleaseDir
Copy-Item (Join-Path $ProjectRoot "src")        $ReleaseDir -Recurse
Copy-Item $VersionFile                          $ReleaseDir

# Updater + config
Copy-Item (Join-Path $ProjectRoot "scripts\update_client.ps1") $ReleaseDir
$ConfigTemplate = Join-Path $ProjectRoot "scripts\update_config.template.json"
Copy-Item $ConfigTemplate (Join-Path $ReleaseDir "update_config.json")

# Download embeddable Python
$EmbedZipPath = Join-Path $CacheDir $EmbedZip
if (-not (Test-Path $EmbedZipPath)) {
    Write-Host "Downloading Python $PythonVer embeddable..."
    Invoke-WebRequest -Uri $EmbedUrl -OutFile $EmbedZipPath -UseBasicParsing
} else {
    Write-Host "Using cached Python embeddable package."
}

$PythonDir = Join-Path $ReleaseDir "python"
Write-Host "Extracting Python to $PythonDir..."
Expand-Archive -Path $EmbedZipPath -DestinationPath $PythonDir -Force

$PythonExe = Join-Path $PythonDir "python.exe"
if (-not (Test-Path $PythonExe)) {
    throw "python.exe not found after extraction: $PythonExe"
}

# Enable site-packages in embeddable Python
$PthFile = Get-ChildItem -Path $PythonDir -Filter "python*._pth" | Select-Object -First 1
if (-not $PthFile) { throw "Could not find python*._pth in $PythonDir" }

$SitePackages = Join-Path $PythonDir "Lib\site-packages"
New-Item -ItemType Directory -Path $SitePackages -Force | Out-Null

$PthContent = @"
python311.zip
.
Lib\site-packages
import site
"@
Set-Content -Path $PthFile.FullName -Value $PthContent -Encoding ASCII

# Install pip + dependencies into embedded Python
$GetPipPath = Join-Path $CacheDir "get-pip.py"
if (-not (Test-Path $GetPipPath)) {
    Write-Host "Downloading get-pip.py..."
    Invoke-WebRequest -Uri $GetPipUrl -OutFile $GetPipPath -UseBasicParsing
}

Write-Host "Installing pip into embedded Python..."
& $PythonExe $GetPipPath --no-warn-script-location
if ($LASTEXITCODE -ne 0) { throw "get-pip.py failed" }

$LockFile = Join-Path $ProjectRoot "requirements-lock.txt"
if (-not (Test-Path $LockFile)) {
    throw "requirements-lock.txt not found at $LockFile"
}

Write-Host "Installing dependencies (this may take a few minutes)..."
& $PythonExe -m pip install --no-warn-script-location -r $LockFile
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

Write-Host "Verifying imports..."
& $PythonExe -c "import streamlit, pandas, openpyxl, xlrd; print('All imports OK')"
if ($LASTEXITCODE -ne 0) { throw "Import verification failed" }

function Write-LauncherBat {
    param(
        [string]$Path,
        [bool]$CheckForUpdates
    )

    $updateBlock = ""
    if ($CheckForUpdates) {
        $updateBlock = @"
echo  Checking for updates...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update_client.ps1"
echo.
"@
    }

    @"
@echo off
cd /d "%~dp0"
title ATP Stock Report
echo.
echo  ATP Stock Report
echo  ================
$updateBlock
echo  Starting... a browser window will open in a few seconds.
echo  Keep this window open while using the app.
echo  Close this window to stop the app.
echo.
start "" cmd /c "ping -n 4 127.0.0.1 >nul && start http://localhost:8501"
"%~dp0python\python.exe" -m streamlit run "%~dp0app.py" --server.headless true --browser.gatherUsageStats false --server.port 8501 --server.address 127.0.0.1
if errorlevel 1 (
    echo.
    echo  Something went wrong. Press any key to close.
    pause >nul
)
"@ | Set-Content -Path $Path -Encoding ASCII
}

# Launchers
$LauncherPath = Join-Path $ReleaseDir "Start ATP Stock Report.bat"
Write-LauncherBat -Path $LauncherPath -CheckForUpdates $true

$NoUpdateLauncherPath = Join-Path $ReleaseDir "Start ATP Stock Report (no update check).bat"
Write-LauncherBat -Path $NoUpdateLauncherPath -CheckForUpdates $false

# Client README
$ReadmePath = Join-Path $ReleaseDir "README.txt"
@'
ATP STOCK REPORT - QUICK START
==============================

1. Unzip this entire folder anywhere on your PC (e.g. Desktop).

2. Double-click "Start ATP Stock Report.bat".

3. Your web browser will open automatically. If it does not, go to:
   http://localhost:8501

4. Drag and drop your Stock on Hand (SOH) file (.xls or .xlsx) into the upload area.

5. View the stock table, print it (Ctrl+P), or click "Download Excel Report".

6. When finished, close the black command window to stop the app.


NOTES
-----
- No internet connection is required for normal use.
- Do not delete or move files inside this folder.
- If you see "unmapped product codes", those codes need to be added to Groups.csv
  (contact your administrator).

UPDATES
-------
- On launch, the app may check for updates (you will be asked before anything installs).
- To skip update checks, use "Start ATP Stock Report (no update check).bat".
- If an update fails, the previous version is restored automatically from backup\.
- You can still replace this entire folder with a new zip from your administrator.


TROUBLESHOOTING
---------------
- "Windows protected your PC": click "More info" then "Run anyway"
  (the app is not signed with a commercial certificate).

- Port already in use: close any other copy of this app, then try again.

- Antivirus blocking: allow "python.exe" inside the python\ subfolder.
'@ | Set-Content -Path $ReadmePath -Encoding UTF8

# App-only patch zip (for remote updates)
$PatchName = "app-$AppVersion.zip"
$PatchZipPath = Join-Path $ArtifactsDir $PatchName
$PatchStage = Join-Path $ProjectRoot "release\.patch_stage"
if (Test-Path $PatchStage) { Remove-Item $PatchStage -Recurse -Force }
New-Item -ItemType Directory -Path $PatchStage -Force | Out-Null
Copy-Item (Join-Path $ProjectRoot "app.py") $PatchStage
Copy-Item (Join-Path $ProjectRoot "Groups.csv") $PatchStage
Copy-Item (Join-Path $ProjectRoot "src") (Join-Path $PatchStage "src") -Recurse
Copy-Item $VersionFile $PatchStage
if (Test-Path $PatchZipPath) { Remove-Item $PatchZipPath -Force }
Write-Host "Creating app patch zip..."
Compress-Archive -Path (Join-Path $PatchStage "*") -DestinationPath $PatchZipPath -CompressionLevel Optimal
Remove-Item $PatchStage -Recurse -Force

# version.json manifest for upload
$baseUrl = $UpdateBaseUrl.TrimEnd("/")
if ([string]::IsNullOrWhiteSpace($baseUrl)) {
    $baseUrl = "https://YOUR_HOST/releases"
}
$manifest = [ordered]@{
    version = $AppVersion
    app_zip_url = "$baseUrl/$PatchName"
    full_zip_url = "$baseUrl/$ReleaseName-$AppVersion.zip"
    requirements_changed = $false
    release_notes = ""
}
$ManifestPath = Join-Path $ArtifactsDir "version.json"
$manifest | ConvertTo-Json | Set-Content -Path $ManifestPath -Encoding UTF8

# Full zip archive
$ZipPath = Join-Path $ProjectRoot "release\$ReleaseName.zip"
$VersionedZipPath = Join-Path $ArtifactsDir "$ReleaseName-$AppVersion.zip"
foreach ($path in @($ZipPath, $VersionedZipPath)) {
    if (Test-Path $path) { Remove-Item $path -Force }
}

Write-Host "Creating full zip archives..."
Compress-Archive -Path $ReleaseDir -DestinationPath $ZipPath -CompressionLevel Optimal
Copy-Item $ZipPath $VersionedZipPath

$FolderSize = (Get-ChildItem $ReleaseDir -Recurse | Measure-Object -Property Length -Sum).Sum / 1MB
$ZipSize    = (Get-Item $ZipPath).Length / 1MB
$PatchSize  = (Get-Item $PatchZipPath).Length / 1MB

Write-Host ""
Write-Host "=== Build complete ===" -ForegroundColor Green
Write-Host "Version: $AppVersion"
Write-Host "Folder:  $ReleaseDir ($([math]::Round($FolderSize, 1)) MB)"
Write-Host "Zip:     $ZipPath ($([math]::Round($ZipSize, 1)) MB)"
Write-Host ""
Write-Host "Publish artifacts (upload to your update host):" -ForegroundColor Cyan
Write-Host "  Patch:    $PatchZipPath ($([math]::Round($PatchSize, 2)) MB)"
Write-Host "  Manifest: $ManifestPath"
Write-Host "  Full zip: $VersionedZipPath ($([math]::Round($ZipSize, 1)) MB)"
Write-Host ""
Write-Host "Before enabling updates for clients:" -ForegroundColor Yellow
Write-Host "  1. Upload app-$AppVersion.zip and version.json to your host"
Write-Host "  2. Edit update_config.json in the client folder (or rebuild with -UpdateBaseUrl)"
Write-Host ""
Write-Host "Ship release\$ReleaseName.zip to new clients. They unzip and double-click the .bat file."
