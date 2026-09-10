<#
Registers the bot as a Windows Scheduled Task that starts at boot (no Docker, no third-party service wrapper).
Run once from an elevated PowerShell:
    cd "<install-path>\backend"
    .\scripts\install_windows_task.ps1                      # HTTP on port 8000 (put HTTPS in front, or use -CertFile/-KeyFile)
    .\scripts\install_windows_task.ps1 -Port 443 -CertFile C:\certs\fullchain.pem -KeyFile C:\certs\privkey.pem
Manage:  Start-ScheduledTask OrderStatusBot | Stop-ScheduledTask OrderStatusBot | Unregister-ScheduledTask OrderStatusBot
Logs:    backend\logs\bot.out.log / bot.err.log
#>
param(
  [int]$Port = 8000,
  [string]$CertFile = "",
  [string]$KeyFile = "",
  [string]$TaskName = "OrderStatusBot"
)
$backend = Split-Path -Parent $PSScriptRoot
$python = Join-Path $backend ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "venv not found at $python. Run: python -m venv .venv; .\.venv\Scripts\pip install -r requirements.txt" }
New-Item -ItemType Directory -Force (Join-Path $backend "logs") | Out-Null

# --workers 1: the message queue worker and the scheduled jobs live in the web process (see run_prod.ps1)
$args = "-m uvicorn app.main:app --host 0.0.0.0 --port $Port --workers 1 --proxy-headers --forwarded-allow-ips=*"
if ($CertFile -and $KeyFile) { $args += " --ssl-certfile `"$CertFile`" --ssl-keyfile `"$KeyFile`"" }
$runner = Join-Path $backend "scripts\run_prod.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runner`" -Args `"$args`"" -WorkingDirectory $backend
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
"Task '$TaskName' registered and started. Port $Port. Logs in $backend\logs"
