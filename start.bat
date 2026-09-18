@echo off
rem ============================================================
rem  LiveTalking 启动脚本 ----- 库 / 素材链 模式
rem ------------------------------------------------------------
rem  用法:
rem    start.bat                 启动服务 (默认端口 8010)
rem    start.bat <额外参数...>   原样透传给 app.py, 例如:
rem        start.bat --listenport 8020
rem        start.bat --transport rtmp
rem        start.bat --tts edgetts                临时改回微软 TTS
rem        start.bat --avatar_id 数字人1
rem ------------------------------------------------------------
rem  TTS (默认豆包 + 声音复刻音色):
rem    默认追加: --tts doubao --doubao_resource_id seed-icl-2.0
rem    · 想换音色: 素材页 - 素材链编排 - 本链音色 里按【素材链】绑定音色,
rem      写进 <库>\playlist.json 的 voice 字段, 连接该库时自动生效, 不用重启。
rem    · 素材链没绑音色时, 用豆包自己的默认音色 (config.py 会把 edgetts 的
rem      默认音色名自动换成豆包的, 避免把 edge 音色名发给豆包导致没声音)。
rem    · 未配豆包 API Key 时自动回退 edgetts (保证有声音), 见下面 [WARN]。
rem      配置: 素材页 - 素材链编排 - 本链音色 - 粘贴 Key - 点「保存Key」
rem      (保存后生成 data\tts_key_ok.flag, 下次双击本脚本即用豆包复刻音色)
rem    · 显式传参会覆盖默认值 (argparse 后者优先): start.bat --tts edgetts
rem ------------------------------------------------------------
rem  与旧版的区别 (重要):
rem    旧版在启动时写死「模型 + 素材名」(wav2lip256 + wav2lip256_avatar1),
rem    那些素材已删除, 且一次只能跑一种模型。
rem    现在模型由服务在【连接时】按素材目录特征自动判定并懒加载
rem    (avatars/auto_loader.py), 任意时刻只驻留一个模型:
rem      · 启动快, 不占显存
rem      · 网页里「角色 ID」填素材目录名或库名即可, 模型自动匹配
rem    因此本脚本不再传 --model (代码里 opt.model 没有任何读取点)。
rem ------------------------------------------------------------
rem  两个已验证的坑:
rem    ① 直接调 .venv\Scripts\python.exe 不会把该目录加进 PATH
rem       (只有 activate 会), 而 ffmpeg/ffprobe 就硬链在那里,
rem       不显式加 PATH 则录制合流 (stop_recording 调 ffmpeg) 必失败。
rem       故下面显式 set PATH 并做一次自检打印。
rem    ② 控制流全 ASCII (中文只出现在 echo); 文件必须 UTF-8 BOM + CRLF,
rem       否则 cmd 在 chcp 生效前解析多字节字符会乱码/撕碎逻辑。
rem  本脚本不修改任何源码, 只设环境变量并调用 app.py。
rem ============================================================

setlocal
chcp 65001 >nul 2>&1
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PORT=8010
set PYEXE=%~dp0.venv\Scripts\python.exe

rem ---- 把 venv\Scripts 放到 PATH 最前: ffmpeg / ffprobe 硬链在此 ----
set PATH=%~dp0.venv\Scripts;%PATH%

cd /d "%~dp0"

rem ---- 检查 venv ----
if not exist "%PYEXE%" (
    echo [ERROR] 未找到 .venv, 请先创建虚拟环境并安装依赖:
    echo         python -m venv .venv
    echo         .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

rem ---- TTS 选择: 默认豆包 + 声音复刻音色; 没有 Key 则回退 edgetts ----
set "TTSARGS=--tts doubao --doubao_resource_id seed-icl-2.0"
set "TTSNAME=doubao 声音复刻 (seed-icl-2.0)"
if defined DOUBAO_API_KEY goto tts_ready
if exist "data\tts_key_ok.flag" goto tts_ready
set "TTSARGS="
set "TTSNAME=edgetts 回退 (未检测到豆包 Key)"
echo [WARN] 未检测到豆包 API Key, 本次以 edgetts 启动: 有声音, 但不是你的复刻音色。
echo [WARN] 配置方法: 打开素材页 - 素材链编排 - 本链音色 - 粘贴豆包 API Key
echo [WARN]          - 点「保存Key」, 然后重新双击本脚本, 即自动切换到豆包复刻音色。
:tts_ready

echo ============================================================
echo  环境自检
echo ============================================================
"%PYEXE%" -c "import sys,shutil;print('  python  :',sys.executable);print('  ffmpeg  :',shutil.which('ffmpeg'));print('  ffprobe :',shutil.which('ffprobe'))"

rem ---- 素材概览 + 自动挑选默认角色 ID ----
set DEFAULT_AVATAR=
echo ============================================================
echo  素材 / 库 概览:  data\avatars
echo ============================================================
for /d %%d in ("data\avatars\*") do (
    if exist "%%d\playlist.json" (
        echo   [可播-素材链] %%~nxd
        if not defined DEFAULT_AVATAR set DEFAULT_AVATAR=%%~nxd
    ) else (
        if exist "%%d\coords.pkl" (
            echo   [可播-单素材] %%~nxd
            if not defined DEFAULT_AVATAR set DEFAULT_AVATAR=%%~nxd
        ) else (
            echo   [未就绪] %%~nxd   ^<- 库需先在 materials.html 保存素材链
        )
    )
)
if not defined DEFAULT_AVATAR (
    echo   [WARN] 没有可播素材。请先在 materials.html 上传并训练素材。
)
echo ============================================================

rem ---- 清理监听端口占用 (按 PID 精确结束, 绝不用 taskkill /IM) ----
for /f "tokens=5" %%p in ('netstat -ano -p tcp ^| findstr ":%PORT% " ^| findstr "LISTENING"') do (
    echo [INFO] 结束占用 %PORT% 的进程 PID=%%p
    taskkill /F /PID %%p >nul 2>&1
)
ping -n 3 127.0.0.1 >nul 2>&1

echo.
echo   端口   : %PORT%
echo   TTS    : %TTSNAME%
if not "%*"=="" echo   手动参数 : %*   （命令行覆盖默认 TTS）
echo   直播页 : http://127.0.0.1:%PORT%/index.html
echo   素材页 : http://127.0.0.1:%PORT%/materials.html
if defined DEFAULT_AVATAR echo   默认角色 ID（网页里留空时使用）: %DEFAULT_AVATAR%
echo.
echo [INFO] 保持本窗口打开, 关闭窗口即停止服务。
echo.

if defined DEFAULT_AVATAR (
    "%PYEXE%" app.py --transport webrtc --avatar_id %DEFAULT_AVATAR% --batch_size 16 --listenport %PORT% %TTSARGS% %*
) else (
    "%PYEXE%" app.py --transport webrtc --batch_size 16 --listenport %PORT% %TTSARGS% %*
)

echo.
echo [INFO] 服务已退出, 退出码 = %ERRORLEVEL%
pause
endlocal
