#!/usr/bin/env pwsh
<#
.SYNOPSIS
  Build the standalone Agent365PurviewSDKOnboarder.exe using PyInstaller.

.DESCRIPTION
  Creates a fresh build venv (.venv-build/), installs the package + build
  extras, then runs PyInstaller with the bundled spec. Two layouts:

    * Default (one-FILE): dist\Agent365PurviewSDKOnboarder.exe
        Self-contained .exe (~26 MB). Extracts python3XX.dll to %TEMP% on
        each launch. Convenient — but some WDAC / Application Control
        policies block DLL execution from %TEMP%.
    * -OneDir (one-DIR): dist\Agent365PurviewSDKOnboarder\
        Folder containing .exe + python3XX.dll + libs sibling. Larger on
        disk but DLLs live in a stable, user-app path that many WDAC
        policies allow even when temp-extraction is blocked. Distribute as
        a .zip.

  The build venv is kept around (about 200 MB) so subsequent runs are fast.
  Delete .venv-build/ to force a clean reinstall.

.EXAMPLE
  .\scripts\build_exe.ps1
  .\scripts\build_exe.ps1 -OneDir
  .\scripts\build_exe.ps1 -Clean   # nukes .venv-build, dist, build first
#>
param(
  [switch]$Clean,
  [switch]$OneDir
)

$ErrorActionPreference = 'Stop'
$projectRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $projectRoot

if ($Clean) {
  Write-Host "[clean] removing .venv-build, dist, build" -ForegroundColor Yellow
  Remove-Item -Recurse -Force .venv-build, dist, build -ErrorAction SilentlyContinue
}

$venv = Join-Path $projectRoot '.venv-build'
$py   = Join-Path $venv 'Scripts\python.exe'

if (-not (Test-Path $py)) {
  Write-Host "[venv] creating build venv at .venv-build/" -ForegroundColor Cyan
  python -m venv $venv
  & $py -m pip install --upgrade pip wheel setuptools
  Write-Host "[deps] installing project + [build] extras" -ForegroundColor Cyan
  & $py -m pip install -e ".[build]"
} else {
  Write-Host "[venv] reusing existing .venv-build/" -ForegroundColor Cyan
}

Write-Host "[pyinstaller] building $(if($OneDir){'one-DIR folder'}else{'one-file .exe'})" -ForegroundColor Cyan
$spec = if ($OneDir) { 'installer\agent365_onboarder_onedir.spec' } else { 'installer\agent365_onboarder.spec' }
& $py -m PyInstaller $spec --clean --noconfirm

if ($OneDir) {
  $target = Join-Path $projectRoot 'dist\Agent365PurviewSDKOnboarder\Agent365PurviewSDKOnboarder.exe'
} else {
  $target = Join-Path $projectRoot 'dist\Agent365PurviewSDKOnboarder.exe'
}
if (Test-Path $target) {
  $size = [math]::Round((Get-Item $target).Length / 1MB, 1)
  Write-Host ""
  Write-Host "[ok] built: $target ($size MB)" -ForegroundColor Green
  Write-Host "     run it: $target"
} else {
  Write-Host "[error] expected $target was not produced" -ForegroundColor Red
  exit 1
}
