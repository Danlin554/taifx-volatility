from __future__ import annotations

import datetime
import logging
import os

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values

log = logging.getLogger(__name__)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS tvol_indices_daily (
  symbol     TEXT        NOT NULL,
  trade_date DATE        NOT NULL,
  open_px    NUMERIC(12,4),
  high_px    NUMERIC(12,4),
  low_px     NUMERIC(12,4),
  close_px   NUMERIC(12,4),
  volume     BIGINT,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (symbol, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_tvol_indices_date
  ON tvol_indices_daily(trade_date DESC);
"""

_UPSERT = """
INSERT INTO tvol_indices_daily
  (symbol, trade_date, open_px, high_px, low_px, close_px, volume)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (symbol, trade_date) DO UPDATE SET
  open_px    = EXCLUDED.open_px,
  high_px    = EXCLUDED.high_px,
  low_px     = EXCLUDED.low_px,
  close_px   = EXCLUDED.close_px,
  volume     = EXCLUDED.volume,
  fetched_at = NOW();
"""

_UPSERT_BATCH = """
INSERT INTO tvol_indices_daily
  (symbol, trade_date, open_px, high_px, low_px, close_px, volume)
VALUES %s
ON CONFLICT (symbol, trade_date) DO UPDATE SET
  open_px    = EXCLUDED.open_px,
  high_px    = EXCLUDED.high_px,
  low_px     = EXCLUDED.low_px,
  close_px   = EXCLUDED.close_px,
  volume     = EXCLUDED.volume,
  fetched_at = NOW()
"""


def get_conn():
    conn = psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=10)
    with conn.cursor() as cur:
        cur.execute("SET timezone='Asia/Taipei'")
    return conn


def init_tvol_tables():
    """建表（idempotent），啟動時跑一次。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_CREATE_TABLE)
        conn.commit()
    log.info("tvol tables initialized")


# ---------------------------------------------------------------------------
# 寫入
# ---------------------------------------------------------------------------

def _rows_for(symbol: str, df: pd.DataFrame) -> list:
    return [
        (
            symbol,
            row.Index.date() if hasattr(row.Index, "date") else row.Index,
            float(row.open) if not pd.isna(row.open) else None,
            float(row.high) if not pd.isna(row.high) else None,
            float(row.low) if not pd.isna(row.low) else None,
            float(row.close) if not pd.isna(row.close) else None,
            int(row.volume) if not pd.isna(row.volume) else None,
        )
        for row in df.itertuples()
    ]


def upsert_indices(symbol: str, df: pd.DataFrame) -> None:
    """thin wrapper — 委派給 batch 版本（單一 transaction）。"""
    upsert_indices_batch({symbol: df})


def upsert_indices_batch(rows: dict[str, pd.DataFrame]) -> None:
    """
    在單一 connection + 單一 transaction 內 upsert 多個 symbol。
    任一 symbol 寫入失敗 → 整批 ROLLBACK，避免部分提交污染 DB。
    """
    conn = psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute("SET timezone='Asia/Taipei'")
            for symbol, df in rows.items():
                data_rows = _rows_for(symbol, df)
                if data_rows:
                    execute_values(cur, _UPSERT_BATCH, data_rows)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    log.info("upsert_indices_batch done symbols=%s", list(rows.keys()))


# ---------------------------------------------------------------------------
# 讀取
# ---------------------------------------------------------------------------

def read_indices_until(
    symbol: str, asof: datetime.date, min_rows: int
) -> pd.DataFrame:
    """
    從 DB 讀 symbol 在 asof（含）之前的最近 min_rows 筆，升冪排序回傳。
    不足 min_rows 筆 → raise InsufficientDataError。
    """
    from src.exceptions import InsufficientDataError

    sql = """
    SELECT trade_date, open_px, high_px, low_px, close_px, volume
    FROM tvol_indices_daily
    WHERE symbol = %s AND trade_date <= %s
    ORDER BY trade_date DESC
    LIMIT %s
    """
    with get_conn() as conn:
        df = pd.read_sql(
            sql, conn,
            params=(symbol, asof, min_rows),
            parse_dates=["trade_date"],
        )

    df = df.rename(columns={
        "open_px": "open", "high_px": "high",
        "low_px": "low", "close_px": "close",
    })
    df = df.set_index("trade_date")
    df = df.sort_index()  # ascending

    if len(df) < min_rows:
        actual_last = df.index[-1].date() if len(df) > 0 else None
        symbol_earliest = _query_symbol_earliest(symbol)
        raise InsufficientDataError(
            earliest=None,          # caller（read_db_data）填 TXFR1 earliest
            got=len(df),
            symbol=symbol,
            actual_last=actual_last,
            symbol_earliest=symbol_earliest,
        )

    return df


def earliest_trade_date(symbol: str) -> datetime.date | None:
    """回傳 DB 中 symbol 的最早 trade_date，無資料或 DB 不可用回 None。"""
    return _query_symbol_earliest(symbol)


def _query_symbol_earliest(symbol: str) -> datetime.date | None:
    sql = "SELECT MIN(trade_date) FROM tvol_indices_daily WHERE symbol = %s"
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (symbol,))
                result = cur.fetchone()
        if result and result[0]:
            return result[0]
        return None
    except Exception as e:
        log.warning("_query_symbol_earliest failed: %s", e)
        return None


def read_indices(symbol: str, lookback_days: int = 90) -> pd.DataFrame:
    """從 DB 讀最近 N 天的日 K（用日曆天，適合快速看圖）。"""
    sql = """
    SELECT trade_date, open_px, high_px, low_px, close_px, volume
    FROM tvol_indices_daily
    WHERE symbol = %s AND trade_date >= CURRENT_DATE - %s
    ORDER BY trade_date
    """
    with get_conn() as conn:
        df = pd.read_sql(
            sql, conn,
            params=(symbol, lookback_days),
            parse_dates=["trade_date"],
        )
    df = df.rename(columns={
        "open_px": "open", "high_px": "high",
        "low_px": "low", "close_px": "close",
    })
    df = df.set_index("trade_date")
    return df
