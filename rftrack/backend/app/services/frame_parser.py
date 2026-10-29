"""帧解析：`0xA5 'R' type ver u16len payload crc32`。

**这里是第三份镜像**（固件 `firmware/src/proto/frames.h`、Web `web/src/proto/frames.ts`、
本文件），所以它唯一的正确性标准是"跟 TS 那份逐字段相等"，由
`tests/test_frame_parity.py` 拿 TS 现场导出的 golden 夹具把关。三个刻意的选择：

- **字段名照抄 TS 的驼峰**，不改成 Python 风格。对拍要的是逐字段相等；前端也按这套键消费。
- **不校验帧头 version 字节**。TS 从不读它（只靠 CRC 认帧），我若在这里加校验，
  两份镜像立刻分叉，而且真机上"TS 能读、Python 拒读"是最难查的那种故障。
  R20 想要的版本协商得改 `web/src/serial/parser.ts`，属阶段出口的申报偏差。
- **单位一律 dBFS**：帧里没有 dBm，dBm 只在 Web 侧由校准曲线换算，这里不得先折算。
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass
from struct import Struct

SYNC0 = 0xA5
SYNC1 = ord("R")
HEADER_SIZE = 6
CRC_SIZE = 4
MAX_PAYLOAD = 0xFFFF
#: 无换行的垃圾超过这个数就整段丢掉（镜像 parser.ts 的 4096 那条）。
GARBAGE_LIMIT = 4096

FRAME_METRICS = 1
FRAME_SPECTRUM = 2
FRAME_TELEMETRY = 3
FRAME_RAWIQ = 4
FRAME_LOG = 5
FRAME_LEVELS = 6

#: LEVELS_MODES = ["FULL", "RSSI", "SNR"]，越界回 "RSSI"（TS 的 `?? "RSSI"`）。
LEVELS_MODES = ("FULL", "RSSI", "SNR")

# 尺寸由 firmware/src/proto/frames.h 的 _Static_assert 锁死：34 / 26 / 32 / 4 / 59 / 14。
METRICS_HDR = Struct("<IIIIIIHhhBBBB")
CHANNEL = Struct("<BBhhhhhhiihh")
LEVELS_HDR = Struct("<IIIIIIHBBBBBB")
LEVEL = Struct("<BBBB")
SPECTRUM_HDR = Struct("<IIiIHBB")
TELEMETRY = Struct("<IIIIIIIIIIIHIIIB")
RAWIQ_HDR = Struct("<IIHHBB")

#: bytes 的下标出来是 0..255，TS 用的是 DataView.getInt8；这个换算是两份镜像的同一件事。
def signed_byte(b: int) -> int:
    return b - 256 if b > 127 else b


#: 固件把"这条信道从没清过"编码成 -32768 哨兵；TS 翻成 null，这里也必须 null，不能报 0。
NO_CLEAR_CHANNEL = -32768

# --- 编码换算（与 frames.ts 的两个常量函数一一对应） -----------------------

def level_db(code: int) -> float:
    """电平码 0.25 dB 步进：dBFS = code / 4 - 63.75。"""
    return code / 4 - 63.75


def spec_db(code: int) -> float:
    """频谱码：dBFS = code / 2 - 127.5。"""
    return code / 2 - 127.5


def centi_db(value: int) -> float:
    return value / 100


@dataclass
class StreamItem:
    kind: str  # "frame" | "text"
    type: int = 0
    payload: bytes = b""
    line: str = ""


_TEXT_LINE = re.compile(r"[A-Z][A-Z0-9_?]*( |$)")


class FrameStream:
    """字节流 → 文本行 / CRC 校验过的帧，行为镜像 `web/src/serial/parser.ts`。

    坏帧的处理是**一个字节一个字节往前挪**找下一个 sync，因此一帧坏掉不会让后面全丢；
    代价由 `crc_errors` / `resync_bytes` 计出来，供健康检查如实呈现。
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self.crc_errors = 0
        self.resync_bytes = 0
        self.bytes_in = 0

    def push(self, chunk: bytes) -> list[StreamItem]:
        self.bytes_in += len(chunk)
        self._buf += chunk
        out: list[StreamItem] = []
        pos = 0
        buf = self._buf
        n = len(buf)
        while pos < n:
            if buf[pos] == SYNC0:
                if n - pos < HEADER_SIZE:
                    break
                if buf[pos + 1] != SYNC1:
                    pos += 1
                    self.resync_bytes += 1
                    continue
                length = buf[pos + 4] | (buf[pos + 5] << 8)
                total = HEADER_SIZE + length + CRC_SIZE
                if length > MAX_PAYLOAD:  # 两条 sync 之间混进了别的字节
                    pos += 1
                    continue
                if n - pos < total:
                    break
                want = int.from_bytes(buf[pos + HEADER_SIZE + length : pos + total], "little")
                if zlib.crc32(bytes(buf[pos : pos + HEADER_SIZE + length])) & 0xFFFFFFFF != want:
                    self.crc_errors += 1
                    pos += 1
                    self.resync_bytes += 1
                    continue
                out.append(
                    StreamItem(
                        kind="frame",
                        type=buf[pos + 2],
                        payload=bytes(buf[pos + HEADER_SIZE : pos + HEADER_SIZE + length]),
                    )
                )
                pos += total
                continue
            # 文本行：到换行或下一个 sync0 为止（帧可能正好把一行打断）。
            end = pos
            while end < n and buf[end] != 0x0A and buf[end] != SYNC0:
                end += 1
            if end >= n:
                if n - pos > GARBAGE_LIMIT:
                    # 没有换行的垃圾（多半是从半截帧中间接上的）：整段扔掉。
                    self.resync_bytes += n - pos
                    pos = n
                break
            line = bytes(buf[pos:end]).decode("utf-8", "replace").removesuffix("\r")
            if end < n and buf[end] == 0x0A:
                end += 1
            if _TEXT_LINE.match(line):
                out.append(StreamItem(kind="text", line=line))
            elif line:
                self.resync_bytes += len(line)
            pos = end
        del self._buf[:pos]
        return out


# --- 字段解码：每个函数对应 frames.ts 里的一个 parseX ----------------------


def parse_metrics(payload: bytes) -> dict:
    """METRICS_FULL：`frame_metrics_t` + nch × `frame_channel_t`，电平是 centi-dB。"""
    h = METRICS_HDR.unpack_from(payload)
    nch = payload[METRICS_HDR.size - 1]
    channels = []
    for j in range(nch):
        o = METRICS_HDR.size + j * CHANNEL.size
        if o + CHANNEL.size > len(payload):
            break  # 只解放得下的那几个：多出来的必须是"不存在"，不能是补出来的 0
        c = CHANNEL.unpack_from(payload, o)
        # c[6]=o+10 是 clearFloor、c[7]=o+12 是 peak —— 顺序照抄 frames.ts 的 push，
        # 这两处对调过就会让"从没清过"的信道报出一个假的 dBFS 数。
        clear = c[6]
        channels.append(
            {
                "id": c[0],
                "flags": c[1],
                "rssi": centi_db(c[2]),
                "rssiEma": centi_db(c[3]),
                "floor": centi_db(c[4]),
                "snr": centi_db(c[5]),
                "clearFloor": None if clear == NO_CLEAR_CHANNEL else centi_db(clear),
                "peak": centi_db(c[7]),
                "peakOffHz": c[8],
                "centroidOffHz": c[9],
                "obwLoHz": c[10] * 1000,
                "obwHiHz": c[11] * 1000,
            }
        )
    return {
        "seq": h[0],
        "tMs": h[1],
        "loKhz": h[2],
        "tuneUs": h[3],
        "captureUs": h[4],
        "dspUs": h[5],
        "saturated": h[6],
        "mode": "FULL",
        "floorBin": centi_db(h[7]),
        "dc": centi_db(h[8]),
        "gain": h[9],
        "hopIndex": h[10],
        "hops": h[11],
        "channels": channels,
    }


def parse_levels(payload: bytes) -> dict:
    """METRICS_RSSI / METRICS_SNR 发的 `frame_levels_t`：这个模式没测的量必须回 None。"""
    h = LEVELS_HDR.unpack_from(payload)
    mode = LEVELS_MODES[h[10]] if h[10] < len(LEVELS_MODES) else "RSSI"
    snr_mode = mode == "SNR"
    nch = payload[LEVELS_HDR.size - 1]
    channels = []
    for j in range(nch):
        o = LEVELS_HDR.size + j * LEVEL.size
        if o + LEVEL.size > len(payload):
            break
        c = LEVEL.unpack_from(payload, o)
        rssi = level_db(c[2])
        snr = c[3] / 4 if snr_mode else None
        channels.append(
            {
                "id": c[0],
                "flags": c[1],
                "rssi": rssi,
                "rssiEma": None,
                "floor": None if snr is None else rssi - snr,
                "snr": snr,
                "clearFloor": None,
                "peak": None,
                "peakOffHz": None,
                "centroidOffHz": None,
                "obwLoHz": None,
                "obwHiHz": None,
            }
        )
    return {
        "seq": h[0],
        "tMs": h[1],
        "loKhz": h[2],
        "tuneUs": h[3],
        "captureUs": h[4],
        "dspUs": h[5],
        "saturated": h[6],
        "gain": h[7],
        "hopIndex": h[8],
        "hops": h[9],
        "mode": mode,
        # RSSI 模式下固件把 floor_bin 写成 0，那是"没测"而不是 -127.5 dBFS。
        "floorBin": spec_db(h[11]) if snr_mode else None,
        "dc": None,
        "channels": channels,
    }


def parse_spectrum(payload: bytes) -> dict:
    """显示频谱：每 bin 一个 u8 码。②a 的 UART 出口被固件钳到 512 bins。"""
    h = SPECTRUM_HDR.unpack_from(payload)
    n = h[4]
    db = [spec_db(payload[SPECTRUM_HDR.size + j]) for j in range(n)]
    return {
        "seq": h[0],
        "loKhz": h[1],
        "startOffHz": h[2],
        "binHz": h[3],
        "detector": h[5],
        "db": db,
    }


def parse_telemetry(payload: bytes) -> dict:
    h = TELEMETRY.unpack_from(payload)
    return {
        "tMs": h[0],
        "rotations": h[1],
        "hopsTotal": h[2],
        "rotationHz": h[3] / 1000,
        "hopUsAvg": h[4],
        "hopUsMax": h[5],
        "tuneUsAvg": h[6],
        "tuneUsMax": h[7],
        "captureUsAvg": h[8],
        "dspUsAvg": h[9],
        "dspUsMax": h[10],
        "linkFill": h[11] / 1000,
        "heapFree": h[12],
        "drops": h[13],
        "captureErrors": h[14],
        "running": h[15] != 0,
    }


def parse_rawiq_chunk(payload: bytes) -> dict:
    h = RAWIQ_HDR.unpack_from(payload)
    return {
        "loKhz": h[0],
        "rateHz": h[1],
        "n": h[2],
        "offset": h[3],
        "count": (len(payload) - RAWIQ_HDR.size) >> 1,
        "conjugated": h[4] != 0,
        "shift": h[5],
    }


class RawIqAssembler:
    """把 RAWIQ 分块拼成一个快照；**缺块就整快照作废**，不拼半张混了旧数据的图。"""

    def __init__(self) -> None:
        self._cur: dict | None = None
        self._next = 0

    def add(self, payload: bytes) -> dict | None:
        c = parse_rawiq_chunk(payload)
        if c["offset"] == 0:
            self._cur = {
                "loKhz": c["loKhz"],
                "rateHz": c["rateHz"],
                "n": c["n"],
                "conjugated": c["conjugated"],
                "shift": c["shift"],
                "i": [0] * c["n"],
                "q": [0] * c["n"],
            }
            self._next = 0
        cur = self._cur
        if (
            cur is None
            or c["offset"] != self._next
            or c["n"] != cur["n"]
            or c["offset"] + c["count"] > cur["n"]
        ):
            self._cur = None
            return None
        body = payload[RAWIQ_HDR.size :]
        for j in range(c["count"]):
            cur["i"][c["offset"] + j] = signed_byte(body[2 * j])
            cur["q"][c["offset"] + j] = signed_byte(body[2 * j + 1])
        self._next = c["offset"] + c["count"]
        if self._next < cur["n"]:
            return None
        self._cur = None
        return cur


#: 帧类型 → 解码器。FRAME_LOG 不在表里：TS 也没有 parseLog，未知类型一律交回原字节。
DECODERS = {
    FRAME_METRICS: parse_metrics,
    FRAME_LEVELS: parse_levels,
    FRAME_SPECTRUM: parse_spectrum,
    FRAME_TELEMETRY: parse_telemetry,
    FRAME_RAWIQ: parse_rawiq_chunk,
}


def decode(type_code: int, payload: bytes) -> dict | None:
    """认得的帧给字段，认不出的返回 None（LOG 帧走这里，不得抛异常打断采集）。"""
    decoder = DECODERS.get(type_code)
    return None if decoder is None else decoder(payload)
