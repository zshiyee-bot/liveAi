@echo off
rem ===================================================================
rem  One-click rescue: "web pages stopped loading / no internet" after
rem  using the danmaku grabber in browser mode (it set a system proxy
rem  and got force-killed before it could restore it).
rem
rem  This just turns the leftover system proxy OFF. It is safe to run:
rem  it only touches the proxy when it still points at the grabber
rem  (127.0.0.1:8827) - your own VPN/proxy settings are left alone.
rem
rem  Works both in the package root and in a subfolder one level below
rem  it (python\ is then found at ..\python\).
rem
rem  NOTE: keep this file ASCII-only, CRLF, no BOM (cmd requirement).
rem ===================================================================
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
title danmaku proxy rescue

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

"%PYEXE%" "%~dp0danmaku_wizard.py" --fix-proxy
exit /b 0