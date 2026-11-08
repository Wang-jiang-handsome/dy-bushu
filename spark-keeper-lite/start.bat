@echo off
rem ============================================================================
rem  SparkKeeper - Windows one-click launcher
rem
rem  DO NOT "improve" THIS FILE CARELESSLY. A .bat file has three hard rules:
rem    1) PURE ASCII only. cmd.exe reads .bat with the OEM codepage (GBK on a
rem       Chinese Windows), so UTF-8 Chinese text becomes mojibake and can break
rem       parsing. All user-facing text here is English on purpose - the real
rem       Chinese messages come from Python, which handles encoding correctly.
rem    2) CRLF line endings, 3) no UTF-8 BOM.
rem    tools/check_bat.py enforces all three; run it after editing this file.
rem
rem  Usage:
rem    start.bat                  bind 127.0.0.1:8000 (local only, safe default)
rem    start.bat --lan            bind 0.0.0.0:8000  (reachable from your LAN)
rem    start.bat --port 9000      use another port
rem    start.bat --init-only      initialize only, do not start the server
rem    start.bat --check          environment check + migrate, then exit
rem    start.bat --demo           demo mode (no real browser, for UI review)
rem
rem  If the window opens and closes instantly, run this instead so you can read
rem  the error:
rem      cd /d <this folder>
rem      start.bat --check
rem ============================================================================
setlocal enableextensions enabledelayedexpansion
cd /d "%~dp0"

rem --- point the app at this folder: in the flat delivery layout web/static
rem     lives right here. (app/config.py can auto-detect it too, this is belt+braces)
set "SK_PROJECT_DIR=%~dp0"
if not defined SK_DATA_DIR set "SK_DATA_DIR=%~dp0data"
if not defined SK_ENV set "SK_ENV=dev"
if not defined SK_HOST set "SK_HOST=127.0.0.1"
if not defined SK_PORT set "SK_PORT=8000"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

rem ---------------------------------------------------------------------------
rem  maybe_pause: keep the window open so the user can read what happened.
rem  This is THE fix for "the black window flashes and disappears": every exit
rem  path below goes through here. Set SK_NO_PAUSE=1 for unattended/CI runs.
rem  `timeout` needs a real console; in a redirected/CI shell it fails, so we
rem  count it as handled instead of falling back to an invisible `pause`.
rem ---------------------------------------------------------------------------
set "MODE=serve"
set "OPEN_BROWSER="
goto :parse

:maybe_pause
if defined SK_NO_PAUSE goto :eof
timeout /t 8 >nul 2>nul
if errorlevel 1 goto :eof
pause >nul
goto :eof

:parse
if "%~1"=="" goto parsed
if /i "%~1"=="--lan"       ( set "SK_HOST=0.0.0.0" & shift & goto parse )
if /i "%~1"=="--port"      ( set "SK_PORT=%~2"    & shift & shift & goto parse )
if /i "%~1"=="--init-only" ( set "MODE=init"      & shift & goto parse )
if /i "%~1"=="--check"     ( set "MODE=check"     & shift & goto parse )
if /i "%~1"=="--demo"      ( set "SK_DEMO_MODE=1" & shift & goto parse )
if /i "%~1"=="--open"      ( set "OPEN_BROWSER=1" & shift & goto parse )
shift
goto parse
:parsed

echo.
echo   ============================================================
echo     SparkKeeper  -  Douyin streak keeper
echo   ============================================================
echo.

rem ---------------------------------------------------------------------------
rem  1. locate a usable Python 3.9+
rem     order: SK_PYTHON  ->  py -3  ->  python  ->  python3
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
  echo   Fix it in one of these ways:
  echo     1^) Install Python from https://www.python.org/downloads/
  echo        and tick "Add python.exe to PATH" during the setup.
  echo     2^) If Python is already installed, point this script at it:
  echo            set SK_PYTHON=C:\Path\to\python.exe
  echo            start.bat
  echo.
  call :maybe_pause
  exit /b 3
)

if not exist "%~dp0main.py" (
  echo   [ERROR] main.py was not found next to this script.
  echo   Put start.bat in the project root ^(the folder that contains main.py^).
  echo   Current folder: %~dp0
  echo.
  call :maybe_pause
  exit /b 2
)

for /f "delims=" %%v in ('%PY% -c "import sys;print(sys.version.split()[0])"') do set "PYVER=%%v"
echo   Python    : %PYVER%    ^(%PY%^)
echo   Folder    : %~dp0
echo   Data      : %SK_DATA_DIR%
echo   Listen    : http://%SK_HOST%:%SK_PORT%/
if defined SK_DEMO_MODE echo   Mode      : DEMO ^(no real browser^)
echo.

if /i "%MODE%"=="check" goto mode_check

rem ---------------------------------------------------------------------------
rem  2. first run: create database + administrator, remember the password
rem ---------------------------------------------------------------------------
if not exist "%SK_DATA_DIR%" mkdir "%SK_DATA_DIR%" >nul 2>nul
if not exist "%SK_DATA_DIR%" (
  echo   [ERROR] cannot create the data folder: %SK_DATA_DIR%
  echo   The folder may be read-only, or the path may be on a locked drive.
  echo.
  call :maybe_pause
  exit /b 4
)

if not exist "%SK_DATA_DIR%\spark.db" goto first_run
echo   [1/2] Database already exists, skipping init.
goto after_init

:first_run
echo   [1/2] First run: creating database and administrator ...
set "SK_ADMIN_PASSWORD="
for /f "delims=" %%p in ('%PY% -c "import secrets,string;a=string.ascii_letters+string.digits;print(''.join(secrets.choice(a) for _ in range(12))+'8a')"') do set "SK_ADMIN_PASSWORD=%%p"
if not defined SK_ADMIN_PASSWORD set "SK_ADMIN_PASSWORD=SparkKeep%RANDOM%8a"
%PY% "%~dp0main.py" init
if errorlevel 1 (
  echo.
  echo   [ERROR] Initialization failed. Read the message above.
  echo.
  call :maybe_pause
  exit /b 1
)
rem NOTE: a literal "(" inside echo must be escaped as ^( otherwise cmd treats it
rem as the start of a block and the rest of the line is swallowed.
> "%SK_DATA_DIR%\INITIAL_ADMIN.txt" echo # SparkKeeper initial admin ^(generated on first start^)
>>"%SK_DATA_DIR%\INITIAL_ADMIN.txt" echo # Change this password after your first login, then delete this file.
>>"%SK_DATA_DIR%\INITIAL_ADMIN.txt" echo username=admin
>>"%SK_DATA_DIR%\INITIAL_ADMIN.txt" echo password=%SK_ADMIN_PASSWORD%
echo.
echo   Administrator created:
echo     username: admin
echo     password: %SK_ADMIN_PASSWORD%
echo   ^(also saved to %SK_DATA_DIR%\INITIAL_ADMIN.txt - delete it after your first login^)

:after_init

if /i "%MODE%"=="init" (
  echo.
  echo   Done ^(--init-only^). Start it later by running start.bat
  echo.
  call :maybe_pause
  exit /b 0
)

rem ---------------------------------------------------------------------------
rem  3. start the server (this blocks until Ctrl+C / window close)
rem ---------------------------------------------------------------------------
echo   [2/2] Starting server ... press Ctrl+C to stop.
echo.
if defined OPEN_BROWSER start "" "http://127.0.0.1:%SK_PORT%/"

%PY% "%~dp0main.py" serve --host %SK_HOST% --port %SK_PORT%
set "RC=%errorlevel%"

echo.
echo   Server exited with code %RC%.
if not "%RC%"=="0" (
  echo.
  echo   Common causes:
  echo     * port %SK_PORT% is already in use   -^>  start.bat --port 9000
  echo     * firewall or antivirus blocked python.exe
  echo     * the data folder is not writable: %SK_DATA_DIR%
  echo     * some application files are missing - re-extract the whole package
)
echo.
call :maybe_pause
exit /b %RC%

:mode_check
echo   Running environment check ...
echo.
%PY% "%~dp0main.py" check
echo   Applying database migrations ...
echo.
%PY% "%~dp0main.py" migrate
echo.
echo   Check finished.
call :maybe_pause
exit /b 0
