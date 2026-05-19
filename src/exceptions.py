from __future__ import annotations

import datetime


class InsufficientDataError(Exception):
    """資料不足時由 db.read_indices_until / main.read_db_data 拋出；api_snapshot 捕捉後回 422。"""

    def __init__(
        self,
        *,
        earliest: datetime.date | None = None,
        got: int | None = None,
        symbol: str | None = None,
        actual_last: datetime.date | None = None,
        symbol_earliest: datetime.date | None = None,
        code: str = "insufficient_data",
        missing_fields: list[str] | None = None,
        asof: datetime.date | None = None,
        expected_us_session: str | None = None,
        resolved_effective_date: str | None = None,
        actual_us_session_dates: dict | None = None,
    ):
        self.earliest = earliest
        self.got = got
        self.symbol = symbol
        self.actual_last = actual_last
        self.symbol_earliest = symbol_earliest
        self.code = code
        self.missing_fields = missing_fields
        self.asof = asof
        self.expected_us_session = expected_us_session
        self.resolved_effective_date = resolved_effective_date
        self.actual_us_session_dates = actual_us_session_dates
        super().__init__(f"InsufficientDataError: {code}")


class LivePublishValidationError(Exception):
    """live publish 前的原始資料驗證失敗（不寫 DB、不更新 cache）。"""
    pass
