# ============================================================================
# DeployGuard - step by step demo (Windows PowerShell)
# Run from this folder:   powershell -ExecutionPolicy Bypass -File .\demo.ps1
# Every step waits for you to press ENTER, then runs ONE action and shows the result.
# ============================================================================
$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot
$Api = "http://localhost:8000"
$script:StepNo = 0

function Step([string]$title, [string]$explain) {
    $script:StepNo++
    Write-Host ""
    Write-Host ("=" * 78) -ForegroundColor Cyan
    Write-Host ("STEP {0}: {1}" -f $script:StepNo, $title) -ForegroundColor Yellow
    if ($explain) { Write-Host $explain -ForegroundColor Gray }
    Write-Host ("=" * 78) -ForegroundColor Cyan
    Read-Host "Press ENTER to run this step" | Out-Null
}

function Show-Containers {
    Write-Host ""
    Write-Host "> docker ps   (DeployGuard containers only)" -ForegroundColor DarkYellow
    docker ps --filter "name=dg-" --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
}

function Get-State { Invoke-RestMethod -Uri "$Api/api/state" -TimeoutSec 10 }

function Show-NewEvents($id, [ref]$printed) {
    $dep = @((Get-State).deployments) | Where-Object { $_.id -eq $id } | Select-Object -First 1
    if (-not $dep) { return $null }
    $events = @($dep.events)
    while ($printed.Value -lt $events.Count) {
        $e = $events[$printed.Value]
        $color = "White"
        if ($e.level -eq "ok") { $color = "Green" }
        if ($e.level -eq "error") { $color = "Red" }
        Write-Host ("  [{0}] {1}" -f $e.t, $e.msg) -ForegroundColor $color
        $printed.Value++
    }
    return $dep
}

function Deploy-Scenario([string]$key, [bool]$watchRollback = $false) {
    $body = @{ scenario = $key } | ConvertTo-Json
    try {
        $r = Invoke-RestMethod -Method Post -Uri "$Api/api/deploy" -ContentType "application/json" -Body $body
    } catch {
        Write-Host "Could not start deployment: $($_.Exception.Message)" -ForegroundColor Red
        return
    }
    $id = $r.id
    $printed = 0
    $dep = $null
    $deadline = (Get-Date).AddSeconds(120)
    # 1) wait until validation is finished (the API is no longer busy)
    while ((Get-Date) -lt $deadline) {
        $dep = Show-NewEvents $id ([ref]$printed)
        if ($dep -and -not (Get-State).busy) { break }
        Start-Sleep -Milliseconds 700
    }
    $dep = Show-NewEvents $id ([ref]$printed)
    if ($dep -and $dep.rows -and @($dep.rows).Count -gt 0) {
        Write-Host ""
        Write-Host "Live version vs new version (identical requests sent to both):" -ForegroundColor DarkYellow
        Write-Host ("  {0,-16} {1,-14} {2,-14} {3}" -f "Endpoint", "Live (st/ms)", "New (st/ms)", "Result") -ForegroundColor White
        foreach ($row in @($dep.rows)) {
            $liveTxt = "-"
            if ($null -ne $row.live_status) { $liveTxt = "{0} / {1}" -f $row.live_status, $row.live_ms }
            $newTxt = "{0} / {1}" -f $row.cand_status, $row.cand_ms
            $rc = "Green"
            if (-not $row.ok) { $rc = "Red" }
            Write-Host ("  {0,-16} {1,-14} {2,-14} {3}" -f $row.path, $liveTxt, $newTxt, $row.note) -ForegroundColor $rc
        }
    }
    # 2) optionally keep watching: the "fails after going live" release is rolled back automatically
    if ($watchRollback) {
        Write-Host "Release is live. Users are being served by it. Watching the post-deploy monitor (up to 60s)..." -ForegroundColor DarkYellow
        $deadline = (Get-Date).AddSeconds(60)
        while ((Get-Date) -lt $deadline) {
            $dep = Show-NewEvents $id ([ref]$printed)
            if ($dep.status -ne "live" -or $dep.stage -eq "done") { break }
            Write-Host "  ... monitoring (error rate $($dep.monitor.error_rate)%)" -ForegroundColor DarkGray
            Start-Sleep -Seconds 3
        }
        $dep = Show-NewEvents $id ([ref]$printed)
    }
    $score = "-"
    if ($null -ne $dep.score) { $score = $dep.score }
    $final = $dep.status
    $color = "Green"
    if ($final -eq "blocked" -or $final -eq "failed") { $color = "Red" }
    if ($final -eq "rolled_back") { $color = "Yellow" }
    Write-Host ""
    Write-Host ("RESULT: {0}   |   risk score: {1}/100" -f $final.ToUpper(), $score) -ForegroundColor $color
    Write-Host ("VERDICT: {0}" -f $dep.verdict) -ForegroundColor $color
    $st = Get-State
    if ($st.active) { Write-Host ("LIVE VERSION NOW: v{0}" -f $st.active.version) -ForegroundColor Cyan }
    Show-Containers
}

function Send-UserTraffic([int]$count = 5) {
    Write-Host ""
    Write-Host "> $count user requests through the DeployGuard gateway ($Api/live/api/products)" -ForegroundColor DarkYellow
    for ($i = 1; $i -le $count; $i++) {
        try {
            $r = Invoke-WebRequest -UseBasicParsing -Uri "$Api/live/api/products" -TimeoutSec 10
            Write-Host ("  request {0}: HTTP {1}  served by v{2}  {3}" -f $i, $r.StatusCode, (@($r.Headers["X-App-Version"]) -join ","), $r.Content.Trim()) -ForegroundColor Green
        } catch {
            Write-Host ("  request {0}: FAILED  {1}" -f $i, $_.Exception.Message) -ForegroundColor Red
        }
        Start-Sleep -Milliseconds 300
    }
}

# ---------------------------------------------------------------------------
Clear-Host
Write-Host "DeployGuard - safe deployment with differential verification and automatic rollback" -ForegroundColor Cyan
Write-Host "Each step waits for ENTER. Press Ctrl+C at any time to stop."

Step "Check that Docker is running" "DeployGuard needs Docker Desktop (Linux containers)."
docker version --format 'Docker client {{.Client.Version}}  |  server {{.Server.Version}}'
docker info 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Docker is not running. Start Docker Desktop, wait until it says 'running', then run this script again." -ForegroundColor Red
    exit 1
}
Write-Host "Docker is running." -ForegroundColor Green

Step "Build and start the platform (docker compose up)" "Starts RabbitMQ (job queue) and the DeployGuard API, and builds the sample shopping-app image. First run takes a few minutes."
docker compose up -d --build
if ($LASTEXITCODE -ne 0) {
    Write-Host "docker compose failed. Copy the error above and fix it before continuing." -ForegroundColor Red
    exit 1
}

Step "Wait until DeployGuard is ready" "Polls the API until it answers and the RabbitMQ worker is connected."
$ready = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        $s = Get-State
        if ($s.queue.connected) { $ready = $true; break }
    } catch { }
    Write-Host "  waiting..." -ForegroundColor DarkGray
    Start-Sleep -Seconds 2
}
if (-not $ready) {
    Write-Host "DeployGuard did not become ready. Check:  docker compose logs deployguard" -ForegroundColor Red
    exit 1
}
Write-Host "DeployGuard API is up and the RabbitMQ worker is connected." -ForegroundColor Green
Write-Host "Dashboard:        $Api"
Write-Host "RabbitMQ console: http://localhost:15672   (login guest / guest)"
Start-Process $Api

Step "Show the running containers" "Only the platform is running. No application version is deployed yet."
Show-Containers

Step "Deploy v1.0 (the stable baseline)" "The request goes into the RabbitMQ queue, a worker starts v1.0 in its OWN Docker container, health-checks it and makes it live."
Deploy-Scenario "v1_stable"

Step "Send user traffic" "Real requests go through the DeployGuard gateway to whichever version is live."
Send-UserTraffic 5

Step "Deploy v2.0 - a GOOD release (adds a new field)" "DeployGuard replays the same requests against v1.0 and v2.0 and compares status, response structure and speed. A NEW field is a safe change, so it is approved and traffic switches. v1.0 stays as rollback standby."
Deploy-Scenario "v2_good"
Send-UserTraffic 3

Step "Deploy v2.1 - CRASHES on startup" "The new container exits immediately. It must never replace the working version."
Deploy-Scenario "crash"

Step "Deploy v2.2 - LOGIN API is broken" "The app starts and /health is fine, but /api/login returns HTTP 500."
Deploy-Scenario "login500"

Step "Deploy v2.3 - SLOW release" "Everything works but every call takes about 1.5 seconds. Compared with the live version's speed, it is blocked."
Deploy-Scenario "slow"

Step "Deploy v2.4 - BREAKING API change (the novelty)" "Every endpoint still returns HTTP 200, so a normal health check approves it. But 'price' was renamed to 'cost', which would break every client. DeployGuard compares the response structure against the live version and blocks it."
Deploy-Scenario "schema"

Step "Deploy v2.5 - fails AFTER going live" "It passes validation (so traffic switches), then starts failing about 15 seconds later. The post-deploy monitor sees the errors and rolls back to v2.0 automatically."
Deploy-Scenario "latent" $true
Send-UserTraffic 3

Step "Prove the RabbitMQ queue was used" "Shows the queue and its consumer inside the RabbitMQ container."
docker exec dg-rabbitmq rabbitmqctl list_queues name messages consumers
Write-Host "Every deployment above was published as a job to the 'deploy_jobs' queue and consumed by a worker." -ForegroundColor Green

Step "Deployment history" "All releases, their status and risk score."
Write-Host ("  {0,-9} {1,-14} {2,-12} {3,-6} {4}" -f "Time", "Version", "Status", "Risk", "Via") -ForegroundColor White
foreach ($d in @((Get-State).deployments)) {
    Write-Host ("  {0,-9} {1,-14} {2,-12} {3,-6} {4}" -f $d.started, ("v" + $d.version), $d.status, $d.score, $d.via)
}
Show-Containers

Step "Clean up (optional)" "Removes every container, the network and the queue. Type n at the next prompt to keep everything running."
$ans = Read-Host "Remove everything now? (y/n)"
if ($ans -eq "y") {
    try { Invoke-RestMethod -Method Post -Uri "$Api/api/reset" | Out-Null } catch { }
    docker compose down
    Write-Host "Everything removed." -ForegroundColor Green
} else {
    Write-Host "Left running. Stop later with:  docker compose down" -ForegroundColor Yellow
}
