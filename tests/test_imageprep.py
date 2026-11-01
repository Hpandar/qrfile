"""照片链路：多渲染变体解码，以及"它其实没什么用"这个结论的固化。

这个文件里最要紧的不是"robust 能解出来"，而是**记录实测的边界** ——
哪些劣化快路径就够了、哪些怎么试都没用。免得以后有人以为 robust 是万灵药。
"""

from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter

from qrfile.capacity import get_profile
from qrfile.chunk import pack_frame
from qrfile.decode import decode_bytes, decode_image
from qrfile.imageprep import (
    BINARIZERS,
    MAX_ATTEMPTS,
    UPSCALE_THRESHOLD,
    decode_robust,
    describe_failure,
    prepare_variants,
)
from qrfile.render import Renderer

SESSION = b"\x01" * 8


def _code(profile_name: str = "screen", body: bytes | None = None) -> tuple[bytes, bytes]:
    """返回 (PNG 字节, 载荷)。"""
    profile = get_profile(profile_name)
    payload = pack_frame(
        session_id=SESSION, index=0, total=1, body=body or b"x" * profile.body_capacity
    )
    return Renderer(profile).render(payload), payload


def _gray(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png)).convert("L")


def _jpeg(image: Image.Image, quality: int = 65) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def _keystone(image: Image.Image, strength: float, pad: int = 160) -> Image.Image:
    """把码拍成梯形，但四个角都留在画面内（不能裁掉 —— 裁掉就无从解起了）。"""
    width, height = image.size
    canvas = (width + 2 * pad, height + 2 * pad)
    corner = pad
    narrow = strength * height / 2
    dst = [(corner, corner), (corner + width, corner),
           (corner + width, corner + height), (corner, corner + height)]
    src = [(corner + narrow, corner), (corner + width - narrow, corner),
           (corner + width, corner + height), (corner, corner + height)]

    matrix = []
    for (x, y), (u, v) in zip(dst, src):
        matrix.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        matrix.append([0, 0, 0, x, y, 1, -v * x, -v * y])
    coeffs = np.linalg.solve(np.array(matrix, float), np.array(src, float).reshape(8))
    return image.transform(canvas, Image.PERSPECTIVE, coeffs, Image.BICUBIC, fillcolor=255)


# --------------------------------------------------------------------------
# prepare_variants
# --------------------------------------------------------------------------


def test_first_variant_is_always_the_original() -> None:
    """干净的图应该第一次就解出来 —— 别让预处理挡在前面。"""
    name, image = prepare_variants(_gray(_code()[0]))[0]
    assert name == "原图"
    assert image.mode == "L"


def test_variants_are_all_images() -> None:
    for name, image in prepare_variants(_gray(_code()[0])):
        assert isinstance(image, Image.Image), name


def test_small_image_gets_an_upscale_variant() -> None:
    small = _gray(_code()[0]).resize((400, 400), Image.LANCZOS)
    names = [name for name, _ in prepare_variants(small)]
    assert any("放大" in name for name in names)


def test_large_image_does_not_get_upscaled() -> None:
    """大照片放大只会白花时间 —— 实测无收益。"""
    big = _gray(_code()[0]).resize((UPSCALE_THRESHOLD + 200, UPSCALE_THRESHOLD + 200))
    names = [name for name, _ in prepare_variants(big)]
    assert not any("放大" in name for name in names)


def test_rgb_input_produces_a_grayscale_variant() -> None:
    """彩色输入（真实照片）必须产出灰度变体 —— 二值化只在单通道上做。"""
    png, _ = _code()
    rgb = Image.open(io.BytesIO(png)).convert("RGB")
    variants = prepare_variants(rgb)
    assert variants[0][1] is rgb  # 原图原样保留
    assert any(image.mode == "L" for _, image in variants[1:])


# --------------------------------------------------------------------------
# decode_robust
# --------------------------------------------------------------------------


def test_decode_robust_handles_a_clean_code() -> None:
    png, payload = _code()
    outcome = decode_robust(_gray(png))
    assert [s.payload for s in outcome.symbols] == [payload]
    assert outcome.strategy
    assert outcome.attempts >= 1


def test_decode_robust_returns_empty_on_garbage() -> None:
    noise = Image.frombytes("L", (200, 200), bytes(np.random.default_rng(0).integers(0, 256, 40000, dtype=np.uint8)))
    outcome = decode_robust(noise)
    assert outcome.symbols == ()


def test_decode_robust_deduplicates_payloads() -> None:
    """同一张图在多个变体下都解出同一个码 —— 结果里只能有一份。"""
    png, payload = _code()
    outcome = decode_robust(_gray(png))
    assert len(outcome.symbols) == len({s.payload for s in outcome.symbols})


def test_decode_robust_respects_the_attempt_budget() -> None:
    png, _ = _code()
    outcome = decode_robust(_gray(png), max_attempts=1)
    assert outcome.attempts <= 1


def test_decode_robust_is_safe_by_construction() -> None:
    """多试不可能造出假结果：解出来的每一个载荷都必须能被分片头解析。"""
    from qrfile.chunk import unpack_frame

    png, payload = _code()
    for symbol in decode_robust(_gray(png)).symbols:
        unpack_frame(symbol.payload)  # 不该抛


# --------------------------------------------------------------------------
# 实测边界的固化 —— 这些数字是"不要再指望更多"的证据
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "build"),
    [
        ("JPEG q25", lambda im: _jpeg(im, 25)),
        ("旋转 5°", lambda im: _jpeg(im.rotate(5, expand=True, fillcolor=255))),
        ("高斯模糊 1.2", lambda im: _jpeg(im.filter(ImageFilter.GaussianBlur(1.2)))),
        ("调暗 40%", lambda im: _jpeg(im.point(lambda v: int(v * 0.6 + 50)))),
        ("低对比度", lambda im: _jpeg(im.point(lambda v: int(v * 0.25 + 150)))),
        ("反色（深色模式）", lambda im: _jpeg(im.point(lambda v: 255 - v))),
        ("缩到 2px/模块", lambda im: _jpeg(im.resize((300, 300), Image.LANCZOS))),
        ("半边强阴影", lambda im: _jpeg(_half_shadow(im))),
        ("极暗 ×0.3", lambda im: _jpeg(im.point(lambda v: int(v * 0.3)))),
    ],
)
def test_fast_path_already_handles_common_degradation(label, build) -> None:
    """快路径（不用 robust）就能扛住这些 —— robust 在这类场景没有额外收益。"""
    png, payload = _code()
    degraded = build(_gray(png))
    got = decode_bytes(degraded)
    assert got and got[0].payload == payload, f"{label} 快路径解不出来"


def _half_shadow(image: Image.Image) -> Image.Image:
    overlay = image.copy()
    ImageDraw.Draw(overlay).rectangle(
        [0, 0, image.size[0] // 2, image.size[1]], fill=40
    )
    return Image.blend(image, overlay, 0.75)


def test_robust_adds_nothing_on_common_degradation() -> None:
    """把"robust 没收益"这件事固化下来。

    如果哪天换了 zxing-cpp 版本、这个断言开始失败，说明新版默认行为变弱了 ——
    那时才应该把 robust 变成默认。
    """
    png, payload = _code()
    degraded = _jpeg(_gray(png).filter(ImageFilter.GaussianBlur(1.2)))

    assert decode_bytes(degraded)[0].payload == payload  # 快路径就够
    outcome = decode_robust(Image.open(io.BytesIO(degraded)).convert("L"))
    assert outcome.symbols[0].payload == payload
    # 而且是在第一个变体、第一个二值化上就成功 —— 说明默认配置本来就是最优的
    assert outcome.strategy.startswith("原图")
    assert BINARIZERS[0][0] in outcome.strategy


def test_perspective_beyond_five_percent_is_a_known_limit() -> None:
    """已知边界：梯形畸变 ≥10% 怎么都解不出来。

    这不是"还没做"，是**量过之后决定不做** —— 四种二值化、放大 2×、四个 profile
    全试过，都无效。透视纠正要重写检测器。这个断言是为了防止有人误以为
    "再加点预处理就能行"。
    """
    png, payload = _code()
    bad = _jpeg(_keystone(_gray(png), 0.15))

    assert not decode_bytes(bad)  # 快路径不行
    assert not decode_robust(Image.open(io.BytesIO(bad)).convert("L")).symbols  # robust 也不行


def test_mild_perspective_is_fine() -> None:
    """5% 以内的梯形畸变是能扛的 —— 手别太斜就够用。"""
    png, payload = _code()
    mild = _jpeg(_keystone(_gray(png), 0.05))
    got = decode_bytes(mild)
    assert got and got[0].payload == payload


# --------------------------------------------------------------------------
# 失败提示
# --------------------------------------------------------------------------


def test_describe_failure_is_actionable() -> None:
    """解不出来时要告诉用户**怎么办**，而不是只说"失败了"。"""
    message = describe_failure(["a.png", "b.png"])
    assert "2 张图" in message
    assert "畸变" in message
    assert "--profile camera" in message
    assert "print" in message


# --------------------------------------------------------------------------
# 接口
# --------------------------------------------------------------------------


def test_decode_file_and_bytes_default_to_the_fast_path() -> None:
    """robust 不是默认 —— 实测没收益，不该让所有人付 3 倍代价。"""
    import inspect

    assert inspect.signature(decode_bytes).parameters["robust"].default is False

    from qrfile.decode import decode_file

    assert inspect.signature(decode_file).parameters["robust"].default is False


def test_robust_flag_also_works_through_decode_bytes() -> None:
    png, payload = _code()
    got = decode_bytes(_jpeg(_gray(png)), robust=True)
    assert got[0].payload == payload


def test_decode_image_fast_path_unchanged() -> None:
    png, payload = _code()
    assert decode_image(_gray(png))[0].payload == payload


def test_max_attempts_is_bounded() -> None:
    """尝试次数必须有上限，否则一张解不出的图会拖很久。"""
    assert MAX_ATTEMPTS <= 12
