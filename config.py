###############################################################################
#  配置解析 — CLI 参数 + YAML 配置
###############################################################################

import argparse
import json
import os
import sys

try:
    import yaml
    _has_yaml = True
except ImportError:
    _has_yaml = False


def str_or_int(value):
    """尝试转换为 int，失败则返回 str"""
    try:
        return int(value)
    except ValueError:
        return value


def _yaml_to_args(yaml_cfg):
    """将 YAML 字典中的 key 转换为 argparse 兼容的 `--key` 形式。

    argparse 的 dest 默认规则：`--model` → `model`，`--push-url` → `push_url`。
    此函数同时支持两种 key 写法：
      - model / batch_size          → 直接透传
      - model-name / batch-size    → 转换为 model_name / batch_size
    """
    result = {}
    for k, v in yaml_cfg.items():
        dest = k.replace('-', '_')
        result[dest] = v
    return result


def parse_args():
    """解析命令行参数，支持 YAML 配置文件覆盖默认值。

    优先级：CLI 参数 > YAML 配置文件 > add_argument(default=...)
    """
    parser = argparse.ArgumentParser(description="LiveTalking Digital Human Server")

    # ─── 配置文件 ──────────────────────────────────────────────────────
    parser.add_argument('--config', '-c', type=str, default='config.yaml',
                        help='YAML 配置文件路径（设为空字符串可跳过）')

    # ─── 音频 ──────────────────────────────────────────────────────────
    parser.add_argument('--fps', type=int, default=25, help="video fps, must be 25")
    parser.add_argument('-l', type=int, default=10)
    parser.add_argument('-m', type=int, default=8)
    parser.add_argument('-r', type=int, default=10)

    # ─── 画面 ──────────────────────────────────────────────────────────
    # parser.add_argument('--W', type=int, default=450, help="GUI width")
    # parser.add_argument('--H', type=int, default=450, help="GUI height")

    # ─── 数字人模型 ────────────────────────────────────────────────────
    parser.add_argument('--model', type=str, default='wav2lip',
                        help="avatar model: musetalk/wav2lip/ultralight")
    parser.add_argument('--avatar_id', type=str, default='wav2lip256_avatar1',
                        help="avatar id in data/avatars")
    parser.add_argument('--batch_size', type=int, default=16, help="infer batch")
    parser.add_argument('--modelres', type=int, default=192)
    parser.add_argument('--modelfile', type=str, default='')

    # ─── 自定义动作和多形象 ────────────────────────────────────────────
    parser.add_argument('--customvideo_config', type=str, default='',
                        help="custom action json")

    # ─── TTS ───────────────────────────────────────────────────────────
    parser.add_argument('--tts', type=str, default='edgetts',
                        help="tts plugin: edgetts/gpt-sovits/cosyvoice/fishtts/tencent/doubao/indextts2/azuretts/qwentts")
    parser.add_argument('--REF_FILE', type=str, default="zh-CN-YunxiaNeural",
                        help="参考文件名或语音模型ID")
    parser.add_argument('--REF_TEXT', type=str, default=None)
    parser.add_argument('--TTS_SERVER', type=str, default='http://127.0.0.1:9880')
    # 豆包(火山引擎)专用：X-Api-Resource-Id
    #   seed-tts-2.0 = 预置「大模型音色」（默认）
    #   seed-icl-2.0 = 你自己「声音复刻」出来的音色（配合 --REF_FILE <复刻音色ID>）
    # 注：素材链（playlist.json）里绑定的音色会覆盖此处的默认值，且不用重启。
    parser.add_argument('--doubao_resource_id', type=str, default='seed-tts-2.0',
                        help="doubao TTS resource id: seed-tts-2.0(预置音色) / seed-icl-2.0(声音复刻)")

    # ─── LLM ──────────────────────────────────────────────────────────
    parser.add_argument('--llm_provider', type=str, default='dashscope',
                        help="llm provider: dashscope/orcarouter")
    parser.add_argument('--llm_model', type=str, default='',
                        help="llm model override, empty = provider default (qwen-plus / orcarouter/auto)")

    # ─── 传输 ─────────────────────────────────────────────────────────
    parser.add_argument('--transport', type=str, default='webrtc',
                        help="output: rtcpush/webrtc/rtmp/virtualcam")
    parser.add_argument('--stun', type=str, default='stun:stun.freeswitch.org:3478',
                        help="stun server url")
    parser.add_argument('--push_url', type=str,
                        default='http://localhost:1985/rtc/v1/whip/?app=live&stream=livestream')
    parser.add_argument('--max_session', type=int, default=5)
    parser.add_argument('--listenport', type=int, default=8010,
                        help="web listen port")

    # ─── 虚拟摄像头 ───────────────────────────────────────────────────
    parser.add_argument('--audio_output_device', type=int, default=None,
                        help="音频输出设备索引（None=系统默认，仅用于 --transport=virtualcam）。使用 python list_audio_devices.py 查看所有设备")

    # ─── 加载 YAML 配置文件 ────────────────────────────────────────────
    if _has_yaml:
        # 先用 parser 的已知参数做一次临时解析，只拿 --config 的值
        tmp_opt, _ = parser.parse_known_args()
        config_path = tmp_opt.config
        if config_path and os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                yaml_cfg = yaml.safe_load(f)
            if yaml_cfg and isinstance(yaml_cfg, dict):
                yaml_defaults = _yaml_to_args(yaml_cfg)
                parser.set_defaults(**yaml_defaults)
    else:
        print("[config] PyYAML 未安装，跳过 YAML 配置文件加载。"
              "安装: pip install pyyaml")

    # ─── 正式解析 CLI 参数 ─────────────────────────────────────────────
    opt = parser.parse_args()

    # ─── 后处理 ────────────────────────────────────────────────────────
    # 选了豆包但没指定音色时：把 edgetts 的默认值（zh-CN-YunxiaNeural）换成豆包自己的默认音色。
    # 原因：start.bat 现在默认 --tts doubao，若不换，就会把 edge 的音色名发给豆包 →
    # 语音合成报错 → 表现为"没有声音"。
    # 素材链里绑定的音色（<库>/playlist.json 的 voice 字段）会再覆盖这里，
    # 见 avatars/base_avatar.py::_apply_tts_voice()。
    if getattr(opt, 'tts', '') == 'doubao' and (not opt.REF_FILE
                                               or opt.REF_FILE == 'zh-CN-YunxiaNeural'):
        opt.REF_FILE = 'zh_female_vv_uranus_bigtts'

    # ─── 让页面里的豆包配置成为唯一真源（start.bat 不再写死 resource_id）───
    # 页面（素材页「音色」/ 运营后台「系统配置」）保存到 data/tts_config.json。
    # 这里在启动时兜底读取：命令行显式传的参数优先，没传就用文件里的值。
    # 否则会出现「页面上选了预置音色，start.bat 却仍按复刻资源发请求」→
    # 火山报 500/55000000 resource ID is mismatched → 表现为没有声音。
    if getattr(opt, 'tts', '') == 'doubao':
        try:
            _cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     'data', 'tts_config.json')
            if not os.path.exists(_cfg_path):
                _cfg_path = os.path.join('data', 'tts_config.json')
            if os.path.exists(_cfg_path):
                with open(_cfg_path, 'r', encoding='utf-8') as _f:
                    _tcfg = json.load(_f) or {}
                if not os.environ.get('DOUBAO_API_KEY'):
                    _k = str(_tcfg.get('doubao_api_key') or '').strip()
                    if _k:
                        os.environ['DOUBAO_API_KEY'] = _k
                        print('[config] 已从 data/tts_config.json 载入豆包 API Key')
                if '--doubao_resource_id' not in sys.argv:
                    _rid = str(_tcfg.get('doubao_resource_id') or '').strip()
                    if _rid:
                        opt.doubao_resource_id = _rid
                        print('[config] 豆包 resource_id = %s（来自 data/tts_config.json）' % _rid)
        except Exception as _e:
            print('[config] 读取 data/tts_config.json 失败（忽略）:', _e)

    opt.customopt = []
    if opt.customvideo_config:
        with open(opt.customvideo_config, 'r') as f:
            opt.customopt = json.load(f)

    return opt
