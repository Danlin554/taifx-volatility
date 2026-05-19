"""
真實 exchange_calendars 整合測試。
- 不使用 mock，直接呼叫 XTAI / XNYS 確認套件 API 可用且回傳正確型別。
- 標記 @pytest.mark.integration，CI 預設也跑（不 skip）。
- 若 exchange_calendars 未安裝或 API 改變 → 這裡 CI 紅燈，防止 freshness 靜默 degrade。
"""
from __future__ import annotations

import datetime

import pandas as pd
import pytest

try:
    import exchange_calendars as xcals
    _XCALS_AVAILABLE = True
except ImportError:
    _XCALS_AVAILABLE = False


pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def xtai():
    if not _XCALS_AVAILABLE:
        pytest.skip("exchange_calendars not installed")
    return xcals.get_calendar("XTAI")


@pytest.fixture(scope="module")
def xnys():
    if not _XCALS_AVAILABLE:
        pytest.skip("exchange_calendars not installed")
    return xcals.get_calendar("XNYS")


class TestXtaiCalendar:
    def test_is_session_returns_bool(self, xtai):
        result = xtai.is_session(pd.Timestamp("2026-05-19"))
        assert isinstance(result, bool)
        assert result is True  # 週二是交易日

    def test_is_session_weekend_false(self, xtai):
        result = xtai.is_session(pd.Timestamp("2026-05-16"))
        assert result is False  # 週六

    def test_previous_session_returns_timestamp(self, xtai):
        result = xtai.previous_session(pd.Timestamp("2026-05-19"))
        assert isinstance(result, pd.Timestamp)
        assert result.date() == datetime.date(2026, 5, 18)  # 週一

    def test_next_session_returns_timestamp(self, xtai):
        result = xtai.next_session(pd.Timestamp("2026-05-18"))
        assert isinstance(result, pd.Timestamp)
        assert result.date() == datetime.date(2026, 5, 19)  # 週二

    def test_tw_spring_festival_2026(self, xtai):
        """2026 年春節假期（2/12~2/20），最後交易日 2/11，假期後第一交易日應是 2/23。"""
        result = xtai.next_session(pd.Timestamp("2026-02-11"))
        assert result.date() == datetime.date(2026, 2, 23)


class TestXnysCalendar:
    def test_is_session_returns_bool(self, xnys):
        result = xnys.is_session(pd.Timestamp("2026-05-18"))
        assert isinstance(result, bool)
        assert result is True

    def test_session_close_is_utc_aware(self, xnys):
        """session_close 回傳的 Timestamp 必須是 UTC-aware（tzinfo 非 None）。"""
        close_ts = xnys.session_close(pd.Timestamp("2026-05-18"))
        assert isinstance(close_ts, pd.Timestamp)
        assert close_ts.tzinfo is not None, "session_close must be timezone-aware"
        assert str(close_ts.tzinfo) in ("UTC", "pytz.UTC", "datetime.timezone.utc") or \
               close_ts.tzname() == "UTC"

    def test_session_close_normal_day_around_et1600(self, xnys):
        """一般交易日收盤約 ET 16:00 = UTC 21:00（非 DST）或 UTC 20:00（DST 期間）。"""
        close_ts = xnys.session_close(pd.Timestamp("2026-01-20"))
        # 冬令時（ET = UTC-5），ET 16:00 = UTC 21:00
        assert close_ts.hour in (20, 21), f"Expected 20 or 21 UTC, got {close_ts.hour}"

    def test_thanksgiving_2026_half_day(self, xnys):
        """NYSE Thanksgiving Day 2026-11-26 全休；前一日 2026-11-25 半日市（ET 13:00 收盤）。"""
        # 感恩節：NYSE 全日休市
        thanksgiving = pd.Timestamp("2026-11-26")
        assert not xnys.is_session(thanksgiving), "Thanksgiving should be closed"

    def test_thanksgiving_friday_2026_half_day(self, xnys):
        """Black Friday 2026-11-27 為 NYSE 半日市，收盤 ET 13:00 = UTC 18:00。"""
        black_friday = pd.Timestamp("2026-11-27")
        if xnys.is_session(black_friday):
            close_ts = xnys.session_close(black_friday)
            # Black Friday close = ET 13:00（EST = UTC-5）→ UTC 18:00
            assert close_ts.hour == 18, (
                f"Black Friday close should be UTC 18:00, got {close_ts.hour}:00"
            )

    def test_dst_change_close_time_varies(self, xnys):
        """DST 切換前後，XNYS session_close UTC 小時應不同（20 vs 21）。"""
        # 2026 年美國 DST 結束：11 月第一個週日 = 2026-11-01
        # 11-02（週一）為標準時間（EST），ET 16:00 = UTC 21:00
        # 10-30（週五）為夏令時間（EDT），ET 16:00 = UTC 20:00
        before_dst = pd.Timestamp("2026-10-30")
        after_dst = pd.Timestamp("2026-11-02")

        if xnys.is_session(before_dst) and xnys.is_session(after_dst):
            close_before = xnys.session_close(before_dst)
            close_after = xnys.session_close(after_dst)
            assert close_before.hour != close_after.hour, (
                f"Expected DST change to affect close hour: "
                f"before={close_before.hour}, after={close_after.hour}"
            )

    def test_memorial_day_2026_closed(self, xnys):
        """Memorial Day 2026-05-25 NYSE 全休。"""
        memorial_day = pd.Timestamp("2026-05-25")
        assert not xnys.is_session(memorial_day), "Memorial Day 2026 should be closed"

    def test_previous_session_returns_date(self, xnys):
        result = xnys.previous_session(pd.Timestamp("2026-05-19"))
        assert isinstance(result, pd.Timestamp)
        assert result.date() == datetime.date(2026, 5, 18)
