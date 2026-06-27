@echo off
REM Double-click to launch the Meter Query Tool.
cd /d "%~dp0"
python meter_query_tool.py
if errorlevel 1 (
  echo.
  echo If you see "psycopg2 not installed", run:  pip install psycopg2-binary
  echo If "python not found", install Python from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" during setup.
  echo.
  pause
)
