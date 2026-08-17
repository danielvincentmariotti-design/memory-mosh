@echo off
setlocal
set SCRIPT_DIR=%~dp0
set PY_EXE=%SCRIPT_DIR%.venv\Scripts\python.exe
"%PY_EXE%" "%SCRIPT_DIR%memory_mosh.py"
