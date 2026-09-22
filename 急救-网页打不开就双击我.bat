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
rem  NOTE: keep this file ASCII-only, CRLF, no BOM (cmd requirement).
rem ===================================================================
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
title danmaku proxy rescue

if not exist "%~dp0python\python.exe" (
  echo [ERROR] python\python.exe not found - run this inside the full package.
  pause
  exit /b 1
)

"%~dp0python\python.exe" "%~dp0danmaku_wizard.py" --fix-proxy
exit /b 0
