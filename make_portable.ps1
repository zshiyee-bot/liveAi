<#
  LiveTalking portable packager  (goal: copy the folder to another machine, double-click, run)

  Usage (in project root; make_portable.bat is the double-click wrapper):
      make_portable.bat                 pack with defaults
      make_portable.bat -DryRun         only print the plan, copy nothing
      make_portable.bat -Force          wipe the target dir first
      make_portable.bat -SkipModels -SkipData
      make_portable.bat -Zip            also compress with 7-Zip (if installed)

  Options:
      -Dst <path>        target folder (default: <project parent>\LiveTalking-portable)
      -Force             wipe target if it already exists
      -DryRun            dry run (no copy)
      -KeepWav2lip384    also copy models\wav2lip384.pth (+1.98GB; this project rejects 384 material)
      -SkipModels        do not copy models\
      -SkipData          do not copy data\
      -Zip               compress with 7-Zip after packing

  Result layout:
      python\            full CPython 3.12 + merged site-packages (torch cu128 etc.)
      bin\ffmpeg.exe     ffmpeg / ffprobe (recording, rtmp)
      models\ data\      weights + trained material / config / db
      start.bat          one-click launcher (relative paths, works from any folder)
      README-portable.txt, README-zh.md
#>
param(
    [string]$Dst = '',
    [switch]$Force,
    [switch]$DryRun,
    [switch]$KeepWav2lip384,
    [switch]$SkipModels,
    [switch]$SkipData,
    [switch]$Zip
)

$ErrorActionPreference = 'Stop'

$root = $PSScriptRoot
if (-not $root) { $root = (Get-Location).Path }
$root = (Resolve-Path $root).Path

function Info($m) { Write-Host "[pack] $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "[warn] $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "[fail] $m" -ForegroundColor Red; exit 1 }

function Robo($src, $dst, $xd, $xf) {
    $rc = @($src, $dst, '/E', '/NFL', '/NDL', '/NJH', '/NJS', '/NP', '/R:1', '/W:1', '/MT:16')
    if ($xd -ne $null -and $xd.Count -gt 0) { $rc += '/XD'; $rc += $xd }
    if ($xf -ne $null -and $xf.Count -gt 0) { $rc += '/XF'; $rc += $xf }
    if ($DryRun) { Info ('[dry] robocopy ' + ($rc -join ' ')); return }
    & robocopy @rc | Out-Null
    if ($LASTEXITCODE -ge 8) { Fail "robocopy failed (exit=$LASTEXITCODE): $src -> $dst" }
}

function DirSizeMB($p) {
    if (-not (Test-Path $p)) { return 0 }
    $s = (Get-ChildItem $p -Recurse -File -Force -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum
    if ($s -eq $null) { return 0 }
    return [math]::Round($s / 1MB, 1)
}

# ---------- find a full python install (NOT the venv) ----------
$basePy = ''
$cands = @()
$cands += (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312')
$cands += (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311')
$cands += 'C:\Python312'
$cands += 'C:\Python311'
foreach ($c in $cands) {
    if ($c -and (Test-Path (Join-Path $c 'python.exe'))) { $basePy = $c; break }
}
if (-not $basePy) { Fail 'full Python install not found (need python.exe + Lib\site-packages, e.g. %LOCALAPPDATA%\Programs\Python\Python312)' }

$venv = Join-Path $root '.venv'
$spSrc = Join-Path $venv 'Lib\site-packages'
if (-not (Test-Path $spSrc)) { Fail "virtualenv dependencies not found: $spSrc" }
if (-not (Test-Path (Join-Path $root 'app.py'))) { Fail 'this folder does not look like the LiveTalking project (app.py missing)' }

if (-not $Dst) { $Dst = Join-Path (Split-Path $root -Parent) 'LiveTalking-portable' }
Info "project : $root"
Info "python  : $basePy"
Info "target  : $Dst"

if (Test-Path $Dst) {
    if (-not $Force) { Fail "target exists: $Dst  (use -Force to wipe it)" }
    Info 'wiping old target ...'
    if (-not $DryRun) { Remove-Item $Dst -Recurse -Force }
}
if (-not $DryRun) { New-Item -ItemType Directory -Path $Dst -Force | Out-Null }

try {
    $letter = (Split-Path $Dst -Qualifier).TrimEnd(':')
    $freeGB = [math]::Round((Get-PSDrive -Name $letter).Free / 1GB, 1)
    Info "free space on target drive: $freeGB GB (about 14 GB needed)"
    if ($freeGB -lt 14 -and -not $DryRun) { Warn 'low free space; consider -SkipData / -SkipModels' }
} catch { Warn 'cannot read free space (ignored)' }

# ---------- 1) project code ----------
Info '1/7 copy project code (excluding .venv .git frontend-ls __pycache__ data models dist, *.log, *.bak)'
Robo $root $Dst @((Join-Path $root '.venv'), (Join-Path $root '.git'), (Join-Path $root 'frontend-ls'), '__pycache__', (Join-Path $root 'data'), (Join-Path $root 'models'), 'dist') @('*.log', '*.bak', '*.upstream.bak', '.backup_*')

# ---------- 2) portable python runtime ----------
Info '2/7 copy full python runtime ...'
Robo $basePy (Join-Path $Dst 'python') @('__pycache__') @()
Info '3/7 merge virtualenv site-packages into python\Lib\site-packages (about 5.6GB, slowest step) ...'
Robo $spSrc (Join-Path $Dst 'python\Lib\site-packages') @('__pycache__') @()

$binDst = Join-Path $Dst 'bin'
if (-not $DryRun) { New-Item -ItemType Directory -Path $binDst -Force | Out-Null }
foreach ($e in @('ffmpeg.exe', 'ffprobe.exe')) {
    $s = Join-Path $venv ('Scripts\' + $e)
    if (Test-Path $s) {
        if ($DryRun) { Info ('[dry] copy ' + $s) } else { Copy-Item $s $binDst -Force }
    } else { Warn ("$e missing (recording / rtmp will not work)") }
}

# ---------- 4) models ----------
if (-not $SkipModels) {
    if ($KeepWav2lip384) {
        Info '4/7 copy models\ (including wav2lip384.pth) ...'
        Robo (Join-Path $root 'models') (Join-Path $Dst 'models') @('__pycache__') @()
    } else {
        Info '4/7 copy models\ (skip wav2lip384.pth: 1.98GB, this project rejects 384 material) ...'
        Robo (Join-Path $root 'models') (Join-Path $Dst 'models') @('__pycache__') @('wav2lip384.pth')
    }
} else { Warn 'models\ skipped' }

# ---------- 5) data (material / config / db) ----------
if (-not $SkipData) {
    Info '5/7 copy data\ (trained material test1/test2/test3 + config + livestream.db) ...'
    Warn 'data\tts_config.json may contain your plaintext Doubao API key - blank it before sharing the package'
    Robo (Join-Path $root 'data') (Join-Path $Dst 'data') @('__pycache__') @()
} else { Warn 'data\ skipped' }

# ---------- 6) launcher + readme ----------
Info '6/7 write start.bat / README ...'
$launcherLines = @(
    '@echo off',
    'chcp 65001 >nul 2>&1',
    'setlocal',
    'cd /d "%~dp0"',
    'set PYTHONUTF8=1',
    'set PYTHONIOENCODING=utf-8',
    'set PATH=%~dp0python;%~dp0python\Scripts;%~dp0bin;%PATH%',
    'if not exist "%~dp0python\python.exe" (',
    '  echo [ERROR] python\python.exe not found - package incomplete.',
    '  pause',
    '  exit /b 1',
    ')',
    'echo ============================================================',
    'echo  LiveTalking portable - close this window to stop the server',
    'echo  live page : http://127.0.0.1:8063/index.html',
    'echo  material  : http://127.0.0.1:8063/materials.html',
    'echo  admin     : http://127.0.0.1:8063/ls/',
    'echo ============================================================',
    '"%~dp0python\python.exe" app.py --transport webrtc --avatar_id test1 --batch_size 16 --listenport 8063 --tts doubao',
    'echo.',
    'echo [INFO] server exited, code=%ERRORLEVEL%',
    'pause'
)
$launcherText = $launcherLines -join "`r`n"
if (-not $DryRun) { Set-Content -Path (Join-Path $Dst 'start.bat') -Value $launcherText -Encoding ASCII }

$readmeLines = @(
    'LiveTalking portable package',
    '============================',
    '1) Double-click start.bat. Wait for "start http server".',
    '2) Open in a browser:',
    '     http://127.0.0.1:8063/index.html      (live page; avatar id defaults to test1)',
    '     http://127.0.0.1:8063/materials.html  (material / playlist editor)',
    '     http://127.0.0.1:8063/ls/             (live-stream admin)',
    '3) Closing that console window stops the server.',
    '',
    'Requirements',
    '  - Windows 10/11 x64 + NVIDIA GPU + installed driver (CUDA runtime ships inside torch)',
    '  - put this folder somewhere writable (not C:\Program Files); prefer an ASCII path',
    '  - first start takes 10-45s to load models',
    '',
    'Included',
    '  - full Python 3.12 runtime + all dependencies (torch cu128 / PyAV / aiortc / transformers / diffusers ...)',
    '  - models\ weights (wav2lip384.pth excluded: this project rejects 384 material)',
    '  - data\ material and config (test1 = MuseTalk chain, test3 = wav2lip chain)',
    '  - bin\ffmpeg.exe / ffprobe.exe',
    '',
    'Caveats',
    '  - data\tts_config.json may hold a plaintext Doubao API key: blank it before sharing',
    '  - on a non-NVIDIA machine MuseTalk cannot run in real time (falls back to CPU, very slow)'
)
if (-not $DryRun) { Set-Content -Path (Join-Path $Dst 'README-portable.txt') -Value ($readmeLines -join "`r`n") -Encoding ASCII }

# optional: copy the Chinese notes (pure file copy, no string handling) and a Chinese launcher name on PS7+
$zhSrc = Join-Path $root 'PORTABLE-README-zh.md'
if (-not $DryRun -and (Test-Path $zhSrc)) { Copy-Item $zhSrc (Join-Path $Dst 'README-zh.md') -Force }
if (-not $DryRun -and $PSVersionTable.PSVersion.Major -ge 6) {
    Copy-Item (Join-Path $Dst 'start.bat') (Join-Path $Dst ([char]0x542F + [char]0x52A8 + '.bat')) -Force
}

# ---------- 7) self test ----------
if (-not $DryRun) {
    Info '7/7 self test: import key modules with the packaged python ...'
    $py = Join-Path $Dst 'python\python.exe'
    & $py '-c' "import sys;print('  python :',sys.version.split()[0]);import torch;print('  torch  :',torch.__version__,'cuda=',torch.cuda.is_available());import av,aiortc,cv2,soundfile,resampy,transformers,diffusers;print('  deps   : OK')"
    if ($LASTEXITCODE -ne 0) { Warn 'self test failed - some dependency was not merged correctly' }

    $pths = Get-ChildItem (Join-Path $Dst 'python\Lib\site-packages') -Filter *.pth -File -ErrorAction SilentlyContinue
    $bad = @()
    foreach ($p in $pths) {
        $t = Get-Content $p.FullName -Raw -ErrorAction SilentlyContinue
        if ($t -match 'LiveTalking' -or $t -match '\.venv') { $bad += $p.Name }
    }
    if ($bad.Count -gt 0) { Warn ('these .pth files contain old absolute paths - check them: ' + ($bad -join ', ')) }
}

# ---------- summary ----------
if (-not $DryRun) {
    Write-Host ''
    Write-Host '================ packed ================' -ForegroundColor Green
    Write-Host ('target   : ' + $Dst)
    Write-Host ('total    : ' + (DirSizeMB $Dst) + ' MB')
    Write-Host ('python\  : ' + (DirSizeMB (Join-Path $Dst 'python')) + ' MB')
    Write-Host ('models\  : ' + (DirSizeMB (Join-Path $Dst 'models')) + ' MB')
    Write-Host ('data\    : ' + (DirSizeMB (Join-Path $Dst 'data')) + ' MB')
    Write-Host ''
    Write-Host 'next steps:'
    Write-Host ('  1) run ' + (Join-Path $Dst 'start.bat') + ' to verify')
    Write-Host '  2) move/rename the whole folder and run start.bat again to prove portability'
    Write-Host '  3) compress for sharing (7-Zip):'
    Write-Host ("     & 'C:\Program Files\7-Zip\7z.exe' a -mx=3 -v4096m '" + (Split-Path $Dst -Parent) + '\LiveTalking-portable.7z" "' + $Dst + '"')
    if ($Zip) {
        $sevenZip = @('C:\Program Files\7-Zip\7z.exe', 'C:\Program Files (x86)\7-Zip\7z.exe') | Where-Object { Test-Path $_ } | Select-Object -First 1
        if ($sevenZip) {
            $arch = Join-Path (Split-Path $Dst -Parent) 'LiveTalking-portable.7z'
            Info ('compressing to ' + $arch + ' ...')
            & $sevenZip a -mx=3 -v4096m $arch $Dst | Select-Object -Last 5
        } else { Warn '7z.exe not found - skipped compression' }
    }
}
