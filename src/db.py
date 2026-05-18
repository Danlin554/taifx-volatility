import logging
import os
import psycopg2
import pandas as pd

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


def get_conn():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
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


def upsert_indices(symbol: str, df: pd.DataFrame):
    """把 DataFrame 寫進 tvol_indices_daily（upsert）。"""
    rows = [
        (
            symbol,
            row.Index.date(),
            float(row.open) if not pd.isna(row.open) else None,
            float(row.high) if not pd.isna(row.high) else None,
            float(row.low)  if not pd.isna(row.low)  else None,
            float(row.close) if not pd.isna(row.close) else None,
            int(row.volume) if not pd.isna(row.volume) else None,
        )
        for row in df.itertuples()
    ]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany(_UPSERT, rows)
        conn.commit()
    log.info("upserted %d rows for %s", len(rows), symbol)


def read_indices(symbol: str, lookback_days: int = 90) -> pd.DataFrame:
    """從 DB 讀最近 N 天的日 K，回 DataFrame columns=[open,high,low,close,volume]。"""
    sql = """
    SELECT trade_date, open_px, high_px, low_px, close_px, volume
    FROM tvol_indices_daily
    WHERE symbol = %s AND trade_date >= CURRENT_DATE - %s
    ORDER BY trade_date
    """
    with get_conn() as conn:
        df = pd.read_sql(sql, conn, params=(symbol, lookback_days), parse_dates=["trade_date"])
    df = df.rename(columns={
        "trade_date": "trade_date",
        "open_px": "open", "high_px": "high",
        "low_px": "low", "close_px": "close",
    })
    df = df.set_index("trade_date")
    return df
