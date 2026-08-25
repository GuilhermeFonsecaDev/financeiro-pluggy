@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_CMD="
if exist ".venv\Scripts\python.exe" set "PYTHON_CMD=.venv\Scripts\python.exe"
if "%PYTHON_CMD%"=="" where py >nul 2>nul && set "PYTHON_CMD=py -3"
if "%PYTHON_CMD%"=="" where python >nul 2>nul && set "PYTHON_CMD=python"

if "%PYTHON_CMD%"=="" (
  echo Python nao encontrado. Instale Python 3.10+ ou crie um ambiente .venv.
  pause
  exit /b 1
)

%PYTHON_CMD% backend_pluggy.py --open
pause
