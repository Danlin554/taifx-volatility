from __future__ import annotations

import math
from types import MappingProxyType

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# OHLC 完整性驗證（B5.2.b，live + DB 路徑共用）
# ---------------------------------------------------------------------------

def validate_txf_ohlc_frame(df: pd.DataFrame, *, tail: int = 20) -> list[str]:
    """檢查 TXF DataFrame 最近 N 筆 OHLC 完整性。違反清單回傳 list[str]，空 list = 通過。

    規則：
      - open/high/low/close 必須 finite（非 NaN/inf）
      - 四欄必須 > 0
      - high >= max(open, close) 且 high >= low
      - low <= min(open, close)
    """
    violations: list[str] = []
    sub = df.tail(tail)
    for col in ("open", "high", "low", "close"):
        if col not in sub.columns:
            violations.append(f"missing column: {col}")
            return violations
    for d, row in sub.iterrows():
        o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
        date_str = d.date().isoformat() if hasattr(d, "date") else str(d)
        for name, v in (("open", o), ("high", h), ("low", l), ("close", c)):
            if not np.isfinite(v):
                violations.append(f"{date_str}: {name}={v} not finite")
            elif v <= 0:
                violations.append(f"{date_str}: {name}={v} <= 0")
        if np.isfinite(o) and np.isfinite(h) and np.isfinite(l) and np.isfinite(c):
            if h < max(o, c):
                violations.append(f"{date_str}: high={h} < max(open={o}, close={c})")
            if h < l:
                violations.append(f"{date_str}: high={h} < low={l}")
            if l > min(o, c):
                violations.append(f"{date_str}: low={l} > min(open={o}, close={c})")
    return violations


# ---------------------------------------------------------------------------
# Rounding（half-up，與前端 JS Math.floor(x+0.5) 完全一致）
# ---------------------------------------------------------------------------

def _round_half_up(x: float) -> int:
    """Half-up 四捨五入，避免 Python round() 的銀行家捨入。"""
    return int(math.floor(x + 0.5))


# ---------------------------------------------------------------------------
# 簡單振幅（主要指標）
# ---------------------------------------------------------------------------

def simple_range(df: pd.DataFrame) -> pd.Series:
    """簡單振幅 H-L，不含跳空修正。這是試算表的主要計算基礎。"""
    hl = df["high"] - df["low"]
    hl.index = df.index
    return hl


# ---------------------------------------------------------------------------
# ATR（補充指標）
# ---------------------------------------------------------------------------

def true_range(df: pd.DataFrame) -> pd.Series:
    """ATR 的 True Range：考慮跳空的當日振幅。第 1 筆無前收用 high-low 兜底。"""
    prev_close = df["close"].shift(1)
    hl = df["high"] - df["low"]
    hc = (df["high"] - prev_close).abs()
    lc = (df["low"] - prev_close).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    tr.iloc[0] = df["high"].iloc[0] - df["low"].iloc[0]
    return tr


def atr_sma(tr: pd.Series, n: int) -> float:
    """N 日 TR 的簡單移動平均（SMA），取最後一筆。"""
    return float(tr.rolling(n).mean().iloc[-1])


def atr_wilder(tr: pd.Series, n: int) -> float:
    """Wilder 平滑 ATR：seed = 前 N 筆 TR 的 SMA，ATR_t = (ATR_{t-1}×(n-1) + TR_t) / n。"""
    if len(tr) < n:
        return float("nan")
    seed = float(tr.iloc[:n].mean())
    atr = seed
    for val in tr.iloc[n:]:
        atr = (atr * (n - 1) + val) / n
    return atr


# ---------------------------------------------------------------------------
# 統計
# ---------------------------------------------------------------------------

def rolling_stats(hl: pd.Series) -> dict:
    """基於 H-L 的 SMA 與標準差。資料不足時相應項目回 NaN。"""
    return {
        "a20": float(hl.rolling(20).mean().iloc[-1]),
        "a10": float(hl.rolling(10).mean().iloc[-1]),
        "a5":  float(hl.rolling(5).mean().iloc[-1]),
        "s20": float(hl.rolling(20).std(ddof=1).iloc[-1]),
        "s10": float(hl.rolling(10).std(ddof=1).iloc[-1]),
        "s5":  float(hl.rolling(5).std(ddof=1).iloc[-1]),
    }


def atr_rolling_stats(tr: pd.Series) -> dict:
    """基於 ATR True Range 的統計。"""
    return {
        "atr20":  atr_sma(tr, 20),
        "atr10":  atr_sma(tr, 10),
        "atr5":   atr_sma(tr, 5),
        "atr20w": atr_wilder(tr, 20),
        "atr10w": atr_wilder(tr, 10),
        "atr5w":  atr_wilder(tr, 5),
    }


def sigma_bands(a10: float, s10: float) -> tuple[float, float, float]:
    """一/二/三個標準差震幅 = a10 + N × s10。"""
    return a10 + s10, a10 + 2 * s10, a10 + 3 * s10


def weekday_avg(hl: pd.Series, months: int = 6) -> dict:
    """最近 N 個月的週別平均振幅。"""
    if hl.empty:
        return {"mon": 0.0, "tue": 0.0, "wed": 0.0, "thu": 0.0, "fri": 0.0}
    cutoff = hl.index[-1] - pd.DateOffset(months=months)
    recent = hl[hl.index >= cutoff]
    by_day = recent.groupby(recent.index.weekday).mean()
    keys = ["mon", "tue", "wed", "thu", "fri"]
    return {keys[i]: round(float(by_day.get(i, 0)), 2) for i in range(5)}


def weekday_avg_multi(hl: pd.Series) -> dict:
    """回 {m1, m2, m3, m6}，每個值為 5 個週別的平均振幅 dict；資料不足時為 None。"""
    return {
        key: _weekday_avg_for_months(hl, months)
        for months, key in [(1, "m1"), (2, "m2"), (3, "m3"), (6, "m6")]
    }


def _weekday_avg_for_months(hl: pd.Series, months: int) -> dict | None:
    if hl.empty:
        return None
    cutoff = hl.index[-1] - pd.DateOffset(months=months)
    recent = hl[hl.index >= cutoff]
    if len(recent) == 0:
        return None
    by_day = recent.groupby(recent.index.weekday).mean()
    keys = ["mon", "tue", "wed", "thu", "fri"]
    result = {}
    for i, k in enumerate(keys):
        val = by_day.get(i, None)
        result[k] = round(float(val), 2) if val is not None and not pd.isna(val) else None
    return result


# ---------------------------------------------------------------------------
# 當沖目標
# ---------------------------------------------------------------------------

def daytrade_targets(hl: pd.Series, window: int = 20) -> dict:
    """
    當沖目標分級（b1=MIN / b2=AVERAGEIF<avg / b3=AVG / b4=AVERAGEIF>avg / b5=MAX）。
    資料不足時回 NaN。
    """
    recent = hl.iloc[-window:]
    if len(recent) == 0:
        nan = float("nan")
        return {"b1": nan, "b2": nan, "b3": nan, "b4": nan, "b5": nan}
    avg = float(recent.mean())
    below = recent[recent < avg]
    above = recent[recent > avg]
    return {
        "b1": round(float(recent.min()), 1),
        "b2": round(float(below.mean()) if len(below) > 0 else avg, 1),
        "b3": round(avg, 2),
        "b4": round(float(above.mean()) if len(above) > 0 else avg, 1),
        "b5": round(float(recent.max()), 1),
    }


def calc_r(a10: float) -> int:
    """單位風險 R = FLOOR(10日平均振幅 / 5, 1)。"""
    return int(math.floor(a10 / 5))


# ---------------------------------------------------------------------------
# 預估開盤
# ---------------------------------------------------------------------------

DEFAULT_OPEN_WEIGHTS = MappingProxyType({"dj": 50, "nq": 50, "spy": 0, "tsm": 0})


def default_open_price(
    prev_close: float,
    us_pcts: dict,
    weights: dict | None = None,
) -> int:
    """
    美股加權漲幅 → D+1 預估開盤（half-up 整數）。
    weights 預設 DJ50/NQ50/SPY0/TSM0；傳入 {} 會 raise ValueError。
    """
    if weights is None:
        weights = DEFAULT_OPEN_WEIGHTS
    missing = {"dj", "nq", "spy", "tsm"} - weights.keys()
    if missing:
        raise ValueError(f"default_open_price weights missing keys: {missing}")

    total_w = sum(weights.values())
    if total_w == 0:
        weighted_pct = 0.0
    else:
        weighted_pct = sum(us_pcts[k] * weights[k] for k in weights) / total_w

    return _round_half_up(prev_close * (1 + weighted_pct / 100))


# ---------------------------------------------------------------------------
# 舊版預估（保留 backward-compat）
# ---------------------------------------------------------------------------

def forecast(prev_close: float, us_pcts: dict, weights: dict) -> dict:
    """美股加權漲幅 → 台指期預估開盤，合理範圍 = 期貨預估 × ±0.5%。"""
    total_w = sum(weights.values())
    if total_w == 0:
        weighted_pct = 0.0
    else:
        weighted_pct = sum(us_pcts[k] * weights[k] for k in us_pcts) / total_w

    fut_est = round(prev_close * (1 + weighted_pct / 100))
    return {
        "weighted_pct": weighted_pct,
        "fut_est": int(fut_est),
        "range_low": round(fut_est * 0.995),
        "range_high": round(fut_est * 1.005),
    }


def build_forecast(prev_close: float, us_pcts: dict, weights: dict) -> dict:
    """含合理範圍的完整預估。"""
    total_w = sum(weights.values())
    weighted_pct = (
        sum(us_pcts[k] * weights[k] for k in us_pcts) / total_w
        if total_w > 0 else 0.0
    )
    fut_est = round(prev_close * (1 + weighted_pct / 100))
    return {
        "weighted_pct": round(weighted_pct, 4),
        "fut_est": int(fut_est),
        "range_low": round(fut_est * 0.995),
        "range_high": round(fut_est * 1.005),
    }
