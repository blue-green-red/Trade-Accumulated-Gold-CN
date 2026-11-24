# Run from a signed-in Windows desktop. The JD window must show the bank's gold page.
param(
    [ValidateSet('zheshang', 'minsheng')]
    [string]$Bank = 'zheshang',
    [switch]$EnableAI,
    [switch]$EnableAutoPrepare,
    [switch]$EnableNotifications
)

$ErrorActionPreference = 'Stop'
if ($EnableAutoPrepare -and !$EnableAI) {
    throw '-EnableAutoPrepare requires -EnableAI.'
}
$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo '.venv\Scripts\python.exe'
$frontend = Join-Path $repo 'frontend'
$npm = (Get-Command npm.cmd).Source
$dataDir = Join-Path $repo 'monitor\data\desktop'
if (!(Test-Path -LiteralPath $python) -or !(Test-Path -LiteralPath (Join-Path $frontend 'node_modules\vite'))) {
    throw 'Install the Python requirements in .venv and run npm ci in frontend before starting.'
}
if (Get-NetTCPConnection -State Listen -LocalPort 18080,5173 -ErrorAction SilentlyContinue) {
    throw 'Port 18080 or 5173 is already occupied.'
}

New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
$env:GOLD_MONITOR_DB = Join-Path $dataDir 'monitor.db'
$env:GOLD_JD_LIVE_SOURCE = 'desktop'
$env:GOLD_AI_TRADING_MODE = 'live_only'
$env:GOLD_AI_ANALYSTS = if ($EnableAI) { 'codex' } else { 'none' }
$env:GOLD_JD_DESKTOP_AUTO_PREPARE = if ($EnableAutoPrepare) { '1' } else { '0' }
$env:GOLD_WEB_HOST = '127.0.0.1'
$env:GOLD_WEB_PORT = '18080'
$env:PYTHONUTF8 = '1'
$env:PYTHONUNBUFFERED = '1'

Push-Location -LiteralPath $repo
try {
    & $python -c "import sys; from monitor.config import Settings; from monitor.store import Store; from monitor.jd_live_readonly import ensure_schema, set_enabled_banks; s = Store(Settings().db_path); ensure_schema(s.conn); set_enabled_banks(s.conn, [sys.argv[1]]); s.conn.close()" $Bank
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to initialize the local desktop database.'
    }
} finally {
    Pop-Location
}

function Start-LocalWorker($name, $program, $arguments, $directory) {
    $process = Start-Process -FilePath $program -ArgumentList $arguments -WorkingDirectory $directory -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $dataDir "$name.log") -RedirectStandardError (Join-Path $dataDir "$name.err.log")
    Write-Output "$name PID $($process.Id)"
}

Start-LocalWorker 'jd-desktop' $python @('-m', 'monitor.jd_live_sync') $repo
$monitorArguments = @('-m', 'monitor')
if (!$EnableNotifications) {
    $monitorArguments += '--dry-run'
}
Start-LocalWorker 'monitor' $python $monitorArguments $repo
Start-LocalWorker 'web' $python @('-m', 'monitor.web') $repo
Start-LocalWorker 'frontend' $npm @('run', 'dev', '--', '--strictPort', '--port', '5173') $frontend
Write-Output 'Dashboard http://127.0.0.1:5173/'
Write-Output 'Public page http://127.0.0.1:18080/public/'
