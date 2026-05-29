#requires -Version 5.1
<#
.SYNOPSIS
  Kill any existing Conduit server on the configured port, then restart.

.PARAMETER Port
  Port to bind. Resolution order: -Port flag > $env:CONDUIT_PORT > CONDUIT_PORT
  in .env > 8765.

.PARAMETER Host_
  Bind address. Default: 127.0.0.1 (or CONDUIT_HOST in .env / env).

.PARAMETER Env
  Conda env name. Default: conduit.
#>
[CmdletBinding()]
param(
    [int]    $Port  = 0,            # 0 = unspecified; resolved below
    [string] $Host_ = '',           # '' = unspecified; resolved below
    [string] $Env   = 'conduit'
)

$ErrorActionPreference = 'Stop'

# Read a KEY=value from the project-root .env (pydantic reads this file too, but
# PowerShell doesn't auto-load it, so we parse it here to keep run.ps1 in sync).
function Get-DotEnvValue([string]$key) {
    $envFile = Join-Path $PSScriptRoot '.env'
    if (-not (Test-Path $envFile)) { return $null }
    foreach ($line in Get-Content $envFile) {
        if ($line -match "^\s*$([regex]::Escape($key))\s*=\s*(.+?)\s*$") {
            return $Matches[1]
        }
    }
    return $null
}

function Resolve-Port {
    if ($Port -ne 0) { return $Port }                                 # explicit -Port
    if ($env:CONDUIT_PORT) { return [int]$env:CONDUIT_PORT }           # shell env
    $fromFile = Get-DotEnvValue 'CONDUIT_PORT'
    if ($fromFile -and $fromFile -match '^\d+$') { return [int]$fromFile }  # .env
    return 8765
}

function Resolve-Host {
    if ($Host_) { return $Host_ }                                     # explicit -Host_
    if ($env:CONDUIT_HOST) { return $env:CONDUIT_HOST }               # shell env
    $fromFile = Get-DotEnvValue 'CONDUIT_HOST'
    if ($fromFile) { return $fromFile }                               # .env
    return '127.0.0.1'
}

$Port  = Resolve-Port
$Host_ = Resolve-Host

function Stop-OnPort([int]$p) {
    $conns = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
    if (-not $conns) {
        Write-Host "[run] port $p is free" -ForegroundColor DarkGray
        return
    }
    foreach ($c in $conns) {
        $procId = $c.OwningProcess
        try {
            $proc = Get-Process -Id $procId -ErrorAction Stop
            Write-Host "[run] killing PID $procId ($($proc.ProcessName)) on port $p" -ForegroundColor Yellow
            Stop-Process -Id $procId -Force -ErrorAction Stop
        } catch {
            Write-Warning "[run] could not stop PID ${procId}: $_"
        }
    }
    # Give Windows a moment to release the socket
    $deadline = (Get-Date).AddSeconds(5)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue)) {
            return
        }
        Start-Sleep -Milliseconds 200
    }
    Write-Warning "[run] port $p still in use after 5s, attempting start anyway"
}

Stop-OnPort -p $Port

Write-Host "[run] starting Conduit on http://${Host_}:${Port}" -ForegroundColor Green
& conda run -n $Env --no-capture-output `
    python -m uvicorn conduit.app:app --host $Host_ --port $Port
