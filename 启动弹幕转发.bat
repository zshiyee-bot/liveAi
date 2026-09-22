@echo off
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

rem ===================================================================
rem  EDIT THIS LINE: your LiveTalking server address
rem  ===================================================================
set LS_SERVER=http://192.168.1.10:8063

rem  If the server has LS_DANMAKU_TOKEN set, put the same value here
set LS_DANMAKU_TOKEN=

rem  Room key: only needed when one server hosts several live rooms.
rem  Leave empty for a single-room setup.
set LS_ROOM_KEY=

rem  Local DouyinBarrageGrab websocket (default 8888; usually no change)
set LS_RELAY_WS=ws://127.0.0.1:8888

rem  报文方言 / message dialect of the grabber:
rem    auto    = 自动识别（默认，看字段特征判断） / detect automatically
rem    ape     = DouyinBarrageGrab (ape-byte)    / 抖音抓包经典版（推荐）
rem    wushuai = BarrageGrab (wushuaihua520)     / 另一家的开源抖音版
set LS_DIALECT=auto
rem ===================================================================

if not exist "%~dp0python\python.exe" (
  echo [ERROR] python\python.exe not found.
  echo         Please run this inside the full portable package.
  pause
  exit /b 1
)
if not exist "%~dp0danmaku_forward.py" (
  echo [ERROR] danmaku_forward.py not found next to this .bat
  pause
  exit /b 1
)

echo ============================================================
echo  Danmaku forwarder
echo    relay  : %LS_RELAY_WS%   (DouyinBarrageGrab / BarrageGrab)
echo    dialect: %LS_DIALECT%
echo    target : %LS_SERVER%
if not "%LS_ROOM_KEY%"=="" echo    room   : %LS_ROOM_KEY%
echo  Keep this window open while streaming. Ctrl+C to stop.
echo ============================================================

set EXTRA=
if not "%LS_DANMAKU_TOKEN%"=="" set EXTRA=%EXTRA% --token "%LS_DANMAKU_TOKEN%"
if not "%LS_ROOM_KEY%"=="" set EXTRA=%EXTRA% --key "%LS_ROOM_KEY%"
if not "%LS_DIALECT%"=="" set EXTRA=%EXTRA% --dialect "%LS_DIALECT%"

"%~dp0python\python.exe" "%~dp0danmaku_forward.py" --server "%LS_SERVER%" --relay "%LS_RELAY_WS%" %EXTRA% %*
echo.
echo [INFO] forwarder exited, code=%ERRORLEVEL%
pause
