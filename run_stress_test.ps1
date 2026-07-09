# Set console encoding to UTF-8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "  Starting Webhook Delivery Stress Test Setup " -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan

# 1. Start Redis in WSL (Ubuntu) and clean existing DB
Write-Host "[1/5] Ensuring Redis is running in WSL..." -ForegroundColor Yellow
wsl -u root service redis-server start | Out-Null
Write-Host "[2/5] Flushing Redis DB to start fresh..." -ForegroundColor Yellow
wsl redis-cli flushall | Out-Null

# 2. Define process list to clean up later
$Processes = @()

# 3. Start Mock Target Server
Write-Host "[3/5] Starting Mock Target Server (Port 8081)..." -ForegroundColor Yellow
$MockProcess = Start-Process -FilePath ".venv\Scripts\python.exe" -ArgumentList "tests\mock_target_server.py" -NoNewWindow -PassThru
$Processes += $MockProcess

# 4. Start Webhook API Server
Write-Host "[4/5] Starting Webhook API Server (Port 8000)..." -ForegroundColor Yellow
$ApiProcess = Start-Process -FilePath ".venv\Scripts\python.exe" -ArgumentList "-m uvicorn app.main:app --host 127.0.0.1 --port 8000" -NoNewWindow -PassThru
$Processes += $ApiProcess

# 5. Start Webhook Delivery Worker
Write-Host "[5/5] Starting Webhook Delivery Worker..." -ForegroundColor Yellow
$WorkerProcess = Start-Process -FilePath ".venv\Scripts\python.exe" -ArgumentList "-m app.workers.delivery_worker" -NoNewWindow -PassThru
$Processes += $WorkerProcess

# Wait for services to bind to their ports
Write-Host "Waiting 3 seconds for all servers to initialize..." -ForegroundColor Green
Start-Sleep -Seconds 3

# Check if processes are running
foreach ($p in $Processes) {
    if ($p.HasExited) {
        Write-Host "ERROR: Process $($p.ProcessName) (PID: $($p.Id)) failed to start! Exiting." -ForegroundColor Red
        # Kill others
        $Processes | Where-Object { !$_.HasExited } | Stop-Process -Force
        Exit 1
    }
}

Write-Host "All processes started successfully!" -ForegroundColor Green
Write-Host "Running k6 stress test..." -ForegroundColor Cyan
Write-Host "---------------------------------------------" -ForegroundColor Cyan

try {
    # Run k6 synchronously in the current console
    & "tools\k6.exe" run tests\k6_stress_test.js
}
finally {
    Write-Host "---------------------------------------------" -ForegroundColor Cyan
    Write-Host "Shutting down background processes..." -ForegroundColor Yellow
    foreach ($p in $Processes) {
        if (!$p.HasExited) {
            Write-Host "Stopping process $($p.ProcessName) (PID: $($p.Id))..." -ForegroundColor DarkGray
            Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
        }
    }
    Write-Host "Cleanup completed successfully!" -ForegroundColor Green
}
