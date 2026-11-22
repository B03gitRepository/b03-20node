"""带有文件、字段和对象位置的校验工具。"""

from decimal import Decimal
import math
from typing import Any, NoReturn


class ScenarioValidationError(ValueError):
    """输入错误；属性可用于命令行或 JSON 错误报告。"""

    def __init__(self, file: str, field: str, object_id: str, message: str):
        self.file, self.field, self.object_id = file, field, object_id or "$"
        self.message = message
        super().__init__(f"{file} | {field} | {self.object_id}: {message}")

    def to_dict(self) -> dict[str, str]:
        """输入当前异常，返回含文件、字段、对象和原因的 JSON 兼容字典。"""
        return dict(file=self.file, field=self.field, object_id=self.object_id, message=self.message)


def fail(file: str, field: str, obj: str, message: str) -> NoReturn:
    """输入错误位置与原因，抛出 ScenarioValidationError，不返回结果。"""
    raise ScenarioValidationError(file, field, obj, message)


def mapping(value: Any, file: str, field: str, obj: str = "") -> dict:
    """输入待检值及来源位置，返回通过对象类型校验的原 dict。"""
    if not isinstance(value, dict):
        fail(file, field, obj, "必须是 JSON 对象")
    return value


def array(value: Any, file: str, field: str, obj: str = "") -> list:
    """输入待检值及来源位置，返回通过数组类型校验的原 list。"""
    if not isinstance(value, list):
        fail(file, field, obj, "必须是 JSON 数组")
    return value


def string(value: Any, file: str, field: str, obj: str = "") -> str:
    """输入待检值及来源位置，返回通过非空检查的原字符串。"""
    if not isinstance(value, str) or not value.strip():
        fail(file, field, obj, "必须是非空字符串")
    return value


def number(value: Any, file: str, field: str, obj: str = "", *, minimum=0, maximum=None, integer=False):
    """输入数值、边界及整数要求，返回合法原值；类型或范围错误时抛出异常。"""
    # bool 在 Python 中是 int 的子类；显式排除，防止 true 变成 1 bps。
    valid = type(value) is int if integer else type(value) in (int, float)
    if not valid:
        fail(file, field, obj, "必须是整数" if integer else "必须是数值，不能是 bool 或字符串")
    # JSON 解析后仍检查有限性，兼顾 1e999 这类溢出输入。
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or value < minimum or (maximum is not None and value > maximum):
        fail(file, field, obj, f"数值必须有限且处于 [{minimum}, {maximum if maximum is not None else '+∞'}]")
    return value


def microseconds(value: Any, file: str, field: str, obj: str = "") -> int:
    """输入非负秒数及来源位置，返回整数微秒；不静默四舍五入。"""
    number(value, file, field, obj)
    # 先用十进制文本转换，避免二进制浮点把 0.1 秒截成错误的微秒。
    scaled = Decimal(str(value)) * 1_000_000
    if scaled != scaled.to_integral_value():
        fail(file, field, obj, "时间精度不能小于一微秒")
    return int(scaled)


def capacity(params: dict, file: str, field: str, obj: str, warnings: list[str]) -> int:
    """接受数据集和 PDF 两种拼写；冲突必须报错，内部返回 bit/s 整数。"""
    aliases = [key for key in ("bandwidthBps", "bandwitdhBps") if key in params]
    if not aliases:
        fail(file, field, obj, "缺少 bandwidthBps 或 bandwitdhBps")
    values = [number(params[k], file, f"{field}.{k}", obj, minimum=1, integer=True) for k in aliases]
    if len(values) == 2:
        if values[0] != values[1]:
            fail(file, field, obj, "带宽两个别名的值冲突")
        warnings.append(f"{file} | {field} | {obj}: 两个带宽别名值一致，内部统一为 capacity_bps")
    return values[0]


def finite_tree(value: Any, file: str, field: str = "$", obj: str = "") -> None:
    """连未消费的元数据也禁止包含非有限浮点数。"""
    if isinstance(value, dict):
        obj = str(value.get("nodeId", value.get("linkId", value.get("serviceId", obj))))
        for key, child in value.items():
            finite_tree(child, file, f"{field}.{key}", obj)
    elif isinstance(value, list):
        for i, child in enumerate(value):
            finite_tree(child, file, f"{field}[{i}]", obj)
    elif type(value) is float and not math.isfinite(value):
        fail(file, field, obj, "JSON 不允许 NaN、Infinity 或溢出的非有限数值")
