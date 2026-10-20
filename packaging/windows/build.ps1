# Builds the Windows release: OpenDriveHealth.exe (GUI) and odh.exe (CLI),
# both with smartctl bundled, then zips them with the licences.
#
# Requirements: Python 3.9+ and smartmontools installed
# (https://www.smartmontools.org/wiki/Download or `choco install smartmontools`).
#
#   powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1

param(
    [string]$SmartmontoolsDir = "C:\Program Files\smartmontools",
    [string]$Version = ""
)
$ErrorActionPreference = "Stop"
$Root = Resolve-Path "$PSScriptRoot\..\.."
$Here = $PSScriptRoot
Set-Location $Root

$Smartctl = Join-Path $SmartmontoolsDir "bin\smartctl.exe"
if (-not (Test-Path $Smartctl)) { throw "smartctl.exe not found at $Smartctl" }

python -m pip install --upgrade pip pyinstaller
python -m pip install .
if (-not $Version) { $Version = python -c "import opendrivehealth; print(opendrivehealth.__version__)" }

$Common = @(
    "--noconfirm", "--clean", "--onefile",
    "--icon", "$Here\icon.ico",
    "--collect-data", "opendrivehealth",
    "--add-binary", "$Smartctl;smartmontools",
    "--distpath", "dist", "--workpath", "build\pyinstaller", "--specpath", "build"
)
$DriveDb = Join-Path $SmartmontoolsDir "bin\drivedb.h"
if (Test-Path $DriveDb) { $Common += @("--add-data", "$DriveDb;smartmontools") }

# The GUI asks for Administrator rights on launch (S.M.A.R.T. needs them).
python -m PyInstaller @Common --windowed --uac-admin --name OpenDriveHealth "$Here\odh_gui.py"
if ($LASTEXITCODE) { throw "GUI build failed" }
python -m PyInstaller @Common --console --name odh "$Here\odh_cli.py"
if ($LASTEXITCODE) { throw "CLI build failed" }

# Package with licences. smartctl is GPL-2.0; we ship it unmodified and point
# to its source, which satisfies its licence for redistribution.
$Pkg = "dist\OpenDriveHealth-$Version-windows"
Remove-Item $Pkg -Recurse -ErrorAction SilentlyContinue
New-Item -ItemType Directory $Pkg | Out-Null
Copy-Item dist\OpenDriveHealth.exe, dist\odh.exe, LICENSE $Pkg
Copy-Item "$Here\README-windows.txt" "$Pkg\README.txt"
$Copying = Get-ChildItem $SmartmontoolsDir -Recurse -Include "COPYING*" | Select-Object -First 1
if ($Copying) { Copy-Item $Copying.FullName "$Pkg\smartmontools-COPYING.txt" }
Compress-Archive -Path "$Pkg\*" -DestinationPath "$Pkg.zip" -Force
Write-Host "Built $Pkg.zip"
