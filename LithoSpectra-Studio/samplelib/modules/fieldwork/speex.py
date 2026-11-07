# -*- coding: utf-8 -*-
"""奥维录音（OSPX，内含 Speex 帧）转成 WAV，页面里才放得出来。

容器怎么拆见 :func:`ovobj.ospx_frames`；这里只管解码。解码用 libspeex（C 库，经 ctypes
调用），按下面的顺序找：

1. 系统或当前 Python 环境里的 libspeex（conda-forge 的 ``speex`` 包）；
2. 打包产物里随附的那份；
3. OpenCV（opencv-python）自带的那份——macOS 上它的视频模块带了 libspeex，装了 OpenCV
   就有。

都找不到时 :func:`available` 为假，录音照常入库，只是不能在窗口里播放。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import glob
import importlib.util
import io
import os
import sys
import wave
from typing import List, Optional

from . import ovobj

#: libspeex 的 ctl 请求号（speex.h）
_SET_ENH = 0
_GET_FRAME_SIZE = 3

_lib: Optional[ctypes.CDLL] = None
_tried = False


def _candidates() -> List[str]:
    out: List[str] = []
    found = ctypes.util.find_library("speex")
    if found:
        out.append(found)
    roots = [sys.prefix, getattr(sys, "_MEIPASS", "")]
    for root in filter(None, roots):
        for pat in ("lib/libspeex*.dylib", "lib/libspeex.so*", "Library/bin/libspeex*.dll", "Library/bin/speex*.dll",
                    "libspeex*.dylib", "libspeex.so*", "libspeex*.dll", "speex*.dll"):
            out += sorted(glob.glob(os.path.join(root, pat)))
    spec = importlib.util.find_spec("cv2")
    if spec and spec.origin:
        base = os.path.dirname(spec.origin)
        for sub in (".dylibs", "__dot__dylibs", "../opencv_python.libs"):
            out += sorted(glob.glob(os.path.join(base, sub, "libspeex*")))
    return out


def _load() -> Optional[ctypes.CDLL]:
    global _lib, _tried
    if _tried:
        return _lib
    _tried = True
    for path in _candidates():
        try:
            lib = ctypes.CDLL(path)
            lib.speex_lib_get_mode.restype = ctypes.c_void_p
            lib.speex_lib_get_mode.argtypes = [ctypes.c_int]
            lib.speex_decoder_init.restype = ctypes.c_void_p
            lib.speex_decoder_init.argtypes = [ctypes.c_void_p]
            lib.speex_decoder_destroy.argtypes = [ctypes.c_void_p]
            lib.speex_decoder_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
            lib.speex_decode_int.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
            lib.speex_bits_init.argtypes = [ctypes.c_void_p]
            lib.speex_bits_destroy.argtypes = [ctypes.c_void_p]
            lib.speex_bits_read_from.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        except (OSError, AttributeError):
            continue
        _lib = lib
        break
    return _lib


def available() -> bool:
    return _load() is not None


def is_ospx(data: bytes) -> bool:
    return data[:4] == b"OSPX"


def ospx_to_wav(data: bytes) -> bytes:
    """奥维录音 → WAV（PCM 16 位，采样率、声道照录音原样）。"""
    lib = _load()
    if lib is None:
        raise RuntimeError("找不到 Speex 解码库（libspeex），无法转换奥维录音")
    info, frames = ovobj.ospx_frames(data)
    if info.get("bits") != 16 or info.get("channels") != 1:
        raise ovobj.OvobjError("奥维录音的格式未见过：%s" % info)
    rate = info["rate"]
    mode_id = 0 if rate <= 8000 else 1 if rate <= 16000 else 2   # 窄带、宽带、超宽带
    state = lib.speex_decoder_init(lib.speex_lib_get_mode(mode_id))
    if not state:
        raise RuntimeError("Speex 解码器初始化失败")
    bits = ctypes.create_string_buffer(256)          # SpeexBits 结构，留足余量
    lib.speex_bits_init(bits)
    try:
        size = ctypes.c_int(0)
        lib.speex_decoder_ctl(state, _GET_FRAME_SIZE, ctypes.byref(size))
        enh = ctypes.c_int(1)
        lib.speex_decoder_ctl(state, _SET_ENH, ctypes.byref(enh))
        n = size.value or 160
        want = info.get("pcm_bytes") or 0
        # 一包里装几帧：按解码后应有的长度推（奥维一包一帧）
        per_packet = max(1, round(want / 2 / n / len(frames))) if want else 1
        out = (ctypes.c_short * n)()
        pcm = bytearray()
        for fr in frames:
            lib.speex_bits_read_from(bits, fr, len(fr))
            for _ in range(per_packet):
                if lib.speex_decode_int(state, bits, out) != 0:
                    break
                pcm += bytes(out)
    finally:
        lib.speex_bits_destroy(bits)
        lib.speex_decoder_destroy(state)
    if want:
        pcm = pcm[:want]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(pcm))
    return buf.getvalue()
