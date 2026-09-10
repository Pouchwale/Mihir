@echo off
REM ---------------------------------------------------------------------------
REM DEVELOPMENT launcher: auto-reload, binds to this machine, WhatsApp simulated
REM while WATI_TOKEN is empty. Do NOT use this on the server.
REM
REM Production: see README section 6, then
REM   backend\scripts\install_windows_task.ps1   (Windows, starts at boot)
REM   backend\scripts\order-status-bot.service   (Linux, systemd)
REM Both run a single worker with no reload, which is what the message queue and
REM the scheduled imports require.
REM ---------------------------------------------------------------------------
echo Starting WhatsApp Order Status Bot in DEVELOPMENT mode (http://localhost:8000)...
backend\.venv\Scripts\python.exe start.py
pause
