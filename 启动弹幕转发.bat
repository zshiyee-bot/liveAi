@echo off
rem ===================================================================
rem  Danmaku forwarder launcher (qidong danmu zhuanfa)
rem
rem  This .bat is only a thin launcher: all the Chinese interactive
rem  questions live in danmaku_wizard.py, because cmd.exe mis-parses
rem  Chinese text in a .bat while chcp 65001 is on.
rem
rem  NOTE: keep this file ASCII-only, CRLF line endings and NO BOM.
rem        That is a hard requirement, not a style choice:
rem          - LF endings   -> cmd jumps to the wrong labels
rem          - a BOM        -> "@echo off" is not recognised
rem          - Chinese text -> cmd mis-parses lines (executes rem text)
rem ===================================================================
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
title Danmaku forwarder

if not exist "%~dp0python\python.exe" (
  echo [ERROR] python\python.exe not found - please run this inside the full package.
  pause
  exit /b 1
)
if not exist "%~dp0danmaku_wizard.py" (
  echo [ERROR] danmaku_wizard.py not found next to this .bat
  pause
  exit /b 1
)

"%~dp0python\python.exe" "%~dp0danmaku_wizard.py" %*
set RC=%ERRORLEVEL%

echo.
echo [INFO] exited, code=%RC%  (this window can now be closed)
pause
