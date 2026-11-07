# -*- coding: utf-8 -*-
"""录音转文字：野外测点时口述的录音转成文字，方便追加到点位备注。

识别用 SenseVoice（阿里 FunAudioLLM 开源，中英日韩粤，自带标点），经 sherpa-onnx 在本机
离线运行，不联网。模型约 240 MB，不随包分发，第一次用时下载到 ``<数据目录>/data/models``，
国内先走魔搭（ModelScope），再试 Hugging Face 镜像与原站，下载后按 SHA-256 核对。

录音先解成 16 kHz 单声道浮点：

* WAV 直接读；
* 奥维录音（OSPX，内含 Speex）经 :mod:`speex` 解成 WAV；
* 其他格式（m4a、mp3、aac、caf、amr……）在 macOS 上用系统自带的 ``afconvert`` 转成 WAV。

SenseVoice 一次只适合处理几十秒，长录音在停顿处切成不超过 :data:`SEGMENT_SECONDS` 的段
分别识别，再拼起来。
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
import wave
from typing import Callable, List, Optional

import numpy as np

from ... import config
from . import speex

# =============================================================================
# 配置
# =============================================================================

MODEL_NAME = "sensevoice-zh-en-ja-ko-yue-2024-07-17"
#: (文件名, 字节数, SHA-256)
MODEL_FILES = [
    ("tokens.txt", 315894, "f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc"),
    ("model.int8.onnx", 239233841, "c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51"),
]
#: 依次尝试的下载地址；{file} 换成文件名
MODEL_URLS = [
    "https://modelscope.cn/models/pengzhendong/sherpa-onnx-sense-voice-zh-en-ja-ko-yue/resolve/master/{file}",
    "https://hf-mirror.com/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/{file}",
    "https://huggingface.co/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/{file}",
]
RATE = 16000
#: 长录音切段的上限（秒）；切点取这附近最安静的地方
SEGMENT_SECONDS = 25
#: 识别时用的线程数
THREADS = 2

_recognizer = None
_lock = threading.Lock()


def model_dir() -> str:
    return os.path.join(config.app_data_dir(), "data", "models", MODEL_NAME)


def model_ready() -> bool:
    return all(os.path.isfile(os.path.join(model_dir(), name)) and os.path.getsize(os.path.join(model_dir(), name)) == size
               for name, size, _ in MODEL_FILES)


def engine_available() -> bool:
    try:
        import sherpa_onnx  # noqa: F401
    except ImportError:
        return False
    return True


def status() -> dict:
    return {"engine": engine_available(), "model": model_ready(), "model_path": model_dir(),
            "model_size": sum(size for _, size, _ in MODEL_FILES)}


def download_model(progress: Callable[[int, int, str], bool] = lambda *a: True) -> str:
    """下载识别模型并逐个核对 SHA-256，返回模型目录。progress 返回 False 时中止。"""
    folder = model_dir()
    os.makedirs(folder, exist_ok=True)
    total = sum(size for _, size, _ in MODEL_FILES)
    base = 0
    for name, size, sha in MODEL_FILES:
        target = os.path.join(folder, name)
        if os.path.isfile(target) and os.path.getsize(target) == size:
            base += size
            continue
        part = target + ".part"
        errors = []
        for tpl in MODEL_URLS:
            url = tpl.format(file=name)
            try:
                h = hashlib.sha256()
                with urllib.request.urlopen(url, timeout=30) as resp, open(part, "wb") as fh:
                    done = 0
                    while True:
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        fh.write(chunk)
                        h.update(chunk)
                        done += len(chunk)
                        if not progress(base + done, total, "下载语音识别模型 %.0f / %.0f MB" % ((base + done) / 1e6, total / 1e6)):
                            raise InterruptedError("已取消")
                if h.hexdigest() != sha:
                    raise IOError("校验不符（%s）" % h.hexdigest()[:12])
                os.replace(part, target)
                break
            except InterruptedError:
                if os.path.exists(part):
                    os.remove(part)
                raise
            except Exception as exc:
                errors.append("%s：%s" % (url.split("/")[2], exc))
                if os.path.exists(part):
                    os.remove(part)
        else:
            raise IOError("语音识别模型下载失败：" + "；".join(errors))
        base += size
    return folder


def _recognizer_get():
    global _recognizer
    with _lock:
        if _recognizer is None:
            if not engine_available():
                raise RuntimeError("没有安装语音识别引擎（sherpa-onnx）")
            if not model_ready():
                raise RuntimeError("还没有下载语音识别模型")
            import sherpa_onnx
            folder = model_dir()
            _recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=os.path.join(folder, "model.int8.onnx"), tokens=os.path.join(folder, "tokens.txt"),
                num_threads=THREADS, use_itn=True, language="auto", debug=False)
        return _recognizer


# =============================================================================
# 读录音
# =============================================================================

def _wav_samples(data: bytes) -> np.ndarray:
    """WAV（PCM 8 / 16 / 32 位）→ 16 kHz 单声道 float32。"""
    with wave.open(io.BytesIO(data)) as w:
        ch, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if width == 2:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
    elif width == 4:
        x = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648
    elif width == 1:
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128) / 128
    else:
        raise ValueError("不支持 %d 位的 WAV" % (width * 8))
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return _resample(x, rate)


def _resample(x: np.ndarray, rate: int) -> np.ndarray:
    if rate == RATE or not len(x):
        return x.astype(np.float32)
    n = int(round(len(x) * RATE / rate))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


def can_read(ext: str) -> bool:
    """这种录音能不能转文字（不实际读）。"""
    ext = (ext or "").lower()
    if ext == "wav":
        return True
    if ext == "spx":
        return speex.available()
    return sys.platform == "darwin" and shutil.which("afconvert") is not None


def load_audio(path: str) -> np.ndarray:
    """录音文件 → 16 kHz 单声道 float32。"""
    with open(path, "rb") as fh:
        data = fh.read()
    if speex.is_ospx(data):
        return _wav_samples(speex.ospx_to_wav(data))
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        try:
            return _wav_samples(data)
        except (wave.Error, ValueError):
            pass                       # 压缩编码的 WAV，交给系统转换
    if sys.platform == "darwin" and shutil.which("afconvert"):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "a.wav")
            r = subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@%d" % RATE, "-c", "1", path, out],
                               capture_output=True, timeout=300)
            if r.returncode != 0 or not os.path.isfile(out):
                raise ValueError("无法读取这段录音：%s" % (r.stderr.decode("utf-8", "replace").strip() or "格式不支持"))
            with open(out, "rb") as fh:
                return _wav_samples(fh.read())
    raise ValueError("无法读取这种格式的录音，目前支持 WAV 与奥维录音")


# =============================================================================
# 识别
# =============================================================================

def _cuts(x: np.ndarray) -> List[int]:
    """长录音的切点：每隔约 SEGMENT_SECONDS 秒，取前后几秒里最安静的地方。"""
    seg = SEGMENT_SECONDS * RATE
    if len(x) <= seg:
        return [0, len(x)]
    hop = RATE // 50                                   # 20 ms 一格算能量
    frames = len(x) // hop                             # 分格求均方，长录音也只过一遍数据
    energy = np.sqrt((x[:frames * hop].reshape(frames, hop) ** 2).mean(axis=1))
    cuts, pos = [0], 0
    while len(x) - pos > seg:
        lo = (pos + seg - 5 * RATE) // hop
        hi = (pos + seg) // hop
        best = lo + int(np.argmin(energy[lo:hi])) if hi > lo else hi
        pos = max(pos + RATE, best * hop)
        cuts.append(pos)
    cuts.append(len(x))
    return cuts


def transcribe(samples: np.ndarray, progress: Callable[[int, int, str], bool] = lambda *a: True) -> str:
    """16 kHz 单声道录音 → 文字。"""
    rec = _recognizer_get()
    cuts = _cuts(samples)
    parts = []
    for i in range(len(cuts) - 1):
        if not progress(i, len(cuts) - 1, "识别第 %d / %d 段" % (i + 1, len(cuts) - 1)):
            raise InterruptedError("已取消")
        piece = samples[cuts[i]:cuts[i + 1]]
        if len(piece) < RATE // 5 or float(np.abs(piece).max()) < 1e-4:
            continue                                  # 太短或全是静音
        stream = rec.create_stream()
        stream.accept_waveform(RATE, piece)
        rec.decode_stream(stream)
        text = stream.result.text.strip()
        if text:
            parts.append(text)
    return "".join(parts)


def transcribe_file(path: str, progress: Callable[[int, int, str], bool] = lambda *a: True) -> str:
    return transcribe(load_audio(path), progress)


# =============================================================================
# 点位上的录音
# =============================================================================

def note_line(text: str, taken: Optional[str]) -> str:
    """追加到备注里的一行：「［录音 10:45］……」。"""
    hhmm = (taken or "")[11:16]
    return "［录音%s］%s" % (" " + hhmm if hhmm else "", text)


def transcribe_attachments(fs, uids: List[str], append: bool = False,
                           progress: Callable[[int, int, str], bool] = lambda *a: True) -> dict:
    """逐段转写录音附件，文字记在附件上；``append`` 时追加到所属点位的备注末尾（同样的文字
    已经在备注里就不再追加）。返回每段的结果与失败原因。"""
    rep = {"done": [], "failed": [], "appended": 0, "cancelled": False}
    for i, uid in enumerate(uids):
        a = fs.get_attachment(uid)
        name = (a or {}).get("orig_name") or uid
        if not progress(i, len(uids), "转写 %s" % name):
            rep["cancelled"] = True
            break
        if a is None or a["kind"] != "audio":
            rep["failed"].append({"uid": uid, "name": name, "reason": "不是录音"})
            continue
        try:
            def sub(d, t, x="", _i=i):
                return progress(_i, len(uids), "转写 %s：%s" % (name, x))
            text = transcribe_file(fs.attachment_wav(a) if a["transcode"] == "wav" else a["path"], sub)
        except InterruptedError:
            rep["cancelled"] = True
            break
        except Exception as exc:                       # noqa: BLE001（一段失败不影响其余的）
            rep["failed"].append({"uid": uid, "name": name, "reason": str(exc)})
            continue
        fs.update_attachment(uid, {"transcript": text})
        item = {"uid": uid, "name": name, "text": text, "point_uid": a["point_uid"], "appended": False}
        if append and text and a["point_uid"]:
            pt = fs.get_point(a["point_uid"])
            note = (pt or {}).get("note") or ""
            if pt is not None and text not in note:
                line = note_line(text, a["taken"])
                fs.update_point(a["point_uid"], {"note": (note.rstrip() + "\n" + line) if note.strip() else line})
                item["appended"] = True
                rep["appended"] += 1
        rep["done"].append(item)
    progress(len(uids), len(uids), "")
    return rep
