import datetime
import pandas as pd

_TW_HOLIDAYS_2026 = {
    datetime.date(2026, 1, 1),
    datetime.date(2026, 1, 2),
    datetime.date(2026, 2, 16),
    datetime.date(2026, 2, 17),
    datetime.date(2026, 2, 18),
    datetime.date(2026, 2, 19),
    datetime.date(2026, 2, 20),
    datetime.date(2026, 4, 3),
    datetime.date(2026, 4, 4),
    datetime.date(2026, 5, 1),
    datetime.date(2026, 6, 19),
    datetime.date(2026, 9, 4),
    datetime.date(2026, 10, 9),
    datetime.date(2026, 10, 10),
}


def last_tw_trading_day(asof: datetime.date) -> datetime.date:
    """從 asof（不含）往前找最近的台股交易日（排週末 + 已知假日）。"""
    d = asof - datetime.timedelta(days=1)
    for _ in range(14):
        if d.weekday() < 5 and d not in _TW_HOLIDAYS_2026:
            return d
        d -= datetime.timedelta(days=1)
    return d


def is_stale(df: pd.DataFrame, asof: datetime.date) -> bool:
    """回傳 True 表示資料落後（最新一筆日期 < 預期最近交易日）。"""
    if df.empty:
        return True
    last_date = df.index[-1]
    if hasattr(last_date, "date"):
        last_date = last_date.date()
    expected = last_tw_trading_day(asof)
    return last_date < expected
