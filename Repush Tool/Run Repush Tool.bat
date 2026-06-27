@echo off
REM Double-click to launch the Repush Tool.
cd /d "%~dp0"
python main.py
if errorlevel 1 (
  echo.
  echo If you see "psycopg2 not installed", run:  pip install psycopg2-binary
  echo If you load Excel files and see "openpyxl" error, run:  pip install openpyxl
  echo If "python not found", install Python from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" during setup.
  echo.
  pause
)
