@echo off
rem ===================================================================
rem  Diagnostic collector -- double-click this when something "flashes
rem  a black box" or does not start, then send the log file to the author.
rem
rem  It only READS state (ports / processes / proxy / files) and writes
rem  one text file next to itself. It changes nothing.
rem
rem  Works both in the package root and in a subfolder one level below
rem  it (python\ is then found at ..\python\).
rem
rem  Keep this file ASCII-only, CRLF and no BOM (cmd requirement).
rem ===================================================================
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set LOG=%~dp0_diag_log.txt

rem  locate python (root, or one level down, or on PATH)
set "PYEXE="
if exist "%~dp0python\python.exe" set "PYEXE=%~dp0python\python.exe"
if not defined PYEXE if exist "%~dp0..\python\python.exe" set "PYEXE=%~dp0..\python\python.exe"
if not defined PYEXE for %%P in (python.exe) do set "PYEXE=%%~$PATH:P"

rem  locate the grabber tool (root, or one level down)
set "TOOLS=%~dp0tools\DouyinBarrageGrab"
if not exist "%TOOLS%" set "TOOLS=%~dp0..\tools\DouyinBarrageGrab"

echo ============================================================ > "%LOG%"
echo  tzlive diagnostic  %date% %time% >> "%LOG%"
echo ============================================================ >> "%LOG%"

echo. >> "%LOG%"
echo [0] resolved paths >> "%LOG%"
echo   bat dir  = %~dp0 >> "%LOG%"
echo   python   = %PYEXE% >> "%LOG%"
echo   tools    = %TOOLS% >> "%LOG%"

echo. >> "%LOG%"
echo [1] package path / files >> "%LOG%"
echo   cwd = %CD% >> "%LOG%"
dir /b >> "%LOG%" 2>&1

echo. >> "%LOG%"
echo [2] python present? >> "%LOG%"
if defined PYEXE (
  echo   python = FOUND >> "%LOG%"
  "%PYEXE%" -c "import sys,platform;print('   python',sys.version.split()[0],platform.architecture()[0])" >> "%LOG%" 2>&1
  "%PYEXE%" -c "import requests,websocket;print('   requests/websocket OK')" >> "%LOG%" 2>&1
  "%PYEXE%" -c "import danmaku_wizard;print('   danmaku_wizard import OK')" >> "%LOG%" 2>&1
) else (
  echo   python = NOT FOUND >> "%LOG%"
)

echo. >> "%LOG%"
echo [3] ports 8063 / 8888 >> "%LOG%"
netstat -ano | findstr ":8063" >> "%LOG%" 2>&1
netstat -ano | findstr ":8888" >> "%LOG%" 2>&1

echo. >> "%LOG%"
echo [4] related processes >> "%LOG%"
tasklist /fi "imagename eq python.exe" >> "%LOG%" 2>&1
tasklist /fi "imagename eq pythonw.exe" >> "%LOG%" 2>&1
tasklist /fi "imagename eq WssBarrageServer.exe" >> "%LOG%" 2>&1

echo. >> "%LOG%"
echo [5] system proxy (a leftover here = "no internet") >> "%LOG%"
reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyEnable >> "%LOG%" 2>&1
reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyServer >> "%LOG%" 2>&1

echo. >> "%LOG%"
echo [6] saved forwarder config (if any) >> "%LOG%"
if exist "%~dp0danmaku_config.json" (
  type "%~dp0danmaku_config.json" >> "%LOG%" 2>&1
) else (
  echo   no danmaku_config.json next to this .bat >> "%LOG%"
)

echo. >> "%LOG%"
echo [7] last run log (if any) >> "%LOG%"
if exist "%~dp0_danmaku_run.log" (
  powershell -NoProfile -Command "Get-Content '%~dp0_danmaku_run.log' -Tail 40" >> "%LOG%" 2>&1
) else (
  echo   no _danmaku_run.log yet >> "%LOG%"
)

echo. >> "%LOG%"
echo [8] grabber tool files >> "%LOG%"
if exist "%TOOLS%" (
  dir /b "%TOOLS%" >> "%LOG%" 2>&1
) else (
  echo   tools\DouyinBarrageGrab NOT FOUND >> "%LOG%"
)

echo. >> "%LOG%"
echo ============================================================ >> "%LOG%"
echo  done. >> "%LOG%"

echo ============================================================
echo  Diagnostic finished.
echo  Log file:
echo    %LOG%
echo  Please send that file to the author.
echo ============================================================
pause