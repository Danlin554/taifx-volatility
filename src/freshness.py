from __future__ import annotations

import datetime
import warnings

import pandas as pd


# ---------------------------------------------------------------------------
# Calendar helpers（lazy，不在 module level 快取以利 mock）
# ---------------------------------------------------------------------------

def _get_calendar(name: str):
    """Return exchange calendar by name. Raises if exchange_calendars unavailable."""
    import exchange_calendars
    return exchange_calendars.get_calendar(name)


def _market_close_for(d: datetime.date) -> datetime.time:
    """台指期收盤時間（預設 13:45 TPE；可未來改成 callback）。"""
    return datetime.time(13, 45)


# ---------------------------------------------------------------------------
# 核心公開 API
# ---------------------------------------------------------------------------

def check_freshness(df: pd.DataFrame, observed_at: datetime.datetime) -> dict:
    """
    Returns {is_stale, mode, expected_trade_date}
      mode = "normal"   — 用 XTAI 推算並比對
      mode = "degraded" — calendar 不可用，保守不誤警

    observed_at 必須是 timezone-aware datetime。
    """
    from src import config

    if df.empty:
        return {"is_stale": True, "mode": "normal", "expected_trade_date": None}

    try:
        xtai = _get_calendar("XTAI")
    except Exception:
        return {"is_stale": False, "mode": "degraded", "expected_trade_date": None}

    try:
        observed_local = observed_at.astimezone(config.TZ)
        observed_date = observed_local.date()
        obs_ts = pd.Timestamp(observed_date)

        if xtai.is_session(obs_ts):
            close_time = _market_close_for(observed_date)
            close_aware = config.TZ.localize(
                datetime.datetime.combine(observed_date, close_time)
            )
            if observed_local >= close_aware:
                expected_trade_date = observed_date
            else:
                expected_trade_date = xtai.previous_session(obs_ts).date()
        else:
            expected_trade_date = xtai.previous_session(obs_ts).date()

        last_trade = df.index[-1]
        if hasattr(last_trade, "date"):
            last_trade = last_trade.date()

        is_stale = last_trade < expected_trade_date

        return {
            "is_stale": is_stale,
            "mode": "normal",
            "expected_trade_date": expected_trade_date,
        }
    except Exception:
        return {"is_stale": False, "mode": "degraded", "expected_trade_date": None}


def is_trading_day(d: datetime.date) -> bool | None:
    """
    三態：True（XTAI session）/ False（非交易日）/ None（calendar 不可用）。
    週末直接 False，不需 calendar。
    """
    if d.isoweekday() >= 6:
        return False
    try:
        xtai = _get_calendar("XTAI")
        return bool(xtai.is_session(pd.Timestamp(d)))
    except Exception:
        return None


def last_completed_us_session(observed_at: datetime.datetime) -> datetime.date | None:
    """
    回傳 observed_at 當下已收盤的最後一個 NYSE session 日期。
    不可用 → None（caller 應 fail-closed 處理）。
    """
    try:
        xnys = _get_calendar("XNYS")
        observed_utc = pd.Timestamp(observed_at.astimezone(datetime.timezone.utc))

        start = pd.Timestamp(
            (observed_at - datetime.timedelta(days=10)).astimezone(
                datetime.timezone.utc
            ).date()
        )
        end = pd.Timestamp(observed_utc.date())
        sessions = xnys.sessions_in_range(start, end)

        if len(sessions) == 0:
            return None

        for session in reversed(sessions.tolist()):
            close_ts = xnys.session_close(session)  # UTC-aware pd.Timestamp
            if close_ts <= observed_utc:
                return session.date()

        return None
    except Exception:
        return None


def previous_xtai_session(d: datetime.date) -> datetime.date | None:
    """回傳 d 之前最近的 XTAI session 日期（不含 d 本身）。
    Calendar 不可用 → None（caller fail-closed 不亂判 stale）。
    """
    try:
        xtai = _get_calendar("XTAI")
        return xtai.previous_session(pd.Timestamp(d)).date()
    except Exception:
        return None


def previous_nyse_session(d: datetime.date) -> datetime.date | None:
    """回傳 d 之前最近的 NYSE session 日期。不可用 → None。"""
    try:
        xnys = _get_calendar("XNYS")
        result = xnys.previous_session(pd.Timestamp(d))
        return result.date()
    except Exception:
        return None


def compute_us_session_close_tpe(session_date: datetime.date) -> datetime.datetime | None:
    """回傳 NYSE 該 session 的收盤對應台北時間（DST + 半日交易日自動處理）。

    XNYS.session_close() 回傳 UTC-aware Timestamp，astimezone(TZ) 轉台北。
    半日交易日（Black Friday、Christmas Eve、Independence Day eve）由 calendar 自動推導。
    Calendar 不可用 → None（前端 fallback「NYSE 收盤後」）。
    """
    from src import config

    try:
        xnys = _get_calendar("XNYS")
        close_ts = xnys.session_close(pd.Timestamp(session_date))
        return close_ts.astimezone(config.TZ).to_pydatetime()
    except Exception:
        return None


def expected_us_session_for_effective_date(
    effective_date: datetime.date,
) -> datetime.date | None:
    """
    給定快照 effective_date D，回傳「該快照理應採用的 NYSE session 日期」。

    邏輯：XTAI.next_session(D) = D+N（台灣下一開盤日），
          last_completed_us_session(D+N 06:00 TPE) = 該開盤日前已完成的最後 NYSE session。

    在台灣長假（如春節）情境下，D+N 可能隔 10+ 曆天，
    使得 expected_us 正確指向假期結束後的最後已完成 NYSE session，
    而非（錯誤的）D+1 曆天那個 session。

    XTAI 或 XNYS 不可用 → None（caller 走保守 degraded 分支）。
    """
    try:
        from src import config

        xtai = _get_calendar("XTAI")
        next_xtai = xtai.next_session(pd.Timestamp(effective_date))
        next_xtai_date = next_xtai.date()

        observed_for_next = config.TZ.localize(
            datetime.datetime.combine(next_xtai_date, datetime.time(6, 0))
        )
        return last_completed_us_session(observed_for_next)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Deprecated wrappers（本批次加 DeprecationWarning；下一批次刪除）
# ---------------------------------------------------------------------------

def last_tw_trading_day(asof: datetime.date) -> datetime.date:
    """Deprecated. Use check_freshness(df, observed_at) instead."""
    warnings.warn(
        "last_tw_trading_day is deprecated, use check_freshness(df, observed_at)",
        DeprecationWarning,
        stacklevel=2,
    )
    from src import config

    observed_at = config.TZ.localize(
        datetime.datetime.combine(asof, datetime.time(13, 45))
    )
    dummy = pd.DataFrame(
        {"high": [0], "low": [0], "close": [0]},
        index=pd.DatetimeIndex([pd.Timestamp("2000-01-01")]),
    )
    result = check_freshness(dummy, observed_at)
    if result["expected_trade_date"] is not None:
        return result["expected_trade_date"]
    # Fallback: weekday-only
    d = asof - datetime.timedelta(days=1)
    for _ in range(14):
        if d.isoweekday() < 6:
            return d
        d -= datetime.timedelta(days=1)
    return d


def is_stale(df: pd.DataFrame, asof: datetime.date) -> bool:
    """Deprecated. Use check_freshness(df, observed_at) instead."""
    warnings.warn(
        "is_stale(df, asof) is deprecated, use check_freshness(df, observed_at)",
        DeprecationWarning,
        stacklevel=2,
    )
    from src import config

    observed_at = config.TZ.localize(
        datetime.datetime.combine(asof, datetime.time(13, 45))
    )
    return check_freshness(df, observed_at)["is_stale"]
