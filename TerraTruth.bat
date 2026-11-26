@echo off
title TerraTruth Oracle
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First-time setup: creating Python environment and installing dependencies...
  python -m venv .venv || (echo Python 3.9+ is required: https://www.python.org/downloads/ & pause & exit /b 1)
  ".venv\Scripts\python.exe" -m pip install --retries 10 --timeout 60 --only-binary=:all: -r requirements.txt || (echo Install failed - check your internet connection and run again. & pause & exit /b 1)
)
".venv\Scripts\python.exe" launch.py
if errorlevel 1 pause
