@echo off
rem ===================================================================
rem  Danmaku forwarder (qidong danmu zhuanfa)
rem
rem  NOTE: keep this file ASCII-only, CRLF line endings and NO BOM.
rem        cmd.exe mis-parses Chinese text in .bat while chcp 65001 is on.
rem        Chinese help is printed by the python program below / see the doc.
rem ===================================================================
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set LOG=%~dp0_danmaku_run.log
echo. >> "%LOG%"
echo [%date% %time%] ==== bat start (elevated=%LS_ELEVATED%) ==== >> "%LOG%"

rem -------------------------------------------------------------------
rem  EDIT THIS LINE: your LiveTalking server address
rem    server runs on THIS pc  ->  set LS_SERVER=http://127.0.0.1:8063
rem    server is another pc    ->  set LS_SERVER=http://192.168.1.50:8063
rem  If you leave the example value the bat refuses to run and tells you.
rem -------------------------------------------------------------------
set LS_SERVER=http://192.168.1.10:8063

rem  If the server has LS_DANMAKU_TOKEN set, put the same value here
set LS_DANMAKU_TOKEN=

rem  Room key: only needed when one server hosts several live rooms.
rem  Leave empty for a single-room setup.
set LS_ROOM_KEY=

rem  Data source:
rem    relay  = grabber tool over websocket (douyin / kuaishou ...)
rem    taobao = taobao live: official mtop polling, NO grabber tool needed
rem    both   = run both sources at the same time
set LS_SOURCE=relay

rem  Auto start the bundled douyin grabber (tools\DouyinBarrageGrab):
rem    1 = yes: start it silently, stop it when this window closes
rem    0 = no : you already run a grabber yourself
set LS_AUTOSTART_GRABBER=1

rem  Which preset config the grabber starts with:
rem    companion = live-companion mode, no system proxy, no certificate (YOUR OWN live)
rem    browser   = browser mode, uses system proxy (testing other people's rooms)
set LS_GRABBER_MODE=companion

rem  --- source=relay (douyin / kuaishou) ---
rem  Local DouyinBarrageGrab websocket (default 8888; usually no change)
set LS_RELAY_WS=ws://127.0.0.1:8888
rem  Message dialect: auto | ape (DouyinBarrageGrab, recommended) | wushuai (BarrageGrab)
set LS_DIALECT=auto

rem  --- source=taobao ---
rem  Taobao live room id: the number after "liveId=" in the address bar, e.g.
rem    https://tbzb.taobao.com/live?...&liveId=2318604422529278  ->  2318604422529278
set LS_LIVE_ID=
rem  Poll interval in seconds (do not go below 2, or taobao may rate-limit you)
set LS_TAOBAO_INTERVAL=3
rem  On start also send the last N old comments (0 = only new ones, 3 = see it work at once)
set LS_TAOBAO_REPLAY=0
rem -------------------------------------------------------------------

set GRABBER_DIR=%~dp0tools\DouyinBarrageGrab
set GRABBER_EXE=%GRABBER_DIR%\WssBarrageServer.exe
set CFGSRC=%GRABBER_DIR%\config-companion.xml
if /i "%LS_GRABBER_MODE%"=="browser" set CFGSRC=%GRABBER_DIR%\config-browser.xml

if not exist "%~dp0python\python.exe" (
  echo [ERROR] python\python.exe not found - please run this inside the full package.
  echo [ERROR] python\python.exe not found >> "%LOG%"
  pause
  exit /b 1
)
if not exist "%~dp0danmaku_forward.py" (
  echo [ERROR] danmaku_forward.py not found next to this .bat
  pause
  exit /b 1
)

set NEED_GRABBER=0
if /i not "%LS_AUTOSTART_GRABBER%"=="1" goto :after_grabber_flag
if not exist "%GRABBER_EXE%" goto :after_grabber_flag
if /i "%LS_SOURCE%"=="relay" set NEED_GRABBER=1
if /i "%LS_SOURCE%"=="both" set NEED_GRABBER=1
:after_grabber_flag

echo ============================================================
echo  Danmaku forwarder
echo    source : %LS_SOURCE%
echo    target : %LS_SERVER%
if not "%LS_ROOM_KEY%"=="" echo    room   : %LS_ROOM_KEY%
if /i "%LS_SOURCE%"=="taobao" echo    taobao : liveId=%LS_LIVE_ID%   every %LS_TAOBAO_INTERVAL%s, no grabber tool
if /i "%LS_SOURCE%"=="both" echo    taobao : liveId=%LS_LIVE_ID%   every %LS_TAOBAO_INTERVAL%s
if /i "%LS_SOURCE%"=="relay" echo    relay  : %LS_RELAY_WS%   dialect=%LS_DIALECT%
if "%NEED_GRABBER%"=="1" echo    grabber: %LS_GRABBER_MODE% mode, silent, auto stopped on exit
echo  Keep this window open while streaming. Ctrl+C to stop.
echo ============================================================

if /i "%LS_SERVER%"=="http://192.168.1.10:8063" goto :need_server
if "%NEED_GRABBER%"=="1" goto :start_grabber
goto :run_forwarder

rem -------------------------------------------------------------------
rem  start the bundled douyin grabber, silently
rem
rem  NOTE: start it with /MIN (it gets its OWN console window), never /B.
rem        /B shares our console, and the grabber hides its own console
rem        (hideConsole=true) -> our window disappears too, which looks
rem        exactly like "the bat flashed and closed".
rem  It needs admin rights (it hooks/patches the live-companion app), so
rem  if we are not elevated: keep THIS window, tell the user, wait for a
rem  key, then relaunch elevated (exactly one UAC prompt).
rem -------------------------------------------------------------------
:start_grabber
if exist "%CFGSRC%" copy /y "%CFGSRC%" "%GRABBER_DIR%\WssBarrageServer.exe.config" >nul

rem already relaunched as admin? do not check again (avoids a UAC loop)
if "%LS_ELEVATED%"=="1" goto :grabber_ok
rem robust check: only an elevated process has the High Mandatory Level SID
whoami /groups 2>nul | find /i "S-1-16-12288" >nul 2>&1
if not errorlevel 1 goto :grabber_ok

echo.
echo  ------------------------------------------------------------
echo   [ADMIN NEEDED] The douyin grabber must run as administrator
echo                  (it has to hook into the live-companion app).
echo.
echo   Press any key, then a UAC window will pop up:
echo        *** PLEASE CLICK YES ***
echo.
echo   Nothing happened? Close this and right-click the .bat -
echo   "Run as administrator".
echo  ------------------------------------------------------------
echo.
echo [%date% %time%] not elevated - asking for UAC >> "%LOG%"
pause

echo [%date% %time%] launching elevated instance ... >> "%LOG%"
powershell -NoProfile -Command "$env:LS_ELEVATED='1'; Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b

:grabber_ok
echo [START] douyin grabber, silent (it hides its own console) ...
echo [%date% %time%] starting grabber (%LS_GRABBER_MODE%) >> "%LOG%"
echo [%date% %time%] killing leftover grabbers >> "%LOG%"
taskkill /IM WssBarrageServer.exe /F >nul 2>&1
start "DouyinBarrageGrab" /MIN "%GRABBER_EXE%"
echo [WAIT] 6 seconds for it to hook the live channel ...
ping -n 7 127.0.0.1 >nul
goto :run_forwarder

rem -------------------------------------------------------------------
rem  run the forwarder
rem -------------------------------------------------------------------
:run_forwarder
set EXTRA=
if not "%LS_DANMAKU_TOKEN%"=="" set EXTRA=%EXTRA% --token "%LS_DANMAKU_TOKEN%"
if not "%LS_ROOM_KEY%"=="" set EXTRA=%EXTRA% --key "%LS_ROOM_KEY%"
if not "%LS_DIALECT%"=="" set EXTRA=%EXTRA% --dialect "%LS_DIALECT%"
if not "%LS_SOURCE%"=="" set EXTRA=%EXTRA% --source "%LS_SOURCE%"
if not "%LS_LIVE_ID%"=="" set EXTRA=%EXTRA% --live-id "%LS_LIVE_ID%"
if not "%LS_TAOBAO_INTERVAL%"=="" set EXTRA=%EXTRA% --interval "%LS_TAOBAO_INTERVAL%"
if not "%LS_TAOBAO_REPLAY%"=="" set EXTRA=%EXTRA% --replay-backlog "%LS_TAOBAO_REPLAY%"

echo [%date% %time%] forwarder start: source=%LS_SOURCE% target=%LS_SERVER% >> "%LOG%"
"%~dp0python\python.exe" "%~dp0danmaku_forward.py" --server "%LS_SERVER%" --relay "%LS_RELAY_WS%" %EXTRA% %*
set RC=%ERRORLEVEL%
echo [%date% %time%] forwarder exited, code=%RC% >> "%LOG%"

if not "%NEED_GRABBER%"=="1" goto :finish
echo [CLEAN] stopping the douyin grabber ...
taskkill /IM WssBarrageServer.exe /F >nul 2>&1

:finish
echo.
echo [INFO] forwarder exited, code=%RC%
echo  (this window can now be closed)
pause
exit /b 0

rem -------------------------------------------------------------------
rem  the server address was never edited -> stop and tell the user,
rem  instead of silently running against a server that does not exist
rem -------------------------------------------------------------------
:need_server
echo.
echo  ------------------------------------------------------------
echo   [STOP] LS_SERVER is still the example address:
echo            %LS_SERVER%
echo.
echo   Open this .bat with Notepad, find the line
echo            set LS_SERVER=...
echo   and change it to your LiveTalking server:
echo            set LS_SERVER=http://127.0.0.1:8063     (server on this PC)
echo            set LS_SERVER=http://192.168.1.50:8063  (server elsewhere)
echo   Save it, then double-click this .bat again.
echo  ------------------------------------------------------------
echo [%date% %time%] STOP: LS_SERVER was not edited >> "%LOG%"
echo.
pause
exit /b 1
