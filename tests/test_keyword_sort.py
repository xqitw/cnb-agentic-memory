"""keyword_search 合并去重后的时序排序测试（#51）。

CNB 当前统一返回 UTC Z 后缀，但时区表示不能依赖：混入 +08:00 等
偏移时字符串比较会错序（"09:00+08:00" 实际晚于 "16:00Z"，字符串比较相反）。
"""

from __future__ import annotations

from cnb_agentic_memory.memory import _updated_at_sort_key
from cnb_agentic_memory.models import Issue


def _issue(number: int, updated_at: str | None) -> Issue:
    return Issue(number=number, title=f"t{number}", updated_at=updated_at)


def test_mixed_timezone_ordering() -> None:
    """+08:00 偏移与 Z 后缀混排时按真实时刻排序，而非字符串比较。"""
    # 18:00+08:00 = 10:00Z；16:00Z 晚于它——字符串比较会错序，解析后正确
    issues = [
        _issue(1, "2026-08-29T18:00:00+08:00"),
        _issue(2, "2026-08-29T16:00:00Z"),
        _issue(3, "2026-08-29T10:00:00Z"),
    ]
    ordered = sorted(issues, key=_updated_at_sort_key, reverse=True)
    assert [i.number for i in ordered] == [2, 1, 3]


def test_naive_and_malformed_timestamps_fallback() -> None:
    """畸形时间戳回退字符串语义；naive 显式按 UTC 解释（不随部署机 TZ 变化）。"""
    issues = [
        _issue(1, "2026-08-29T10:00:00Z"),
        _issue(2, "not-a-date"),
        _issue(3, None),
        _issue(4, "2026-08-29T10:00:00"),  # naive：可解析但无时区
    ]
    # 不抛错即可（naive datetime 与 aware 不能互相比较，key 用 timestamp
    # 会在 naive 上抛 TypeError——此处验证实现选择的行为）
    ordered = sorted(issues, key=_updated_at_sort_key, reverse=True)
    assert len(ordered) == 4


def test_naive_timestamp_treated_as_utc() -> None:
    """naive 时间戳显式按 UTC 解释：与等价 UTC aware 排序一致（不随 TZ 变化）。"""
    issues = [
        _issue(1, "2026-08-29T10:00:00Z"),
        _issue(2, "2026-08-29T09:00:00"),  # naive，语义上等同 09:00Z
        _issue(3, "2026-08-29T11:00:00Z"),
    ]
    ordered = sorted(issues, key=_updated_at_sort_key, reverse=True)
    assert [i.number for i in ordered] == [3, 1, 2]
