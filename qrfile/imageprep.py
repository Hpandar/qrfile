"""照片 / 截图解码的预处理。

主链路是「屏幕显示 + 手机拍」，所以进来的是带 EXIF、可能有反色、对比度不均、
对焦不实、有旋转的**照片**，不是我们自己生成的干净 PNG。

**这个模块刻意不做几何纠正**，理由是实测出来的（数字见下面，可复现）：

===============================================  ==========================
劣化类型                                          结果
===============================================  ==========================
JPEG q25~q60、旋转 ≤5°、高斯模糊、调暗 40%        ✅ 能解
缩到 2 px/模块（300px 显示 v20）                  ✅ 能解
反色（深色模式截图）                              ✅ 能解（try_invert 默认开）
梯形畸变 5%                                       ✅ camera / screen 能解
**梯形畸变 ≥10%**                                 ❌ **怎么都解不出**
===============================================  ==========================

对付 ≥10% 畸变，我把能试的都试了：**四种 binarizer 全试一遍、放大 2×、换四个
profile** —— 全部无效。透视纠正需要先可靠地定位二维码的四个角，那等于重写一遍
检测器；而 zxing 自己都定位不到时，外部也很难更可靠。这属于"复杂度不值当"的
范畴，所以不做。

于是这个模块只做**安全的多次尝试**：同一个图换几种渲染 + 几种二值化再解一遍。
之所以敢多试，是因为每个分片都自带 magic + CRC32 —— **解错了会被校验挡掉**，
多试不可能制造出假结果，只可能多找回真结果。

**但必须如实说明：实测下来这套多次尝试基本没有收益。**

在 12 种劣化场景上量过（干净 PNG、JPEG q25/q65、旋转、缩放、高斯模糊、
调暗 40%、低对比度、反色、阴影渐变 0.8/0.9、半边强阴影、极暗 ×0.3、极亮、
打印质量 150px），**快路径（单次 LocalAverage 解码）全部直接成功**，
robust 从来没有走到第二个变体。原因是：

* zxing-cpp 的 ``try_rotate`` / ``try_downscale`` / ``try_invert`` **本来就是默认开的**；
* ``LocalAverage`` 是自适应二值化，本来就能扛住上述光照极端情况。

而代价是实测 **2.7 倍**的解码耗时（5 张图 115ms → 316ms）。

所以：**默认走快路径**，这个模块的多次尝试只在 ``--robust`` 时启用，
留给"我这张图就是解不出来、想多试一把"的场合。它真正的价值其实是下面那个
:func:`describe_failure` —— 解不出来时告诉用户**该怎么做**。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

import zxingcpp
from PIL import Image, ImageOps

from .decode import DecodedSymbol

#: 二值化算法。BoolCast 是给已经是 1-bit 的输入用的，对照片没意义，故不列。
BINARIZERS: tuple[tuple[str, object], ...] = (
    ("LocalAverage", zxingcpp.Binarizer.LocalAverage),
    ("GlobalHistogram", zxingcpp.Binarizer.GlobalHistogram),
    ("FixedThreshold", zxingcpp.Binarizer.FixedThreshold),
)

#: 小于这个宽度才值得放大 —— 大照片放大只会白花时间（实测无收益）
UPSCALE_THRESHOLD = 1200

#: 总共最多尝试多少次解码（变体 × 二值化的组合数）
MAX_ATTEMPTS = 9

#: 连续多少次尝试毫无收获就收工
BARREN_LIMIT = 2


@dataclass(frozen=True, slots=True)
class DecodeOutcome:
    symbols: tuple[DecodedSymbol, ...]
    strategy: str  # 描述是哪一种组合奏效的；全部无效时为空
    attempts: int


def prepare_variants(image: Image.Image) -> list[tuple[str, Image.Image]]:
    """按"最可能先成功"的顺序给出候选图。干净图排在最前面。"""
    variants: list[tuple[str, Image.Image]] = [("原图", image)]

    # EXIF 方向：手机竖拍常带 Orientation 标记
    try:
        transposed = ImageOps.exif_transpose(image)
        if transposed is not None and transposed.size != image.size:
            variants.append(("EXIF 方向校正", transposed))
    except Exception:
        pass

    gray = image.convert("L") if image.mode != "L" else image

    # 对比度拉伸：屏幕反光 / 曝光不足时有用
    try:
        variants.append(("自动对比度", ImageOps.autocontrast(gray)))
    except Exception:
        pass

    # 小图放大：低分辨率截图里单模块不足 4px 时有用
    if image.size[0] < UPSCALE_THRESHOLD:
        factor = 2 if image.size[0] * 2 <= UPSCALE_THRESHOLD * 2 else 1
        if factor > 1:
            variants.append(
                (f"放大 {factor}×", gray.resize(
                    (gray.size[0] * factor, gray.size[1] * factor), Image.LANCZOS
                ))
            )

    return variants


def decode_robust(
    image: Image.Image,
    *,
    source: str = "",
    max_attempts: int = MAX_ATTEMPTS,
) -> DecodeOutcome:
    """多渲染变体 × 多二值化地解，返回所有解出的分片（按载荷去重）。

    **安全性**：多试不会带来错数据的风险 —— 每个分片自带 magic + CRC32，
    解错了在容器层就被挡掉。所以这里可以放心地"宁可多试"。
    """
    collected: dict[bytes, DecodedSymbol] = {}
    strategy = ""
    attempts = 0
    barren = 0

    for variant_name, candidate in prepare_variants(image):
        if attempts >= max_attempts or barren >= BARREN_LIMIT:
            break

        before = len(collected)
        for bin_name, binarizer in BINARIZERS:
            if attempts >= max_attempts:
                break
            attempts += 1
            try:
                results = zxingcpp.read_barcodes(
                    candidate,
                    formats=zxingcpp.BarcodeFormat.QRCode,
                    binarizer=binarizer,
                )
            except Exception:
                continue

            for result in results:
                if result.format != zxingcpp.BarcodeFormat.QRCode:
                    continue
                payload = bytes(result.bytes)
                if payload not in collected:
                    collected[payload] = DecodedSymbol(
                        payload=payload, format=str(result.format), source=source
                    )
                    if not strategy:
                        strategy = f"{variant_name} + {bin_name}"

        if len(collected) == before:
            barren += 1
        else:
            barren = 0

    return DecodeOutcome(
        symbols=tuple(collected.values()), strategy=strategy, attempts=attempts
    )


def describe_failure(paths: Sequence[str]) -> str:
    """解不出内容时给用户一段**可操作**的话，而不是干巴巴的"失败了"。"""
    return (
        f"{len(paths)} 张图解不出任何 qrfile 分片。按实测，最可能的原因是"
        f"拍摄时的透视畸变太大（超过约 10% 就解不出来了，换二值化算法和放大都救不回来）。\n"
        f"可以这样试：\n"
        f"  1. 手机尽量正对屏幕，别从侧面斜着拍\n"
        f"  2. 让二维码在画面里占得更大一些（单模块至少 4 像素）\n"
        f"  3. 重新编码时用 --profile camera（每张码模块更少、单模块更大，最抗畸变）\n"
        f"  4. 或者干脆用 --profile print 打印出来再扫 —— 纸面的几何形状你能完全控制"
    )
