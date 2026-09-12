param(
    [int]$RedisDb = $(if ($env:REDIS_DB) { [int]$env:REDIS_DB } else { 15 }),
    [string]$RedisHost = $(if ($env:REDIS_HOST) { $env:REDIS_HOST } else { "127.0.0.1" }),
    [int]$RedisPort = $(if ($env:REDIS_PORT) { [int]$env:REDIS_PORT } else { 6379 }),
    [string]$WebhookSecret = $(if ($env:WEBHOOK_SECRET) { $env:WEBHOOK_SECRET } else { "local-stress-secret" })
)

# Set console encoding to UTF-8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

if ($RedisDb -lt 0 -or $RedisDb -gt 15) {
    throw "RedisDb must be between 0 and 15."
}
if ([string]::IsNullOrWhiteSpace($WebhookSecret)) {
    throw "WebhookSecret must not be empty."
}

Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "  Starting Webhook Delivery Stress Test Setup " -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan

# Configure isolated Redis database URL for this stress run
$env:REDIS_URL = "redis://${RedisHost}:${RedisPort}/${RedisDb}"
$env:ENVIRONMENT = "development"
$env:WEBHOOK_SECRET = $WebhookSecret
$env:LOGURU_LEVEL = "WARNING"
Write-Host "Configured isolated Redis database: DB $RedisDb ($env:REDIS_URL)" -ForegroundColor Cyan
New-Item -ItemType Directory -Force "tools" | Out-Null

# Track every helper process so the finally block can clean it up.
$Processes = @()

# 1. Start Redis in WSL if available, or verify reachability
Write-Host "[1/3] Ensuring Redis is running..." -ForegroundColor Yellow
if (Get-Command wsl -ErrorAction SilentlyContinue) {
    # Keep the WSL VM alive for the full benchmark; otherwise localhost forwarding
    # can disappear after the one-shot service command exits.
    $WslKeepalive = Start-Process -FilePath "wsl.exe" -ArgumentList @("--exec", "sleep", "120") -WindowStyle Hidden -PassThru
    $Processes += $WslKeepalive
    wsl -u root service redis-server start 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Notice: WSL redis-server service start returned code $LASTEXITCODE; verifying connection directly." -ForegroundColor DarkGray
    }
    Write-Host "[2/3] Flushing dedicated Redis DB $RedisDb..." -ForegroundColor Yellow
    wsl redis-cli -h $RedisHost -p $RedisPort -n $RedisDb flushdb 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Warning: WSL redis-cli flushdb on DB $RedisDb returned code $LASTEXITCODE." -ForegroundColor Yellow
    }
} else {
    Write-Host "[1/3] Checking native redis-cli on ${RedisHost}:${RedisPort}..." -ForegroundColor Yellow
    if (Get-Command redis-cli -ErrorAction SilentlyContinue) {
        & redis-cli -h $RedisHost -p $RedisPort -n $RedisDb flushdb 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "Warning: redis-cli flushdb on DB $RedisDb returned code $LASTEXITCODE." -ForegroundColor Yellow
        }
    } else {
        Write-Host "Notice: redis-cli not found locally. Ensure Redis is running on ${RedisHost}:${RedisPort} with DB $RedisDb." -ForegroundColor DarkGray
    }
}

# Resolve Python interpreter
$PythonExe = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }

$k6ExitCode = 0

try {
    # Start only the API: this benchmark measures signed ingestion and Redis Stream enqueueing.
    Write-Host "[3/3] Starting Webhook API Server (Port 8000)..." -ForegroundColor Yellow
    $ApiProcess = Start-Process -FilePath $PythonExe -ArgumentList "-m uvicorn app.main:app --host 127.0.0.1 --port 8000 --log-level warning --no-access-log" -NoNewWindow -PassThru
    $Processes += $ApiProcess

    # Wait for services to bind to their ports
    Write-Host "Waiting 3 seconds for all servers to initialize..." -ForegroundColor Green
    Start-Sleep -Seconds 3

    # Check if all spawned processes are running
    foreach ($p in $Processes) {
        if ($p.HasExited) {
            Write-Host "ERROR: Process $($p.ProcessName) (PID: $($p.Id)) failed to start! Exiting." -ForegroundColor Red
            $k6ExitCode = 1
            exit 1
        }
    }

    Write-Host "All processes started successfully!" -ForegroundColor Green
    Write-Host "Running k6 stress test..." -ForegroundColor Cyan
    Write-Host "---------------------------------------------" -ForegroundColor Cyan

    # Resolve k6 on PATH or fallback to pinned Docker container
    if (Get-Command k6 -ErrorAction SilentlyContinue) {
        Write-Host "Using k6 found on PATH..." -ForegroundColor Cyan
        & k6 run --summary-export tools/k6-summary.json tests\k6_stress_test.js
        $k6ExitCode = $LASTEXITCODE
    } elseif (Get-Command docker -ErrorAction SilentlyContinue) {
        Write-Host "k6 not found on PATH; running pinned Docker fallback (grafana/k6:2.1.0)..." -ForegroundColor Cyan
        & docker run --rm -i --add-host host.docker.internal:host-gateway -e "WEBHOOK_SECRET=$WebhookSecret" -e "API_URL=http://host.docker.internal:8000/webhooks" -v "${PWD}:/work" -w /work grafana/k6:2.1.0 run --summary-export /work/tools/k6-summary.json tests/k6_stress_test.js
        $k6ExitCode = $LASTEXITCODE
    } else {
        Write-Host "ERROR: k6 is not installed on PATH and Docker is not available." -ForegroundColor Red
        Write-Host "Please install k6 (e.g. 'winget install grafana.k6') or start Docker." -ForegroundColor Red
        $k6ExitCode = 1
    }
}
finally {
    Write-Host "---------------------------------------------" -ForegroundColor Cyan
    Write-Host "Shutting down background processes..." -ForegroundColor Yellow
    foreach ($p in $Processes) {
        if ($p -and !$p.HasExited) {
            Write-Host "Stopping process $($p.ProcessName) (PID: $($p.Id))..." -ForegroundColor DarkGray
            Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
        }
    }
    Write-Host "Cleanup completed successfully!" -ForegroundColor Green
}

if ($k6ExitCode -ne 0) {
    Write-Host "Stress test run failed with exit code $k6ExitCode" -ForegroundColor Red
    exit $k6ExitCode
}
