import os
import base64
import time
import json
import uuid
import numpy as np
import resampy
import requests
from typing import Iterator

from utils.logger import logger
from .base_tts import BaseTTS, State
from registry import register

# 火山引擎豆包语音合成 - HTTP Chunked 单向流式合成接口
# 参考: https://docs.volcengine.com/docs/6561/2528925
URL = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
END_CODE = 20000000  # 服务端流式结束标记


@register("tts", "doubao")
class DoubaoTTS(BaseTTS):
    """豆包语音合成 (火山引擎) — 基于 HTTP Chunked 协议的单向流式合成。

    一次性输入文本，流式返回音频，支持中、英、日、西等多语种及多种方言口音。

    需要设置环境变量 DOUBAO_API_KEY (火山引擎控制台 > API Key 管理)。
    REF_FILE 用作音色ID (speaker)，例如 zh_female_vv_uranus_bigtts。
    """

    def __init__(self, opt, parent):
        super().__init__(opt, parent)

        # API Key 从控制台 > API Key 管理 获取
        self.api_key = os.getenv("DOUBAO_API_KEY")
        # X-Api-Resource-Id: seed-tts-2.0 (大模型2.0音色) / seed-icl-2.0 (声音复刻音色)
        self.resource_id = getattr(opt, "doubao_resource_id", "seed-tts-2.0")
        # 音色ID, 复用 REF_FILE 参数
        self.voice = opt.REF_FILE or "zh_female_vv_uranus_bigtts" 

        # 音频参数: format 支持 mp3 / pcm / ogg_opus / wav, 流式推荐 pcm
        self.audio_format = getattr(opt, "doubao_audio_format", "pcm")
        self.src_sr = int(getattr(opt, "doubao_sample_rate", self.sample_rate))

        # 复用连接 (Connection: keep-alive)
        self.session = requests.Session()

        if not self.api_key:
            logger.warning("DoubaoTTS: DOUBAO_API_KEY 未设置，请设置环境变量")

        logger.info(
            f"DoubaoTTS init: resource_id={self.resource_id}, voice={self.voice}, "
            f"format={self.audio_format}, sample_rate={self.src_sr}"
        )

    def txt_to_audio(self, msg: tuple[str, dict]):
        text, textevent = msg

        # 每条消息可覆盖音色 / 模型版本
        tts_cfg = textevent.get("tts", {})
        speaker = tts_cfg.get("ref_file", self.voice)
        resource_id = tts_cfg.get("resource_id", self.resource_id)
        # 语速：由 LLM 的语气标签决定（-50~100，0=正常）；没传就是豆包默认
        self._pending_rate = tts_cfg.get("speech_rate")

        self.stream_tts(
            self._synthesize(text=text, speaker=speaker, resource_id=resource_id),
            msg,
        )

    # ── API 调用 ──────────────────────────────────────────────

    def _synthesize(self, text: str, speaker: str, resource_id: str) -> Iterator[bytes]:
        """调用单向流式接口，逐段 yield 解码后的音频字节。"""
        headers = {
            "X-Api-Key": self.api_key,
            "X-Api-Resource-Id": resource_id,
            "X-Api-Request-Id": str(uuid.uuid4()),
            "Content-Type": "application/json",
            "Connection": "keep-alive",
            "X-Control-Require-Usage-Tokens-Return": "*",
        }

        audio_params = {
            "format": self.audio_format,
            "sample_rate": self.src_sr,
        }
        # 语速：来自 LLM 的语气标签（豆包 -50~100，负数更慢、正数更快）
        rate = getattr(self, "_pending_rate", None)
        if rate:
            audio_params["speech_rate"] = int(rate)
        payload = {
            "req_params": {
                "text": text,
                "speaker": speaker,
                "audio_params": audio_params,
            }
        }

        start = time.perf_counter()
        # 原来打 text[:60]：用户连点 7 次会被合并成一条长文本 → 日志超长刷屏。只留摘要。
        _txt = text.replace('\n', ' ')
        logger.info("DoubaoTTS POST speaker=%s text_len=%d text=%s", speaker, len(text),
                    _txt[:24] + ('...' if len(_txt) > 24 else ''))

        try:
            with self.session.post(
                URL, headers=headers, json=payload, stream=True, timeout=120
            ) as res:
                if res.status_code != 200:
                    logger.error("DoubaoTTS server error: %s", res.text)
                    return

                first = True
                for line in res.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        logger.error("DoubaoTTS 无法解析响应行: %s", line)
                        continue

                    code = data.get("code", 0)
                    if code == 0:
                        b64 = data.get("data")
                        if not b64:
                            continue
                        if first:
                            end = time.perf_counter()
                            logger.info(f"doubao tts Time to first chunk: {end - start}s")
                            first = False
                        if self.state == State.RUNNING:
                            yield base64.b64decode(b64)
                    elif code == END_CODE:
                        break
                    else:
                        logger.error("DoubaoTTS error response: %s", data)
                        break

        except requests.exceptions.Timeout:
            logger.error("DoubaoTTS request timeout")
        except Exception:
            logger.exception("DoubaoTTS synthesize error")

    # ── stream → frames (同 omnitts / fish 模式) ──────────────

    def stream_tts(self, audio_stream: Iterator[bytes], msg: tuple[str, dict]):
        text, textevent = msg
        first = True
        last_stream = np.array([], dtype=np.float32)

        for chunk in audio_stream:
            if not chunk or len(chunk) == 0:
                continue

            # Raw PCM 16-bit → float32
            stream = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32767

            # 采样率不一致时重采样到 16kHz
            if self.src_sr != self.sample_rate:
                stream = resampy.resample(
                    x=stream, sr_orig=self.src_sr, sr_new=self.sample_rate
                )

            # 拼接上次剩余
            if last_stream.shape[0] > 0:
                stream = np.concatenate([last_stream, stream])

            total = stream.shape[0]
            idx = 0
            while total - idx >= self.chunk and self.state == State.RUNNING:
                eventpoint = {}
                if first:
                    eventpoint = {"status": "start", "text": text}
                    first = False
                eventpoint.update(**textevent)
                self.parent.put_audio_frame(
                    stream[idx : idx + self.chunk], eventpoint
                )
                idx += self.chunk

            last_stream = stream[idx:]  # 不足一 chunk 的留到下次

        # ── 发送结束标记 ──
        eventpoint = {"status": "end", "text": text}
        eventpoint.update(**textevent)
        self.parent.put_audio_frame(
            np.zeros(self.chunk, dtype=np.float32), eventpoint
        )

    def stop_tts(self):
        self.session.close()
        logger.info("DoubaoTTS session closed")
