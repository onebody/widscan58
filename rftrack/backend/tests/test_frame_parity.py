"""帧对拍：同一批字节，Python 解析结果必须与 TS 解析结果**逐字段相等**。

这是 C / TS / Python 三份协议镜像唯一的防漂移手段（落地方案 §5 最后一条）。
期望值不由本文件手写，全部来自 `rftrack/tools/export_golden_frames.mjs` 现场调用
`web/src/serial/parser.ts` + `web/src/proto/frames.ts` 导出的 golden JSON——
我用 TS 跑出来的数字当权威，而不是"我觉得应该是这个值"。

golden 文件缺失时**标 skip，不得假绿**：那意味着 TS 侧权威没跑过，比较无从谈起。
"""

import json
from pathlib import Path

import pytest

GOLDEN = Path(__file__).parent / "golden" / "frames-v1.json"

if not GOLDEN.exists():
    pytest.skip(
        "golden/frames-v1.json 不存在：先跑 "
        "`node --experimental-strip-types rftrack/tools/export_golden_frames.mjs`",
        allow_module_level=True,
    )

DOC = json.loads(GOLDEN.read_text())
CASES = {c["name"]: c for c in DOC["cases"]}
STREAMS = {s["name"]: s for s in DOC["streams"]}

from app.services.frame_parser import (  # noqa: E402
    FRAME_LEVELS,
    FRAME_LOG,
    FRAME_METRICS,
    FRAME_RAWIQ,
    FRAME_SPECTRUM,
    FRAME_TELEMETRY,
    FrameStream,
    RawIqAssembler,
    decode,
    parse_levels,
    parse_metrics,
    parse_rawiq_chunk,
    parse_spectrum,
    parse_telemetry,
)

METRICS_CASES = ["metrics_full_3ch"]
LEVELS_CASES = ["levels_mode_1", "levels_mode_2", "levels_mode_0_is_full", "levels_truncated_nch"]
SPECTRUM_CASES = ["spectrum_n_3", "spectrum_n_8", "spectrum_n_512"]


def payload_of(name: str) -> bytes:
    """夹具里的 `hex` 一律是**帧的 payload**（帧头与 CRC 由 FrameStream 负责）。"""
    return bytes.fromhex(CASES[name]["hex"])


def test_golden_was_generated_not_hand_written():
    """夹具必须写明权威来源，否则它跟手写期望值没有区别。"""
    assert DOC["generated_by"].endswith("export_golden_frames.mjs")
    assert set(DOC["authority"]) == {"web/src/serial/parser.ts", "web/src/proto/frames.ts"}
    assert DOC["layout_source"] == "firmware/src/proto/frames.h"
    # "合成帧"这条局限必须写在夹具自己身上，防止后人把对拍当成"固件已验证"。
    assert "合成" in DOC["caveat"]


@pytest.mark.parametrize("name", METRICS_CASES)
def test_metrics_frame_matches_ts(name):
    assert parse_metrics(payload_of(name)) == CASES[name]["parsed"]


def test_metrics_full_layout_and_units():
    """逐项盯住最容易漂移的四处：centi-dB 除 100、-32768 当"没测过"、
    obw 的 kHz→Hz、以及 dsp_us 已经超出旧 u16 量程这件事。"""
    m = parse_metrics(payload_of("metrics_full_3ch"))
    assert m["floorBin"] == -68.6
    assert m["dc"] == -38.0
    assert m["dspUs"] == 67602
    ch = m["channels"]
    assert ch[0]["clearFloor"] is None  # -32768 哨兵 = 这条信道从没清过
    assert ch[1]["clearFloor"] == -81.0
    assert ch[0]["obwLoHz"] == -4000000 and ch[0]["obwHiHz"] == 4400000
    assert [c["id"] for c in ch] == [2, 4, 7]


@pytest.mark.parametrize("name", LEVELS_CASES)
def test_levels_frame_matches_ts(name):
    assert parse_levels(payload_of(name)) == CASES[name]["parsed"]


def test_levels_mode_byte_zero_is_full_because_that_is_what_ts_does():
    """`LEVELS_MODES[0] === "FULL"`：这是既成事实，Python 不许"纠正"成别的。"""
    assert parse_levels(payload_of("levels_mode_0_is_full"))["mode"] == "FULL"


def test_levels_stops_at_channels_that_fit_and_does_not_fabricate():
    """nch 说 4 个但字节只放得下 2 个：TS 在 `o + 4 > byteLength` 处 break。
    多出来的通道必须是"不存在"，不能是 0 或 null 补出来的假读数。"""
    parsed = parse_levels(payload_of("levels_truncated_nch"))
    assert len(parsed["channels"]) == 2 == len(CASES["levels_truncated_nch"]["parsed"]["channels"])


@pytest.mark.parametrize("name", SPECTRUM_CASES)
def test_spectrum_frame_matches_ts(name):
    assert parse_spectrum(payload_of(name)) == CASES[name]["parsed"]


def test_spectrum_codes_compare_exactly_without_a_tolerance():
    """`code/2 - 127.5` 全是 0.5 的整数倍，float32 与 float64 都能精确表示，
    所以这里用**严格相等**——一旦哪天加了 pytest.approx，就等于放行漂移。"""
    bins = parse_spectrum(payload_of("spectrum_n_512"))["db"]
    assert bins == CASES["spectrum_n_512"]["parsed"]["db"]
    assert len(bins) == 512  # ②a 的 UART 上限就是 512 bins，必须整帧解得出


def test_spectrum_does_not_invent_a_field_from_the_reserved_byte():
    """偏移 19 的 reserved TS 不读，Python 也不得多读出一个字段来。"""
    assert set(parse_spectrum(payload_of("spectrum_n_3"))) == set(CASES["spectrum_n_3"]["parsed"])


def test_telemetry_frame_matches_ts():
    assert parse_telemetry(payload_of("telemetry")) == CASES["telemetry"]["parsed"]


def test_telemetry_scaling_is_per_mille_not_per_cent():
    t = parse_telemetry(payload_of("telemetry"))
    assert t["rotationHz"] == 3.456  # rotation_mhz / 1000
    assert t["linkFill"] == 0.25  # link_fill_pm / 1000
    assert t["running"] is True


def test_rawiq_chunk_header_matches_ts():
    case = CASES["rawiq_chunk_header_fields"]
    got = parse_rawiq_chunk(payload_of("rawiq_chunk_header_fields"))
    assert got == case["parsed"]
    assert (got["offset"] + got["count"] >= got["n"]) == case["is_last"]


@pytest.mark.parametrize(
    "name", ["rawiq_snapshot_assembled", "rawiq_missing_chunk_drops_snapshot"]
)
def test_rawiq_assembler_matches_ts_over_the_same_chunks(name):
    """逐块喂 golden 的分块序列，**每一步的返回都必须与 TS 一致**（含"这一步还没有快照"）。

    缺块那条尤其要紧：TS 是"整快照作废 + 从 offset 0 重新开始"。
    拼出半张混了旧快照的图，比直接报错更难被发现。
    """
    case = CASES[name]
    chunks = [bytes.fromhex(h) for h in case["hex"].split(":")]
    assembler = RawIqAssembler()
    assert [assembler.add(chunk) for chunk in chunks] == case["parsed"]


def test_assembler_forgets_the_previous_snapshot_when_a_new_one_starts():
    """新快照（offset 0）必须覆盖半张旧图，不能把两次的样本接在一起。"""
    chunks = [bytes.fromhex(h) for h in CASES["rawiq_snapshot_assembled"]["hex"].split(":")]
    assembler = RawIqAssembler()
    assembler.add(chunks[0])  # 2500 点的头一块
    restart = assembler.add(bytes.fromhex(CASES["rawiq_missing_chunk_drops_snapshot"]["hex"].split(":")[2]))
    assert restart is not None and restart["n"] == 100


def test_a_log_frame_has_no_decoder_and_does_not_raise():
    """TS 侧根本没有 parseLog：帧照样交出来，但没人解。Python 必须一样不崩。"""
    assert CASES["log_frame_no_decoder"]["parsed"] is None
    # LOG 帧的 payload 就是文本，不是结构体——认不出类型也不能抛异常。
    assert payload_of("log_frame_no_decoder") == b"LOG hello\n"
    assert decode(FRAME_LOG, payload_of("log_frame_no_decoder")) is None


STREAM_NAMES = [
    "text_frame_text_interleaved",
    "frame_split_across_chunks",
    "corrupted_frame_then_good_one",
    "joined_mid_frame_tail_ignored",
    "leading_garbage_needs_resync",
    "sync0_without_sync1",
    "empty_stream",
    "spectrum_on_uart_cap_512",
]


@pytest.mark.parametrize("name", STREAM_NAMES)
def test_stream_framing_matches_ts(name):
    want = STREAMS[name]
    stream = FrameStream()
    got = []
    for chunk in want["feed"]:
        for item in stream.push(bytes.fromhex(chunk)):
            got.append(
                {"kind": "text", "line": item.line}
                if item.kind == "text"
                else {
                    "kind": "frame",
                    "type": item.type,
                    "payload_hex": item.payload.hex(),
                    "payload_len": len(item.payload),
                }
            )
    assert got == want["items"]
    assert (stream.crc_errors, stream.resync_bytes, stream.bytes_in) == (
        want["counters"]["crcErrors"],
        want["counters"]["resyncBytes"],
        want["counters"]["bytes"],
    )


def test_a_bad_crc_resyncs_one_byte_at_a_time_and_keeps_the_next_frame():
    """坏帧不能让解析器失聪：计数要涨，下一帧照收——TS 就是这个行为。"""
    want = STREAMS["corrupted_frame_then_good_one"]
    assert want["counters"]["crcErrors"] == 1
    assert len(want["items"]) == 2


def test_the_version_byte_is_not_validated_by_anybody():
    """TS 从不读偏移 3 的版本字节，只靠 CRC 认帧。

    这一条钉住的是**事实**，不是愿望：R20 想要的"版本不符就拒绝"今天不存在。
    要做版本协商得改 web/src/serial/parser.ts —— 那是 S3 出口的申报偏差，
    不在这里悄悄加一条 Python 独有的校验（那样两份镜像就分叉了）。
    """
    case = CASES["version_byte_2_still_parses"]
    whole = bytes.fromhex(case["whole_hex"])
    assert whole[3] == 2
    stream = FrameStream()
    frames = [item for item in stream.push(whole) if item.kind == "frame"]
    assert len(frames) == 1
    assert stream.crc_errors == 0
    assert parse_spectrum(frames[0].payload) == case["parsed"]


@pytest.mark.parametrize(
    "name,type_code",
    [
        ("metrics_full_3ch", FRAME_METRICS),
        ("levels_mode_2", FRAME_LEVELS),
        ("spectrum_n_8", FRAME_SPECTRUM),
        ("telemetry", FRAME_TELEMETRY),
        ("log_frame_no_decoder", FRAME_LOG),
        ("rawiq_chunk_header_fields", FRAME_RAWIQ),
    ],
)
def test_the_type_codes_are_the_ones_frames_h_declares(name, type_code):
    """类型码写错一位，对拍会"全绿但解错帧"——所以把码也钉住。"""
    assert CASES[name]["type"] == type_code
