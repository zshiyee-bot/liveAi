"""素材链音色（TTS voice）路由 —— 「一个素材链 = 一个音色」

设计要点（为什么这样做）：
  1. 音色存在**该素材链自己的 playlist.json** 里（字段 `voice`），而不是全局参数。
     原因：音色属于"这个人"，跟着链走 —— 换链即换音色，直播端不需要任何额外动作。
  2. 建会话时由 `avatars/wav2lip_avatar.py::_load_segments` 读出 `voice`，
     经 `init_playlist(..., voice=...)` 交给 `BaseAvatar._apply_tts_voice()` 套用到 TTS 实例。
  3. 在页面上改音色时，本文件直接对**活跃会话**调 `_apply_tts_voice()`（热生效、不断音频、
     不需要重载素材链）—— 豆包引擎每条消息都读 `self.voice / self.resource_id`。
  4. 火山引擎**没有**"列出全部已复刻音色"的接口（只有按 speaker_id 查单个的
     `/api/v3/tts/get_voice`），所以这里是"你填音色ID → 我们校验状态 + 给出官方试听音频"，
     不做音色列表同步。

对外接口：
  GET    /api/tts/config            → 豆包 key 是否已配（掩码）+ 当前引擎/默认音色
  PUT    /api/tts/config            → 保存豆包 key / resource_id（立即生效，无需重启）
  GET    /api/tts/voice_options     → 页面下拉用的静态选项（resource_id / 常用预置音色）
  POST   /api/tts/voice_check       → 用 get_voice 校验某个音色ID（返回状态/语言/官方试听）
  GET    /api/libs/{lib}/voice      → 读该素材链绑定的音色
  PUT    /api/libs/{lib}/voice      → 写该素材链的音色（并热套用到活跃会话）
  DELETE /api/libs/{lib}/voice      → 清除该素材链的音色（回落到启动参数默认）
"""

import os
import json
import uuid
import asyncio

import requests
from aiohttp import web

from utils.logger import logger

AVATARS_ROOT = './data/avatars'
PLAYLIST_NAME = 'playlist.json'
TTS_CONFIG_PATH = './data/tts_config.json'
DOUBAO_VOICE_URL = 'https://openspeech.bytedance.com/api/v3/tts/get_voice'

# 火山官方 resource_id：预置大模型音色 / 声音复刻音色
RESOURCE_IDS = {
    'seed-tts-2.0': '预置大模型音色（官方音色列表里的音色）',
    'seed-icl-2.0': '声音复刻音色（你自己克隆出来的音色）',
}

# get_voice 返回的 status（2/4 都能合成）
STATUS_TEXT = {0: '不存在', 1: '训练中', 2: '成功', 3: '训练失败', 4: '可用(Active)'}
LANG_TEXT = {0: '中文', 1: '英文', 2: '日语', 3: '西班牙语', 4: '印尼语', 5: '葡萄牙语',
             6: '德语', 7: '法语', 8: '韩语', 9: '意大利语', 10: '泰语', 11: '越南语',
             12: '俄语', 13: '菲律宾语', 14: '马来语', 15: '阿拉伯语', 16: '墨西哥西语',
             17: '巴西葡语', 19: '波兰语', 20: '土耳其语', 21: '瑞典语'}
# 代码里验证过的豆包默认音色（tts/doubao.py:39），用作页面占位提示
KNOWN_PRESET_VOICES = [
    {'ref_file': 'zh_female_vv_uranus_bigtts', 'label': '女声 · 通用（官方默认）'},
]


# ─── 响应助手（与 libs_routes 同风格）──────────────────────────────

def json_ok(data=None):
    body = {"code": 0, "msg": "ok"}
    if data is not None:
        body["data"] = data
    return web.Response(content_type="application/json", text=json.dumps(body, ensure_ascii=False))


def json_error(msg, code=-1):
    return web.Response(content_type="application/json",
                        text=json.dumps({"code": code, "msg": str(msg)}, ensure_ascii=False))


def _lib_dir(lib):
    return os.path.join(AVATARS_ROOT, lib)


def _safe_lib(lib):
    """库名合法性：禁止路径穿越（与 libs_routes 同口径）。"""
    if not lib or lib in ('.', '..'):
        return False
    if any(ch in lib for ch in ('/', '\\', ':', '*', '?', '"', '<', '>', '|')):
        return False
    return os.path.isdir(_lib_dir(lib))


# ─── 豆包 key 配置（存 data/tts_config.json，data/ 已被 .gitignore 忽略）──

def read_tts_config():
    if not os.path.isfile(TTS_CONFIG_PATH):
        return {}
    try:
        with open(TTS_CONFIG_PATH, 'r', encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        logger.warning(f"TTS 配置损坏，忽略：{TTS_CONFIG_PATH}")
        return {}


def _write_tts_config(patch):
    cur = read_tts_config()
    cur.update(patch or {})
    os.makedirs(os.path.dirname(TTS_CONFIG_PATH), exist_ok=True)
    tmp = TTS_CONFIG_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
    os.replace(tmp, TTS_CONFIG_PATH)
    # 立即生效：豆包插件在**每次建会话**时 os.getenv，所以不需要重启服务
    key = str(cur.get('doubao_api_key') or '').strip()
    if key:
        os.environ['DOUBAO_API_KEY'] = key
    # 给 start.bat 用的标记：有这个文件说明"Key 已配好"，双击启动就直接用豆包，
    # 否则回退 edgetts（保证有声音）。cmd 里解析 JSON 不可靠，所以用标记文件。
    flag = os.path.join(os.path.dirname(TTS_CONFIG_PATH) or '.', 'tts_key_ok.flag')
    try:
        if key:
            with open(flag, 'w', encoding='utf-8') as f:
                f.write('doubao key configured\n')
        elif os.path.isfile(flag):
            os.remove(flag)
    except Exception:
        logger.warning(f"写/删豆包 Key 标记文件失败：{flag}", exc_info=True)
    return cur


def _doubao_api_key():
    """优先用环境变量（可能由 .env / start.bat 提供），否则用页面保存的。"""
    return (os.getenv('DOUBAO_API_KEY') or read_tts_config().get('doubao_api_key') or '').strip()


def _mask(key):
    key = key or ''
    return ('*' * max(0, len(key) - 4) + key[-4:]) if len(key) > 4 else ('已配置' if key else '')


# ─── 素材链读写（合并式，绝不覆盖链本身）────────────────────────────

def _playlist_path(lib):
    return os.path.join(_lib_dir(lib), PLAYLIST_NAME)


def read_chain_voice(lib):
    p = _playlist_path(lib)
    if not os.path.isfile(p):
        return None, False
    try:
        with open(p, 'r', encoding='utf-8') as f:
            d = json.load(f)
    except Exception:
        logger.warning(f"playlist.json 损坏：{p}")
        return None, False
    v = d.get('voice') if isinstance(d, dict) else None
    return (v if isinstance(v, dict) else None), True


def write_chain_voice(lib, voice):
    """把 voice 合并进 playlist.json（保留 groups/segments/entry/mode 等字段）。"""
    p = _playlist_path(lib)
    cur = {}
    if os.path.isfile(p):
        try:
            with open(p, 'r', encoding='utf-8') as f:
                cur = json.load(f)
            if not isinstance(cur, dict):
                cur = {}
        except Exception:
            return None, f"playlist.json 解析失败，未写入：{p}"
    if voice:
        cur['voice'] = voice
    else:
        cur.pop('voice', None)
    tmp = p + '.tmp'
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return cur, None


# ─── 热套用：直接改活跃会话的 TTS（不断音频、不需重启）──────────────

def _apply_voice_to_live_sessions(lib, voice):
    """把音色套用到「avatar_id 等于该库」的活跃会话上。返回命中的 sessionid 列表。"""
    applied = []
    try:
        from server.session_manager import session_manager
        for sid, sess in list(session_manager.sessions.items()):
            if sess is None:
                continue
            opt = getattr(sess, 'opt', None)
            if opt is None or getattr(opt, 'avatar_id', None) != lib:
                continue
            try:
                sess._apply_tts_voice(voice or {})
                applied.append(sid)
            except Exception:
                logger.exception(f"会话 {sid[:8]} 套用音色失败")
    except Exception:
        logger.warning("热套用音色时读取会话表失败", exc_info=True)
    return applied


# ─── HTTP 处理 ────────────────────────────────────────────────────

async def api_tts_config_get(request):
    try:
        cfg = read_tts_config()
        key = _doubao_api_key()
        opt = request.app.get('opt')
        return json_ok(data={
            'engine': getattr(opt, 'tts', '') if opt else '',
            'ref_file': getattr(opt, 'REF_FILE', '') if opt else '',
            'cwd': os.getcwd(),                       # 诊断用：相对路径 ./data 的落点
            'avatars_root': os.path.abspath(AVATARS_ROOT),
            'doubao': {
                'has_key': bool(key),
                'key_masked': _mask(key),
                'key_from_env': bool(os.getenv('DOUBAO_API_KEY')),
                'resource_id': (cfg.get('doubao_resource_id')
                                or (getattr(opt, 'doubao_resource_id', '') if opt else '')
                                or 'seed-tts-2.0'),
            },
        })
    except Exception as e:
        logger.exception('api_tts_config_get:')
        return json_error(str(e))


async def api_tts_config_put(request):
    """body: {"doubao_api_key": "sk-...", "doubao_resource_id": "seed-icl-2.0"}
    只传 resource_id 也可以（不会覆盖已存的 key）。"""
    try:
        p = await request.json()
    except Exception:
        return json_error("请求体不是合法 JSON")
    patch = {}
    if p.get('doubao_api_key') is not None:
        patch['doubao_api_key'] = str(p.get('doubao_api_key') or '').strip()
    if p.get('doubao_resource_id') is not None:
        rid = str(p.get('doubao_resource_id') or '').strip()
        if rid not in RESOURCE_IDS:
            return json_error(f"resource_id 只能是 {' 或 '.join(RESOURCE_IDS)}")
        patch['doubao_resource_id'] = rid
    if not patch:
        return json_error("没有要保存的字段")
    cur = _write_tts_config(patch)
    key = str(cur.get('doubao_api_key') or os.getenv('DOUBAO_API_KEY') or '')
    logger.info("豆包 TTS 配置已保存：resource_id=%s, key=%s",
                cur.get('doubao_resource_id'), _mask(key))
    return json_ok(data={'has_key': bool(key.strip()), 'key_masked': _mask(key.strip()),
                         'resource_id': cur.get('doubao_resource_id') or 'seed-tts-2.0',
                         'note': '已生效（无需重启）：下一次建会话即用新 key'})


async def api_tts_voice_options(request):
    return json_ok(data={'resource_ids': [{'value': k, 'label': v} for k, v in RESOURCE_IDS.items()],
                         'preset_voices': KNOWN_PRESET_VOICES})


def _get_voice_sync(ref_file, custom_speaker_id=''):
    """同步查一个音色（在线程池里跑）。"""
    key = _doubao_api_key()
    if not key:
        return {'ok': False, 'error': 'DOUBAO_API_KEY 未配置：请在「音色」面板填入豆包 API Key'}
    body = {'speaker_id': custom_speaker_id and 'custom_speaker_id' or ref_file}
    if custom_speaker_id:
        body['custom_speaker_id'] = custom_speaker_id
    headers = {
        'Content-Type': 'application/json',
        'X-Api-Key': key,
        'X-Api-Request-Id': str(uuid.uuid4()),
    }
    try:
        resp = requests.post(DOUBAO_VOICE_URL, headers=headers, json=body, timeout=30)
    except Exception as e:
        return {'ok': False, 'error': f'请求火山接口失败：{type(e).__name__}: {e}'}
    logid = resp.headers.get('X-Tt-Logid', '')
    try:
        data = resp.json()
    except Exception:
        data = {}
    if resp.status_code != 200 or not isinstance(data, dict) or data.get('code') not in (0, None):
        return {'ok': False, 'http_status': resp.status_code, 'logid': logid,
                'error': (data.get('message') if isinstance(data, dict) else '') or resp.text[:300]}
    status = data.get('status')
    lang = data.get('language')
    speaker_status = data.get('speaker_status') or []
    demo = ''
    model_type = None
    if isinstance(speaker_status, list) and speaker_status and isinstance(speaker_status[0], dict):
        demo = speaker_status[0].get('demo_audio') or ''
        model_type = speaker_status[0].get('model_type')
    return {
        'ok': True,
        'ref_file': data.get('speaker_id') or ref_file,
        'status': status,
        'status_text': STATUS_TEXT.get(status, f'未知({status})'),
        'usable': status in (2, 4),
        'language': lang,
        'language_text': LANG_TEXT.get(lang, f'未知({lang})'),
        'model_type': model_type,
        'demo_audio': demo,
        'available_training_times': data.get('available_training_times'),
        'logid': logid,
    }


async def api_tts_voice_check(request):
    """body: {"ref_file": "S_xxx"} 或 {"custom_speaker_id": "custom_zh_xxx"}
    返回音色状态 + 官方试听音频（Success 时 demo_audio 有效 1 小时）。"""
    try:
        p = await request.json()
    except Exception:
        return json_error("请求体不是合法 JSON")
    ref = str(p.get('ref_file') or p.get('speaker_id') or '').strip()
    custom = str(p.get('custom_speaker_id') or '').strip()
    if not ref and not custom:
        return json_error("请先填音色ID（ref_file）")
    loop = asyncio.get_event_loop()
    out = await loop.run_in_executor(None, _get_voice_sync, ref or 'custom_speaker_id', custom)
    if not out.get('ok'):
        return json_error(out.get('error') or '音色查询失败')
    return json_ok(data=out)


async def api_chain_voice_get(request):
    try:
        lib = request.match_info['lib']
        if not _safe_lib(lib):
            return json_error(f"库「{lib}」不存在", code=404)
        voice, has_playlist = read_chain_voice(lib)
        cfg = read_tts_config()
        return json_ok(data={
            'lib': lib,
            'voice': voice,
            'has_playlist': has_playlist,
            'saved_resource_id': cfg.get('doubao_resource_id') or '',
            'has_key': bool(_doubao_api_key()),
        })
    except Exception as e:
        logger.exception('api_chain_voice_get:')
        return json_error(str(e))


async def api_chain_voice_put(request):
    """body: {"ref_file": "S_xxx", "resource_id": "seed-icl-2.0", "engine": "doubao"}
    传 {"ref_file": ""} 表示清除（回落启动参数默认）。"""
    try:
        lib = request.match_info['lib']
        if not _safe_lib(lib):
            return json_error(f"库「{lib}」不存在", code=404)
        try:
            p = await request.json()
        except Exception:
            return json_error("请求体不是合法 JSON")
        ref = str(p.get('ref_file') or p.get('voice') or '').strip()
        rid = str(p.get('resource_id') or '').strip()
        engine = str(p.get('engine') or 'doubao').strip()
        if rid and rid not in RESOURCE_IDS:
            return json_error(f"resource_id 只能是 {' 或 '.join(RESOURCE_IDS)}")
        voice = ({'engine': engine, 'ref_file': ref, 'resource_id': rid or 'seed-tts-2.0'}
                 if ref else None)
        _, err = write_chain_voice(lib, voice)
        if err:
            return json_error(err)
        applied = _apply_voice_to_live_sessions(lib, voice) if voice else []
        logger.info("素材链音色已绑定：库=%s, ref_file=%s, resource_id=%s, 热套用会话=%s",
                    lib, ref or '(清除)', rid or '-', applied)
        return json_ok(data={'voice': voice, 'applied_sessions': applied,
                             'note': '已保存到该素材链，且对活跃会话立即生效（无需重启）'})
    except Exception as e:
        logger.exception('api_chain_voice_put:')
        return json_error(str(e))


async def api_chain_voice_delete(request):
    try:
        lib = request.match_info['lib']
        if not _safe_lib(lib):
            return json_error(f"库「{lib}」不存在", code=404)
        _, err = write_chain_voice(lib, None)
        if err:
            return json_error(err)
        applied = _apply_voice_to_live_sessions(lib, {})
        logger.info("素材链音色已清除：库=%s（活跃会话 %s 保持当前音色，重建会话后回落默认）", lib, applied)
        return json_ok(data={'voice': None, 'applied_sessions': applied})
    except Exception as e:
        logger.exception('api_chain_voice_delete:')
        return json_error(str(e))


def setup_tts_routes(app):
    """注册音色路由（必须在 add_static('/', path='web') 之前调用）。"""
    app.router.add_get('/api/tts/config', api_tts_config_get)
    app.router.add_put('/api/tts/config', api_tts_config_put)
    app.router.add_post('/api/tts/config', api_tts_config_put)     # 兼容用 POST 的调用方
    app.router.add_get('/api/tts/voice_options', api_tts_voice_options)
    app.router.add_post('/api/tts/voice_check', api_tts_voice_check)
    app.router.add_get('/api/libs/{lib}/voice', api_chain_voice_get)
    app.router.add_put('/api/libs/{lib}/voice', api_chain_voice_put)
    app.router.add_post('/api/libs/{lib}/voice', api_chain_voice_put)
    app.router.add_delete('/api/libs/{lib}/voice', api_chain_voice_delete)
    logger.info("素材链音色路由已注册：/api/tts/* + /api/libs/{lib}/voice")
