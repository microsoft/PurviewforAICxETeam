param()

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogFile = Join-Path $ScriptDir ".purview-refresh.log"
$Script = Join-Path $ScriptDir "purview-auto-refresh.py"
$Python = (Get-Command python -ErrorAction SilentlyContinue).Path
if (-not $Python) { $Python = (Get-Command py -ErrorAction SilentlyContinue).Path }
if (-not $Python) { throw "Python is not installed or not on PATH." }

& $Python $Script 2>&1 | Out-File -FilePath $LogFile -Append -Encoding utf8
