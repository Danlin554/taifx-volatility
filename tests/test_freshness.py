"""Unit tests for src/freshness.py（以 mock 為主，不依賴真實 exchange_calendars）。"""
from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
import pytz

from src import config, freshness


TZ = config.TZ


def _make_txf(last_date: str, n: int = 5) -> pd.DataFrame:
    """製造一個 DataFrame，最後一筆 trade_date = last_date。"""
    end = pd.Timestamp(last_date)
    dates = pd.bdate_range(end=end, periods=n)
    return pd.DataFrame(
        {"open": 1.0, "high": 2.0, "low": 1.0, "close": 1.5, "volume": 100},
        index=dates,
    )


def _tpe(dt_str: str) -> datetime.datetime:
    """快速產生 Asia/Taipei 時區的 datetime。"""
    return TZ.localize(datetime.datetime.fromisoformat(dt_str))


# ---------------------------------------------------------------------------
# check_freshness
# ---------------------------------------------------------------------------

class TestCheckFreshness:
    def test_normal_after_close_stale_when_txf_lags(self):
        """收盤後（14:00 TPE），txf 最後日比 expected 舊 → is_stale=True, mode=normal。"""
        txf = _make_txf("2026-05-18")  # 週一
        observed_at = _tpe("2026-05-19T14:00:00")  # 週二收盤後

        mock_xtai = MagicMock()
        mock_xtai.is_session.return_value = True
        mock_xtai.previous_session.return_value = pd.Timestamp("2026-05-18")

        with patch.object(freshness, "_get_calendar", return_value=mock_xtai):
            result = freshness.check_freshness(txf, observed_at)

        assert result["is_stale"] is True
        assert result["mode"] == "normal"
        assert result["expected_trade_date"] == datetime.date(2026, 5, 19)

    def test_normal_before_close_not_stale(self):
        """盤中（10:00 TPE），txf 最後日為昨日 → expected = 昨日 → not stale。"""
        txf = _make_txf("2026-05-18")
        observed_at = _tpe("2026-05-19T10:00:00")  # 今日盤中，收盤前

        mock_xtai = MagicMock()
        mock_xtai.is_session.return_value = True
        mock_xtai.previous_session.return_value = pd.Timestamp("2026-05-18")

        with patch.object(freshness, "_get_calendar", return_value=mock_xtai):
            result = freshness.check_freshness(txf, observed_at)

        assert result["is_stale"] is False
        assert result["mode"] == "normal"
        assert result["expected_trade_date"] == datetime.date(2026, 5, 18)

    def test_non_session_uses_previous(self):
        """週末（non-session day），expected = previous_session。"""
        txf = _make_txf("2026-05-15")  # 週五
        observed_at = _tpe("2026-05-16T12:00:00")  # 週六

        mock_xtai = MagicMock()
        mock_xtai.is_session.return_value = False
        mock_xtai.previous_session.return_value = pd.Timestamp("2026-05-15")

        with patch.object(freshness, "_get_calendar", return_value=mock_xtai):
            result = freshness.check_freshness(txf, observed_at)

        assert result["is_stale"] is False
        assert result["mode"] == "normal"
        assert result["expected_trade_date"] == datetime.date(2026, 5, 15)

    def test_degraded_when_calendar_unavailable(self):
        """exchange_calendars import 失敗 → mode=degraded, is_stale=False。"""
        txf = _make_txf("2026-05-01")
        observed_at = _tpe("2026-05-19T14:00:00")

        with patch.object(freshness, "_get_calendar", side_effect=Exception("no calendar")):
            result = freshness.check_freshness(txf, observed_at)

        assert result["mode"] == "degraded"
        assert result["is_stale"] is False

    def test_empty_df_returns_stale(self):
        """空 DataFrame → is_stale=True。"""
        empty_df = pd.DataFrame(columns=["high", "low", "close"])
        empty_df.index = pd.DatetimeIndex([])
        observed_at = _tpe("2026-05-19T14:00:00")
        result = freshness.check_freshness(empty_df, observed_at)
        assert result["is_stale"] is True

    def test_uses_previous_session_not_fixed_window(self):
        """驗證實作呼叫 previous_session 而非 sessions_in_range。"""
        txf = _make_txf("2026-05-15")
        observed_at = _tpe("2026-05-16T12:00:00")

        mock_xtai = MagicMock()
        mock_xtai.is_session.return_value = False
        mock_xtai.previous_session.return_value = pd.Timestamp("2026-05-15")

        with patch.object(freshness, "_get_calendar", return_value=mock_xtai):
            freshness.check_freshness(txf, observed_at)

        mock_xtai.previous_session.assert_called()
        assert not hasattr(mock_xtai, "sessions_in_range") or not mock_xtai.sessions_in_range.called

    def test_after_close_marks_today_as_expected(self):
        """收盤後（14:00）觀測，txf 最後日是今日 → not stale（expected = today）。"""
        txf = _make_txf("2026-05-19")
        observed_at = _tpe("2026-05-19T14:00:00")

        mock_xtai = MagicMock()
        mock_xtai.is_session.return_value = True

        with patch.object(freshness, "_get_calendar", return_value=mock_xtai):
            result = freshness.check_freshness(txf, observed_at)

        assert result["is_stale"] is False
        assert result["expected_trade_date"] == datetime.date(2026, 5, 19)


# ---------------------------------------------------------------------------
# is_trading_day
# ---------------------------------------------------------------------------

class TestIsTradingDay:
    def test_weekend_returns_false_without_calendar(self):
        """週六直接回 False，不需 calendar。"""
        sat = datetime.date(2026, 5, 16)
        result = freshness.is_trading_day(sat)
        assert result is False

    def test_weekday_uses_xtai(self):
        """平日透過 XTAI 判斷。"""
        mock_xtai = MagicMock()
        mock_xtai.is_session.return_value = True
        with patch.object(freshness, "_get_calendar", return_value=mock_xtai):
            result = freshness.is_trading_day(datetime.date(2026, 5, 19))
        assert result is True

    def test_calendar_unavailable_returns_none(self):
        """calendar 不可用 → 回 None（三態）。"""
        with patch.object(freshness, "_get_calendar", side_effect=Exception("missing")):
            result = freshness.is_trading_day(datetime.date(2026, 5, 19))
        assert result is None


# ---------------------------------------------------------------------------
# last_completed_us_session
# ---------------------------------------------------------------------------

class TestLastCompletedUsSession:
    def _make_xnys(self, sessions: list[str], closes_utc: list[str]):
        """製造 mock XNYS。sessions 是日期字串，closes_utc 是對應的 UTC close 時間。"""
        mock_xnys = MagicMock()
        # 必須是 DatetimeIndex（code 裡呼叫 .tolist()，Python list 沒有此方法）
        ts_sessions = pd.DatetimeIndex([pd.Timestamp(s) for s in sessions])
        mock_xnys.sessions_in_range.return_value = ts_sessions
        close_map = {
            pd.Timestamp(s): pd.Timestamp(c, tz="UTC")
            for s, c in zip(sessions, closes_utc)
        }
        mock_xnys.session_close.side_effect = lambda s: close_map[s]
        return mock_xnys

    def test_returns_prev_when_observed_before_close(self):
        """TPE D 16:00（NYSE D 尚未收盤）→ 回 D-1。"""
        # NYSE D 收盤 = UTC 21:00，D-1 = UTC 21:00
        # TPE D 16:00 = UTC D 08:00 < 21:00 → D 未收盤
        observed_at = _tpe("2026-05-18T16:00:00")  # TPE = UTC 08:00
        mock_xnys = self._make_xnys(
            ["2026-05-15", "2026-05-18"],
            ["2026-05-15T21:00:00", "2026-05-18T21:00:00"],  # 兩日的 close
        )
        with patch.object(freshness, "_get_calendar", return_value=mock_xnys):
            result = freshness.last_completed_us_session(observed_at)
        assert result == datetime.date(2026, 5, 15)

    def test_returns_today_after_close(self):
        """TPE D+1 06:00（NYSE D 已收盤）→ 回 D。"""
        observed_at = _tpe("2026-05-19T06:00:00")  # TPE D+1 = UTC D 22:00
        mock_xnys = self._make_xnys(
            ["2026-05-15", "2026-05-16", "2026-05-18", "2026-05-19"],
            [
                "2026-05-15T21:00:00",
                "2026-05-16T21:00:00",
                "2026-05-18T21:00:00",  # D 收盤 UTC 21:00 = TPE D+1 05:00
                "2026-05-19T21:00:00",
            ],
        )
        with patch.object(freshness, "_get_calendar", return_value=mock_xnys):
            result = freshness.last_completed_us_session(observed_at)
        assert result == datetime.date(2026, 5, 18)

    def test_calendar_unavailable_returns_none(self):
        """calendar 不可用 → None。"""
        observed_at = _tpe("2026-05-19T06:00:00")
        with patch.object(freshness, "_get_calendar", side_effect=Exception("no calendar")):
            result = freshness.last_completed_us_session(observed_at)
        assert result is None


# ---------------------------------------------------------------------------
# previous_nyse_session
# ---------------------------------------------------------------------------

class TestPreviousNyseSession:
    def test_returns_previous_session(self):
        mock_xnys = MagicMock()
        mock_xnys.previous_session.return_value = pd.Timestamp("2026-05-14")
        with patch.object(freshness, "_get_calendar", return_value=mock_xnys):
            result = freshness.previous_nyse_session(datetime.date(2026, 5, 15))
        assert result == datetime.date(2026, 5, 14)

    def test_calendar_unavailable_returns_none(self):
        with patch.object(freshness, "_get_calendar", side_effect=Exception("missing")):
            result = freshness.previous_nyse_session(datetime.date(2026, 5, 15))
        assert result is None


# ---------------------------------------------------------------------------
# expected_us_session_for_effective_date
# ---------------------------------------------------------------------------

class TestExpectedUsSessionForEffectiveDate:
    def _patch_both_calendars(self, xtai_next: str, completed_us: str | None):
        """同時 patch _get_calendar 讓 XTAI 和 XNYS 各自有期待的行為。"""
        mock_xtai = MagicMock()
        mock_xtai.next_session.return_value = pd.Timestamp(xtai_next)

        mock_xnys = MagicMock()
        sessions = (
            pd.DatetimeIndex([pd.Timestamp(completed_us)])
            if completed_us
            else pd.DatetimeIndex([])
        )
        mock_xnys.sessions_in_range.return_value = sessions
        if completed_us:
            mock_xnys.session_close.return_value = pd.Timestamp(
                completed_us + "T21:00:00", tz="UTC"
            )

        def _get_calendar_side(name):
            return mock_xtai if name == "XTAI" else mock_xnys

        return _get_calendar_side

    def test_normal_day(self):
        """正常交易日：effective_date=D → next XTAI session = D+1 → completed_us = D。"""
        # D = 2026-05-18（週一），next_session = 2026-05-19（週二）
        # observed = 2026-05-19 06:00 TPE，last completed NYSE = 2026-05-18
        side = self._patch_both_calendars(
            xtai_next="2026-05-19",
            completed_us="2026-05-18",
        )
        with patch.object(freshness, "_get_calendar", side_effect=side):
            result = freshness.expected_us_session_for_effective_date(datetime.date(2026, 5, 18))
        assert result == datetime.date(2026, 5, 18)

    def test_xtai_fail_returns_none(self):
        """XTAI 不可用 → None（R9 #2）。"""
        call_count = {"n": 0}

        def _get_cal(name):
            if name == "XTAI":
                raise Exception("xtai unavailable")
            return MagicMock()

        with patch.object(freshness, "_get_calendar", side_effect=_get_cal):
            result = freshness.expected_us_session_for_effective_date(datetime.date(2026, 5, 18))
        assert result is None

    def test_xnys_fail_returns_none(self):
        """XNYS 不可用（R9 #2）→ None。"""
        mock_xtai = MagicMock()
        mock_xtai.next_session.return_value = pd.Timestamp("2026-05-19")

        def _get_cal(name):
            if name == "XTAI":
                return mock_xtai
            raise Exception("xnys unavailable")

        with patch.object(freshness, "_get_calendar", side_effect=_get_cal):
            result = freshness.expected_us_session_for_effective_date(datetime.date(2026, 5, 18))
        assert result is None

    def test_both_calendars_unavailable_returns_none(self):
        with patch.object(freshness, "_get_calendar", side_effect=Exception("all down")):
            result = freshness.expected_us_session_for_effective_date(datetime.date(2026, 5, 18))
        assert result is None


# ---------------------------------------------------------------------------
# Deprecated wrappers
# ---------------------------------------------------------------------------

class TestDeprecatedWrappers:
    def test_last_tw_trading_day_emits_warning(self):
        with pytest.warns(DeprecationWarning):
            with patch.object(freshness, "_get_calendar", side_effect=Exception("no cal")):
                freshness.last_tw_trading_day(datetime.date(2026, 5, 16))

    def test_is_stale_emits_warning(self):
        txf = _make_txf("2026-05-15")
        with pytest.warns(DeprecationWarning):
            with patch.object(freshness, "_get_calendar", side_effect=Exception("no cal")):
                freshness.is_stale(txf, datetime.date(2026, 5, 16))
