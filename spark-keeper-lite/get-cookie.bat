@echo off
rem ============================================================================
rem  SparkKeeper - Douyin cookie fetcher (one-click launcher)
rem
rem  Opens a REAL Chrome window, you log in to Douyin by hand (QR scan or
rem  phone + password), then this tool grabs the login cookies automatically
rem  and either prints them or writes them straight into an account.
rem
rem  Usage:
rem    get-cookie.bat                 print cookies (paste into the web UI)
rem    get-cookie.bat --account 1     write them into local account #1
rem    get-cookie.bat --help          show all options
rem
rem  Why this exists: the in-app QR login depends on Douyin's own login modal
rem  and QR rendering, which sometimes never renders (risk control / network /
rem  version differences). This tool drives a normal visible browser instead,
rem  so there is no headless fingerprint and no dependency on the modal DOM.
rem
rem  .bat hard rules (tools/check_bat.py enforces them):
rem    1) PURE ASCII only  -- cmd.exe reads .bat with the OEM codepage
rem    2) CRLF line endings
rem    3) no UTF-8 BOM
rem  Chinese user-facing text comes from get_cookie.py, not from here.
rem ============================================================================
setlocal enableextensions enabledelayedexpansion
cd /d "%~dp0"

if not defined SK_DATA_DIR set "SK_DATA_DIR=%~dp0data"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

set "MODE=run"
goto :parse

:maybe_pause
if defined SK_NO_PAUSE goto :eof
timeout /t 8 >nul 2>nul
if errorlevel 1 goto :eof
pause >nul
goto :eof

:parse
if "%~1"=="" goto parsed
if /i "%~1"=="--help" ( set "MODE=help" & shift & goto parse )
shift
goto parse
:parsed

echo.
echo   ============================================================
echo     SparkKeeper  -  Douyin cookie fetcher
echo   ============================================================
echo.

rem ---------------------------------------------------------------------------
rem  Locate a usable Python 3.9+ (same order as start.bat):
rem  SK_PYTHON -> py -3 -> python -> python3
rem ---------------------------------------------------------------------------
set "PY="
if defined SK_PYTHON (
  "%SK_PYTHON%" -c "import sys" >nul 2>nul && set "PY=%SK_PYTHON%"
)
if not defined PY (
  py -3 -c "import sys;raise SystemExit(0 if sys.version_info>=(3,9) else 1)" >nul 2>nul
  if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
  python -c "import sys;raise SystemExit(0 if sys.version_info>=(3,9) else 1)" >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  python3 -c "import sys;raise SystemExit(0 if sys.version_info>=(3,9) else 1)" >nul 2>nul
  if not errorlevel 1 set "PY=python3"
)

if not defined PY (
  echo   [ERROR] Python 3.9+ was not found on this computer.
  echo.
  echo   Install Python from https://www.python.org/downloads/
  echo   and tick "Add python.exe to PATH" during the setup.
  echo   If it is already installed, point this script at it:
  echo       set SK_PYTHON=C:\Path\to\python.exe
  echo.
  call :maybe_pause
  exit /b 3
)

if not exist "%~dp0tools\get_cookie.py" (
  echo   [ERROR] tools\get_cookie.py was not found next to this script.
  echo   Put get-cookie.bat in the project root ^(the folder containing tools\^).
  echo   Current folder: %~dp0
  echo.
  call :maybe_pause
  exit /b 2
)

echo   Using Python: %PY%
echo.

%PY% "%~dp0tools\get_cookie.py" %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo   [FAILED] exit code %RC% -- read the messages above.
call :maybe_pause
exit /b %RC%
