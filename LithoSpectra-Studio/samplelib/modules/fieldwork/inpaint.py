# -*- coding: utf-8 -*-
"""按掩膜修补图像：LaMa（ONNX）为主，OpenCV 兜底。

LaMa 是 Samsung 开源的大面积修补模型（Apache-2.0），这里用 Carve 导出的 ONNX 版，
输入固定 512×512。整张照片缩到 512 再修，修出来的那块放回原图会糊成一片，和周围
清晰的岩石纹理对不上；所以只在掩膜附近开 512 的窗口，按原分辨率（掩膜太大时
适度缩小）逐窗修补，只把掩膜内的像素换掉，窗口外一个像素都不动。

模型文件约 200 MB，不随包分发，第一次用时下载到 ``<数据目录>/data/models``。没有
模型、没装 onnxruntime 时自动退回 OpenCV 的 Telea 算法，能用但纹理会被抹平。
"""

from __future__ import annotations

import hashlib
import math
import os
import threading
import urllib.request
from typing import Callable, List, Optional, Tuple

import numpy as np
from PIL import Image, ImageFilter

from ... import config

# =============================================================================
# 配置
# =============================================================================

MODEL_FILE = "lama_fp32.onnx"
MODEL_SHA256 = "1faef5301d78db7dda502fe59966957ec4b79dd64e16f03ed96913c7a4eb68d6"
MODEL_SIZE = 208044816
#: 依次尝试的下载地址；huggingface.co 在国内常连不上，镜像放前面
MODEL_URLS = [
    "https://hf-mirror.com/Carve/LaMa-ONNX/resolve/main/lama_fp32.onnx",
    "https://huggingface.co/Carve/LaMa-ONNX/resolve/main/lama_fp32.onnx",
]
WINDOW = 512
#: 掩膜在窗口里最多占的比例，剩下的是给模型看的上下文
MASK_SHARE = 0.55
#: 掩膜边缘羽化半径（像素），让修补块与原图过渡自然
FEATHER = 2

_session = None
_session_lock = threading.Lock()


def model_dir() -> str:
    return os.path.join(config.app_data_dir(), "data", "models")


def model_path() -> str:
    return os.path.join(model_dir(), MODEL_FILE)


def lama_available() -> bool:
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return os.path.isfile(model_path())


def status() -> dict:
    try:
        import onnxruntime  # noqa: F401
        ort = True
    except ImportError:
        ort = False
    try:
        import cv2  # noqa: F401
        cv = True
    except ImportError:
        cv = False
    return {"onnxruntime": ort, "opencv": cv, "model": os.path.isfile(model_path()),
            "model_path": model_path(), "model_size": MODEL_SIZE}


def download_model(progress: Callable[[int, int, str], bool] = lambda *a: True) -> str:
    """下载模型并核对 SHA-256，返回模型路径。progress 返回 False 时中止。"""
    os.makedirs(model_dir(), exist_ok=True)
    target = model_path()
    part = target + ".part"
    errors = []
    for url in MODEL_URLS:
        try:
            h = hashlib.sha256()
            with urllib.request.urlopen(url, timeout=30) as resp, open(part, "wb") as fh:
                total = int(resp.headers.get("Content-Length") or MODEL_SIZE)
                done = 0
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    fh.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    if not progress(done, total, "下载 LaMa 模型 %.0f / %.0f MB" % (done / 1e6, total / 1e6)):
                        raise InterruptedError("已取消")
            if h.hexdigest() != MODEL_SHA256:
                raise IOError("校验不符（%s）" % h.hexdigest()[:12])
            os.replace(part, target)
            return target
        except InterruptedError:
            if os.path.exists(part):
                os.remove(part)
            raise
        except Exception as exc:
            errors.append("%s：%s" % (url.split("/")[2], exc))
            if os.path.exists(part):
                os.remove(part)
    raise IOError("LaMa 模型下载失败：" + "；".join(errors))


def _lama():
    global _session
    with _session_lock:
        if _session is None:
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.log_severity_level = 3
            _session = ort.InferenceSession(model_path(), opts, providers=["CPUExecutionProvider"])
        return _session


# =============================================================================
# 修补
# =============================================================================

def _lama_window(img: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """img: HxWx3 uint8（512x512），mask: HxW bool。返回修好的 uint8 图。"""
    sess = _lama()
    x = img.astype(np.float32).transpose(2, 0, 1)[None] / 255.0
    m = mask.astype(np.float32)[None, None]
    names = [i.name for i in sess.get_inputs()]
    out = sess.run(None, {names[0]: x, names[1]: m})[0][0]
    out = out.transpose(1, 2, 0)
    if out.max() <= 1.5:                  # 有的导出版本输出 0..1
        out = out * 255.0
    return np.clip(out, 0, 255).astype(np.uint8)


def _cv_window(img: np.ndarray, mask: np.ndarray) -> np.ndarray:
    import cv2
    radius = max(3, int(round(min(img.shape[:2]) * 0.01)))
    return cv2.inpaint(img, mask.astype(np.uint8) * 255, radius, cv2.INPAINT_TELEA)[..., :3]


def _bbox(mask: np.ndarray) -> Optional[Tuple[int, int, int, int]]:
    ys, xs = np.nonzero(mask)
    if ys.size == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def _windows(box: Tuple[int, int, int, int], size: int, w: int, h: int) -> List[Tuple[int, int, int, int]]:
    """覆盖掩膜外接框的一串正方形窗口（原图坐标），掩膜大约占窗口中部。"""
    x0, y0, x1, y1 = box
    core = max(1, int(size * MASK_SHARE))
    out = []
    ny = max(1, math.ceil((y1 - y0) / core))
    nx = max(1, math.ceil((x1 - x0) / core))
    for j in range(ny):
        for i in range(nx):
            cx0 = x0 + i * core
            cy0 = y0 + j * core
            cx1 = min(x1, cx0 + core)
            cy1 = min(y1, cy0 + core)
            mx = (cx0 + cx1) // 2 - size // 2
            my = (cy0 + cy1) // 2 - size // 2
            mx = max(0, min(mx, w - size))
            my = max(0, min(my, h - size))
            out.append((mx, my, mx + size, my + size))
    return out


def inpaint(image: Image.Image, mask: np.ndarray, engine: str = "auto",
            progress: Callable[[int, int, str], bool] = lambda *a: True) -> Tuple[Image.Image, str]:
    """修补 ``mask``（与图同尺寸的 bool 数组，真为要抹掉的像素）。返回 (新图, 实际用的引擎)。"""
    if engine == "auto":
        engine = "lama" if lama_available() else "opencv"
    rgb = np.asarray(image.convert("RGB")).copy()
    h, w = rgb.shape[:2]
    mask = mask.astype(bool)
    box = _bbox(mask)
    if box is None:
        return image.copy(), engine
    # 掩膜太高时整体缩小再修，保证一个窗口能放下掩膜高度并留出上下文
    span = max(box[3] - box[1], 1)
    scale = min(1.0, WINDOW * MASK_SHARE / span) if engine == "lama" else 1.0
    if engine == "lama" and min(w, h) * scale < WINDOW:
        scale = max(scale, WINDOW / min(w, h))
    work = rgb
    wmask = mask.copy()
    if scale < 0.999:
        sw, sh = max(WINDOW, int(round(w * scale))), max(WINDOW, int(round(h * scale)))
        work = np.asarray(Image.fromarray(rgb).resize((sw, sh), Image.LANCZOS)).copy()
        wmask = np.asarray(Image.fromarray(mask.astype(np.uint8) * 255).resize((sw, sh), Image.NEAREST)) > 0
        wbox = _bbox(wmask) or (0, 0, sw, sh)
    else:
        wbox = box
    hh, ww = work.shape[:2]
    if engine == "lama":
        wins = _windows(wbox, WINDOW, ww, hh)
        for k, (x0, y0, x1, y1) in enumerate(wins):
            if not progress(k, len(wins), "修补 %d / %d" % (k + 1, len(wins))):
                raise InterruptedError("已取消")
            sub_m = wmask[y0:y1, x0:x1]
            if not sub_m.any():
                continue
            fixed = _lama_window(work[y0:y1, x0:x1], sub_m)
            region = work[y0:y1, x0:x1]
            region[sub_m] = fixed[sub_m]
            # 已修好的像素当成上下文，后面的窗口不再重复修
            wmask[y0:y1, x0:x1] = False
    else:
        pad = 40
        x0, y0 = max(0, wbox[0] - pad), max(0, wbox[1] - pad)
        x1, y1 = min(ww, wbox[2] + pad), min(hh, wbox[3] + pad)
        work[y0:y1, x0:x1] = _cv_window(work[y0:y1, x0:x1], wmask[y0:y1, x0:x1])
    progress(1, 1, "")
    if scale < 0.999:
        work = np.asarray(Image.fromarray(work).resize((w, h), Image.LANCZOS))
    # 只换掩膜内（略羽化）的像素
    alpha = Image.fromarray(mask.astype(np.uint8) * 255)
    if FEATHER:
        alpha = alpha.filter(ImageFilter.MaxFilter(2 * FEATHER + 1)).filter(ImageFilter.GaussianBlur(FEATHER))
    a = np.asarray(alpha).astype(np.float32)[..., None] / 255.0
    out = rgb.astype(np.float32) * (1 - a) + work.astype(np.float32) * a
    return Image.fromarray(np.clip(out + 0.5, 0, 255).astype(np.uint8)), engine


def rect_mask(size: Tuple[int, int], rects: List[Tuple[float, float, float, float]]) -> np.ndarray:
    """由若干矩形（像素坐标 x0, y0, x1, y1）拼出掩膜。"""
    w, h = size
    m = np.zeros((h, w), dtype=bool)
    for x0, y0, x1, y1 in rects:
        xa, xb = sorted((int(round(x0)), int(round(x1))))
        ya, yb = sorted((int(round(y0)), int(round(y1))))
        m[max(0, ya):min(h, yb), max(0, xa):min(w, xb)] = True
    return m
