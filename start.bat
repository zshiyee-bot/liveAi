@echo off
rem ============================================================
rem  LiveTalking 启动脚本 (D:\desk\新猜想\LiveTalking)
rem  用法:
rem    start.bat            -> wav2lip256 + wav2lip256_avatar1
rem    start.bat 384        -> wav2lip384 + wav2lip384_avatar1
rem    start.bat musetalk   -> musetalk  + musetalk_avatar1
rem    start.bat long       -> wav2lip256 + test2  (4932 帧长素材)
rem    start.bat test <id>  -> wav2lip256 + 指定 avatar_id
rem ------------------------------------------------------------
rem  设计说明:
rem   - 控制流全部使用 ASCII + goto 标签, 中文只出现在 echo 中,
rem     避免 cmd 在 chcp 生效前解析多字节字符导致乱码/逻辑被撕碎。
rem   - 本脚本不改动项目源码, 仅设置环境变量并调用 app.py。
rem   - ffmpeg 已硬链到 .venv\Scripts, venv 激活后即在 PATH 中,
rem     因此无需修改任何源码中的 ffmpeg 调用。
rem ============================================================

setlocal
chcp 65001 >nul 2>&1
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PORT=8010

cd /d "%~dp0"

rem ---- 检查 venv ----
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] 未找到 .venv, 请先创建虚拟环境并安装依赖:
    echo         python -m venv .venv
    echo         .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

rem ---- 检查模型权重 ----
if not exist "models\wav2lip.pth" (
    echo [WARN] 未找到 models\wav2lip.pth, wav2lip 模型将无法加载
)

rem ---- 清理 8010 端口占用 ----
for /f "tokens=5" %%p in ('netstat -ano -p tcp ^| findstr ":8010 " ^| findstr "LISTENING"') do (
    echo [INFO] 结束占用 8010 的进程 PID=%%p
    taskkill /F /PID %%p >nul 2>&1
)
timeout /t 2 /nobreak >nul

rem ---- 解析模式 ----
set MODE=%~1
if "%MODE%"==""      goto MODE256
if /i "%MODE%"=="256"      goto MODE256
if /i "%MODE%"=="384"      goto MODE384
if /i "%MODE%"=="musetalk" goto MODEMUSE
if /i "%MODE%"=="long"     goto MODELONG
if /i "%MODE%"=="test"     goto MODETEST

echo [ERROR] 未知模式: %MODE%
echo         可用: 256 / 384 / musetalk / long / test ^<avatar_id^>
pause
exit /b 1

:MODE256
set AVATAR_ID=wav2lip256_avatar1
set MODEL_NAME=wav2lip
set EXTRA_ARGS=
goto RUN

:MODE384
set AVATAR_ID=wav2lip384_avatar1
set MODEL_NAME=wav2lip
set EXTRA_ARGS=--modelfile ./models/wav2lip384.pth
goto RUN

:MODEMUSE
set AVATAR_ID=musetalk_avatar1
set MODEL_NAME=musetalk
set EXTRA_ARGS=
goto RUN

:MODELONG
set AVATAR_ID=test2
set MODEL_NAME=wav2lip
set EXTRA_ARGS=
goto RUN

:MODETEST
set AVATAR_ID=%~2
if "%AVATAR_ID%"=="" (
    echo [ERROR] 用法: start.bat test ^<avatar_id^>
    pause
    exit /b 1
)
set MODEL_NAME=wav2lip
set EXTRA_ARGS=
goto RUN

:RUN
echo ============================================================
echo  模型   : %MODEL_NAME%
echo  角色   : %AVATAR_ID%
echo  端口   : %PORT%
echo  地址   : http://127.0.0.1:%PORT%/index.html
echo ============================================================
echo.
echo [INFO] 保持本窗口打开, 关闭窗口即停止服务。
echo.

.venv\Scripts\python.exe app.py --transport webrtc --model %MODEL_NAME% --avatar_id %AVATAR_ID% --batch_size 16 %EXTRA_ARGS% --listenport %PORT%

echo.
echo [INFO] 服务已退出, 退出码 = %ERRORLEVEL%
pause
endlocal
