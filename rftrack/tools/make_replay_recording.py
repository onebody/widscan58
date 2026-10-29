#!/usr/bin/env python3
"""造一份**合成**的回放录制，给采集层与工作台当数据源（`app/services/replay.py` 读它）。

先说清它不是什么：**这不是真机录制**。字节形状取自 `backend/tests/golden/frames-v1.json`
的 `metrics_full_3ch`（那是"两份解析器口径一致"的对拍夹具），电平数字是这里按公式算出来的。
所以产出文件里 `source` 一律写 `mock`，`caveat` 一句话跟着走——S6 出口判据里那句"用真机录制
回放做数据源"，靠这个脚本只完成了**管道**那半，另外半（任务 #7 / M0-3 的 60 s 真机录制）今天
还不存在。真机那份录完之后照同一个格式放进目录即可，本脚本与它无关。

三条口径：

1. **只动电平，不动别的**：`seq`/`tMs` 按帧号与节拍重写，三条信道的 `rssi`/`rssiEma` 按下面的
   公式算；`loKhz`、`flags`、频道号、`floor`/`snr`/`peak`/带宽等一律照夹具。**不去编一个信道计划
   或跳频序列**——那些数一旦是编的，页面上就没有任何东西还能当真。
2. **公式可复现**：没有随机数。同一个 `--seconds/--hz` 跑两次产出逐字节相同，所以冒烟脚本能钉住
   "第 40 帧那条信道正好是 −30.0 dB"这种断言，而不是"看起来在动"。
3. **写法与读取端同源**：帧的封包（`0xA5 'R' type ver u16len payload crc32`）在这里算 CRC，
   读进去的是 `frame_parser.FrameStream`。对不上就当场 `SystemExit`——一份演不动的录制比没有
   录制更糟，它会让人以为自己验过了。

用法：

    python3 rftrack/tools/make_replay_recording.py --addr /dev/widscan58-demo
    python3 rftrack/tools/make_replay_recording.py --addr /dev/widscan58-demo \\
        --out-dir /tmp/rftrack-replay --seconds 20 --hz 5 --chunk-bytes 64

之后：`RFTRACK_REPLAY_DIR=/tmp/rftrack-replay` 起后端。目录里每一份的 `transport_addr` 都要能在
某个节点行里找到归属，否则健康检查会点名 `node_row_missing` 而一个字节都不喂。
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import struct
import sys
import zlib
from pathlib import Path
from typing import Any

TOOLS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TOOLS_DIR.parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services import frame_parser  # noqa: E402
from app.services.replay import FORMAT, SOURCE, ReplayError, load  # noqa: E402

GOLDEN_PATH = BACKEND_DIR / "tests" / "golden" / "frames-v1.json"
GOLDEN_CASE = "metrics_full_3ch"
DEFAULT_OUT = Path("/tmp/rftrack-replay")

#: 第 0 条信道演一次"飞过去再飞回来"：最远 −95 dB、最近 −30 dB（和夹具的量级一致）。
FAR_DB = -95.0
NEAR_DB = -30.0
#: 第 1 条信道是"安静的信道在漂"：围绕 −78 dB 摆 ±2 dB，周期 4 s。
QUIET_DB = -78.0
QUIET_SWING_DB = 2.0
QUIET_PERIOD_S = 4.0
#: 平滑系数：够让 `rssiEma` 明显落后于 `rssi`，又不至于在 20 s 里追不上去。
EMA_ALPHA = 0.3


def golden_payload() -> tuple[bytes, dict[str, Any]]:
    """夹具里那条 `metrics_full_3ch`：要的是"字段顺序与长度都由固件侧定义"这个既有事实。"""
    manifest = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    for case in manifest["cases"]:
        if case["name"] == GOLDEN_CASE:
            return bytes.fromhex(case["hex"]), case["parsed"]
    raise SystemExit(f"夹具 {GOLDEN_PATH} 里没有 {GOLDEN_CASE} 这一条")


def channel_levels(slot: int, frame_index: int, frames: int, hz: float, base_db: float) -> float:
    """第 `slot` 条信道在第 `frame_index` 帧的电平（dB）。三条各演一件事，都是闭式公式。"""
    if slot == 0:
        half = max(2, frames // 2)
        position = frame_index if frame_index < half else 2 * half - 2 - frame_index
        position = max(0, position)
        return FAR_DB + (NEAR_DB - FAR_DB) * (position / (half - 1))
    if slot == 1:
        period = max(2.0, QUIET_PERIOD_S * hz)
        return QUIET_DB + QUIET_SWING_DB * math.sin(2 * math.pi * frame_index / period)
    return base_db


def _assemble(payload: bytes) -> bytes:
    """`0xA5 'R' type ver u16len payload crc32(le)`——与 `FrameStream` 认的那一条同式。"""
    header = bytes((0xA5, ord("R"), frame_parser.FRAME_METRICS, 1))
    head = header + struct.pack("<H", len(payload))
    crc = zlib.crc32(head + payload) & 0xFFFFFFFF
    return head + payload + struct.pack("<I", crc)


def _rewrite(payload: bytes, levels: list[float], emas: list[float], seq: int, t_ms: int) -> bytes:
    """重写 `seq`/`tMs` 与每条信道的 `rssi`/`rssiEma`；其余字节（含 `flags`）原样留着。"""
    body = bytearray(payload)
    struct.pack_into("<II", body, 0, seq, t_ms)
    for slot, (db, ema) in enumerate(zip(levels, emas)):
        offset = frame_parser.METRICS_HDR.size + slot * frame_parser.CHANNEL.size
        struct.pack_into("<hh", body, offset + 2, round(db * 100), round(ema * 100))
    return bytes(body)


def synthesize(*, seconds: float, hz: float, addr: str, chunk_bytes: int = 0) -> dict[str, Any]:
    """算出整份录制的 manifest（还没落盘）。纯函数，测试直接拿它和 `load()` 对拍。"""
    if seconds <= 0 or hz <= 0:
        raise SystemExit("--seconds 与 --hz 都要大于 0")
    if chunk_bytes < 0:
        raise SystemExit("--chunk-bytes 不能为负")
    payload, parsed = golden_payload()
    channels = parsed["channels"]
    if len(channels) * frame_parser.CHANNEL.size + frame_parser.METRICS_HDR.size != len(payload):
        raise SystemExit("夹具那条 METRICS 的长度与信道数对不上，脚本的偏移失效了")
    frames = max(2, round(seconds * hz))
    base_dbs = [c["rssi"] for c in channels]
    emas = list(base_dbs)
    chunks: list[dict[str, Any]] = []
    for index in range(frames):
        levels = [
            channel_levels(slot, index, frames, hz, base)
            for slot, base in enumerate(base_dbs)
        ]
        for slot, db in enumerate(levels):
            emas[slot] = db if index == 0 else emas[slot] + (db - emas[slot]) * EMA_ALPHA
        t_ms = round(index * 1000 / hz)
        frame = _assemble(_rewrite(payload, levels, emas, index, t_ms))
        if chunk_bytes:
            for start in range(0, len(frame), chunk_bytes):
                piece = frame[start : start + chunk_bytes]
                chunks.append({"t_ms": t_ms, "bytes": base64.b64encode(piece).decode()})
        else:
            chunks.append({"t_ms": t_ms, "bytes": base64.b64encode(frame).decode()})
    return {
        "format": FORMAT,
        "source": SOURCE,
        "transport_addr": addr,
        "generated_by": Path(__file__).name,
        "notes": (
            f"合成回放：{frames} 帧 METRICS_FULL，{hz:g} Hz / {seconds:g} s，"
            f"3 条信道的电平由闭式公式给出（第 0 条 −95→−30→−95 dB 往返一次）"
        ),
        "caveat": (
            "合成帧：形状取自 backend/tests/golden/frames-v1.json 的 "
            f"{GOLDEN_CASE}，电平是公式算的。不证明固件实际吐过这些字节"
            "（那要 M0-3 的 60 s 真机录制，见实现计划 §11）。"
        ),
        "shape": {
            "frame_type": "METRICS_FULL",
            "frames": frames,
            "hz": hz,
            "seconds": round((frames - 1) / hz, 3),
            "chunk_bytes": chunk_bytes,
            "lo_khz": parsed["loKhz"],
            "channel_ids": [c["id"] for c in channels],
            "golden_fields_kept": "flags/floor/snr/peak/peakOffHz/centroidOffHz/obw*/loKhz",
        },
        "chunks": chunks,
    }


def write(manifest: dict[str, Any], out_dir: Path, name: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="导出一份合成的回放录制（source=mock）")
    parser.add_argument("--addr", required=True, help="这份录制属于哪个 transport_addr")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--name", default=None, help="文件名（默认 synthetic-<seconds>s）")
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--hz", type=float, default=5.0)
    parser.add_argument(
        "--chunk-bytes",
        type=int,
        default=0,
        help="按几字节切一块（0 = 一帧一块）。模拟串口碎片时用 64",
    )
    args = parser.parse_args()

    manifest = synthesize(
        seconds=args.seconds, hz=args.hz, addr=args.addr, chunk_bytes=args.chunk_bytes
    )
    path = write(manifest, args.out_dir, args.name or f"synthetic-{args.seconds:g}s")

    # 自检：产出必须能被读取端原样吃进去，而且解析回来到这一层就还在。写得出、演不动的录制，
    # 只会让人以为"管道没问题"，实际问题在文件里。
    recording = load(path)
    _verify(recording, args.seconds, args.hz, args.addr)
    print(f"录制：{path}（{recording.span_s} s / {len(recording.chunks)} 块，source={SOURCE}）")
    print(f"  归属地址：{recording.transport_addr}；notes：{recording.notes}")
    return 0


def _verify(recording, seconds: float, hz: float, addr: str) -> None:
    if recording.transport_addr != addr:
        raise SystemExit(f"自检失败：地址写成了 {recording.transport_addr!r}")
    stream = frame_parser.FrameStream()
    points: list[dict[str, Any]] = []
    for _, chunk in recording.chunks:
        for item in stream.push(chunk):
            if item.kind != "text":
                decoded = frame_parser.decode(item.type, item.payload)
                if decoded is None:
                    raise SystemExit(f"自检失败：第 {len(points)} 帧解析不出来")
                points.append(decoded)
    if not points:
        raise SystemExit("自检失败：一块帧都没解出来")
    if any(p["seq"] != n for n, p in enumerate(points)):
        raise SystemExit("自检失败：seq 不连续，回放页面上的'帧号'会跳")
    if stream.crc_errors:
        raise SystemExit(f"自检失败：{stream.crc_errors} 帧 CRC 不对")
    if points[0]["channels"][0]["rssi"] != FAR_DB or abs(seconds * hz - len(points)) > 1:
        raise SystemExit(f"自检失败：首帧不是 {FAR_DB} dB，或帧数 {len(points)} 与节拍不符")


if __name__ == "__main__":
    raise SystemExit(main())
