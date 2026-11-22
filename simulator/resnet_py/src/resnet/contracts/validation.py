"""数据契约共用的基础校验，不依赖场景加载器或仿真内核。"""


def require_int(value: object, name: str, minimum: int = 0) -> int:
    """接收满足下界的整数并原样返回；拒绝 bool 和浮点数。

    Python 的 bool 继承 int，因此使用 type 精确检查。时间、版本和容量等
    字段共用此规则；调用者通过 minimum 指定允许零还是必须为正。
    """
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} 必须为至少 {minimum} 的整数")
    return value
