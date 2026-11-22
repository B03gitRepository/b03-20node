"""把相对仿真微秒映射成同源的 time 与 tsUs。

仿真只知道走了多少微秒，本模块给它加一个固定起点。
赛题没有明确纪元和时区，因此两者必须由配置提供并保存到运行记录。
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from resnet.contracts.validation import require_int

UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class ClockMapping:
    """输入为 Unix 起点微秒及固定时区偏移；输出为赛题时间字段。"""

    epoch_start_us: int
    timezone_offset_minutes: int = 480

    def __post_init__(self) -> None:
        require_int(self.epoch_start_us, "epoch_start_us")
        offset = self.timezone_offset_minutes
        if type(offset) is not int or not -720 <= offset <= 840:
            raise ValueError("timezone_offset_minutes 必须为 -720 到 840 的整数")

    @classmethod
    def from_iso(cls, epoch_start: str, offset_minutes: int = 480) -> "ClockMapping":
        """从带时区的 ISO 时间创建映射，不接受无时区时间。"""
        dt = datetime.fromisoformat(epoch_start.replace("Z", "+00:00"))
        if dt.tzinfo is None or dt.utcoffset() is None:  # 要求显式指定时区，避免依赖本机设置。
            raise ValueError("epoch_start 必须明确时区，例如 2026-01-01T00:00:00+00:00")
        delta = dt.astimezone(timezone.utc) - UNIX_EPOCH
        us = ((delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds)
        return cls(us, offset_minutes)  # 全程整数运算，保留微秒精度。

    def format_timestamp(self, ts_us: int) -> str:
        """由一个绝对整数时间戳生成 HH:MM:SS.ffffff。"""
        require_int(ts_us, "tsUs")
        zone = timezone(timedelta(minutes=self.timezone_offset_minutes))
        dt = (UNIX_EPOCH + timedelta(microseconds=ts_us)).astimezone(zone)
        return dt.strftime("%H:%M:%S.%f")

    def fields(self, sim_time_us: int) -> dict[str, str | int]:
        """输入相对仿真微秒，返回完全同源的 time 和 tsUs。"""
        require_int(sim_time_us, "sim_time_us")
        ts_us = self.epoch_start_us + sim_time_us  # 不读取电脑当前时间。
        return {"time": self.format_timestamp(ts_us), "tsUs": ts_us}

    def metadata(self) -> dict:
        return {
            "epoch_start_us": self.epoch_start_us,
            "timezone_offset_minutes": self.timezone_offset_minutes,
            "mapping": "tsUs = epoch_start_us + sim_time_us; time derived from tsUs",
            "source": "project_convention_not_defined_by_competition_pdf",
        }
