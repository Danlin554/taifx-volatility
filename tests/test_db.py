"""
Unit tests for src/db.py — read_indices_until 與 upsert_indices_batch。

Unit tests：mock cursor / connection，只驗呼叫契約。
Integration tests：需真實 PostgreSQL（標 @pytest.mark.integration）。
"""
from __future__ import annotations

import datetime
import os
from unittest.mock import MagicMock, call, patch, PropertyMock
import pandas as pd
import pytest

from src.exceptions import InsufficientDataError

# 替 DB unit tests 注入假 DATABASE_URL，避免 get_conn/psycopg2.connect 在 mock 前噴 KeyError
_FAKE_DB_ENV = {"DATABASE_URL": "postgresql://test:test@localhost/test"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_df(dates: list[str]) -> pd.DataFrame:
    """製造簡單 OHLC DataFrame，index 為 DatetimeIndex。"""
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in dates])
    return pd.DataFrame(
        {"open": 1.0, "high": 2.0, "low": 1.0, "close": 1.5, "volume": 100},
        index=idx,
    )


# ---------------------------------------------------------------------------
# read_indices_until
# ---------------------------------------------------------------------------

class TestReadIndicesUntil:
    """以 mock 驗 read_indices_until 行為，不碰真實 DB。"""

    def _mock_read_sql(self, dates: list[str]):
        """mock pd.read_sql 回傳指定日期 rows（降冪，模擬 DB ORDER BY DESC）。
        pd.read_sql 回傳的 trade_date 是欄位（非 index），符合 db.py 的後續 set_index。
        """
        rows = [
            {
                "trade_date": pd.Timestamp(d),
                "open_px": 100.0, "high_px": 110.0,
                "low_px": 95.0, "close_px": 105.0, "volume": 1000,
            }
            for d in reversed(dates)   # 降冪，模擬 DB ORDER BY DESC
        ]
        if rows:
            return pd.DataFrame(rows)
        return pd.DataFrame(columns=["trade_date", "open_px", "high_px", "low_px", "close_px", "volume"])

    def test_basic_returns_ascending_dataframe(self):
        """基本：mock DB 回 5 筆，取 min_rows=5 → 升冪排序，index 為 DatetimeIndex。"""
        dates = [f"2026-05-{i:02d}" for i in range(11, 16)]  # 11~15

        with patch("src.db.get_conn"), \
             patch("pandas.read_sql", return_value=self._mock_read_sql(dates)):
            from src import db
            result = db.read_indices_until("TXFR1", datetime.date(2026, 5, 15), min_rows=5)

        assert len(result) == 5
        assert result.index.is_monotonic_increasing
        assert result.index[-1].date() == datetime.date(2026, 5, 15)

    def test_raises_when_insufficient(self):
        """DB 只有 3 筆但要求 min_rows=5 → raise InsufficientDataError。"""
        dates = ["2026-05-13", "2026-05-14", "2026-05-15"]

        with patch("src.db.get_conn"), \
             patch("pandas.read_sql", return_value=self._mock_read_sql(dates)), \
             patch("src.db._query_symbol_earliest", return_value=datetime.date(2026, 5, 13)):
            from src import db
            with pytest.raises(InsufficientDataError) as exc_info:
                db.read_indices_until("TXFR1", datetime.date(2026, 5, 15), min_rows=5)

        e = exc_info.value
        assert e.got == 3
        assert e.symbol == "TXFR1"
        assert e.actual_last == datetime.date(2026, 5, 15)

    def test_raises_with_none_actual_last_when_empty(self):
        """DB 完全空 → actual_last 為 None。"""
        with patch("src.db.get_conn"), \
             patch("pandas.read_sql", return_value=self._mock_read_sql([])), \
             patch("src.db._query_symbol_earliest", return_value=None):
            from src import db
            with pytest.raises(InsufficientDataError) as exc_info:
                db.read_indices_until("TXFR1", datetime.date(2026, 5, 15), min_rows=5)

        assert exc_info.value.actual_last is None
        assert exc_info.value.got == 0

    def test_query_uses_correct_params(self):
        """驗證 pd.read_sql 被呼叫時帶正確 symbol / asof / min_rows 參數。"""
        asof = datetime.date(2026, 4, 30)

        with patch("src.db.get_conn"), \
             patch("pandas.read_sql", return_value=self._mock_read_sql([])) as mock_sql, \
             patch("src.db._query_symbol_earliest", return_value=None):
            from src import db
            try:
                db.read_indices_until("^DJI", asof, min_rows=2)
            except InsufficientDataError:
                pass  # 預期不足，只驗呼叫參數

        # 確認 params tuple 包含正確 symbol / asof / min_rows
        call_args = mock_sql.call_args
        params = call_args.kwargs.get("params")
        assert params is not None
        assert "^DJI" in params
        assert asof in params

    def test_symbol_earliest_attached(self):
        """不足時，symbol_earliest 由 _query_symbol_earliest 提供。"""
        earliest = datetime.date(2026, 1, 2)

        with patch("src.db.get_conn"), \
             patch("pandas.read_sql", return_value=self._mock_read_sql([])), \
             patch("src.db._query_symbol_earliest", return_value=earliest):
            from src import db
            with pytest.raises(InsufficientDataError) as exc_info:
                db.read_indices_until("^NQ", datetime.date(2026, 5, 15), min_rows=2)

        assert exc_info.value.symbol_earliest == earliest


# ---------------------------------------------------------------------------
# upsert_indices_batch — unit (mock)
# ---------------------------------------------------------------------------

class TestUpsertIndicesBatchUnit:
    """mock cursor，只驗呼叫契約（不碰真實 DB）。"""

    def _make_batch(self, symbols=("TXFR1", "^DJI", "^NQ")) -> dict[str, pd.DataFrame]:
        dates = ["2026-05-18", "2026-05-19"]
        return {sym: _make_df(dates) for sym in symbols}

    def test_uses_single_connection(self):
        """驗證 psycopg2.connect 只被呼叫一次（不是每 symbol 各自 connect）。"""
        batch = self._make_batch(symbols=("TXFR1", "^DJI", "^NQ"))

        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch.dict(os.environ, _FAKE_DB_ENV), \
             patch("src.db.psycopg2.connect", return_value=mock_conn) as mock_connect, \
             patch("src.db.execute_values"):
            from src import db
            db.upsert_indices_batch(batch)

        assert mock_connect.call_count == 1

    def test_commits_on_success(self):
        """所有 symbol 成功 → conn.commit() 被呼叫、conn.rollback() 未被呼叫。"""
        batch = self._make_batch()

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch.dict(os.environ, _FAKE_DB_ENV), \
             patch("src.db.psycopg2.connect", return_value=mock_conn), \
             patch("src.db.execute_values"):
            from src import db
            db.upsert_indices_batch(batch)

        mock_conn.commit.assert_called_once()
        mock_conn.rollback.assert_not_called()

    def test_rollback_on_partial_failure(self):
        """第 2 個 symbol 的 execute_values raise → rollback 被呼叫、commit 未被呼叫。"""
        batch = self._make_batch(symbols=("TXFR1", "^DJI", "^NQ"))

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        call_count = {"n": 0}

        def side_effect_execute(cur, sql, data):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise Exception("simulated DB error on 2nd symbol")

        with patch.dict(os.environ, _FAKE_DB_ENV), \
             patch("src.db.psycopg2.connect", return_value=mock_conn), \
             patch("src.db.execute_values", side_effect=side_effect_execute):
            from src import db
            with pytest.raises(Exception, match="simulated DB error"):
                db.upsert_indices_batch(batch)

        mock_conn.rollback.assert_called_once()
        mock_conn.commit.assert_not_called()

    def test_execute_values_called_for_each_symbol(self):
        """每個 symbol 各呼叫一次 execute_values。"""
        symbols = ("TXFR1", "^DJI", "^NQ", "^SPX", "TSM")
        batch = self._make_batch(symbols=symbols)

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch.dict(os.environ, _FAKE_DB_ENV), \
             patch("src.db.psycopg2.connect", return_value=mock_conn), \
             patch("src.db.execute_values") as mock_ev:
            from src import db
            db.upsert_indices_batch(batch)

        assert mock_ev.call_count == len(symbols)


# ---------------------------------------------------------------------------
# upsert_indices thin wrapper
# ---------------------------------------------------------------------------

class TestUpsertIndicesThinWrapper:
    def test_delegates_to_batch(self):
        """upsert_indices(symbol, df) 必須委派給 upsert_indices_batch({symbol: df})。"""
        df = _make_df(["2026-05-18"])

        with patch("src.db.upsert_indices_batch") as mock_batch:
            from src import db
            db.upsert_indices("TXFR1", df)

        mock_batch.assert_called_once_with({"TXFR1": df})


# ---------------------------------------------------------------------------
# earliest_trade_date
# ---------------------------------------------------------------------------

class TestEarliestTradeDate:
    def test_returns_none_on_exception(self):
        """DB 不可用時 earliest_trade_date 回 None 而非拋。"""
        with patch("src.db.get_conn", side_effect=Exception("no db")):
            from src import db
            result = db.earliest_trade_date("TXFR1")
        assert result is None

    def test_returns_none_when_no_rows(self):
        """SELECT MIN(...) 回 None（無資料）→ earliest_trade_date 回 None。"""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = (None,)
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch("src.db.get_conn", return_value=mock_conn):
            from src import db
            result = db.earliest_trade_date("TXFR1")
        assert result is None
