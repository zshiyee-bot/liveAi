@echo off
rem ===================================================================
rem  Danmaku forwarder launcher
rem
rem  This .bat is only a thin launcher: all the Chinese interactive
rem  questions live in danmaku_wizard.py, because cmd.exe mis-parses
rem  Chinese text in a .bat while chcp 65001 is on.
rem
rem  Works in two layouts:
rem    1) package root          : .bat next to python\ and web\
rem    2) a subfolder one level below the root (the Windows live-PC
rem       helper folder)        : python\ is then found at ..\python\
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

set "PYEXE="
if exist "%~dp0python\python.exe" set "PYEXE=%~dp0python\python.exe"
if not defined PYEXE if exist "%~dp0..\python\python.exe" set "PYEXE=%~dp0..\python\python.exe"
if not defined PYEXE for %%P in (python.exe) do set "PYEXE=%%~$PATH:P"
if not defined PYEXE (
  echo [ERROR] python not found.
  echo         Put this folder inside the tzlive package - the one that
  echo         contains python\ and web\ - and run it again.
  pause
  exit /b 1
)
if not exist "%~dp0danmaku_wizard.py" (
  echo [ERROR] danmaku_wizard.py not found next to this .bat
  pause
  exit /b 1
)

"%PYEXE%" "%~dp0danmaku_wizard.py" %*
set RC=%ERRORLEVEL%

echo.
echo [INFO] exited, code=%RC%  (this window can now be closed)
pause