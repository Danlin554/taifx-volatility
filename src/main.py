from __future__ import annotations

import asyncio
import datetime
import logging
import pathlib
from typing import Literal

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from src import config, db, fetch, compute, render, freshness
from src.exceptions import InsufficientDataError, LivePublishValidationError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="taifx-volatility")

_HTML_PATH = pathlib.Path(__file__).parent.parent / "data" / "index.html"
_SNAPSHOT: dict = {}
_REFRESH_LOCK: asyncio.Lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Layer 1: External IO
# ---------------------------------------------------------------------------

def fetch_live_data() -> dict[str, pd.DataFrame]:
    return fetch.fetch_all(lookback_days=config.LOOKBACK_DAYS)


# ---------------------------------------------------------------------------
# Raw data validation（live 路徑專用）
# ---------------------------------------------------------------------------

def _validate_live_raw(
    data: dict,
    observed_at: datetime.datetime,
    *,
    skip_staleness: bool = False,
) -> dict:
    """驗證 live fetch 原始資料；通過則回 {txf_calendar_validation_mode, us_calendar_validation_mode}。
    任一條件不符 → raise LivePublishValidationError。

    skip_staleness=True：跳過 TXF staleness / future-bar / calendar-degraded 三項檢查。
    用於 refresh_and_publish 的 D-1 fallback：trim 後的 txf 相對於 now 已知「落後」，
    但這是刻意降級（非 ingestion gap），不需要再做 staleness 判斷。
    """
    txf = data["txf"]

    if len(txf) < 130:
        raise LivePublishValidationError(f"txf rows {len(txf)} < 130")
    if not txf.index.is_monotonic_increasing:
        raise LivePublishValidationError("txf index not monotonic increasing")
    if not txf.index.is_unique:
        raise LivePublishValidationError("txf index has duplicates")

    # R10 #3：驗全部 130 筆 TXF 日期均為合法 XTAI session
    try:
        xtai = freshness._get_calendar("XTAI")
        for ts in txf.index[-130:]:
            if not xtai.is_session(pd.Timestamp(ts)):
                raise LivePublishValidationError(f"txf has non-session date: {ts.date()}")
    except LivePublishValidationError:
        raise
    except Exception:
        pass  # XTAI 不可用時跳過，freshness check 下方會攔截

    if txf[["high", "low", "close"]].tail(20).isna().any().any():
        raise LivePublishValidationError("txf last-20 has NaN in high/low/close")

    # B5.2.b OHLC 完整性檢查（open finite、四欄 > 0、h>=max(o,c)、l<=min(o,c)）
    ohlc_violations = compute.validate_txf_ohlc_frame(txf, tail=20)
    if ohlc_violations:
        raise LivePublishValidationError(
            "txf OHLC integrity check failed: " + "; ".join(ohlc_violations[:5]),
            kind="ohlc_integrity",
        )

    # TXF freshness（fallback 路徑跳過：trim 到 D-1 是刻意降級，非 ingestion gap）
    txf_calendar_validation_mode = "strict"
    if not skip_staleness:
        fresh = freshness.check_freshness(txf, observed_at=observed_at)
        if fresh["is_stale"]:
            raise LivePublishValidationError(
                f"txf last trade {txf.index[-1].date()} lags expected {fresh['expected_trade_date']}"
            )
        # R8 #1：partial/future TXF bar
        if fresh["mode"] == "normal" and fresh["expected_trade_date"] is not None:
            if txf.index[-1].date() > fresh["expected_trade_date"]:
                raise LivePublishValidationError(
                    f"txf last trade {txf.index[-1].date()} is ahead of expected "
                    f"{fresh['expected_trade_date']} — partial or future TXF bar detected"
                )
        # R5 #2 / R6 #4：XTAI calendar degraded → fail-closed（除非 ALLOW_DEGRADED_TXF_FRESHNESS）
        if fresh["mode"] != "normal":
            if not config.ALLOW_DEGRADED_TXF_FRESHNESS:
                raise LivePublishValidationError(
                    f"TXF freshness calendar degraded (mode={fresh['mode']}); "
                    "refusing to publish live snapshot (set ALLOW_DEGRADED_TXF_FRESHNESS=1 to override)"
                )
            txf_calendar_validation_mode = "degraded_override"

    # US series 基本驗證
    for k in config.US_SYMBOLS:
        df = data[k]
        if len(df) < 2:
            raise LivePublishValidationError(f"us[{k}] rows {len(df)} < 2")
        if not df.index.is_monotonic_increasing:
            raise LivePublishValidationError(f"us[{k}] index not monotonic increasing")
        if not df.index.is_unique:
            raise LivePublishValidationError(f"us[{k}] index has duplicates")
        if df["close"].iloc[-2:].isna().any():
            raise LivePublishValidationError(f"us[{k}] last 2 close has NaN")

    # R5 #4：calendar-independent 一致性（四支 US 最後日期必須相同）
    us_last_dates = {k: data[k].index[-1].date() for k in config.US_SYMBOLS}
    if len(set(us_last_dates.values())) > 1:
        raise LivePublishValidationError(f"us series last dates mismatch: {us_last_dates}")
    us_last = next(iter(us_last_dates.values()))

    effective_date = txf.index[-1].date()
    expected_us_last = freshness.expected_us_session_for_effective_date(effective_date)
    completed_us = freshness.last_completed_us_session(observed_at)

    us_calendar_validation_mode = "strict"

    # (a) us_last == expected_us_last
    if expected_us_last is not None:
        if us_last != expected_us_last:
            raise LivePublishValidationError(
                f"us last session {us_last} != expected NYSE session for effective_date "
                f"{effective_date} (expected {expected_us_last}); refusing to publish — "
                f"either wait for NYSE close or accept a snapshot with earlier effective_date",
                kind="us_session_mismatch_for_effective_date",
                effective_date=effective_date,
            )

    # (b) partial-session leakage gate
    if expected_us_last is not None and completed_us is not None:
        if expected_us_last > completed_us:
            raise LivePublishValidationError(
                f"expected NYSE session for effective_date {effective_date} is "
                f"{expected_us_last}, but only {completed_us} has completed at "
                f"{observed_at.isoformat()} — refusing to publish (partial-session leakage risk)",
                kind="partial_session_leakage",
                effective_date=effective_date,
            )

    # (b2) R6 #2：completed_us 可用但 expected 不可用時的保底檢查
    if completed_us is not None and expected_us_last is None:
        if us_last > completed_us:
            raise LivePublishValidationError(
                f"us_last {us_last} > completed_us {completed_us} at {observed_at.isoformat()}; "
                f"refusing to publish — partial session bar detected even without expected_us"
            )

    # (c) calendar 不可用 fail-closed
    if expected_us_last is None or completed_us is None:
        if not config.ALLOW_DEGRADED_US_SESSION:
            raise LivePublishValidationError(
                "NYSE calendar unavailable (expected_us=%s, completed_us=%s); "
                "refusing to publish live snapshot (set ALLOW_DEGRADED_US_SESSION=1 to override)"
                % (expected_us_last, completed_us)
            )
        us_calendar_validation_mode = "degraded_override"

    # (d) R9 #5：連續 NYSE sessions 驗證
    check_us_ref = expected_us_last if expected_us_last is not None else us_last
    prev_expected = freshness.previous_nyse_session(check_us_ref)
    if prev_expected is None:
        if us_calendar_validation_mode != "degraded_override":
            if not config.ALLOW_DEGRADED_US_SESSION:
                raise LivePublishValidationError(
                    "Cannot verify consecutive NYSE sessions (previous_nyse_session returned None); "
                    "refusing to publish (set ALLOW_DEGRADED_US_SESSION=1 to override)"
                )
            us_calendar_validation_mode = "degraded_override"
    else:
        for k in config.US_SYMBOLS:
            second_last = data[k].index[-2].date()
            if second_last != prev_expected:
                raise LivePublishValidationError(
                    f"us[{k}] last two dates are not consecutive NYSE sessions: "
                    f"{second_last} and {us_last} (expected previous: {prev_expected})"
                )

    return {
        "txf_calendar_validation_mode": txf_calendar_validation_mode,
        "us_calendar_validation_mode": us_calendar_validation_mode,
    }


# ---------------------------------------------------------------------------
# Snapshot output validation helpers
# ---------------------------------------------------------------------------

def _required_nan_fields(snapshot: dict) -> list[tuple[str, str, object]]:
    """列出所有「必填、不能 NaN」的欄位。live strict 與 db graceful 都用此清單。"""
    required = []
    for k in ("a20", "a10", "a5", "s20", "s10", "s5"):
        required.append(("stats", k, snapshot["stats"][k]))
    for k in ("atr20", "atr10", "atr5"):
        required.append(("atr", k, snapshot["atr"][k]))
    required += [
        ("root", "sig1", snapshot["sig1"]),
        ("root", "sig2", snapshot["sig2"]),
        ("root", "sig3", snapshot["sig3"]),
        ("root", "r", snapshot["r"]),
        ("root", "default_open_price", snapshot["default_open_price"]),
    ]
    for k in ("b1", "b2", "b3", "b4", "b5"):
        required.append(("targets", k, snapshot["targets"].get(k)))
    # weekday_multi.m6 必須是完整 dict
    m6 = snapshot.get("weekday_multi", {}).get("m6")
    if not isinstance(m6, dict):
        required.append(("weekday_multi", "m6", m6))
    else:
        for wd in ("mon", "tue", "wed", "thu", "fri"):
            required.append(("weekday_multi.m6", wd, m6.get(wd)))
    # B5.2 新欄位：txf_ohlc{o,h,l,c} 與 us_data.<k>.close 不可缺
    txf_ohlc = snapshot.get("txf_ohlc")
    if not isinstance(txf_ohlc, dict):
        required.append(("root", "txf_ohlc", txf_ohlc))
    else:
        for k in ("open", "high", "low", "close"):
            required.append(("txf_ohlc", k, txf_ohlc.get(k)))
    us_data = snapshot.get("us_data")
    if not isinstance(us_data, dict):
        required.append(("root", "us_data", us_data))
    else:
        for sym in ("dj", "nq", "spy", "tsm"):
            sub = us_data.get(sym, {})
            required.append((f"us_data.{sym}", "close", sub.get("close") if isinstance(sub, dict) else None))
    return required


def _is_missing(val) -> bool:
    """統一偵測 None / float NaN / numpy.nan / pd.NA；空 dict/list 也視為缺值。"""
    if isinstance(val, (dict, list)):
        return len(val) == 0
    return bool(pd.isna(val))


def _validate_snapshot_output_strict(snapshot: dict) -> None:
    """Live publish 嚴格驗證：任一必填欄位缺失 → raise LivePublishValidationError。"""
    for group, key, val in _required_nan_fields(snapshot):
        if _is_missing(val):
            raise LivePublishValidationError(f"snapshot.{group}.{key} missing/NaN/empty")


def _validate_snapshot_output_graceful(snapshot: dict) -> None:
    """DB 路徑驗證：必填欄位缺失 → raise InsufficientDataError（→ api 回 422）。"""
    missing = [f"{g}.{k}" for g, k, v in _required_nan_fields(snapshot) if _is_missing(v)]
    if missing:
        earliest_iso = snapshot.get("earliest_db_date")
        earliest = datetime.date.fromisoformat(earliest_iso) if earliest_iso else None
        raise InsufficientDataError(earliest=earliest, missing_fields=missing)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _earliest_db_date_safe() -> datetime.date | None:
    """統一查 TXFR1 最早交易日；無 DB 時回 None。"""
    return db.earliest_trade_date(config.TXF_SYMBOL) if config.DATABASE_URL else None


def _resolve_earliest_for_live(data: dict[str, pd.DataFrame]) -> datetime.date | None:
    """Live 路徑專用：DB 為空時用本次 fetch 首筆日期，避免 fresh deploy 的空窗口。"""
    earliest = _earliest_db_date_safe()
    if earliest is None and "txf" in data and len(data["txf"]) > 0:
        earliest = data["txf"].index[0].date()
    return earliest


# ---------------------------------------------------------------------------
# Layer 2b: DB path data reader
# ---------------------------------------------------------------------------

def read_db_data(
    asof: datetime.date, *, server_today: datetime.date
) -> tuple[
    dict[str, pd.DataFrame],
    Literal["validated", "skipped_calendar_unavailable"],
    Literal["strict", "skipped_calendar_unavailable"],
]:
    """從 DB 讀 asof（含）以前的歷史；回 (data, us_session_validation, txf_asof_validation)。
    任一 symbol 不足 → raise InsufficientDataError；earliest 一律補成 TXFR1 最早日。
    """
    # Step 1: 讀 TXFR1
    try:
        data: dict[str, pd.DataFrame] = {}
        data["txf"] = db.read_indices_until(config.TXF_SYMBOL, asof, min_rows=130)
    except InsufficientDataError as e:
        e.earliest = db.earliest_trade_date(config.TXF_SYMBOL)
        e.code = "insufficient_data"
        raise
    resolved_effective_date = data["txf"].index[-1].date()

    # B5.2.b OHLC 完整性檢查（DB 路徑；違反 → 視為 DB 資料污染，回 422）
    ohlc_violations = compute.validate_txf_ohlc_frame(data["txf"], tail=20)
    if ohlc_violations:
        raise InsufficientDataError(
            earliest=db.earliest_trade_date(config.TXF_SYMBOL),
            code="ohlc_integrity_db",
            asof=asof,
            missing_fields=ohlc_violations[:5],
        )

    # Step 2: TXFR1 缺洞檢查（過去交易日缺洞才 raise；今日 / 非交易日 / calendar 不可用 allow fallback）
    asof_is_trading_day = freshness.is_trading_day(asof)  # True | False | None
    if asof_is_trading_day is True and resolved_effective_date < asof and asof < server_today:
        raise InsufficientDataError(
            earliest=db.earliest_trade_date(config.TXF_SYMBOL),
            missing_fields=[f"txf.{asof.isoformat()}"],
            code="missing_requested_trading_day",
            asof=asof,
        )
    txf_asof_validation: str = (
        "strict" if asof_is_trading_day is not None else "skipped_calendar_unavailable"
    )

    # Step 3: 計算 expected_us，作為 US 查詢上限
    expected_us = freshness.expected_us_session_for_effective_date(resolved_effective_date)
    us_query_upper = expected_us if expected_us is not None else resolved_effective_date

    # Step 4: 讀 US 四指數（收集所有失敗後一次 raise）
    us_errors: dict[str, InsufficientDataError] = {}
    for k, sym in config.US_SYMBOLS.items():
        try:
            data[k] = db.read_indices_until(sym, us_query_upper, min_rows=2)
        except InsufficientDataError as e:
            us_errors[k] = e

    if us_errors:
        earliest_tx = db.earliest_trade_date(config.TXF_SYMBOL)
        actual_dates: dict[str, str | None] = {}
        for k, e in us_errors.items():
            al = getattr(e, "actual_last", None)
            actual_dates[k] = al.isoformat() if al is not None else None
        for k in config.US_SYMBOLS:
            if k not in us_errors and k in data:
                actual_dates[k] = data[k].index[-1].date().isoformat()
        if expected_us is not None:
            missing_session_syms = []
            for k, e in us_errors.items():
                al = getattr(e, "actual_last", None)
                got_k = getattr(e, "got", 0)
                if got_k == 0 or (al is not None and al < expected_us):
                    missing_session_syms.append(k)
            if missing_session_syms:
                code: str = "missing_requested_us_session"
                missing_fields: list[str] | None = [
                    f"us.{k}.{expected_us.isoformat()}" for k in missing_session_syms
                ]
            else:
                code = "insufficient_data"
                missing_fields = None
        else:
            code = "insufficient_data"
            missing_fields = None
        raise InsufficientDataError(
            earliest=earliest_tx,
            missing_fields=missing_fields,
            code=code,
            asof=asof,
            expected_us_session=expected_us.isoformat() if expected_us is not None else None,
            resolved_effective_date=resolved_effective_date.isoformat(),
            actual_us_session_dates=actual_dates,
        )

    # Step 5: calendar-independent US 一致性（四支最後日期必須相同）
    us_last_dates = {k: data[k].index[-1].date() for k in config.US_SYMBOLS}
    if len(set(us_last_dates.values())) > 1:
        raise InsufficientDataError(
            earliest=db.earliest_trade_date(config.TXF_SYMBOL),
            missing_fields=None,
            code="us_session_mismatch",
            asof=asof,
            resolved_effective_date=resolved_effective_date.isoformat(),
            actual_us_session_dates={k: d.isoformat() for k, d in us_last_dates.items()},
        )

    # Step 6: expected_us 日期吻合驗證
    if expected_us is not None:
        mismatched = [k for k, d in us_last_dates.items() if d != expected_us]
        if mismatched:
            all_lower = all(us_last_dates[k] < expected_us for k in mismatched)
            err_code = "missing_requested_us_session" if all_lower else "us_session_mismatch"
            raise InsufficientDataError(
                earliest=db.earliest_trade_date(config.TXF_SYMBOL),
                missing_fields=[f"us.{k}.{expected_us.isoformat()}" for k in mismatched],
                code=err_code,
                asof=asof,
                expected_us_session=expected_us.isoformat(),
                resolved_effective_date=resolved_effective_date.isoformat(),
                actual_us_session_dates={k: d.isoformat() for k, d in us_last_dates.items()},
            )

    # Step 7: R9 #5 連續 NYSE sessions 驗證
    if expected_us is not None:
        prev_expected_session = freshness.previous_nyse_session(expected_us)
        if prev_expected_session is None:
            # R10 #4：XNYS 不可用 → 保守放行，標 skipped
            us_session_validation: str = "skipped_calendar_unavailable"
        else:
            for k in config.US_SYMBOLS:
                if len(data[k]) >= 2:
                    second_last = data[k].index[-2].date()
                    if second_last != prev_expected_session:
                        raise InsufficientDataError(
                            earliest=db.earliest_trade_date(config.TXF_SYMBOL),
                            code="us_pct_gap_in_sessions",
                            asof=asof,
                            expected_us_session=expected_us.isoformat(),
                            resolved_effective_date=resolved_effective_date.isoformat(),
                            actual_us_session_dates={
                                k: d.isoformat() for k, d in us_last_dates.items()
                            },
                        )
            us_session_validation = "validated"
    else:
        us_session_validation = "skipped_calendar_unavailable"

    return data, us_session_validation, txf_asof_validation


# ---------------------------------------------------------------------------
# Layer 2: Pure computation（無 IO、無 wall-clock 讀取）
# ---------------------------------------------------------------------------

def build_snapshot_from_data(
    data: dict[str, pd.DataFrame],
    *,
    source: Literal["live", "db"],
    observed_at: datetime.datetime,
    asof: datetime.date | None = None,
    earliest_db_date: datetime.date | None = None,
    txf_calendar_validation_mode: Literal["strict", "degraded_override"] | None = None,
    us_calendar_validation_mode: Literal["strict", "degraded_override"] | None = None,
    us_session_validation: Literal["validated", "skipped_calendar_unavailable"] | None = None,
    txf_asof_validation: Literal["strict", "skipped_calendar_unavailable"] | None = None,
) -> dict:
    """唯一純計算函式（無 IO）。所有時間決策從 observed_at 推導；caller 必須顯式注入。"""
    if observed_at.tzinfo is None:
        raise TypeError("observed_at must be timezone-aware")
    server_today = observed_at.astimezone(config.TZ).date()

    txf = data["txf"]
    effective = txf.index[-1].date()
    requested_asof = asof if source == "db" else None

    # freshness_mode 從 caller 傳入的 calendar validation 狀態推導（R8 #4）
    if source == "live":
        assert txf_calendar_validation_mode is not None, "live path requires txf_calendar_validation_mode"
        assert us_calendar_validation_mode is not None, "live path requires us_calendar_validation_mode"
        freshness_mode = "normal" if txf_calendar_validation_mode == "strict" else "degraded"
    else:
        assert us_session_validation is not None, "db path requires us_session_validation"
        assert txf_asof_validation is not None, "db path requires txf_asof_validation"
        freshness_mode = "historical"

    # 計算指標
    hl = compute.simple_range(txf)
    tr = compute.true_range(txf)
    stats = compute.rolling_stats(hl)
    atr_stats = compute.atr_rolling_stats(tr)
    sig1, sig2, sig3 = compute.sigma_bands(stats["a10"], stats["s10"])
    weekday_multi = compute.weekday_avg_multi(hl)
    wd = compute.weekday_avg(hl, config.WEEKDAY_MONTHS)
    targets = compute.daytrade_targets(hl)
    r_val = compute.calc_r(stats["a10"])
    us_pcts = {k: fetch.latest_pct(data[k]) for k in config.US_SYMBOLS}
    prev_close = float(txf["close"].iloc[-1])
    default_open = compute.default_open_price(prev_close, us_pcts)

    us_session_dates = {k: data[k].index[-1].date().isoformat() for k in config.US_SYMBOLS}
    unique_us_dates = set(us_session_dates.values())
    us_session_date_val = next(iter(unique_us_dates)) if len(unique_us_dates) == 1 else None

    # B5.2.a 台指期完整 OHLC + 商品標籤
    txf_ohlc = {
        "date":  effective.isoformat(),
        "open":  float(txf["open"].iloc[-1]),
        "high":  float(txf["high"].iloc[-1]),
        "low":   float(txf["low"].iloc[-1]),
        "close": float(txf["close"].iloc[-1]),
    }
    txf_symbol_label = "大臺近月（TXFR1）日盤"

    # B5.2.a 美股個別 close + 日期 + pct
    us_data = {
        k: {
            "date":  data[k].index[-1].date().isoformat(),
            "close": float(data[k]["close"].iloc[-1]),
            "pct":   us_pcts[k],
        }
        for k in config.US_SYMBOLS
    }

    # B5.2.c NYSE close 台北時間（DST/半日交易日自動推導；calendar 不可用 → None）
    us_session_close_tpe = None
    if us_session_date_val:
        try:
            us_close_dt = freshness.compute_us_session_close_tpe(
                datetime.date.fromisoformat(us_session_date_val)
            )
            if us_close_dt is not None:
                us_session_close_tpe = us_close_dt.isoformat()
        except Exception:
            us_session_close_tpe = None

    snapshot = {
        # 舊欄位（保留 backward-compat）
        "asof_iso": effective.isoformat(),
        "us": us_pcts,
        "prev_close": int(prev_close),
        "stats": {k: round(v, 2) for k, v in stats.items()},
        "atr": {k: round(v, 2) for k, v in atr_stats.items()},
        "sig1": round(sig1, 2),
        "sig2": round(sig2, 2),
        "sig3": round(sig3, 2),
        "weekday": wd,
        "weekday_months": config.WEEKDAY_MONTHS,
        "targets": targets,
        "r": r_val,
        # 新欄位（additive，舊前端不受影響）
        "weekday_multi": weekday_multi,
        "default_open_price": default_open,
        "forecast_base_close": int(prev_close),
        "effective_date": effective.isoformat(),
        "resolved_effective_date": effective.isoformat(),
        "asof_adjusted_from": (
            requested_asof.isoformat()
            if requested_asof is not None and requested_asof != effective
            else None
        ),
        "server_today": server_today.isoformat(),
        "earliest_db_date": earliest_db_date.isoformat() if earliest_db_date is not None else None,
        "us_session_dates": us_session_dates,
        "us_session_date": us_session_date_val,
        # B5.2.a 新欄位（additive）
        "txf_ohlc": txf_ohlc,
        "txf_symbol_label": txf_symbol_label,
        "us_data": us_data,
        "us_session_close_tpe": us_session_close_tpe,
        "data_source": source,
        "is_stale": False,  # live 通過驗證即非 stale；db 用 historical 模式不判 stale
        "freshness_mode": freshness_mode,
        "generated_at": observed_at.isoformat(),
        # Calendar validation 結構化欄位（R4 #3 / #4 / R6 #4 / R7 #2）
        "txf_calendar_validation": txf_calendar_validation_mode if source == "live" else None,
        "us_calendar_validation": us_calendar_validation_mode if source == "live" else None,
        "us_session_validation": us_session_validation if source == "db" else None,
        "txf_asof_validation": txf_asof_validation if source == "db" else None,
    }

    if source == "live":
        _validate_snapshot_output_strict(snapshot)
    else:
        _validate_snapshot_output_graceful(snapshot)

    return snapshot


# ---------------------------------------------------------------------------
# Layer 3: Side effects（唯一寫 DB / 渲染 / 更新 cache 的層）
# ---------------------------------------------------------------------------

def publish_snapshot(snapshot: dict, data: dict[str, pd.DataFrame]) -> None:
    """四階段原子 publish：render_tmp → db_upsert → rename_html → update_cache。
    任一階段失敗即 raise，後續階段不執行。
    """
    # ── render_tmp ──
    tmp_html = pathlib.Path(str(_HTML_PATH) + ".tmp")
    tmp_html.unlink(missing_ok=True)
    log.info("publish.render_tmp start tmp_path=%s", tmp_html)
    render.render_to(snapshot, tmp_html)
    log.info("publish.render_tmp done tmp_path=%s", tmp_html)

    # ── db_upsert（單一 transaction，原子寫入全部 symbols）──
    if config.DATABASE_URL:
        log.info("publish.db_upsert start symbols=%s", list(data.keys()))
        db.upsert_indices_batch({
            **{sym: data[k] for k, sym in config.US_SYMBOLS.items()},
            config.TXF_SYMBOL: data["txf"],
        })
        log.info("publish.db_upsert done")

    # ── rename_html（POSIX 原子 rename）──
    log.info("publish.rename_html start tmp=%s target=%s", tmp_html, _HTML_PATH)
    tmp_html.replace(_HTML_PATH)
    log.info("publish.rename_html done")

    # ── update_cache ──
    global _SNAPSHOT
    _SNAPSHOT = snapshot
    log.info("publish.update_cache done effective_date=%s", snapshot.get("effective_date"))


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

_FALLBACK_KINDS = {"partial_session_leakage", "us_session_mismatch_for_effective_date"}


async def refresh_and_publish() -> dict:
    """Cron / startup / 手動 /refresh 都呼叫這個。整個流程用 _REFRESH_LOCK 序列化。

    若 _validate_live_raw 因「effective_date=D 但 NYSE D 尚未收盤」失敗，
    自動 trim txf 最後一列（有效降級到 effective_date=D-1）後重試一次。
    """
    async with _REFRESH_LOCK:
        observed_at = datetime.datetime.now(config.TZ)
        data = await asyncio.to_thread(fetch_live_data)
        try:
            validation = _validate_live_raw(data, observed_at)
        except LivePublishValidationError as e:
            if e.kind not in _FALLBACK_KINDS or len(data["txf"]) <= 1:
                raise
            log.warning(
                "refresh_and_publish: %s for effective_date=%s; "
                "trimming txf and any US partial bars to D-1, retrying once",
                e.kind, e.effective_date,
            )
            data = dict(data)
            data["txf"] = data["txf"].iloc[:-1]
            new_effective = data["txf"].index[-1].date()
            # 計算新的 US trim 上限：calendar 不可用時保守用 new_effective
            new_expected_us = freshness.expected_us_session_for_effective_date(new_effective)
            us_trim_target = new_expected_us if new_expected_us is not None else new_effective
            for k in config.US_SYMBOLS:
                df = data[k]
                trimmed = df[df.index.date <= us_trim_target]
                if len(trimmed) < 2:
                    raise  # US trim 過頭，propagate 原 exception
                data[k] = trimmed
            # skip_staleness=True：D-1 相對於現在是「stale」但這是刻意降級，不是 ingestion gap
            validation = _validate_live_raw(data, observed_at, skip_staleness=True)  # 第二次失敗直接 propagate
        earliest = _resolve_earliest_for_live(data)
        snapshot = build_snapshot_from_data(
            data,
            source="live",
            observed_at=observed_at,
            earliest_db_date=earliest,
            txf_calendar_validation_mode=validation["txf_calendar_validation_mode"],
            us_calendar_validation_mode=validation["us_calendar_validation_mode"],
        )
        await asyncio.to_thread(publish_snapshot, snapshot, data)
        return snapshot


def build_snapshot(
    source: Literal["db", "live"],
    asof: datetime.date | None = None,
    *,
    observed_at: datetime.datetime | None = None,
) -> dict:
    """Facade（純計算，不寫 DB、不渲染、不更新 _SNAPSHOT）。
    live 路徑也會跑 _validate_live_raw，確保不回傳未驗證的 snapshot。
    """
    if observed_at is None:
        observed_at = datetime.datetime.now(config.TZ)
    if observed_at.tzinfo is None:
        raise TypeError("observed_at must be timezone-aware")

    if source == "live":
        data = fetch_live_data()
        validation = _validate_live_raw(data, observed_at)
        earliest = _resolve_earliest_for_live(data)
        return build_snapshot_from_data(
            data,
            source="live",
            observed_at=observed_at,
            earliest_db_date=earliest,
            txf_calendar_validation_mode=validation["txf_calendar_validation_mode"],
            us_calendar_validation_mode=validation["us_calendar_validation_mode"],
        )

    assert asof is not None, "asof required for source='db'"
    earliest = _earliest_db_date_safe()
    server_today = observed_at.astimezone(config.TZ).date()
    data, us_session_validation, txf_asof_validation = read_db_data(asof, server_today=server_today)
    return build_snapshot_from_data(
        data,
        source="db",
        asof=asof,
        observed_at=observed_at,
        earliest_db_date=earliest,
        us_session_validation=us_session_validation,
        txf_asof_validation=txf_asof_validation,
    )


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

async def _startup_refresh():
    try:
        snapshot = await refresh_and_publish()
        log.info(
            "startup refresh succeeded effective_date=%s us_session_date=%s",
            snapshot.get("effective_date"), snapshot.get("us_session_date"),
        )
    except Exception as e:
        log.warning("startup refresh failed: %s", e)


@app.on_event("startup")
async def startup():
    if config.DATABASE_URL:
        try:
            db.init_tvol_tables()
        except Exception as e:
            log.warning("DB init failed (continuing without DB): %s", e)
    asyncio.create_task(_startup_refresh())


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    if _HTML_PATH.exists():
        return FileResponse(str(_HTML_PATH), media_type="text/html")
    raise HTTPException(503, "HTML not yet generated, try POST /refresh first")


@app.get("/api/snapshot")
async def api_snapshot(asof: str | None = None):
    if asof is None:
        if not _SNAPSHOT:
            raise HTTPException(503, "snapshot not ready")
        return JSONResponse(_SNAPSHOT)

    if asof.lower() == "today":
        raise HTTPException(400, "asof=today not supported; omit param to get live cache")

    try:
        d = datetime.date.fromisoformat(asof)
    except ValueError:
        raise HTTPException(400, "invalid date format, expected YYYY-MM-DD")

    # handler 起點取一次 observed_at，整個 request 共用（R1 #6）
    observed_at = datetime.datetime.now(config.TZ)
    server_today = observed_at.astimezone(config.TZ).date()

    if d > server_today:
        raise HTTPException(422, "future date not allowed (server today in Asia/Taipei)")

    try:
        snapshot = build_snapshot(source="db", asof=d, observed_at=observed_at)
    except InsufficientDataError as e:
        raw_asof = getattr(e, "asof", None)
        raise HTTPException(
            422,
            {
                "code": getattr(e, "code", "insufficient_data"),
                "earliest": e.earliest.isoformat() if e.earliest is not None else None,
                "missing_fields": getattr(e, "missing_fields", None),
                "asof": raw_asof.isoformat() if isinstance(raw_asof, datetime.date) else raw_asof,
                "expected_us_session": getattr(e, "expected_us_session", None),
                "resolved_effective_date": getattr(e, "resolved_effective_date", None),
                "actual_us_session_dates": getattr(e, "actual_us_session_dates", None),
            },
        )
    return JSONResponse(snapshot)  # 永遠不更新 _SNAPSHOT


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/refresh")
async def refresh_endpoint(token: str, dry: bool = False):
    if token != config.REFRESH_TOKEN:
        raise HTTPException(403, "invalid token")

    if dry:
        # Dry run：build 但不 publish（不寫 DB、不渲染、不更新 _SNAPSHOT）
        observed_at = datetime.datetime.now(config.TZ)
        data = await asyncio.to_thread(fetch_live_data)
        validation = _validate_live_raw(data, observed_at)
        earliest = _resolve_earliest_for_live(data)
        snapshot = build_snapshot_from_data(
            data,
            source="live",
            observed_at=observed_at,
            earliest_db_date=earliest,
            txf_calendar_validation_mode=validation["txf_calendar_validation_mode"],
            us_calendar_validation_mode=validation["us_calendar_validation_mode"],
        )
        return {
            "ok": True,
            "dry_run": True,
            "generated_at": snapshot["generated_at"],
            "is_stale": snapshot["is_stale"],
            "effective_date": snapshot.get("effective_date"),
        }

    snapshot = await refresh_and_publish()
    return {
        "ok": True,
        "generated_at": snapshot["generated_at"],
        "is_stale": snapshot["is_stale"],
        "effective_date": snapshot.get("effective_date"),
    }
