@echo off
rem Runs the automated tests.
cd /d "%~dp0"
"%USERPROFILE%\.venvs\eval-studio\Scripts\python" -m pytest -q
pause
