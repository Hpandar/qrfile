"""容量表的正确性。

分两层验证：

1. 对几个**公开的权威数字**做硬断言（v40-L 是 2953 等）。
2. 把整张表跟 **segno 自己**的容量逐个版本对照 —— 二进制搜出 segno 能接受的最大
   字节数，跟我们算出来的比。这样表抄错了会被测出来，不用人肉核对。
"""

from __future__ import annotations

import pytest
import segno

from qrfile.capacity import (
    DATA_CODEWORDS,
    ECC_LEVELS,
    PROFILES,
    byte_capacity,
    modules_per_side,
)
from qrfile.errors import InputError

#: 公开资料里的字节模式容量（ISO/IEC 18004 Table 7）。
#: 这里挑了几个跨越 version 1-9（8 bit 计数指示符）和 10-40（16 bit）的值。
PUBLISHED = [
    (1, "L", 17),
    (1, "H", 7),
    (5, "L", 106),
    (9, "L", 230),
    (10, "L", 271),   # 计数指示符从 8 bit 跳到 16 bit 的分界
    (15, "Q", 292),
    (20, "L", 858),
    (30, "M", 1370),
    (40, "L", 2953),
    (40, "M", 2331),
    (40, "Q", 1663),
    (40, "H", 1273),
]


@pytest.mark.parametrize(("version", "ecc", "expected"), PUBLISHED)
def test_published_capacities(version: int, ecc: str, expected: int) -> None:
    assert byte_capacity(version, ecc) == expected


def _max_bytes_segno(version: int, ecc: str) -> int:
    """二分搜出 segno 能塞进这个 version/ECC 的最大字节数。"""
    low, high = 0, 4096
    while low < high:
        mid = (low + high + 1) // 2
        try:
            segno.make(b"\x00" * mid, error=ecc, version=version, mode="byte", boost_error=False)
        except segno.DataOverflowError:
            high = mid - 1
        else:
            low = mid
    return low


#: 全表对照比较慢，挑一组有代表性的版本
SPOT_CHECK_VERSIONS = [1, 2, 5, 9, 10, 15, 20, 25, 30, 35, 40]


@pytest.mark.parametrize("version", SPOT_CHECK_VERSIONS)
@pytest.mark.parametrize("ecc", ECC_LEVELS)
def test_matches_segno(version: int, ecc: str) -> None:
    assert byte_capacity(version, ecc) == _max_bytes_segno(version, ecc), (
        f"version {version} / ECC {ecc} 的容量跟 segno 对不上"
    )


def test_table_is_complete() -> None:
    assert set(DATA_CODEWORDS) == set(range(1, 41))
    for version, row in DATA_CODEWORDS.items():
        assert set(row) == set(ECC_LEVELS), f"version {version} 的纠错等级不全"


def test_capacity_decreases_as_ecc_strengthens() -> None:
    """纠错越强，能装的数据越少 —— 顺序必须是 L > M > Q > H。"""
    for version, row in DATA_CODEWORDS.items():
        values = [row[e] for e in ECC_LEVELS]
        assert values == sorted(values, reverse=True), f"version {version} 的顺序不对"


def test_modules_per_side() -> None:
    assert modules_per_side(1) == 21
    assert modules_per_side(40) == 177


def test_bad_arguments_rejected() -> None:
    with pytest.raises(InputError):
        byte_capacity(41, "L")
    with pytest.raises(InputError):
        byte_capacity(0, "L")
    with pytest.raises(InputError):
        byte_capacity(20, "X")


def test_profile_body_capacities() -> None:
    """净载荷 = 容量 - 32 字节分片头。这几个数字直接决定码的数量，写死防回归。"""
    assert PROFILES["camera"].body_capacity == 260
    assert PROFILES["screen"].body_capacity == 826
    assert PROFILES["print"].body_capacity == 1338
    assert PROFILES["max"].body_capacity == 2921
