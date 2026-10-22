@echo off
setlocal
set "PYTHONPATH=%~dp0..\src;%PYTHONPATH%"
if defined CLAUDE_PLUGIN_DATA if exist "%CLAUDE_PLUGIN_DATA%\site-packages" set "PYTHONPATH=%CLAUDE_PLUGIN_DATA%\site-packages;%PYTHONPATH%"
set "BOOKKEEPING_TEMPLATES_DIR=%~dp0..\skills\books-onboard\templates"
py -3 -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1
if not errorlevel 1 (
  py -3 -m bookkeeping.cli %*
  exit /b
)
python -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1
if not errorlevel 1 (
  python -m bookkeeping.cli %*
  exit /b
)
python3 -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1
if not errorlevel 1 (
  python3 -m bookkeeping.cli %*
  exit /b
)
echo Slashbooks needs Python 3.11 or newer. Install Python, then restart your agent. >&2
exit /b 1
