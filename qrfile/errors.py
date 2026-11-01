"""异常层级与进程退出码。

退出码约定（方案第十二章）::

    0  成功
    1  部分跳过但成功
    2  校验失败
    3  输入错误
    4  内部错误（含只读护栏被触发 —— 这一条永远不该在正常运行中出现）
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_VERIFY_FAILED = 2
EXIT_INPUT_ERROR = 3
EXIT_INTERNAL = 4


class QrFileError(Exception):
    """qrfile 所有异常的基类。"""

    exit_code = EXIT_INTERNAL


class InputError(QrFileError):
    """调用方给的东西不对：路径不存在、参数矛盾、路径非法。"""

    exit_code = EXIT_INPUT_ERROR


class LimitExceeded(InputError):
    """超出了配置的上限。"""


class VerificationError(QrFileError):
    """校验没过 —— 载荷或还原出来的文件与预期不符。"""

    exit_code = EXIT_VERIFY_FAILED


class ChunkError(VerificationError):
    """分片层面的问题：magic 不对、缺片、重复、CRC32 不符。"""


class SourceGuardError(QrFileError):
    """只读护栏被触发：有操作试图修改源文件（方案第零章）。

    这是最严重的一类 —— 说明代码里有 bug 或源树被外部干扰。
    正常运行中永远不该出现。
    """

    exit_code = EXIT_INTERNAL
