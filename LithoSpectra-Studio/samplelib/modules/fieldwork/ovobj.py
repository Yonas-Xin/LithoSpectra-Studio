# -*- coding: utf-8 -*-
"""奥维互动地图 ``.ovobj`` 收藏夹文件的读取。

奥维没有公开这个格式，下面的结构是对照它导出的 GeoJSON 逐字节核出来的，覆盖
「标签（点）」与「轨迹」两类对象及其内嵌附件（照片，以及录音、视频、文档等文件），
「文件夹」只是收藏夹里的分组，读的时候跳过。遇到不认识的对象类型时跳过并记一条
警告，不中断整个文件。

文件布局
--------
::

    0x000  "OviO"，0x0C 处 u32 版本（目前见到 105），0x18 处 u32 文件长度
    0x0F0  根块头 [u32 校验][u32 类型][u32 长度]
    对象区  从 0x108 开始，到 0x108 + 根块长度为止
    附件区  紧跟对象区，一串附件块，块头 [u32 校验][u32 0x1C][u32 长度]，下一块在 +24+长度

根块类型是 0x64 时对象区是一串对象块，块头 [u32 长度][u32 0x1F]，长度不含这 8 字节。
只导出一个标签（或一条轨迹）时奥维省掉这层列表：根块类型就是 0x1F，根块本身即那个
对象，按 0x100 为块首去读（块首那 8 字节是空的，长度在根块头里）。

对象块（偏移相对块首）::

    +0x18  u32 长度-0x40（冗余）
    +0x1C  u32 对象类型：7 = 标签，8 = 轨迹
    +0x20  8 字节对象 id，+0x28 u32 修改时间（Unix 秒）
    +0x4C  u32 创建时间
    +0x70  主附件：8 字节哈希、u32 大小、两个时间、文件名、扩展名（共 0x50 字节）
    +0xC0  其余附件的哈希，每个 8 字节，个数在几何段 +0x18

标签的几何段在块首 +0x158::

    +0x00  u16 图标，u16 1
    +0x18  u32 附加附件个数
    +0x1C  f64 纬度、f64 经度（WGS84，不是 GCJ-02）
    +0x2C  u16 / u16 样式，+0x30 若干标志
    +0x3B  u8 名称长度，随后名称（UTF-8）与 1 字节 0
           u32 备注长度，随后备注与 1 字节 0

轨迹在名称、备注之后是点数 n、4 字节保留，然后三段：

* **坐标**：首点 i64 纬度×1e8、i64 经度×1e8；之后每点一个控制字节——最高位纬度
  取负、次高位经度取负、中间 4 位是后续字节数 k、最低 2 位并上后续 k 字节组成
  2+8k 位的整数，高一半是纬度差、低一半是经度差。
* **时间**：u32 起始时间（Unix 秒），之后 n-1 个无符号变长差值。
* **高程**：i32 起始高程（米），之后 n-1 个有符号变长差值。

变长整数看首字节高两位：``00`` 是 0；``01`` 一字节；``10`` 两字节；``11`` 四字节。
无符号时首字节余下 6 位是最高位，有符号时第 6 位（0x20）是负号、余下 5 位是最高位。

附件块::

    +0x20  8 字节哈希（对象块里用它引用附件），+0x28 u32 数据长度
    +0x2C  u32 / u32 时间，+0x34 文件名，+0x68 扩展名
    +0x7C  数据本体（照片就是完整的 JPEG，EXIF 原样保留；录音、文档也是原文件）

附件的类型看扩展名；扩展名缺了按数据开头的几个字节认（:func:`sniff_ext`）。

奥维的录音（扩展名 spx）不是标准的 Ogg Speex，而是它自己的封装::

    "OSPX"、u32 其后数据长度、u32 校验、u32 0
    随后是一个 WAV 文件：RIFF 头与 fmt 块照录音原样（PCM 16 位），data 块里却不是
    PCM，而是一串 [u32 帧长][Speex 窄带帧]；data 块头写的长度是解码后 PCM 的长度

见 :func:`ospx_frames`。
"""

from __future__ import annotations

import mmap
import os
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterator, List, Optional, Tuple

MAGIC = b"OviO"
OBJECT_TAG = 0x1F
ATTACH_TAG = 0x1C
ROOT_HEADER = 0xF0
ROOT_LIST = 0x64
OBJECTS_START = 0x108

TYPE_PLACEMARK = 7
TYPE_TRACK = 8
TYPE_FOLDER = 30         # 收藏夹里的文件夹，只有名称，不带几何

#: 附件按扩展名分的类型
PHOTO_EXTS = {"jpg", "jpeg", "png", "heic", "heif", "webp", "gif", "bmp", "tif", "tiff"}
AUDIO_EXTS = {"amr", "m4a", "mp3", "aac", "wav", "ogg", "opus", "caf", "wma", "flac", "spx", "awb"}
VIDEO_EXTS = {"mp4", "mov", "m4v", "3gp", "avi", "mkv", "wmv"}

#: 标签几何段相对块首的偏移
_PM_GEOM = 0x158
#: 主附件条目、附加附件哈希相对块首的偏移
_ATT_PRIMARY = 0x70
_ATT_EXTRA = 0xC0
#: 附件块里数据本体的偏移
_ATT_DATA = 0x7C


class OvobjError(ValueError):
    """文件不是奥维收藏夹，或结构与已知格式对不上。"""


@dataclass
class Attachment:
    """内嵌在文件里的一个附件（目前见到的都是 JPEG 照片）。"""

    hash: str              # 16 位十六进制，对象块里靠它引用
    name: str              # 奥维给的文件名，如 pic_20260819_102407
    ext: str
    size: int
    time: Optional[datetime]
    offset: int            # 数据本体在文件里的偏移

    @property
    def filename(self) -> str:
        ext = self.ext or "jpg"
        return "%s.%s" % (self.name or self.hash, ext)

    @property
    def kind(self) -> str:
        """photo / audio / video / file。扩展名缺了按 photo 算（早期文件只存照片）；
        导入时还会按数据开头复核一次。"""
        return attachment_kind(self.ext or "jpg")


@dataclass
class Placemark:
    uid: str               # 奥维对象 id，重复导入时据此认出同一个点
    name: str
    comment: str
    lon: float
    lat: float
    created: Optional[datetime]
    modified: Optional[datetime]
    icon: int = 0
    attachments: List[str] = field(default_factory=list)   # 附件哈希，按奥维里的顺序


@dataclass
class Track:
    uid: str
    name: str
    comment: str
    created: Optional[datetime]
    modified: Optional[datetime]
    lons: List[float] = field(default_factory=list)
    lats: List[float] = field(default_factory=list)
    times: List[int] = field(default_factory=list)          # Unix 秒
    elevations: List[int] = field(default_factory=list)     # 米
    attachments: List[str] = field(default_factory=list)    # 附件哈希（轨迹上的录音、照片等）

    @property
    def start(self) -> Optional[datetime]:
        return _ts(self.times[0]) if self.times else None

    @property
    def end(self) -> Optional[datetime]:
        return _ts(self.times[-1]) if self.times else None


@dataclass
class OvobjFile:
    path: str
    version: int
    placemarks: List[Placemark] = field(default_factory=list)
    tracks: List[Track] = field(default_factory=list)
    attachments: Dict[str, Attachment] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    folders: List[str] = field(default_factory=list)        # 收藏夹里的文件夹名

    @property
    def orphans(self) -> List[str]:
        """文件里有、但没有哪个标签或轨迹引用的附件哈希。"""
        used = {h for o in [*self.placemarks, *self.tracks] for h in o.attachments}
        return [h for h in self.attachments if h not in used]

    def read_attachment(self, att: Attachment) -> bytes:
        with open(self.path, "rb") as fh:
            fh.seek(att.offset)
            data = fh.read(att.size)
        if len(data) != att.size:
            raise OvobjError("附件 %s 数据不完整" % att.filename)
        return data


# =============================================================================
# 底层读取
# =============================================================================

#: 早于它的时间戳当作无效（轨迹块的「创建时间」位置上放的不是时间）
_MIN_TS = 946684800      # 2000-01-01


def attachment_kind(ext: str) -> str:
    ext = (ext or "").lower().lstrip(".")
    if ext in PHOTO_EXTS:
        return "photo"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in VIDEO_EXTS:
        return "video"
    return "file"


def sniff_ext(head: bytes) -> str:
    """按数据开头认出常见的照片、录音、视频、文档格式，认不出返回空串。"""
    if head[:3] == b"\xff\xd8\xff":
        return "jpg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:6] == b"#!AMR\n" or head[:9] == b"#!AMR-WB\n":
        return "amr"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"caff":
        return "caf"
    if head[:4] == b"OSPX":
        return "spx"
    if head[:4] == b"%PDF":
        return "pdf"
    if head[:4] == b"PK\x03\x04":
        return "zip"
    if head[:3] == b"ID3":
        return "mp3"
    if len(head) >= 2 and head[0] == 0xFF:
        if head[1] & 0xF6 == 0xF0:         # ADTS 的 layer 位是 0
            return "aac"
        if head[1] & 0xE0 == 0xE0 and head[1] & 0x06:
            return "mp3"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"mif1", b"msf1", b"heif"):
            return "heic"
        if brand in (b"M4A ", b"M4B "):
            return "m4a"
        if brand == b"qt  ":
            return "mov"
        if brand.startswith(b"3g"):
            return "3gp"
        return "mp4"
    return ""


def _ts(value: int) -> Optional[datetime]:
    if not value or value < _MIN_TS:
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _u32(buf, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def _cstr(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("utf-8", "replace")


def _varint(buf, off: int, signed: bool) -> Tuple[int, int]:
    """读一个变长整数，返回 (值, 新偏移)。编码见模块说明。"""
    b = buf[off]
    cls = b >> 6
    if cls == 0:
        return 0, off + 1
    nbytes = (1, 2, 4)[cls - 1]
    top = b & (0x1F if signed else 0x3F)
    v = top
    for k in range(1, nbytes):
        v = (v << 8) | buf[off + k]
    if signed and b & 0x20:
        v = -v
    return v, off + nbytes


def _strings(buf, off: int, end: int) -> Tuple[str, str, int]:
    """名称（u8 长度）+ 0 + 备注（u32 长度）+ 备注内容。返回名称、备注、备注之后的偏移。"""
    n = buf[off]
    off += 1
    if off + n > end:
        raise OvobjError("名称越界")
    name = bytes(buf[off:off + n]).decode("utf-8", "replace")
    off += n + 1
    m = _u32(buf, off)
    off += 4
    if off + m > end:
        raise OvobjError("备注越界")
    comment = bytes(buf[off:off + m]).decode("utf-8", "replace")
    return name, comment, off + m


def _attachment_refs(buf, start: int, extra: int) -> List[str]:
    refs = []
    primary = bytes(buf[start + _ATT_PRIMARY:start + _ATT_PRIMARY + 8])
    if primary.strip(b"\0"):
        refs.append(primary.hex())
    for i in range(extra):
        h = bytes(buf[start + _ATT_EXTRA + 8 * i:start + _ATT_EXTRA + 8 * i + 8])
        if h.strip(b"\0"):
            refs.append(h.hex())
    return refs


# =============================================================================
# 对象
# =============================================================================

def _header(buf, start: int) -> Tuple[str, Optional[datetime], Optional[datetime]]:
    uid = bytes(buf[start + 0x20:start + 0x28]).hex()
    modified = _ts(_u32(buf, start + 0x28))
    created = _ts(_u32(buf, start + 0x4C))
    return uid, created, modified


def _placemark(buf, start: int, end: int) -> Placemark:
    g = start + _PM_GEOM
    icon = struct.unpack_from("<H", buf, g)[0]
    extra = _u32(buf, g + 0x18)
    lat, lon = struct.unpack_from("<dd", buf, g + 0x1C)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise OvobjError("标签坐标超出范围：%r, %r" % (lon, lat))
    name, comment, _ = _strings(buf, g + 0x3B, end)
    uid, created, modified = _header(buf, start)
    return Placemark(uid=uid, name=name, comment=comment, lon=lon, lat=lat,
                     created=created, modified=modified, icon=icon,
                     attachments=_attachment_refs(buf, start, extra))


def _track_body(buf, off: int, end: int, n: int) -> Tuple[List[int], List[int], List[int], List[int], int]:
    """从首点坐标处开始解三段流，返回 (纬度×1e8, 经度×1e8, 时间, 高程, 结束偏移)。"""
    lat, lon = struct.unpack_from("<qq", buf, off)
    off += 16
    lats, lons = [lat], [lon]
    for _ in range(n - 1):
        ctl = buf[off]
        k = (ctl >> 2) & 0x0F
        v = ctl & 0x03
        for j in range(1, k + 1):
            v = (v << 8) | buf[off + j]
        off += 1 + k
        width = 1 + 4 * k
        dlat, dlon = v >> width, v & ((1 << width) - 1)
        lat += -dlat if ctl & 0x80 else dlat
        lon += -dlon if ctl & 0x40 else dlon
        lats.append(lat)
        lons.append(lon)
        if off > end:
            raise OvobjError("轨迹坐标越界")
    t = _u32(buf, off)
    off += 4
    times = [t]
    for _ in range(n - 1):
        d, off = _varint(buf, off, signed=False)
        t += d
        times.append(t)
    z = struct.unpack_from("<i", buf, off)[0]
    off += 4
    elev = [z]
    for _ in range(n - 1):
        d, off = _varint(buf, off, signed=True)
        z += d
        elev.append(z)
    return lats, lons, times, elev, off


def _track(buf, start: int, end: int) -> Track:
    """轨迹块里名称之前的那段长度随版本而变，靠「三段流恰好读到块尾」定位名称。"""
    uid, created, modified = _header(buf, start)
    for p in range(start + 0x90, min(end, start + 0x400)):
        n_name = buf[p]
        if n_name == 0 or buf[p - 1] or buf[p - 2] or buf[p - 3]:
            continue
        try:
            name, comment, off = _strings(buf, p, end)
        except (OvobjError, struct.error):
            continue
        if off + 24 > end:
            continue
        n = _u32(buf, off)
        if not 1 <= n <= (end - off):
            continue
        try:
            lats, lons, times, elev, stop = _track_body(buf, off + 8, end, n)
        except (OvobjError, struct.error, IndexError):
            continue
        if stop != end:
            continue
        return Track(uid=uid, name=name, comment=comment, created=created, modified=modified,
                     lats=[v / 1e8 for v in lats], lons=[v / 1e8 for v in lons],
                     times=times, elevations=elev)
    raise OvobjError("轨迹块结构无法识别")


def _root(buf) -> Tuple[int, int]:
    """根块的类型与长度。"""
    _, kind, length = struct.unpack_from("<III", buf, ROOT_HEADER)
    return kind, length


def _iter_objects(buf) -> Iterator[Tuple[int, int, int]]:
    """逐个给出 (块首, 块尾, 对象类型)。"""
    size = len(buf)
    kind, length = _root(buf)
    if kind == OBJECT_TAG:
        # 只导出了一个对象：根块就是它
        start, end = OBJECTS_START - 8, OBJECTS_START + length
        if end > size:
            raise OvobjError("对象块越过文件末尾")
        yield start, end, _u32(buf, start + 0x1C)
        return
    off = OBJECTS_START
    while off + 32 <= size:
        length, tag = struct.unpack_from("<II", buf, off)
        if tag != OBJECT_TAG:
            return
        end = off + 8 + length
        if end > size:
            raise OvobjError("对象块越过文件末尾（偏移 %d）" % off)
        yield off, end, _u32(buf, off + 0x1C)
        off = end


def _objects_end(buf) -> int:
    kind, length = _root(buf)
    if kind in (ROOT_LIST, OBJECT_TAG) and OBJECTS_START + length <= len(buf):
        return OBJECTS_START + length
    end = OBJECTS_START
    for _, end, _ in _iter_objects(buf):
        pass
    return end


def _iter_attachments(buf, off: int) -> Iterator[Attachment]:
    size = len(buf)
    while off + 24 <= size:
        _, tag, length = struct.unpack_from("<III", buf, off)
        if tag != ATTACH_TAG:
            return
        dsize = _u32(buf, off + 0x28)
        if off + _ATT_DATA + dsize > size:
            raise OvobjError("附件块越过文件末尾（偏移 %d）" % off)
        yield Attachment(hash=bytes(buf[off + 0x20:off + 0x28]).hex(),
                         name=_cstr(bytes(buf[off + 0x34:off + 0x68])),
                         ext=_cstr(bytes(buf[off + 0x68:off + 0x70])).lower(),
                         size=dsize, time=_ts(_u32(buf, off + 0x2C)),
                         offset=off + _ATT_DATA)
        off += 24 + length


# =============================================================================
# 入口
# =============================================================================

def is_ovobj(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == MAGIC
    except OSError:
        return False


def read(path: str) -> OvobjFile:
    """解析整个文件。照片数据不读进内存，要时用 :meth:`OvobjFile.read_attachment`。"""
    with open(path, "rb") as fh:
        if fh.read(4) != MAGIC:
            raise OvobjError("不是奥维收藏夹文件（缺少 OviO 文件头）：%s" % path)
        if os.fstat(fh.fileno()).st_size < OBJECTS_START:
            raise OvobjError("文件过短：%s" % path)
        with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as buf:
            out = OvobjFile(path=path, version=_u32(buf, 0x0C))
            for start, end, kind in _iter_objects(buf):
                try:
                    if kind == TYPE_PLACEMARK:
                        out.placemarks.append(_placemark(buf, start, end))
                    elif kind == TYPE_TRACK:
                        tr = _track(buf, start, end)
                        # 轨迹只认主附件那一格（附加附件的个数不知道放在哪）
                        tr.attachments = _attachment_refs(buf, start, 0)
                        out.tracks.append(tr)
                    elif kind == TYPE_FOLDER:
                        out.folders.append(_folder_name(buf, start, end))
                    else:
                        out.warnings.append("已跳过不支持的对象（类型 %d，偏移 %d）" % (kind, start))
                except (OvobjError, struct.error, IndexError) as exc:
                    out.warnings.append("对象（类型 %d，偏移 %d）读取失败：%s" % (kind, start, exc))
            for att in _iter_attachments(buf, _objects_end(buf)):
                out.attachments[att.hash] = att
    known = set(out.attachments)
    for pm in out.placemarks:
        missing = [h for h in pm.attachments if h not in known]
        if missing:
            out.warnings.append("「%s」引用的 %d 个附件在文件中缺失" % (pm.name, len(missing)))
            pm.attachments = [h for h in pm.attachments if h in known]
    for tr in out.tracks:
        tr.attachments = [h for h in tr.attachments if h in known]
    return out


def _folder_name(buf, start: int, end: int) -> str:
    """文件夹块里只有一个名称：u8 长度、名称、0，落在块尾。从后往前找能对上的那一处。"""
    for p in range(end - 2, start + 0x20, -1):
        n = buf[p]
        if n and p + 1 + n + 1 == end and buf[end - 1] == 0:
            try:
                return bytes(buf[p + 1:p + 1 + n]).decode("utf-8")
            except UnicodeDecodeError:
                break
    return ""


# =============================================================================
# 奥维录音（OSPX）
# =============================================================================

def ospx_frames(data: bytes) -> Tuple[Dict[str, int], List[bytes]]:
    """拆开奥维录音：返回 (WAV 格式信息, Speex 帧列表)。格式信息含 rate、channels、
    bits、pcm_bytes（解码后 PCM 的字节数）。结构见模块说明。"""
    if data[:4] != b"OSPX" or data[16:20] != b"RIFF" or data[24:28] != b"WAVE":
        raise OvobjError("不是奥维录音（OSPX）")
    w = data[16:]
    off, info, frames = 12, {}, []
    while off + 8 <= len(w):
        cid, size = struct.unpack_from("<4sI", w, off)
        body = off + 8
        if cid == b"fmt ":
            fmt, channels, rate, _, _, bits = struct.unpack_from("<HHIIHH", w, body)
            info = {"format": fmt, "channels": channels, "rate": rate, "bits": bits}
        elif cid == b"data":
            info["pcm_bytes"] = size
            p = body
            while p + 4 <= len(w):
                n = _u32(w, p)
                if n == 0 or p + 4 + n > len(w):
                    break
                frames.append(bytes(w[p + 4:p + 4 + n]))
                p += 4 + n
            break
        off = body + size + (size & 1)
    if not info.get("rate") or not frames:
        raise OvobjError("奥维录音里没有可解的数据")
    return info, frames
