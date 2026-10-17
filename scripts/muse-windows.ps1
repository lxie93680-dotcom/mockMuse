param(
    [ValidateSet('install', 'start', 'restart')][string]$Action = 'start',
    [ValidateRange(1024, 65535)][int]$Port = 8000,
    [switch]$InstallBrowser,
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
$env:PYTHONUTF8 = '1'
$env:MUSE_PORT = [string]$Port
$python = Join-Path $root '.venv\Scripts\python.exe'
$url = "http://127.0.0.1:$Port/"

function Invoke-Checked([string]$Program, [string[]]$Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program failed (exit $LASTEXITCODE)." }
}

if ($Action -eq 'install') {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw 'Install Python 3.11+ and uv, then run this script again.' }
    if (-not (Get-Command node -ErrorAction SilentlyContinue)) { throw 'Install Node.js 22+ and reopen this terminal.' }
    $nodeMajor = & node -p 'parseInt(process.versions.node)'
    if ($LASTEXITCODE -ne 0 -or [int]$nodeMajor -lt 22) { throw 'Node.js 22 or later is required.' }
    Invoke-Checked 'uv' @('sync', '--locked')
    foreach ($vendor in @('vendor/playwright', 'vendor/filesystem')) {
        Invoke-Checked 'npm.cmd' @('--prefix', $vendor, 'ci')
    }
    if ($InstallBrowser) {
        Invoke-Checked 'node' @('vendor/playwright/node_modules/playwright/cli.js', 'install', 'chromium')
    }
    if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
    Write-Host 'Dependencies installed. Configure .env and the Chrome extension before starting Muse.'
    Write-Host 'Windows 10+ uses controlled file tools and NTFS journaled writeback. Agent terminal and external Filesystem MCP remain disabled.'
    exit 0
}

if (-not (Test-Path -LiteralPath $python)) { throw 'Missing virtual environment. Run install-Muse.cmd first.' }
if (-not (Test-Path -LiteralPath '.env')) { throw 'Missing .env. Copy .env.example and configure the model.' }
$listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
if ($listeners.Count -gt 0) {
    $owners = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
    foreach ($ownerId in $owners) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$ownerId"
        if (-not $process -or $process.ExecutablePath -ine $python -or
            $process.CommandLine -notmatch '(?i)\buvicorn\s+muse\.app:app(?:\s|$)') {
            throw "Port $Port belongs to another process. Nothing was stopped."
        }
    }
    if ($Action -eq 'start') {
        Write-Host 'Muse is already running. Use restart-Muse.cmd to load updated code.'
        if (-not $NoBrowser) { Start-Process $url }
        exit 0
    }
    Write-Host 'Stopping Muse. Any running task will be interrupted; Windows cannot resume it automatically.'
    foreach ($ownerId in $owners) { Stop-Process -Id $ownerId -ErrorAction Stop }
    $deadline = (Get-Date).AddSeconds(20)
    while (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
        if ((Get-Date) -gt $deadline) { throw 'Port was not released. Muse was not restarted.' }
        Start-Sleep -Milliseconds 250
    }
}

Write-Host "Starting Muse at $url (Ctrl+C stops the service)."
$opener = $null
try {
    if (-not $NoBrowser) {
        $opener = Start-Job -ArgumentList $url -ScriptBlock {
            param($address)
            for ($attempt = 0; $attempt -lt 120; $attempt++) {
                try {
                    $response = Invoke-WebRequest -UseBasicParsing -Uri $address -TimeoutSec 1
                    if ($response.StatusCode -eq 200) { Start-Process $address; return }
                } catch {}
                Start-Sleep -Milliseconds 500
            }
        }
    }
    Invoke-Checked $python @('-m', 'uvicorn', 'muse.app:app', '--host', '127.0.0.1', '--port', [string]$Port, '--loop', 'asyncio')
} finally {
    if ($null -ne $opener) { Stop-Job $opener; Remove-Job $opener }
}
