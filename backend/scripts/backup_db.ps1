# Daily DB backup. Schedule with Windows Task Scheduler (or cron on Linux: see backup_db.sh).
# orders_cache can be rebuilt from the API; customers, sessions and logs cannot.
#
# Works for both databases. -Mode auto reads DATABASE_URL from backend\.env and picks the right one:
#   MySQL  -> mysqldump
#   SQLite -> sqlite3 online backup (safe while the bot is running; a plain file copy is not, because
#             WAL mode keeps recent writes in a side file)
param(
  [ValidateSet("auto", "mysql", "sqlite")][string]$Mode = "auto",
  [string]$OutDir = "$PSScriptRoot\..\backups",
  [string]$MysqlHost = "localhost",
  [string]$User = "root",
  [string]$Password = "root",
  [string]$Database = "order_bot",
  [int]$KeepDays = 14
)
$ErrorActionPreference = "Stop"
$backend = Split-Path -Parent $PSScriptRoot
New-Item -ItemType Directory -Force $OutDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"

function Get-DatabaseUrl {
  $envFile = Join-Path $backend ".env"
  if (-not (Test-Path $envFile)) { return "" }
  $line = Select-String -Path $envFile -Pattern '^\s*DATABASE_URL\s*=' | Select-Object -Last 1
  if ($null -eq $line) { return "" }
  return ($line.Line -replace '^\s*DATABASE_URL\s*=\s*', '').Trim()
}

$url = Get-DatabaseUrl
if ($Mode -eq "auto") {
  if ($url -like "sqlite*") { $Mode = "sqlite" } else { $Mode = "mysql" }
}

if ($Mode -eq "sqlite") {
  # sqlite+aiosqlite:///./order_bot.db  ->  order_bot.db (relative to backend\)
  $path = ($url -replace '^.*sqlite[^:]*:///', '')
  if (-not $path) { $path = "order_bot.db" }
  if (-not [System.IO.Path]::IsPathRooted($path)) { $path = Join-Path $backend $path }
  if (-not (Test-Path $path)) { throw "SQLite database not found at $path" }
  $file = Join-Path $OutDir "order_bot-$stamp.db"
  $python = Join-Path $backend ".venv\Scripts\python.exe"
  if (-not (Test-Path $python)) { $python = "python" }
  & $python -c "import sqlite3,sys; src=sqlite3.connect(sys.argv[1]); dst=sqlite3.connect(sys.argv[2]); src.backup(dst); dst.close(); src.close()" $path $file
  if ($LASTEXITCODE -ne 0) { throw "sqlite backup failed" }
}
else {
  $file = Join-Path $OutDir "order_bot-$stamp.sql"
  & mysqldump -h $MysqlHost -u $User -p$Password --single-transaction --routines $Database | Out-File -Encoding utf8 $file
  if ($LASTEXITCODE -ne 0) { throw "mysqldump failed" }
}

Get-ChildItem $OutDir -Include "order_bot-*.sql", "order_bot-*.db" -Recurse |
  Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$KeepDays) } | Remove-Item -Force
"backup written: $file"
