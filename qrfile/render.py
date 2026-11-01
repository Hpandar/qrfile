"""把分片渲染成二维码图片。

参数选择（方案第三章）：

* **margin = 4** —— ISO/IEC 18004 §5.3.8 要求的静默区。GitZipQR 默认用 1 是错的，
  有实测显示 0 模块静默区的失败率超过 50%。
* **scale = 8 px/模块** —— ≥4 px/模块是可靠底线。v40 在 scale 8 下是 1480 px，
  满足"v40 至少 1080 px"的下限。
* **纯黑白、无抗锯齿** —— segno 的 PNG 输出是 1-bit 模式，这点已被测试固化。
* **boost_error 开启** —— 有富余空间时自动提升纠错等级，纯赚。对 archive_sha256
  没有影响（哈希算的是载荷，不是图）。

渲染完的图**必须**立刻解回来比对（方案 V1）。那是 :func:`Renderer.render_verified`
做的事，encode 流程默认走它。
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import segno
from PIL import Image

from .capacity import Profile
from .errors import ChunkError

DEFAULT_SCALE = 8
DEFAULT_MARGIN = 4

#: segno 的 save() 需要的关键字；抽出来方便测试断言
_PNG_KWARGS = {"dark": "#000000", "light": "#ffffff"}


@dataclass(frozen=True, slots=True)
class Renderer:
    """按某个 profile 把载荷渲染成二维码 PNG。"""

    profile: Profile
    scale: int = DEFAULT_SCALE
    margin: int = DEFAULT_MARGIN
    boost_error: bool = True

    # ---------------------------------------------------------------- 编码

    def encode(self, payload: bytes) -> segno.QRCode:
        """把载荷变成 segno 的 QR 对象。

        载荷超过 profile 的容量上限就报错 —— 这是调用方算错了分片大小，属于 bug，
        不该静默地生成一张超规格的码。
        """
        if not payload:
            raise ChunkError("载荷为空，没法渲染")
        try:
            qr = segno.make(
                payload,
                error=self.profile.ecc.lower(),
                mode="byte",
                micro=False,
                boost_error=self.boost_error,
            )
        except segno.DataOverflowError as exc:
            raise ChunkError(
                f"载荷 {len(payload)} 字节超出了 {self.profile.name} profile 的容量上限"
                f"（{self.profile.byte_capacity} 字节）：{exc}"
            ) from exc
        except ValueError as exc:
            raise ChunkError(f"载荷无法编码：{exc}") from exc

        if qr.version is None or qr.version > self.profile.version_max:
            raise ChunkError(
                f"segno 选了 version {qr.version}，超过了 {self.profile.name} profile "
                f"的上限 {self.profile.version_max}（载荷 {len(payload)} 字节）"
            )
        return qr

    # ---------------------------------------------------------------- 渲染

    def to_png(self, qr: segno.QRCode) -> bytes:
        buffer = io.BytesIO()
        qr.save(buffer, kind="png", scale=self.scale, border=self.margin, **_PNG_KWARGS)
        return buffer.getvalue()

    def to_image(self, qr: segno.QRCode) -> Image.Image:
        image = Image.open(io.BytesIO(self.to_png(qr)))
        image.load()
        return image

    def render(self, payload: bytes) -> bytes:
        """载荷 → PNG 字节。不做自验证。"""
        return self.to_png(self.encode(payload))

    def render_image(self, payload: bytes) -> Image.Image:
        """载荷 → PIL Image。自验证走这条可以省一次解码往返。"""
        return Image.open(io.BytesIO(self.render(payload)))

    # ---------------------------------------------------------------- V1

    def verify_image(self, image: Image.Image, payload: bytes) -> None:
        """V1 自验证：把刚渲染出来的图解回来，跟喂进去的逐字节比对。

        这是编码期的强制自检。能抓到：segno 的编码 bug、纠错等级容不下载荷、
        PNG 缩放/插值引入的模块畸变、掩码问题。
        """
        from .decode import decode_image  # 延迟导入，避免模块级循环依赖

        symbols = decode_image(image, source="<刚生成的这张码>")
        if not symbols:
            raise ChunkError(
                "编码期自验证失败：刚生成的二维码解不出任何内容。"
                "可能是纠错等级容不下载荷，或渲染参数有问题。"
            )
        decoded = symbols[0].payload
        if decoded != payload:
            raise ChunkError(
                f"编码期自验证失败：解回来的内容和喂进去的不一致"
                f"（喂进去 {len(payload)} 字节，解回来 {len(decoded)} 字节）。"
            )

    def render_verified(self, payload: bytes) -> bytes:
        """载荷 → PNG 字节，并且自验证。代价约等于编码两遍。"""
        png = self.render(payload)
        image = Image.open(io.BytesIO(png))
        image.load()
        self.verify_image(image, payload)
        return png

    def render_verified_image(self, payload: bytes) -> Image.Image:
        """载荷 → PIL Image，并且自验证。

        给"内存里直接拼整屏页"用 —— 省掉 PNG 编解码那一趟。
        """
        image = self.to_image(self.encode(payload))
        self.verify_image(image, payload)
        return image

    @staticmethod
    def tile_size(profile: Profile, *, scale: int, margin: int) -> int:
        """这个 profile 下一张码在屏幕上占多少像素（按最大版本算）。

        用来定整屏网格的格子大小 —— 必须按**最大版本**算，因为 segno 会为
        每个分片挑刚好装得下的最小版本，最后一个不满的分片会小一些。
        格子按最大算，小的码居中放，画面才是齐的。
        """
        modules = profile.version_max * 4 + 17
        return (modules + 2 * margin) * scale


def renderer_for(profile: Profile, *, scale: int = DEFAULT_SCALE, margin: int = DEFAULT_MARGIN) -> Renderer:
    return Renderer(profile=profile, scale=scale, margin=margin)
