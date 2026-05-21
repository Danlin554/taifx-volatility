"""
台指期資料源驗證工具（B5.1）。

兩個 mode：
- `--days N`：直接呼叫 _fetch_txf_fubon vs _fetch_txf_finmind，diff 兩來源。
  富邦污染確認 → exit 1（這是正確結果，證實根因）。
- `--db-check --days N`：讀 DB tvol_indices_daily TXFR1，比對 FinMind。
  DB 必須與 FinMind 一致 → exit 0；不一致 → exit 1。

兩 mode 共用：
- TPE timezone 正規化（兩來源 index 轉 Asia/Taipei date）
- 排除 partial bar（過濾今日台北日界線之後的 bar）
- inner join + min_joined_rows + latest_date 一致性檢查
- OHLC 關係檢查（high >= max(o,c), low <= min(o,c)）
- 失敗閾值 0.5%（close/high/low 任一超過 → 污染）

Exit codes：
- 0: 通過（兩來源/DB 與 FinMind 一致）
- 1: 污染確認（差異 > 0.5% 或 OHLC 關係違反或 latest date 落後）
- 2: 資料不足（join rows < min_joined_rows）
"""

from __future__ import annotations

import argparse
import datetime
import logging
import sys

import pandas as pd

from src import config, fetch

log = logging.getLogger(__name__)

DIFF_THRESHOLD = 0.005  # 0.5%
MIN_JOINED_ROWS_DEFAULT = 20
LATEST_DATE_TOLERANCE_DAYS = 1


def _to_tpe_date_index(df: pd.DataFrame) -> pd.DataFrame:
    """把 index 不論 UTC/naive 一律轉成 Asia/Taipei date（DatetimeIndex normalized）。"""
    idx = df.index
    if idx.tz is None:
        # naive 視為已是 TPE date（fetch 內部已 normalize）
        new_idx = pd.DatetimeIndex(idx).normalize()
    else:
        new_idx = idx.tz_convert(config.TZ).tz_localize(None).normalize()
    out = df.copy()
    out.index = new_idx
    return out


def _exclude_partial_bar(df: pd.DataFrame) -> pd.DataFrame:
    """過濾今日台北日界線之後尚未收盤的 bar。"""
    today_tpe = datetime.datetime.now(config.TZ).date()
    today_ts = pd.Timestamp(today_tpe)
    return df[df.index < today_ts]


def _check_ohlc_relationships(df: pd.DataFrame, source_name: str) -> list[str]:
    """檢查 OHLC 關係：high >= max(o,c), low <= min(o,c)。回傳違反清單。"""
    violations = []
    for d, row in df.iterrows():
        o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
        max_oc = max(o, c)
        min_oc = min(o, c)
        if h < max_oc or h < l:
            violations.append(f"{source_name} {d.date()}: high={h} < max(o,c)={max_oc} or < low={l}")
        if l > min_oc:
            violations.append(f"{source_name} {d.date()}: low={l} > min(o,c)={min_oc}")
    return violations


def _compare_frames(
    fubon: pd.DataFrame,
    finmind: pd.DataFrame,
    *,
    left_label: str = "fubon",
    min_joined_rows: int = MIN_JOINED_ROWS_DEFAULT,
) -> tuple[int, dict]:
    """
    比對兩個 OHLC DataFrame。回 (exit_code, report)。
    report 結構：{
      joined_rows, left_only_dates, right_only_dates,
      latest_left, latest_finmind, latest_lag_days,
      ohlc_violations, price_diffs (list of dict per failing day),
    }
    """
    report: dict = {
        "joined_rows": 0,
        "left_only_dates": [],
        "right_only_dates": [],
        "latest_left": None,
        "latest_finmind": None,
        "latest_lag_days": None,
        "ohlc_violations": [],
        "price_diffs": [],
    }

    left_idx = set(fubon.index)
    right_idx = set(finmind.index)
    common = sorted(left_idx & right_idx)
    left_only = sorted(left_idx - right_idx)
    right_only = sorted(right_idx - left_idx)

    report["joined_rows"] = len(common)
    report["left_only_dates"] = [d.date().isoformat() for d in left_only]
    report["right_only_dates"] = [d.date().isoformat() for d in right_only]

    if len(common) < min_joined_rows:
        return (2, report)

    # latest date 一致性
    latest_left = max(fubon.index) if len(fubon) else None
    latest_fm = max(finmind.index) if len(finmind) else None
    report["latest_left"] = latest_left.date().isoformat() if latest_left is not None else None
    report["latest_finmind"] = latest_fm.date().isoformat() if latest_fm is not None else None
    if latest_left is not None and latest_fm is not None:
        lag_days = (latest_fm - latest_left).days
        report["latest_lag_days"] = lag_days
        if abs(lag_days) > LATEST_DATE_TOLERANCE_DAYS:
            return (1, report)

    # OHLC 關係檢查
    report["ohlc_violations"] = (
        _check_ohlc_relationships(fubon, left_label)
        + _check_ohlc_relationships(finmind, "finmind")
    )

    # 逐日比對 close/high/low/open
    has_diff = False
    for d in common:
        l_row = fubon.loc[d]
        r_row = finmind.loc[d]
        day_diff = {"date": d.date().isoformat()}
        any_exceed = False
        for col in ("open", "close", "high", "low"):
            l_v = float(l_row[col]) if col in l_row.index else None
            r_v = float(r_row[col]) if col in r_row.index else None
            if l_v is None or r_v is None or r_v == 0:
                continue
            diff = abs(l_v - r_v) / r_v
            day_diff[f"{col}_{left_label}"] = l_v
            day_diff[f"{col}_finmind"] = r_v
            day_diff[f"{col}_diff_pct"] = round(diff * 100, 4)
            if col in ("close", "high", "low") and diff > DIFF_THRESHOLD:
                any_exceed = True
        if any_exceed:
            report["price_diffs"].append(day_diff)
            has_diff = True

    if has_diff or report["ohlc_violations"]:
        return (1, report)
    return (0, report)


def _print_report(report: dict, *, mode: str, left_label: str) -> None:
    print(f"\n=== verify_sources report ({mode}) ===")
    print(f"  joined_rows = {report['joined_rows']}")
    print(f"  latest_{left_label} = {report['latest_left']}")
    print(f"  latest_finmind = {report['latest_finmind']}")
    print(f"  latest_lag_days = {report['latest_lag_days']}")
    if report["left_only_dates"]:
        print(f"  {left_label}_only_dates ({len(report['left_only_dates'])}): {report['left_only_dates'][:5]}...")
    if report["right_only_dates"]:
        print(f"  finmind_only_dates ({len(report['right_only_dates'])}): {report['right_only_dates'][:5]}...")
    if report["ohlc_violations"]:
        print(f"  OHLC violations ({len(report['ohlc_violations'])}):")
        for v in report["ohlc_violations"][:10]:
            print(f"    - {v}")
    if report["price_diffs"]:
        print(f"  Price diffs > {DIFF_THRESHOLD:.1%} ({len(report['price_diffs'])} days):")
        for d in report["price_diffs"][:10]:
            print(f"    - {d}")
    print()


def _read_db_txf(days: int) -> pd.DataFrame:
    """從 DB 讀 TXFR1 最近 N 日 OHLC。"""
    if not config.DATABASE_URL:
        raise RuntimeError("DATABASE_URL 未設定，--db-check 無法執行")

    import psycopg2  # type: ignore

    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    conn = psycopg2.connect(config.DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT trade_date, open_px, high_px, low_px, close_px, volume
                FROM tvol_indices_daily
                WHERE symbol = %s AND trade_date >= %s
                ORDER BY trade_date
                """,
                (config.TXF_SYMBOL, cutoff),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        raise RuntimeError(f"DB 內無 {config.TXF_SYMBOL} 最近 {days} 日資料")

    df = pd.DataFrame(rows, columns=["trade_date", "open", "high", "low", "close", "volume"])
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.set_index("trade_date")
    df.index = df.index.normalize()
    return df.astype({"open": float, "high": float, "low": float, "close": float})


def run_diff_mode(days: int, min_rows: int) -> int:
    """Mode --days N: _fetch_txf_fubon vs _fetch_txf_finmind 直接比對。"""
    print(f"[verify_sources] mode=diff days={days} threshold={DIFF_THRESHOLD:.1%}")
    try:
        fubon = fetch._fetch_txf_fubon(days)
    except Exception as e:
        print(f"  富邦 fetch 失敗: {e}")
        print("  → 視為「無法驗證」（exit 2）；若富邦憑證/網路問題請排除後重跑")
        return 2
    finmind = fetch._fetch_txf_finmind(days)

    fubon = _exclude_partial_bar(_to_tpe_date_index(fubon))
    finmind = _exclude_partial_bar(_to_tpe_date_index(finmind))

    print(f"  fubon rows = {len(fubon)}, finmind rows = {len(finmind)}")
    exit_code, report = _compare_frames(fubon, finmind, left_label="fubon", min_joined_rows=min_rows)
    _print_report(report, mode="fubon-vs-finmind", left_label="fubon")

    if exit_code == 0:
        print("  ✅ 兩來源一致，富邦資料可信")
    elif exit_code == 1:
        print("  ❌ 富邦污染確認（這是正確結果，證實根因）")
    elif exit_code == 2:
        print(f"  ⚠️  資料不足（joined_rows={report['joined_rows']} < {min_rows}）")
    return exit_code


def run_db_check_mode(days: int, min_rows: int) -> int:
    """Mode --db-check: DB tvol_indices_daily TXFR1 vs FinMind 比對。"""
    print(f"[verify_sources] mode=db-check days={days} threshold={DIFF_THRESHOLD:.1%}")
    try:
        db_df = _read_db_txf(days)
    except Exception as e:
        print(f"  DB 讀取失敗: {e}")
        return 2
    finmind = fetch._fetch_txf_finmind(days)

    db_df = _exclude_partial_bar(_to_tpe_date_index(db_df))
    finmind = _exclude_partial_bar(_to_tpe_date_index(finmind))

    print(f"  db rows = {len(db_df)}, finmind rows = {len(finmind)}")
    exit_code, report = _compare_frames(db_df, finmind, left_label="db", min_joined_rows=min_rows)
    _print_report(report, mode="db-vs-finmind", left_label="db")

    if exit_code == 0:
        print("  ✅ DB 與 FinMind 一致，無污染殘留")
    elif exit_code == 1:
        print("  ❌ DB 內有污染資料殘留，需跑 B5.4 修復")
    elif exit_code == 2:
        print(f"  ⚠️  資料不足（joined_rows={report['joined_rows']} < {min_rows}）")
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description="台指期資料源驗證（FinMind vs 富邦 or DB）")
    parser.add_argument("--days", type=int, default=30, help="比對天數窗口（預設 30）")
    parser.add_argument("--db-check", action="store_true", help="比對 DB vs FinMind 而非富邦 vs FinMind")
    parser.add_argument("--min-rows", type=int, default=MIN_JOINED_ROWS_DEFAULT, help=f"最少 join 行數（預設 {MIN_JOINED_ROWS_DEFAULT}）")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")

    if args.db_check:
        return run_db_check_mode(args.days, args.min_rows)
    return run_diff_mode(args.days, args.min_rows)


if __name__ == "__main__":
    sys.exit(main())
