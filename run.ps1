#requires -Version 5.1
<#
.SYNOPSIS
  Kill any existing Conduit server on the configured port, then restart.

.PARAMETER Port
  Port to bind. Default: $env:CONDUIT_PORT or 8765.

.PARAMETER Host_
  Bind address. Default: 127.0.0.1.

.PARAMETER Env
  Conda env name. Default: conduit.
#>
[CmdletBinding()]
param(
    [int]    $Port  = $(if ($env:CONDUIT_PORT) { [int]$env:CONDUIT_PORT } else { 8765 }),
    [string] $Host_ = '127.0.0.1',
    [string] $Env   = 'conduit'
)

$ErrorActionPreference = 'Stop'

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
