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


def cumulative_pct(df: pd.DataFrame, gap_start: "datetime.date", gap_end: "datetime.date") -> float:
    """計算台股連休期間美股累積漲跌幅 (%)。

    gap_start：gap 內第一個 NYSE session 日期（台股連休第一天）
    gap_end：  gap 內最後一個 NYSE session 日期

    基準收盤 = gap_start 在 df 中前一筆的 close（即台股連休前市場已知的最後美股收盤）。
    終點收盤 = gap_end 的 close。
    回傳幾何累積漲跌 = (終點 / 基準 - 1) × 100，保留 4 位小數。
    Calendar 異常或資料不足 → 0.0 (fail-open，caller 仍有 calendar_anomaly 標籤揭露)。
    """
    try:
        start_ts = pd.Timestamp(gap_start)
        end_ts = pd.Timestamp(gap_end)

        # 找 gap_start 在 df 中的位置
        matching = (df.index == start_ts).nonzero()[0]
        if len(matching) == 0:
            return 0.0
        start_pos = int(matching[0])
        if start_pos == 0:
            return 0.0  # 無前一筆作為基準

        baseline_close = float(df["close"].iloc[start_pos - 1])

        # 找 gap_end 的收盤
        matching_end = (df.index == end_ts).nonzero()[0]
        if len(matching_end) == 0:
            return 0.0
        end_close = float(df["close"].iloc[int(matching_end[0])])

        if baseline_close == 0:
            return 0.0
        return round((end_close - baseline_close) / baseline_close * 100, 4)
    except Exception:
        return 0.0


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
# 富邦資料污染偵測（B5.1 Auto-fallback）
# ---------------------------------------------------------------------------
# 用 FinMind 純日盤資料作為外部 baseline，比對富邦最新 5 日 OHLC。
# 不用富邦自己歷史中位數 — 若整批被夜盤污染，內部 median 已被拉高，detection 失效。

_FINMIND_BASELINE_CACHE: dict[str, tuple[float, pd.DataFrame]] = {}
_FINMIND_BASELINE_TTL = 300  # 5 分鐘 in-process cache，攤平富邦 fetch 都觸發 FinMind 的延遲


def _get_finmind_baseline_cached(lookback_days: int) -> pd.DataFrame:
    """回傳 FinMind 近 N 日 OHLC，5 分鐘 in-process cache。"""
    now = time.time()
    cached = _FINMIND_BASELINE_CACHE.get("baseline")
    if cached and (now - cached[0]) < _FINMIND_BASELINE_TTL:
        return cached[1]
    df = _fetch_txf_finmind(lookback_days)
    _FINMIND_BASELINE_CACHE["baseline"] = (now, df)
    return df


def _is_fubon_polluted(fubon_df: pd.DataFrame, lookback_days: int = 30, threshold: float = 0.005) -> tuple[bool, str]:
    """
    比對富邦最新 5 日 close/high/low vs FinMind baseline，任一日差 > 0.5% → 污染。
    回 (polluted, reason)。FinMind 取不到時保守視為 not_polluted（不阻斷富邦）。
    """
    try:
        finmind = _get_finmind_baseline_cached(lookback_days)
    except Exception as e:
        log.warning("sanity check 無法取得 FinMind baseline: %s", e)
        return (False, "baseline_unavailable")

    # 兩來源 inner join 最近 5 個交易日
    common_idx = fubon_df.index.intersection(finmind.index)
    if len(common_idx) < 3:
        return (False, f"insufficient_overlap (got {len(common_idx)} common dates)")

    recent = sorted(common_idx)[-5:]
    for d in recent:
        fb = fubon_df.loc[d]
        fm = finmind.loc[d]
        for col in ("close", "high", "low"):
            fb_v, fm_v = float(fb[col]), float(fm[col])
            if fm_v == 0:
                continue
            diff = abs(fb_v - fm_v) / fm_v
            if diff > threshold:
                return (True, f"{d.date()} {col}: fubon={fb_v:.2f} vs finmind={fm_v:.2f} (diff={diff:.2%})")
    return (False, "ok")


# ---------------------------------------------------------------------------
# 台指期主入口
# ---------------------------------------------------------------------------

def fetch_txf(lookback_days: int = 200) -> pd.DataFrame:
    """
    台指期 OHLC，回傳 DataFrame，index=trade_date，columns=[open,high,low,close,volume]。

    策略（B5.1）：預設走 FinMind（純日盤、已知安全）。
    富邦路徑僅在 FUBON_TXF_VERIFIED=True 時啟用（B5.1 步驟 4 完成後才設）；
    走富邦時跑跨來源 sanity check，污染判定 → 自動 fallback FinMind。
    FORCE_FINMIND_TXF=1 可強制 FinMind（緊急止血用）。
    """
    # 1. 強制 FinMind 旗標（最高優先）
    if config.FORCE_FINMIND_TXF:
        log.info("FORCE_FINMIND_TXF=1 → 跳過富邦，直接 FinMind")
        return _fetch_txf_finmind(lookback_days)

    # 2. 富邦路徑（僅在驗證通過時啟用）
    if config.FUBON_TXF_VERIFIED and config.FUBON_ID:
        try:
            fubon_df = _fetch_txf_fubon(lookback_days)
            polluted, reason = _is_fubon_polluted(fubon_df, lookback_days)
            if polluted:
                log.error("富邦資料污染偵測，fallback FinMind: %s", reason)
                return _fetch_txf_finmind(lookback_days)
            log.info("富邦 sanity check 通過: %s", reason)
            return fubon_df
        except Exception as e:
            log.warning("富邦 API 失敗，fallback FinMind: %s", e)
            return _fetch_txf_finmind(lookback_days)

    # 3. 預設安全路徑：FinMind
    return _fetch_txf_finmind(lookback_days)


# ---------------------------------------------------------------------------
# 統一入口（main.py 呼叫）
# ---------------------------------------------------------------------------

def fetch_all(lookback_days: int = 200) -> dict[str, pd.DataFrame]:
    """抓美股四指數 + 台指期，回 {key: DataFrame}。"""
    data = fetch_us()
    data["txf"] = fetch_txf(lookback_days)
    return data
