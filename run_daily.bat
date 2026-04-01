@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if exist "%~dp0local_env.bat" call "%~dp0local_env.bat"

if not exist "%~dp0.venv\Scripts\python.exe" (
  echo ERROR: Virtual env not found.
  echo Create it: python -m venv .venv ^& pip install -r requirements.txt
  exit /b 1
)

if not exist "%~dp0output" mkdir "%~dp0output"

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set ISO=%%i

"%~dp0.venv\Scripts\python.exe" "%~dp0main.py" ^
  --out-dir "%~dp0output" ^
  --non-interactive ^
  >> "%~dp0output\run_%ISO%.log" 2>&1

set ERR=%ERRORLEVEL%
if %ERR% neq 0 exit /b %ERR%
exit /b 0
