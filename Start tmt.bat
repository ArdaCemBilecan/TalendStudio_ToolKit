@echo off
setlocal
cd /d "%~dp0"
title tmt - Dependency Explorer
set PYTHONUTF8=1
set PYTHONDONTWRITEBYTECODE=1

rem Which Python? The bundled one (customer folder), else the project's .venv (developer), else the system one.
if exist "%~dp0python\python.exe" (
  set "PY=%~dp0python\python.exe"
) else if exist "%~dp0.venv\Scripts\python.exe" (
  set "PY=%~dp0.venv\Scripts\python.exe"
  set "PYTHONPATH=%~dp0src"
) else (
  set "PY=python"
  set "PYTHONPATH=%~dp0src"
)

echo Starting the tmt Dependency Explorer ...
echo Your browser opens by itself. Keep this window open while you use it;
echo close it (or press Ctrl+C) to stop.
echo.
"%PY%" -m tmt ui %*
if errorlevel 1 (
  echo.
  echo tmt stopped with an error. The message above says why.
  echo If Python was not found, use the customer folder built by tools\build_portable.py.
  pause
)
