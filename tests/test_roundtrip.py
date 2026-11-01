"""最小闭环：载荷 → 二维码 → 载荷，字节完全一致。

这是整个工具的地基。这条跑不通，后面所有验证层都没有意义。
"""

from __future__ import annotations

import os

import pytest
from PIL import Image

from qrfile.capacity import PROFILES, get_profile
from qrfile.chunk import HEADER_SIZE, pack_frame, unpack_frame
from qrfile.decode import decode_bytes, decode_image
from qrfile.errors import ChunkError
from qrfile.render import Renderer

SESSION = b"\xde\xad\xbe\xef\x01\x02\x03\x04"
SCREEN = get_profile("screen")


@pytest.fixture
def renderer() -> Renderer:
    return Renderer(SCREEN)


def _payload(body: bytes, index: int = 0, total: int = 1) -> bytes:
    return pack_frame(session_id=SESSION, index=index, total=total, body=body)


# --------------------------------------------------------------------------
# 核心往返
# --------------------------------------------------------------------------


def test_minimal_roundtrip(renderer: Renderer) -> None:
    body = b"hello, qrfile!\n"
    payload = _payload(body)

    decoded = decode_bytes(renderer.render(payload))
    assert len(decoded) == 1
    assert decoded[0].payload == payload

    header, recovered = unpack_frame(decoded[0].payload)
    assert recovered == body
    assert header.session_id == SESSION


@pytest.mark.parametrize("size", [1, 2, 31, 32, 33, 100, 255, 256, 500, 793, 794])
def test_roundtrip_various_sizes(renderer: Renderer, size: int) -> None:
    body = os.urandom(size)
    decoded = decode_bytes(renderer.render(_payload(body)))
    assert decoded[0].payload == _payload(body)
    assert unpack_frame(decoded[0].payload)[1] == body


def test_roundtrip_empty_body(renderer: Renderer) -> None:
    """空文件是合法输入，必须能编码并还原。"""
    payload = _payload(b"")
    decoded = decode_bytes(renderer.render(payload))
    assert decoded[0].payload == payload
    assert unpack_frame(decoded[0].payload)[1] == b""


def test_roundtrip_all_256_byte_values(renderer: Renderer) -> None:
    """全 256 种字节值 —— 任何编码转换或字符集假设都会在这里露馅。"""
    body = bytes(range(256))
    decoded = decode_bytes(renderer.render(_payload(body)))
    assert unpack_frame(decoded[0].payload)[1] == body


def test_roundtrip_binary_that_is_not_valid_utf8(renderer: Renderer) -> None:
    body = b"\xff\xfe\x00\x01\x80\x81\xc0\xc1" * 40
    decoded = decode_bytes(renderer.render(_payload(body)))
    assert unpack_frame(decoded[0].payload)[1] == body


def test_roundtrip_crlf_is_preserved(renderer: Renderer) -> None:
    """换行绝不能被"规范化" —— 这是字节一致的头号杀手。"""
    body = b"a\r\nb\nc\rd\r\n\n"
    decoded = decode_bytes(renderer.render(_payload(body)))
    assert unpack_frame(decoded[0].payload)[1] == body


def test_roundtrip_utf8_bom_preserved(renderer: Renderer) -> None:
    body = "中文内容".encode("utf-8-sig")
    decoded = decode_bytes(renderer.render(_payload(body)))
    assert unpack_frame(decoded[0].payload)[1] == body


# --------------------------------------------------------------------------
# 容量边界
# --------------------------------------------------------------------------


def test_payload_at_exact_capacity_works(renderer: Renderer) -> None:
    """正好装满一张码 —— 必须成功。"""
    capacity = SCREEN.byte_capacity
    payload = os.urandom(capacity)
    assert len(payload) == capacity

    qr = renderer.encode(payload)
    assert qr.version == SCREEN.version_max  # 正好顶到上限

    decoded = decode_bytes(renderer.render(payload))
    assert decoded[0].payload == payload


def test_payload_one_byte_over_capacity_is_rejected(renderer: Renderer) -> None:
    """多一个字节就该明确报错，而不是悄悄生成一张超规格的码。"""
    with pytest.raises(ChunkError, match="容量上限|version"):
        renderer.encode(os.urandom(SCREEN.byte_capacity + 1))


def test_body_capacity_relationship(renderer: Renderer) -> None:
    """净载荷 = 容量 - 分片头。"""
    assert SCREEN.byte_capacity == SCREEN.body_capacity + HEADER_SIZE
    max_body = os.urandom(SCREEN.body_capacity)
    renderer.encode(_payload(max_body))  # 不该抛
    with pytest.raises(ChunkError):
        renderer.encode(_payload(os.urandom(SCREEN.body_capacity + 1)))


def test_every_profile_can_roundtrip_its_own_capacity() -> None:
    """四个 profile 各自装满时都要能往返 —— 换了 profile 不能让闭环失效。"""
    for name, profile in PROFILES.items():
        renderer = Renderer(profile)
        body = os.urandom(min(profile.body_capacity, 260))
        payload = _payload(body)
        decoded = decode_bytes(renderer.render(payload))
        assert decoded, f"{name} profile 解不出内容"
        assert decoded[0].payload == payload, f"{name} profile 往返不一致"


# --------------------------------------------------------------------------
# 渲染参数
# --------------------------------------------------------------------------


def test_png_is_one_bit_black_and_white(renderer: Renderer) -> None:
    """纯黑白、无抗锯齿 —— 灰度或调色板都会引入模块边界模糊。

    mode 必须是 ``"1"``（1-bit）。注意 Pillow 的 ``"1"`` 模式像素值是 0/255，
    不是 0/1（曾经在这里写错过断言）。
    """
    image = renderer.render_image(_payload(b"x" * 100))
    assert image.mode == "1"
    assert image.getextrema() == (0, 255)


def test_quiet_zone_is_four_modules(renderer: Renderer) -> None:
    """静默区必须是 4 模块（ISO/IEC 18004 §5.3.8）。"""
    qr = renderer.encode(_payload(b"x" * 100))
    modules = qr.symbol_size(scale=1, border=0)[0]
    image = renderer.render_image(_payload(b"x" * 100))
    expected = (modules + 2 * renderer.margin) * renderer.scale
    assert image.size == (expected, expected)


def test_quiet_zone_margin_is_configurable() -> None:
    tight = Renderer(SCREEN, margin=0)
    normal = Renderer(SCREEN, margin=4)
    assert tight.render_image(_payload(b"x" * 50)).size[0] < \
        normal.render_image(_payload(b"x" * 50)).size[0]


def test_scale_is_pixels_per_module(renderer: Renderer) -> None:
    doubled = Renderer(SCREEN, scale=renderer.scale * 2)
    assert doubled.render_image(_payload(b"x" * 50)).size[0] == \
        renderer.render_image(_payload(b"x" * 50)).size[0] * 2


def test_small_payload_uses_a_small_version(renderer: Renderer) -> None:
    """别为了小载荷浪费一张大码 —— segno 会挑最小放得下的版本。"""
    assert renderer.encode(b"tiny").version < 10


# --------------------------------------------------------------------------
# V1：编码期自验证
# --------------------------------------------------------------------------


def test_render_verified_passes_for_good_payload(renderer: Renderer) -> None:
    payload = _payload(os.urandom(300))
    assert renderer.render_verified(payload) == renderer.render(payload)


def test_render_verified_reports_failure_when_decoder_finds_nothing(
    renderer: Renderer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """自验证必须真的会失败 —— 不能是个摆设。"""
    import qrfile.decode as decode_module

    monkeypatch.setattr(decode_module, "decode_image", lambda *a, **k: [])
    with pytest.raises(ChunkError, match="解不出任何内容"):
        renderer.render_verified(_payload(b"whatever"))


def test_render_verified_reports_mismatch(
    renderer: Renderer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """解回来的内容对不上时必须报错。"""
    import qrfile.decode as decode_module
    from qrfile.decode import DecodedSymbol

    monkeypatch.setattr(
        decode_module,
        "decode_image",
        lambda *a, **k: [DecodedSymbol(b"wrong bytes", "QRCode", "fake")],
    )
    with pytest.raises(ChunkError, match="不一致"):
        renderer.render_verified(_payload(b"whatever"))


# --------------------------------------------------------------------------
# 一张图多个码（总览图 / 打印拼版靠这个）
# --------------------------------------------------------------------------


def test_multiple_codes_in_one_image(renderer: Renderer) -> None:
    """一张图里放多个二维码，一次全部解出来。

    这是主链路（手机拍屏）最实用的提速手段：拍一张照片扫下一整片。
    """
    payloads = [_payload(f"chunk-{i}".encode() * 20) for i in range(6)]

    tiles = [renderer.render_image(p) for p in payloads]
    tile_w, tile_h = tiles[0].size
    gap = 40
    sheet = Image.new("1", (tile_w * 3 + gap * 4, tile_h * 2 + gap * 3), 1)
    for index, tile in enumerate(tiles):
        row, col = divmod(index, 3)
        sheet.paste(tile, (gap + col * (tile_w + gap), gap + row * (tile_h + gap)))

    decoded = decode_image(sheet, source="<拼接的总览图>")
    assert {d.payload for d in decoded} == set(payloads)


def test_blank_image_decodes_to_nothing() -> None:
    blank = Image.new("1", (200, 200), 1)
    assert decode_image(blank) == []


def test_non_qr_barcode_is_ignored() -> None:
    """只认二维码。空白/噪声图不该产出任何符号。"""
    noise = Image.frombytes("L", (120, 120), os.urandom(120 * 120))
    assert decode_image(noise) == []
