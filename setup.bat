@echo off
rem One-time setup. Creates the Python environment outside OneDrive (so OneDrive doesn't sync
rem thousands of package files) and installs Eval Studio and its packages.
set VENV=%USERPROFILE%\.venvs\eval-studio
if not exist "%VENV%\Scripts\python.exe" python -m venv "%VENV%"
"%VENV%\Scripts\python" -m pip install --upgrade pip
"%VENV%\Scripts\python" -m pip install -e "%~dp0.[dev]"
if not exist "%~dp0.env" copy "%~dp0.env.example" "%~dp0.env" >nul
echo.
echo Setup done. Next: open .env and paste your Anthropic API key, then run start.bat
