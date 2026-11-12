@echo off
setlocal
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo The virtual environment is missing. Run:
  echo   py -3.12 -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
  exit /b 2
)
"%~dp0.venv\Scripts\python.exe" -m wallet_analyzer %*
