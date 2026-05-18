"""
驗證腳本：從 yfinance 抓 ^TWII，用 SMA/Wilder/range 三種算法對比 CSV 期望值。
用法：uv run python -m src.verify --asof 2026-05-18
"""

import argparse
import datetime
import sys

import pandas as pd
import yfinance as yf

EXPECTED = {
    "a20": 296.85, "a10": 368.4, "a5": 409.4,
    "s20": 153.18, "s10": 184.29, "s5": 233.67,
    "sig1": 552.69, "sig2": 736.98, "sig3": 921.27,
}
PASS_THRESHOLD = 5.0  # 允許誤差點數


def _fetch(asof: datetime.date) -> pd.DataFrame:
    end = asof + datetime.timedelta(days=1)
    df = yf.download("^TWII", start="2025-10-01", end=end.isoformat(), auto_adjust=False, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close"]].rename(columns=str.lower)
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    return df


def _tr(df):
    from src.compute import true_range
    return true_range(df)


def _results(tr, method):
    from src.compute import atr_sma, atr_wilder, sigma_bands
    if method == "sma":
        a20, a10, a5 = atr_sma(tr, 20), atr_sma(tr, 10), atr_sma(tr, 5)
    elif method == "wilder":
        a20, a10, a5 = atr_wilder(tr, 20), atr_wilder(tr, 10), atr_wilder(tr, 5)
    else:  # range
        hl = tr  # simple high-low passed in; compute separately below
        a20 = float(hl.rolling(20).mean().iloc[-1])
        a10 = float(hl.rolling(10).mean().iloc[-1])
        a5  = float(hl.rolling(5).mean().iloc[-1])

    s20 = float(tr.rolling(20).std(ddof=1).iloc[-1])
    s10 = float(tr.rolling(10).std(ddof=1).iloc[-1])
    s5  = float(tr.rolling(5).std(ddof=1).iloc[-1])
    sig1, sig2, sig3 = sigma_bands(a10, s10)
    return {
        "a20": a20, "a10": a10, "a5": a5,
        "s20": s20, "s10": s10, "s5": s5,
        "sig1": sig1, "sig2": sig2, "sig3": sig3,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--asof", default=datetime.date.today().isoformat())
    args = parser.parse_args()
    asof = datetime.date.fromisoformat(args.asof)

    print(f"Fetching ^TWII up to {asof} ...")
    df = _fetch(asof)
    print(f"  {len(df)} rows, last date: {df.index[-1].date()}\n")

    tr = _tr(df)
    hl = df["high"] - df["low"]

    methods = ["sma", "wilder", "range"]
    all_ok = True

    header = f"{'metric':<8} {'expected':>10}"
    for m in methods:
        header += f"  {m:>10}"
    header += f"  {'best diff':>10}"
    print(header)
    print("-" * (len(header) + 10))

    rows_data = {}
    for m in methods:
        tr_input = hl if m == "range" else tr
        rows_data[m] = _results(tr_input, m)

    for key in EXPECTED:
        exp = EXPECTED[key]
        diffs = {m: abs(rows_data[m][key] - exp) for m in methods}
        best = min(diffs, key=diffs.get)
        best_diff = diffs[best]
        status = "✓" if best_diff <= PASS_THRESHOLD else "✗"
        if best_diff > PASS_THRESHOLD:
            all_ok = False
        row = f"{key:<8} {exp:>10.2f}"
        for m in methods:
            row += f"  {rows_data[m][key]:>10.2f}"
        row += f"  {best_diff:>8.2f} ({best}) {status}"
        print(row)

    print()
    if all_ok:
        print("✓ ALL PASS — 所有誤差 ≤ {} 點".format(PASS_THRESHOLD))
    else:
        print("✗ SOME FAIL — 誤差超過 {} 點，請檢查資料來源與算法".format(PASS_THRESHOLD))
        sys.exit(1)


if __name__ == "__main__":
    main()
