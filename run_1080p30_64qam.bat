@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv || exit /b 1
  .venv\Scripts\python.exe -m pip install -r requirements.txt || exit /b 1
)
.venv\Scripts\python.exe -c "import scipy" >nul 2>&1
if errorlevel 1 (
  .venv\Scripts\python.exe -m pip install -r requirements.txt || exit /b 1
)
.venv\Scripts\python.exe -m video_loopback.gui --profile ofdm-original-1080p30-64qam %*
