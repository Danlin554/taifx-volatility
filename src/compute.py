import math
import pandas as pd


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
    """
    基於簡單振幅 (H-L) 的統計。
    回傳 a20/a10/a5（SMA）與 s20/s10/s5（樣本標準差 ddof=1）。
    """
    return {
        "a20": float(hl.rolling(20).mean().iloc[-1]),
        "a10": float(hl.rolling(10).mean().iloc[-1]),
        "a5":  float(hl.rolling(5).mean().iloc[-1]),
        "s20": float(hl.rolling(20).std(ddof=1).iloc[-1]),
        "s10": float(hl.rolling(10).std(ddof=1).iloc[-1]),
        "s5":  float(hl.rolling(5).std(ddof=1).iloc[-1]),
    }


def atr_rolling_stats(tr: pd.Series) -> dict:
    """基於 ATR True Range 的統計，作為補充輸出。"""
    return {
        "atr20": atr_sma(tr, 20),
        "atr10": atr_sma(tr, 10),
        "atr5":  atr_sma(tr, 5),
        "atr20w": atr_wilder(tr, 20),
        "atr10w": atr_wilder(tr, 10),
        "atr5w":  atr_wilder(tr, 5),
    }


def sigma_bands(a10: float, s10: float) -> tuple[float, float, float]:
    """一/二/三個標準差震幅 = a10 + N × s10。"""
    return a10 + s10, a10 + 2 * s10, a10 + 3 * s10


def weekday_avg(hl: pd.Series, months: int = 6) -> dict:
    """最近 N 個月的週別平均振幅（基於簡單 H-L）。"""
    cutoff = hl.index[-1] - pd.DateOffset(months=months)
    recent = hl[hl.index >= cutoff]
    by_day = recent.groupby(recent.index.weekday).mean()
    keys = ["mon", "tue", "wed", "thu", "fri"]
    return {keys[i]: round(float(by_day.get(i, 0)), 2) for i in range(5)}


# ---------------------------------------------------------------------------
# 當沖目標
# ---------------------------------------------------------------------------

def daytrade_targets(hl: pd.Series, window: int = 20) -> dict:
    """
    當沖目標分級，基於最近 window 筆 H-L 振幅：
      b1 = MIN（一壘打）
      b2 = AVERAGEIF < avg（二壘打）
      b3 = AVERAGE（三壘打）
      b4 = AVERAGEIF > avg（場內全壘打）
      b5 = MAX（場外全壘打）
    對應試算表 TAIFX 欄的公式，非固定倍率。
    """
    recent = hl.iloc[-window:]
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
# 預估開盤（前端用，weights 來自 localStorage）
# ---------------------------------------------------------------------------

def forecast(prev_close: float, us_pcts: dict, weights: dict) -> dict:
    """
    美股加權漲幅 → 台指期預估開盤，合理範圍 = 期貨預估 × ±0.5%。
    分母為 0 時 weighted_pct = 0（全權重設 0 的狀況）。
    """
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
    """含合理範圍的完整預估（合理範圍 = 期貨預估 × ±0.5%）。"""
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
