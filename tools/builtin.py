"""无需外部依赖和副作用的内置只读工具。"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tools.contracts import ToolArgumentError, ToolDefinition


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class GetCurrentTimeTool:
    """返回指定 IANA 时区的当前时间，不读取网络或修改系统状态。"""

    clock: Callable[[], datetime] = _utc_now
    definition = ToolDefinition(
        name="get_current_time",
        description="获取指定 IANA 时区的当前日期和时间",
        parameters={
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": "IANA 时区名，例如 Asia/Shanghai",
                }
            },
            "required": ["timezone"],
            "additionalProperties": False,
        },
    )

    async def invoke(self, arguments: Mapping[str, object]) -> object:
        if set(arguments) != {"timezone"}:
            raise ToolArgumentError("Timezone is invalid")
        timezone_name = arguments["timezone"]
        if not isinstance(timezone_name, str) or not timezone_name.strip():
            raise ToolArgumentError("Timezone is invalid")

        try:
            target_timezone = ZoneInfo(timezone_name)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ToolArgumentError("Timezone is invalid") from exc

        current = self.clock()
        if current.tzinfo is None:
            raise RuntimeError("clock must return an aware datetime")
        localized = current.astimezone(target_timezone)
        return {
            "timezone": timezone_name,
            "datetime": localized.isoformat(),
        }
