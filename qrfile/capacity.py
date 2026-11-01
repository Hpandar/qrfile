"""QR 版本容量表与编解码 profile。纯数据，零依赖。

容量从 **数据码字数**（ISO/IEC 18004 Table 9 的规范常量）推导，而不是硬编码
每个格子 —— 后者容易抄错。推导公式见 :func:`byte_capacity`。

自检：``tests/test_capacity.py`` 会把这张表跟 segno 自己的容量报告逐个版本对照，
所以表错了一定会被测出来，不靠人肉核对。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import InputError

ECC_LEVELS = ("L", "M", "Q", "H")

# 各 version / 纠错等级下的**数据码字数**（不含纠错码字）。
# 校验方式：L+M 的 data + ec == 该 version 的总码字数，且 v40 四档都是 3706。
DATA_CODEWORDS: dict[int, dict[str, int]] = {
    1: {"L": 19, "M": 16, "Q": 13, "H": 9},
    2: {"L": 34, "M": 28, "Q": 22, "H": 16},
    3: {"L": 55, "M": 44, "Q": 34, "H": 26},
    4: {"L": 80, "M": 64, "Q": 48, "H": 36},
    5: {"L": 108, "M": 86, "Q": 62, "H": 46},
    6: {"L": 136, "M": 108, "Q": 76, "H": 60},
    7: {"L": 156, "M": 124, "Q": 88, "H": 66},
    8: {"L": 194, "M": 154, "Q": 110, "H": 86},
    9: {"L": 232, "M": 182, "Q": 132, "H": 100},
    10: {"L": 274, "M": 216, "Q": 154, "H": 122},
    11: {"L": 324, "M": 254, "Q": 180, "H": 140},
    12: {"L": 370, "M": 290, "Q": 206, "H": 158},
    13: {"L": 428, "M": 334, "Q": 244, "H": 180},
    14: {"L": 461, "M": 365, "Q": 261, "H": 197},
    15: {"L": 523, "M": 415, "Q": 295, "H": 223},
    16: {"L": 589, "M": 453, "Q": 325, "H": 253},
    17: {"L": 647, "M": 507, "Q": 367, "H": 283},
    18: {"L": 721, "M": 563, "Q": 397, "H": 313},
    19: {"L": 795, "M": 627, "Q": 445, "H": 341},
    20: {"L": 861, "M": 669, "Q": 485, "H": 385},
    21: {"L": 932, "M": 714, "Q": 512, "H": 406},
    22: {"L": 1006, "M": 782, "Q": 568, "H": 442},
    23: {"L": 1094, "M": 860, "Q": 614, "H": 464},
    24: {"L": 1174, "M": 914, "Q": 664, "H": 514},
    25: {"L": 1276, "M": 1000, "Q": 718, "H": 538},
    26: {"L": 1370, "M": 1062, "Q": 754, "H": 596},
    27: {"L": 1468, "M": 1128, "Q": 808, "H": 628},
    28: {"L": 1531, "M": 1193, "Q": 871, "H": 661},
    29: {"L": 1631, "M": 1267, "Q": 911, "H": 701},
    30: {"L": 1735, "M": 1373, "Q": 985, "H": 745},
    31: {"L": 1843, "M": 1455, "Q": 1033, "H": 793},
    32: {"L": 1955, "M": 1541, "Q": 1115, "H": 845},
    33: {"L": 2071, "M": 1631, "Q": 1171, "H": 901},
    34: {"L": 2191, "M": 1725, "Q": 1231, "H": 961},
    35: {"L": 2306, "M": 1812, "Q": 1286, "H": 986},
    36: {"L": 2434, "M": 1914, "Q": 1354, "H": 1054},
    37: {"L": 2566, "M": 1992, "Q": 1426, "H": 1096},
    38: {"L": 2702, "M": 2102, "Q": 1502, "H": 1142},
    39: {"L": 2812, "M": 2216, "Q": 1582, "H": 1222},
    40: {"L": 2956, "M": 2334, "Q": 1666, "H": 1276},
}

MIN_VERSION = 1
MAX_VERSION = 40

#: 模块数 = 4 * version + 17
def modules_per_side(version: int) -> int:
    return 4 * version + 17


def byte_capacity(version: int, ecc: str) -> int:
    """某 version / 纠错等级下，**字节模式**能装的最大字节数。

    字节模式的位开销 = 4 bit 模式指示符 + 计数指示符（version 1-9 为 8 bit，
    10-40 为 16 bit）。剩余位全给数据，向下取整到整数字节。
    """
    ecc = ecc.upper()
    if ecc not in ECC_LEVELS:
        raise InputError(f"纠错等级必须是 {'/'.join(ECC_LEVELS)} 之一，收到 {ecc!r}")
    if version not in DATA_CODEWORDS:
        raise InputError(f"QR version 必须在 {MIN_VERSION}-{MAX_VERSION} 之间，收到 {version}")
    count_indicator_bits = 8 if version <= 9 else 16
    available = DATA_CODEWORDS[version][ecc] * 8 - 4 - count_indicator_bits
    return available // 8


@dataclass(frozen=True, slots=True)
class Profile:
    """一组编解码参数。决定"一张码能装多少"和"有多好扫"。"""

    name: str
    version_max: int
    ecc: str
    description: str

    @property
    def byte_capacity(self) -> int:
        """该 profile 下一张码能装的最大字节数（含分片头）。"""
        return byte_capacity(self.version_max, self.ecc)

    @property
    def body_capacity(self) -> int:
        """该 profile 下一张码能装的**净载荷**字节数（扣掉 32 字节分片头）。"""
        from .chunk import HEADER_SIZE

        return self.byte_capacity - HEADER_SIZE


#: 默认 profile —— 主链路是「屏幕显示 + 手机扫」，屏幕才是瓶颈。
DEFAULT_PROFILE = "screen"

PROFILES: dict[str, Profile] = {
    "camera": Profile(
        "camera", 15, "Q", "手机实时扫屏幕，容错优先，码小好认"
    ),
    "screen": Profile(
        "screen", 20, "L", "屏幕显示 + 拍照后识别（默认）"
    ),
    "print": Profile(
        "print", 30, "M", "A4 打印后扫纸。打印不是瓶颈，可以放宽"
    ),
    "max": Profile(
        "max", 40, "L", "纯数字归档，不经摄像头，码数量最少"
    ),
}


def get_profile(name: str) -> Profile:
    try:
        return PROFILES[name]
    except KeyError:
        raise InputError(
            f"未知 profile {name!r}，可选：{'、'.join(PROFILES)}"
        ) from None


#: 每张码的**人工扫描**耗时粗估（秒），给 ``plan`` 命令做时长预估用。
#:
#: 这是粗估 —— 含对焦、翻页 / 挪手机的时间，实际因人因设备而异，可能差一倍。
#: 但它的用处不是精确，而是**防呆**：让用户在跑之前就意识到"哦，这要扫一个小时"。
#: ``None`` 表示这条链路不经摄像头，没有人工环节。
SCAN_SECONDS_PER_CODE: dict[str, float | None] = {
    "camera": 1.5,
    "screen": 2.5,
    "print": 5.0,
    "max": None,
}


def format_duration(seconds: float) -> str:
    """把秒数说成人话。"""
    if seconds < 60:
        return f"{seconds:.0f} 秒"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.0f} 分钟"
    hours = minutes / 60
    if hours < 24:
        return f"{hours:.1f} 小时"
    return f"{hours / 24:.1f} 天"
