# Runs uvicorn in the foreground with log files; used by install_windows_task.ps1 (and fine to run by hand).
#
# --workers 1 is deliberate. The inbound-message worker and the scheduled jobs (order refresh,
# customer import) run inside the web process, so a second worker would import customers twice and
# race for queued messages. To scale, put more machines behind a load balancer and run the jobs on
# exactly one of them.
param([string]$Args = "-m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips=*")
$backend = Split-Path -Parent $PSScriptRoot
Set-Location $backend
New-Item -ItemType Directory -Force "$backend\logs" | Out-Null

# Roll the console logs at start-up so they cannot grow without limit across restarts. (Set LOG_FILE
# in .env for continuous rotation while the process is running.)
foreach ($name in @("bot.out.log", "bot.err.log")) {
  $p = Join-Path "$backend\logs" $name
  if ((Test-Path $p) -and ((Get-Item $p).Length -gt 20MB)) {
    for ($i = 4; $i -ge 1; $i--) {
      if (Test-Path "$p.$i") { Move-Item "$p.$i" "$p.$($i+1)" -Force }
    }
    Move-Item $p "$p.1" -Force
  }
}

$python = "$backend\.venv\Scripts\python.exe"
$argList = $Args -split " "
& $python @argList 1>> "$backend\logs\bot.out.log" 2>> "$backend\logs\bot.err.log"
