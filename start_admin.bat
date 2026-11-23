@echo off
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting Administrator privileges for PC client...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
cd /d "%~dp0"
echo [ADMIN] Stopping old server on port 5050 if any...
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 5050 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"
timeout /t 1 /nobreak >nul
echo [ADMIN] Starting Karlan Trade R&D Solver at http://127.0.0.1:5050
python app.py
pause
