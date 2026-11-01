"""让 pytest 把项目根目录放进 sys.path，这样 ``import qrfile`` 能直接用。

（没有这个文件的话，pytest 只会把 ``tests/`` 加进 sys.path。）
"""
