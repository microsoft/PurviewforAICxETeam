#requires -Version 5.1
<#
  Diagnose-Environment.ps1
  Standalone runner for app\diagnostics.py — prints prerequisite checks
  for the local machine, the signed-in tenant, and (optionally) a provider config.

  Usage:
      .\Diagnose-Environment.ps1
      .\Diagnose-Environment.ps1 -Provider custom -HttpUrl https://my-agent.example.com/chat
#>
[CmdletBinding()]
param(
    [string]$Provider,
    [string]$TenantId,
    [int]$ServerPort = 8080,

    # Provider-specific (optional — only used if -Provider is set)
    [string]$VertexProject,
    [string]$VertexLocation,
    [string]$VertexResourceName,
    [string]$AzureOpenAIEndpoint,
    [string]$AzureOpenAIDeployment,
    [string]$AzureOpenAIApiKey,
    [string]$OpenAIApiKey,
    [string]$OpenAIModel = "gpt-4o-mini",
    [string]$HttpUrl,
    [string]$HttpMethod = "POST",
    [string]$HttpPromptField = "prompt",
    [string]$HttpResponseJsonpath = "response",
    [string]$HttpAuthHeader
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppDir    = Join-Path $ScriptDir "app"
$VenvDir   = Join-Path $ScriptDir ".venv"
$VenvPy    = Join-Path $VenvDir   "Scripts\python.exe"

function Find-Python {
    foreach ($cmd in @("py","python","python3")) {
        $exe = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($exe) { return $exe.Source }
    }
    return $null
}

# Prefer existing venv (set up by Launch-AgentOnboarder.ps1); otherwise use system Python.
$Py = if (Test-Path $VenvPy) { $VenvPy } else { Find-Python }
if (-not $Py) {
    Write-Host ""
    Write-Host "ERROR: No Python interpreter found on PATH." -ForegroundColor Red
    Write-Host "Install Python 3.10+ from https://www.python.org/downloads/ and rerun." -ForegroundColor Yellow
    exit 1
}

# Build the config-as-JSON payload for diagnostics.run_all
$cfg = @{
    server          = @{ port = $ServerPort }
    tenant_id       = ""
    provider        = ""
    provider_config = @{}
}

if ($TenantId) { $cfg.tenant_id = $TenantId }

if ($Provider) {
    $cfg.provider = $Provider.ToLower()
    $cfg.provider_config = @{
        vertex_project            = $VertexProject
        vertex_location           = $VertexLocation
        vertex_resource_name      = $VertexResourceName
        vertex_adc_path           = ""
        azure_openai_endpoint     = $AzureOpenAIEndpoint
        azure_openai_deployment   = $AzureOpenAIDeployment
        azure_openai_api_version  = "2024-08-01-preview"
        azure_openai_api_key      = $AzureOpenAIApiKey
        openai_model              = $OpenAIModel
        openai_api_key            = $OpenAIApiKey
        http_url                  = $HttpUrl
        http_method               = $HttpMethod
        http_prompt_field         = $HttpPromptField
        http_response_jsonpath    = $HttpResponseJsonpath
        http_auth_header          = $HttpAuthHeader
    }
}

$cfgJson = $cfg | ConvertTo-Json -Depth 6 -Compress

# Run diagnostics module as `python -m diagnostics` with cfg piped on stdin
Push-Location $AppDir
try {
    # Inline driver: read JSON cfg from stdin, run all checks, pretty-print.
    $driver = @'
import json, sys, os
# Force UTF-8 on Windows consoles
if os.name == "nt":
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.SetConsoleMode(k.GetStdHandle(-11), 7)
        k.SetConsoleOutputCP(65001)
    except Exception: pass
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass
import diagnostics
cfg = None
data = sys.stdin.read().strip()
if data:
    try: cfg = json.loads(data)
    except Exception as e: print(f"(stdin parse error: {e}; running with no provider/tenant filter)")
print("\n>>> Agent SDK Onboarder - Environment Diagnostics\n")
report = diagnostics.run_all(cfg)
diagnostics.print_report(report, color=True)
out = os.path.join(os.path.dirname(diagnostics.__file__), "..", "diagnostics-report.json")
out = os.path.abspath(out)
with open(out, "w", encoding="utf-8") as f: json.dump(report, f, indent=2)
print(f"\n  Full report saved to: {out}")
sys.exit(0 if report["overall"] != "fail" else 1)
'@
    $cfgJson | & $Py -c $driver
    $rc = $LASTEXITCODE
} finally {
    Pop-Location
}

exit $rc
