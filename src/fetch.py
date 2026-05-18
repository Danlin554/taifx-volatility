import datetime
import logging
import time
from collections import defaultdict

import pandas as pd
import requests
import yfinance as yf

from src import config

log = logging.getLogger(__name__)

_RENAME = {"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"}


# ---------------------------------------------------------------------------
# 美股指數（yfinance）
# ---------------------------------------------------------------------------

def _fetch_one(symbol: str, period: str = "6mo", retries: int = 3) -> pd.DataFrame:
    """從 yfinance 抓單一 symbol 的日 K，回 columns=[open,high,low,close,volume]。"""
    delay = 1.0
    for attempt in range(retries):
        try:
            ticker = yf.Ticker(symbol)
            df = ticker.history(period=period, auto_adjust=False)
            if df.empty:
                raise ValueError(f"{symbol} returned empty DataFrame")
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df[["Open", "High", "Low", "Close", "Volume"]].rename(columns=_RENAME)
            df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
            df.index.name = "trade_date"
            return df
        except Exception as e:
            if attempt < retries - 1:
                log.warning("fetch %s attempt %d failed: %s, retrying in %.0fs", symbol, attempt + 1, e, delay)
                time.sleep(delay)
                delay *= 2
            else:
                raise


def fetch_us() -> dict[str, pd.DataFrame]:
    """抓美股四指數（DJ/NQ/SPY/TSM），回 {key: DataFrame}。"""
    result = {}
    for key, symbol in config.US_SYMBOLS.items():
        result[key] = _fetch_one(symbol)
        log.info("fetched %s (%d rows)", symbol, len(result[key]))
    return result


def latest_pct(df: pd.DataFrame) -> float:
    """計算最新一日相對前一日的收盤漲跌幅 (%)。需要至少 2 筆。"""
    if len(df) < 2:
        return 0.0
    prev_close = float(df["close"].iloc[-2])
    last_close = float(df["close"].iloc[-1])
    if prev_close == 0:
        return 0.0
    return round((last_close - prev_close) / prev_close * 100, 4)


# ---------------------------------------------------------------------------
# 台指期（TXFR1）—— 富邦 API（主）
# ---------------------------------------------------------------------------

def _fetch_txf_fubon(lookback_days: int) -> pd.DataFrame:
    """
    富邦 API 抓 TXFR1 近月台指期歷史 OHLC（日盤）。
    需在 Zeabur 設定環境變數：FUBON_ID, FUBON_PWD, FUBON_CERT_PATH, FUBON_CERT_PWD。
    """
    if not all([config.FUBON_ID, config.FUBON_PWD, config.FUBON_CERT_PATH, config.FUBON_CERT_PWD]):
        raise EnvironmentError("富邦憑證未設定（FUBON_ID/PWD/CERT_PATH/CERT_PWD）")

    from fubon_neo.sdk import FubonSDK  # type: ignore

    sdk = FubonSDK()
    sdk.login(
        id=config.FUBON_ID,
        pwd=config.FUBON_PWD,
        cert_path=config.FUBON_CERT_PATH,
        cert_pwd=config.FUBON_CERT_PWD,
    )

    end_dt = datetime.date.today()
    start_dt = end_dt - datetime.timedelta(days=lookback_days)

    result = sdk.marketdata.rest_client.future.history.ohlc(
        id=config.TXF_SYMBOL,
        from_date=start_dt.isoformat(),
        to_date=end_dt.isoformat(),
    )
    rows = result.data if hasattr(result, "data") else result
    df = pd.DataFrame(rows)
    df["trade_date"] = pd.to_datetime(df["date"])
    df = df.rename(columns={"open": "open", "high": "high", "low": "low", "close": "close"})
    df = df[["trade_date", "open", "high", "low", "close"]].set_index("trade_date")
    df.index = df.index.normalize()
    df["volume"] = 0
    log.info("fubon fetched TXFR1 (%d rows)", len(df))
    return df


# ---------------------------------------------------------------------------
# 台指期（TX）—— FinMind API（備援）
# ---------------------------------------------------------------------------
# FinMind 提供台指期每日行情（OHLC）：https://api.finmindtrade.com
# 免費帳號每小時 600 requests；position session = 日盤結算資料。
# 可設 FINMIND_TOKEN env var 解鎖更高頻率上限。

_FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"


def _fetch_txf_finmind(lookback_days: int) -> pd.DataFrame:
    """
    FinMind API 抓 TX 台指期近月合約日盤 OHLC。
    使用 position session（日盤結算），近月 = 該日最高成交量的單月合約。
    """
    end_dt = datetime.date.today()
    start_dt = end_dt - datetime.timedelta(days=lookback_days)

    params: dict = {
        "dataset": "TaiwanFuturesDaily",
        "data_id": "TX",
        "start_date": start_dt.isoformat(),
    }
    finmind_token = config.FINMIND_TOKEN
    if finmind_token:
        params["token"] = finmind_token

    resp = requests.get(_FINMIND_URL, params=params, timeout=60)
    resp.raise_for_status()

    payload = resp.json()
    if payload.get("status") != 200:
        raise RuntimeError(f"FinMind API error: {payload.get('msg')}")

    records = payload["data"]
    if not records:
        raise RuntimeError("FinMind 回傳空資料")

    # 只取日盤（position session）+ 單月合約（排除價差合約）
    day_records = [
        r for r in records
        if r["trading_session"] == "position" and "/" not in r["contract_date"]
    ]
    if not day_records:
        raise RuntimeError("FinMind 無 position session 資料")

    # 按日分組，每日取最高成交量的合約（近月）
    by_date: dict[str, list] = defaultdict(list)
    for r in day_records:
        by_date[r["date"]].append(r)

    rows = []
    for date_str, recs in sorted(by_date.items()):
        best = max(recs, key=lambda x: x["volume"])
        open_px = best["open"] if best["open"] else best["close"]
        rows.append({
            "trade_date": pd.Timestamp(date_str),
            "open":   float(open_px),
            "high":   float(best["max"]),
            "low":    float(best["min"]),
            "close":  float(best["close"]),
            "volume": int(best["volume"]),
        })

    df = pd.DataFrame(rows).set_index("trade_date")
    df.index = df.index.normalize()
    df = df.dropna(subset=["open", "high", "low", "close"])
    log.info("finmind fetched TX (%d rows)", len(df))
    return df


# ---------------------------------------------------------------------------
# 台指期主入口
# ---------------------------------------------------------------------------

def fetch_txf(lookback_days: int = 200) -> pd.DataFrame:
    """
    台指期 OHLC：富邦 API 優先，備援 FinMind 公開 API。
    回傳 DataFrame，index=trade_date，columns=[open,high,low,close,volume]。
    """
    # 1. 富邦 API
    if config.FUBON_ID:
        try:
            return _fetch_txf_fubon(lookback_days)
        except Exception as e:
            log.warning("富邦 API 失敗，切換 FinMind：%s", e)

    # 2. FinMind 公開 API（不需憑證）
    return _fetch_txf_finmind(lookback_days)


# ---------------------------------------------------------------------------
# 統一入口（main.py 呼叫）
# ---------------------------------------------------------------------------

def fetch_all(lookback_days: int = 200) -> dict[str, pd.DataFrame]:
    """抓美股四指數 + 台指期，回 {key: DataFrame}。"""
    data = fetch_us()
    data["txf"] = fetch_txf(lookback_days)
    return data
