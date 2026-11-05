@echo off
rem Imports the demo project (first time only) and starts the web app at http://localhost:8000
cd /d "%~dp0"
set PY=%USERPROFILE%\.venvs\eval-studio\Scripts
"%PY%\evalstudio" show support-email >nul 2>&1 || "%PY%\evalstudio" import examples\support-email
"%PY%\evalstudio" serve
