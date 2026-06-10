# Launch-AgentOnboarder.ps1
# One-click launcher: ensures a Python venv exists, installs dependencies, starts
# the Flask onboarder UI on a free local port, then opens the browser.

param(
    [int] $Port = 0,
    [string] $Host = "127.0.0.1",
    [switch] $NoBrowser
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host ""
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "  Agent SDK Onboarder" -ForegroundColor Cyan
Write-Host "  $root" -ForegroundColor DarkGray
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host ""

# ----- 1. Locate a Python interpreter -----
$python = $null
foreach ($candidate in @("python", "py -3", "python3")) {
    try {
        $ver = (& $candidate.Split()[0] $candidate.Split()[1..($candidate.Split().Length-1)] --version) 2>$null
        if ($LASTEXITCODE -eq 0 -and $ver) {
            $python = $candidate
            Write-Host "Python: $ver" -ForegroundColor Green
            break
        }
    } catch {}
}
if (-not $python) {
    Write-Host "ERROR: Python 3 was not found on PATH." -ForegroundColor Red
    Write-Host "Install Python 3.10+ from https://www.python.org/ and re-run." -ForegroundColor Yellow
    exit 1
}

# ----- 2. Create/refresh the local venv -----
$venv = Join-Path $root ".venv"
if (-not (Test-Path "$venv\Scripts\python.exe")) {
    Write-Host "Creating virtual environment (.venv) ..." -ForegroundColor Yellow
    & $python.Split()[0] $python.Split()[1..($python.Split().Length-1)] -m venv $venv
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Failed to create venv." -ForegroundColor Red
        exit 1
    }
}
$venvPy = Join-Path $venv "Scripts\python.exe"

# ----- 3. Install dependencies -----
$marker = Join-Path $venv ".deps-installed"
$req = Join-Path $root "requirements.txt"
$reqHash = (Get-FileHash $req -Algorithm MD5).Hash
$needsInstall = $true
if (Test-Path $marker) {
    if ((Get-Content $marker -Raw).Trim() -eq $reqHash) { $needsInstall = $false }
}
if ($needsInstall) {
    Write-Host "Installing Python dependencies (one-time)..." -ForegroundColor Yellow
    & $venvPy -m pip install --upgrade pip --quiet
    & $venvPy -m pip install -r $req --quiet
    if ($LASTEXITCODE -ne 0) {
        Write-Host "pip install failed; see output above." -ForegroundColor Red
        exit 1
    }
    Set-Content -Path $marker -Value $reqHash -NoNewline
    Write-Host "Dependencies installed." -ForegroundColor Green
} else {
    Write-Host "Dependencies up-to-date." -ForegroundColor DarkGray
}

# ----- 4. Pick a free port if not specified -----
if ($Port -eq 0) {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $Port = $listener.LocalEndpoint.Port
    $listener.Stop()
}

$env:ONBOARDER_HOST = $Host
$env:ONBOARDER_PORT = "$Port"
$env:ONBOARDER_ROOT = $root

# ----- Azure Key Vault for secrets (no literal secrets on local disk) -----
# Onboarder refuses to create new agents unless this is set. Override by
# exporting AGENT_KV_VAULT_NAME before invoking the launcher.
if (-not $env:AGENT_KV_VAULT_NAME) {
    $env:AGENT_KV_VAULT_NAME = "SDKOnboarder"
}
Write-Host "Key Vault for secrets: $($env:AGENT_KV_VAULT_NAME) (https://$($env:AGENT_KV_VAULT_NAME.ToLower()).vault.azure.net/)" -ForegroundColor Green

$url = "http://${Host}:$Port/"

Write-Host ""
Write-Host "Starting onboarder on $url" -ForegroundColor Cyan
Write-Host ""

# ----- 5. Open browser shortly after start -----
if (-not $NoBrowser) {
    Start-Job -ScriptBlock {
        Start-Sleep -Seconds 2
        Start-Process $using:url
    } | Out-Null
}

# ----- 6. Run Flask (foreground) -----
Set-Location (Join-Path $root "app")
& $venvPy onboarder.py
