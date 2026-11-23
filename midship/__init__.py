"""本船事件中台：只消费、去重、派单、留痕。不自己定时，不写广告后台。"""

from .inbox import write_event, ensure_schema
from .consume import consume

__all__ = ["write_event", "ensure_schema", "consume"]
