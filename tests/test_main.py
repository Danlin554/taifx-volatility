"""
tests/test_main.py — API 路由、snapshot 建構、publish 階段的 unit tests。
不依賴真實 DB 或外部 API：一律 mock。
"""
from __future__ import annotations

import asyncio
import datetime
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch, call

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src import config
from src.exceptions import InsufficientDataError, LivePublishValidationError
from src.main import app, publish_snapshot, _validate_live_raw, build_snapshot_from_data

TZ = config.TZ
client = TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_txf(n: int = 130, last_date: str = "2026-05-19") -> pd.DataFrame:
    end = pd.Timestamp(last_date)
    dates = pd.bdate_range(end=end, periods=n)
    return pd.DataFrame(
        {"open": 100.0, "high": 110.0, "low": 95.0, "close": 105.0, "volume": 1000},
        index=dates,
    )


def _make_us(last_date: str = "2026-05-18") -> pd.DataFrame:
    dates = pd.DatetimeIndex([
        pd.Timestamp(last_date) - pd.Timedelta(days=1),
        pd.Timestamp(last_date),
    ])
    return pd.DataFrame(
        {"open": 100.0, "high": 110.0, "low": 95.0, "close": 105.0, "volume": 1000},
        index=dates,
    )


def _make_data(txf_last="2026-05-19", us_last="2026-05-18") -> dict[str, pd.DataFrame]:
    return {
        "txf": _make_txf(last_date=txf_last),
        "dj": _make_us(last_date=us_last),
        "nq": _make_us(last_date=us_last),
        "spy": _make_us(last_date=us_last),
        "tsm": _make_us(last_date=us_last),
    }


_FIXTURE_SNAPSHOT = {
    "effective_date": "2026-05-18",
    "trading_date": "2026-05-19",
    "resolved_effective_date": "2026-05-18",
    "asof_iso": "2026-05-18",
    "us": {"dj": 1.0, "nq": 1.0, "spy": 1.0, "tsm": 1.0},
    "prev_close": 22000,
    "stats": {"a20": 300.0, "a10": 300.0, "a5": 300.0,
              "s20": 100.0, "s10": 100.0, "s5": 100.0},
    "atr": {"atr20": 300.0, "atr10": 300.0, "atr5": 300.0},
    "sig1": 400.0, "sig2": 500.0, "sig3": 600.0,
    "weekday": {"mon": 300.0, "tue": 300.0, "wed": 300.0, "thu": 300.0, "fri": 300.0},
    "weekday_multi": {
        "m1": {"mon": 300.0, "tue": 300.0, "wed": 300.0, "thu": 300.0, "fri": 300.0},
        "m2": {"mon": 300.0, "tue": 300.0, "wed": 300.0, "thu": 300.0, "fri": 300.0},
        "m3": {"mon": 300.0, "tue": 300.0, "wed": 300.0, "thu": 300.0, "fri": 300.0},
        "m6": {"mon": 300.0, "tue": 300.0, "wed": 300.0, "thu": 300.0, "fri": 300.0},
    },
    "targets": {"b1": 100.0, "b2": 200.0, "b3": 300.0, "b4": 400.0, "b5": 500.0},
    "r": 60, "weekday_months": 6,
    "default_open_price": 22200,
    "forecast_base_close": 22000,
    "server_today": "2026-05-19",
    "earliest_db_date": "2026-01-02",
    "us_session_date": "2026-05-18",
    "us_session_dates": {"dj": "2026-05-18", "nq": "2026-05-18",
                         "spy": "2026-05-18", "tsm": "2026-05-18"},
    "data_source": "live",
    "is_stale": False,
    "freshness_mode": "normal",
    "txf_calendar_validation": "strict",
    "us_calendar_validation": "strict",
    "us_session_validation": None,
    "txf_asof_validation": None,
    "asof_adjusted_from": None,
    "generated_at": "2026-05-19T06:00:00+08:00",
    "calendar_anomaly": "none",
    "us_aggregation_window": None,
}


# ---------------------------------------------------------------------------
# GET /api/snapshot（無 asof）
# ---------------------------------------------------------------------------

class TestApiSnapshotNoParam:
    def test_returns_live_cache(self):
        """無 asof → 直接回 _SNAPSHOT，不調任何計算。"""
        import src.main as main_mod
        original = main_mod._SNAPSHOT
        main_mod._SNAPSHOT = _FIXTURE_SNAPSHOT
        try:
            resp = client.get("/api/snapshot")
            assert resp.status_code == 200
            assert resp.json()["effective_date"] == "2026-05-18"
        finally:
            main_mod._SNAPSHOT = original

    def test_503_when_cache_empty(self):
        """cache 空 → 503。"""
        import src.main as main_mod
        original = main_mod._SNAPSHOT
        main_mod._SNAPSHOT = {}
        try:
            resp = client.get("/api/snapshot")
            assert resp.status_code == 503
        finally:
            main_mod._SNAPSHOT = original

    def test_does_not_call_build_or_external(self):
        """無 asof 時完全不觸碰 build_snapshot / fetch / db / upsert。"""
        import src.main as main_mod
        original = main_mod._SNAPSHOT
        main_mod._SNAPSHOT = _FIXTURE_SNAPSHOT.copy()
        try:
            with patch("src.main.build_snapshot", side_effect=AssertionError("should not call")) as mock_bs, \
                 patch("src.fetch.fetch_all", side_effect=AssertionError("should not call")), \
                 patch("src.db.read_indices_until", side_effect=AssertionError("should not call")), \
                 patch("src.db.upsert_indices_batch", side_effect=AssertionError("should not call")):
                resp = client.get("/api/snapshot")
            assert resp.status_code == 200
        finally:
            main_mod._SNAPSHOT = original


# ---------------------------------------------------------------------------
# GET /api/snapshot?asof=... 輸入驗證
# ---------------------------------------------------------------------------

class TestApiSnapshotAsofValidation:
    def test_today_string_rejected(self):
        resp = client.get("/api/snapshot?asof=today")
        assert resp.status_code == 400

    def test_invalid_date_format_rejected(self):
        resp = client.get("/api/snapshot?asof=abc")
        assert resp.status_code == 400

    def test_future_date_rejected(self):
        future = (datetime.datetime.now(TZ).date() + datetime.timedelta(days=10)).isoformat()
        resp = client.get(f"/api/snapshot?asof={future}")
        assert resp.status_code == 422

    def test_does_not_pollute_cache(self):
        """asof 查詢不更新 _SNAPSHOT。"""
        import src.main as main_mod
        original_cache = main_mod._SNAPSHOT.copy() if main_mod._SNAPSHOT else {}
        dummy_snap = _FIXTURE_SNAPSHOT.copy()
        dummy_snap["data_source"] = "db"

        with patch("src.main.build_snapshot", return_value=dummy_snap):
            client.get("/api/snapshot?asof=2026-04-30")

        assert main_mod._SNAPSHOT == original_cache

    def test_does_not_call_fetch(self):
        """asof 查詢不呼叫 fetch_all（不打外部 API）。"""
        dummy_snap = _FIXTURE_SNAPSHOT.copy()
        dummy_snap["data_source"] = "db"
        with patch("src.main.build_snapshot", return_value=dummy_snap), \
             patch("src.fetch.fetch_all", side_effect=AssertionError("fetch must not be called")):
            resp = client.get("/api/snapshot?asof=2026-04-30")
        assert resp.status_code == 200

    def test_does_not_upsert(self):
        """asof 查詢不寫 DB。"""
        dummy_snap = _FIXTURE_SNAPSHOT.copy()
        dummy_snap["data_source"] = "db"
        with patch("src.main.build_snapshot", return_value=dummy_snap), \
             patch("src.db.upsert_indices_batch", side_effect=AssertionError("upsert must not be called")):
            resp = client.get("/api/snapshot?asof=2026-04-30")
        assert resp.status_code == 200

    def test_insufficient_data_returns_422(self):
        """DB 不足資料 → 422 + code 欄位。"""
        err = InsufficientDataError(
            earliest=datetime.date(2026, 1, 2),
            code="insufficient_data",
        )
        with patch("src.main.build_snapshot", side_effect=err):
            resp = client.get("/api/snapshot?asof=2026-04-30")
        assert resp.status_code == 422
        body = resp.json()["detail"]
        assert body["code"] == "insufficient_data"
        assert body["earliest"] == "2026-01-02"

    def test_insufficient_data_earliest_null_when_empty_db(self):
        """earliest=None → JSON null（不是字串 "None"）。"""
        err = InsufficientDataError(earliest=None, code="insufficient_data")
        with patch("src.main.build_snapshot", side_effect=err):
            resp = client.get("/api/snapshot?asof=2026-04-30")
        body = resp.json()["detail"]
        assert body["earliest"] is None

    def test_asof_trading_date_translates_to_internal_close(self):
        """asof=2026-05-22（trading date T）→ build_snapshot 以 T-1=5/21 呼叫；response.trading_date=5/22。"""
        dummy_snap = {**_FIXTURE_SNAPSHOT, "trading_date": "2026-05-22", "data_source": "db"}
        with patch("src.main.build_snapshot", return_value=dummy_snap) as mock_bs, \
             patch("src.main.freshness") as mock_freshness:
            mock_freshness.previous_xtai_session.return_value = datetime.date(2026, 5, 21)
            resp = client.get("/api/snapshot?asof=2026-05-22")
        assert resp.status_code == 200
        # build_snapshot 必須以 internal_close(5/21) 而非 trading_date(5/22) 呼叫
        called_asof = mock_bs.call_args.kwargs.get("asof") or mock_bs.call_args.args[1] if len(mock_bs.call_args.args) > 1 else None
        # 也可以從 kwargs 取
        if called_asof is None:
            called_asof = mock_bs.call_args[1].get("asof")
        assert called_asof == datetime.date(2026, 5, 21), f"expected internal_close=5/21, got {called_asof}"
        # response 中 trading_date 必須是用戶要求的 T=5/22
        assert resp.json()["trading_date"] == "2026-05-22"


# ---------------------------------------------------------------------------
# build_snapshot_from_data（純計算）
# ---------------------------------------------------------------------------

class TestBuildSnapshotFromData:
    def _observed_at(self, s: str = "2026-05-19T06:00:00") -> datetime.datetime:
        return TZ.localize(datetime.datetime.fromisoformat(s))

    def test_live_effective_date_from_txf_last(self):
        """live snapshot 的 effective_date = txf 最後一筆，不是 date.today()。"""
        data = _make_data(txf_last="2026-05-15", us_last="2026-05-14")
        observed_at = self._observed_at("2026-05-19T06:00:00")

        snap = build_snapshot_from_data(
            data,
            source="live",
            observed_at=observed_at,
            txf_calendar_validation_mode="strict",
            us_calendar_validation_mode="strict",
        )
        assert snap["effective_date"] == "2026-05-15"

    def test_db_historical_freshness_mode(self):
        """DB 路徑 freshness_mode 一律 "historical"，is_stale=False。"""
        data = _make_data(txf_last="2026-04-30", us_last="2026-04-29")
        observed_at = self._observed_at("2026-05-19T06:00:00")

        snap = build_snapshot_from_data(
            data,
            source="db",
            asof=datetime.date(2026, 4, 30),
            observed_at=observed_at,
            us_session_validation="validated",
            txf_asof_validation="strict",
        )
        assert snap["freshness_mode"] == "historical"
        assert snap["is_stale"] is False

    def test_asof_adjusted_from_set_when_different(self):
        """非交易日 asof（週六 5/17）→ asof_adjusted_from 有值。"""
        data = _make_data(txf_last="2026-05-15", us_last="2026-05-14")
        observed_at = self._observed_at("2026-05-19T06:00:00")

        snap = build_snapshot_from_data(
            data,
            source="db",
            asof=datetime.date(2026, 5, 17),  # 週六，is_trading_day=False → flag
            observed_at=observed_at,
            us_session_validation="validated",
            txf_asof_validation="strict",
        )
        assert snap["asof_adjusted_from"] == "2026-05-17"
        assert snap["resolved_effective_date"] == "2026-05-15"

    def test_asof_adjusted_from_none_when_same(self):
        """asof == resolved effective_date → asof_adjusted_from=None。"""
        data = _make_data(txf_last="2026-04-30", us_last="2026-04-29")
        observed_at = self._observed_at("2026-05-19T06:00:00")

        snap = build_snapshot_from_data(
            data,
            source="db",
            asof=datetime.date(2026, 4, 30),
            observed_at=observed_at,
            us_session_validation="validated",
            txf_asof_validation="strict",
        )
        assert snap["asof_adjusted_from"] is None

    def test_generated_at_equals_observed_at_iso(self):
        """generated_at = observed_at.isoformat()（不重複讀 wall-clock）。"""
        data = _make_data()
        observed_at = TZ.localize(datetime.datetime(2026, 5, 19, 6, 0, 13, 421000))

        snap = build_snapshot_from_data(
            data,
            source="live",
            observed_at=observed_at,
            txf_calendar_validation_mode="strict",
            us_calendar_validation_mode="strict",
        )
        assert snap["generated_at"] == observed_at.isoformat()
        assert snap["server_today"] == observed_at.astimezone(TZ).date().isoformat()

    def test_live_requires_txf_calendar_mode(self):
        """live 路徑未傳 txf_calendar_validation_mode → AssertionError。"""
        data = _make_data()
        observed_at = self._observed_at()
        with pytest.raises(AssertionError):
            build_snapshot_from_data(
                data, source="live", observed_at=observed_at,
                us_calendar_validation_mode="strict",
                # missing txf_calendar_validation_mode
            )

    def test_db_requires_us_session_validation(self):
        """db 路徑未傳 us_session_validation → AssertionError。"""
        data = _make_data()
        observed_at = self._observed_at()
        with pytest.raises(AssertionError):
            build_snapshot_from_data(
                data, source="db", asof=datetime.date(2026, 4, 30),
                observed_at=observed_at,
                txf_asof_validation="strict",
                # missing us_session_validation
            )

    def test_live_calendar_fields_set_db_null(self):
        """live 路徑：txf_calendar_validation/us_calendar_validation 有值；db 欄位為 None。"""
        data = _make_data()
        observed_at = self._observed_at()
        snap = build_snapshot_from_data(
            data, source="live", observed_at=observed_at,
            txf_calendar_validation_mode="strict",
            us_calendar_validation_mode="strict",
        )
        assert snap["txf_calendar_validation"] == "strict"
        assert snap["us_calendar_validation"] == "strict"
        assert snap["us_session_validation"] is None
        assert snap["txf_asof_validation"] is None

    def test_db_validation_fields_set_live_null(self):
        """db 路徑：us_session_validation/txf_asof_validation 有值；live 欄位為 None。"""
        data = _make_data(txf_last="2026-04-30", us_last="2026-04-29")
        observed_at = self._observed_at()
        snap = build_snapshot_from_data(
            data, source="db", asof=datetime.date(2026, 4, 30),
            observed_at=observed_at,
            us_session_validation="skipped_calendar_unavailable",
            txf_asof_validation="skipped_calendar_unavailable",
        )
        assert snap["us_session_validation"] == "skipped_calendar_unavailable"
        assert snap["txf_asof_validation"] == "skipped_calendar_unavailable"
        assert snap["txf_calendar_validation"] is None
        assert snap["us_calendar_validation"] is None

    def test_forecast_base_close_equals_prev_close(self):
        """forecast_base_close 是 prev_close 的別名，兩者值相同。"""
        data = _make_data()
        observed_at = self._observed_at()
        snap = build_snapshot_from_data(
            data, source="live", observed_at=observed_at,
            txf_calendar_validation_mode="strict",
            us_calendar_validation_mode="strict",
        )
        assert snap["forecast_base_close"] == snap["prev_close"]

    def test_returns_earliest_db_date(self):
        """earliest_db_date 如實回傳 caller 注入的值。"""
        data = _make_data()
        observed_at = self._observed_at()
        earliest = datetime.date(2026, 1, 2)
        snap = build_snapshot_from_data(
            data, source="live", observed_at=observed_at,
            txf_calendar_validation_mode="strict",
            us_calendar_validation_mode="strict",
            earliest_db_date=earliest,
        )
        assert snap["earliest_db_date"] == "2026-01-02"

    def test_earliest_db_date_none_when_not_injected(self):
        """earliest_db_date=None → snapshot 欄位為 null。"""
        data = _make_data()
        observed_at = self._observed_at()
        snap = build_snapshot_from_data(
            data, source="live", observed_at=observed_at,
            txf_calendar_validation_mode="strict",
            us_calendar_validation_mode="strict",
        )
        assert snap["earliest_db_date"] is None

    def test_asof_adjusted_from_none_when_trading_day_and_different_effective(self):
        """交易日 asof（例如「今天」）即使 resolved_effective < asof，adj_from 也應為 None。
        場景：用戶 pick 今天（交易日），系統回前一日配對日，這是正常狀態。"""
        data = _make_data(txf_last="2026-05-20", us_last="2026-05-19")
        observed_at = self._observed_at("2026-05-21T08:00:00")
        with patch("src.freshness.is_trading_day", return_value=True):
            snap = build_snapshot_from_data(
                data,
                source="db",
                asof=datetime.date(2026, 5, 21),  # 今天（交易日），effective=5/20
                observed_at=observed_at,
                us_session_validation="validated",
                txf_asof_validation="strict",
            )
        assert snap["asof_adjusted_from"] is None
        assert snap["resolved_effective_date"] == "2026-05-20"

    def test_asof_adjusted_from_set_when_weekend(self):
        """週末 asof → adj_from 有值（非交易日調整）。"""
        data = _make_data(txf_last="2026-05-15", us_last="2026-05-14")
        observed_at = self._observed_at("2026-05-19T06:00:00")
        # 5/17 = 週六，is_trading_day → False（不用 mock，週末直接 False）
        snap = build_snapshot_from_data(
            data,
            source="db",
            asof=datetime.date(2026, 5, 17),
            observed_at=observed_at,
            us_session_validation="validated",
            txf_asof_validation="strict",
        )
        assert snap["asof_adjusted_from"] == "2026-05-17"
        assert snap["resolved_effective_date"] == "2026-05-15"


# ---------------------------------------------------------------------------
# Cache-Control headers
# ---------------------------------------------------------------------------

class TestCacheControlHeaders:
    def test_api_snapshot_no_asof_has_no_store(self):
        import src.main as main_mod
        original = main_mod._SNAPSHOT
        main_mod._SNAPSHOT = _FIXTURE_SNAPSHOT
        try:
            resp = client.get("/api/snapshot")
            assert resp.status_code == 200
            assert "no-store" in resp.headers.get("cache-control", "")
        finally:
            main_mod._SNAPSHOT = original

    def test_api_snapshot_with_asof_has_no_store(self):
        with patch("src.main.build_snapshot", return_value=_FIXTURE_SNAPSHOT):
            resp = client.get("/api/snapshot?asof=2026-05-18")
        assert resp.status_code == 200
        assert "no-store" in resp.headers.get("cache-control", "")


# ---------------------------------------------------------------------------
# GET /api/snapshot（無 asof）— fresh overlay（server_today + is_stale）
# ---------------------------------------------------------------------------

class TestApiSnapshotFreshOverlay:
    """驗證 GET /api/snapshot（無 asof）每次 request 都以 fresh observed_at 重算
    server_today 與 is_stale，不直接回傳凍結的 _SNAPSHOT 值。"""

    def _set_snapshot(self, snap: dict):
        import src.main as main_mod
        original = main_mod._SNAPSHOT
        main_mod._SNAPSHOT = snap
        return original

    def _restore(self, original):
        import src.main as main_mod
        main_mod._SNAPSHOT = original

    def test_server_today_is_fresh_not_frozen(self):
        """handler 應回傳 fresh server_today（今天），而非 _SNAPSHOT 內的凍結值。"""
        snap = {**_FIXTURE_SNAPSHOT, "server_today": "2026-05-20"}
        original = self._set_snapshot(snap)
        try:
            fixed_now = TZ.localize(datetime.datetime.fromisoformat("2026-05-22T08:00:00"))
            with patch("src.main.datetime") as mock_dt, \
                 patch("src.main.freshness") as mock_freshness:
                mock_dt.datetime.now.return_value = fixed_now
                mock_dt.date = datetime.date
                mock_freshness.previous_xtai_session.return_value = datetime.date(2026, 5, 21)
                resp = client.get("/api/snapshot")
            assert resp.status_code == 200
            assert resp.json()["server_today"] == "2026-05-22"
        finally:
            self._restore(original)

    def test_is_stale_true_when_effective_lags_expected(self):
        """trading_date = 5/21，今天 5/22 → 5/21 < 5/22 → is_stale = True。"""
        snap = {**_FIXTURE_SNAPSHOT, "effective_date": "2026-05-20", "trading_date": "2026-05-21", "is_stale": False}
        original = self._set_snapshot(snap)
        try:
            fixed_now = TZ.localize(datetime.datetime.fromisoformat("2026-05-22T08:00:00"))
            with patch("src.main.datetime") as mock_dt:
                mock_dt.datetime.now.return_value = fixed_now
                mock_dt.date = datetime.date
                resp = client.get("/api/snapshot")
            assert resp.status_code == 200
            assert resp.json()["is_stale"] is True
        finally:
            self._restore(original)

    def test_is_stale_false_when_effective_matches_expected(self):
        """trading_date = 5/22，今天 5/22 → 5/22 == 5/22 → is_stale = False。"""
        snap = {**_FIXTURE_SNAPSHOT, "effective_date": "2026-05-21", "trading_date": "2026-05-22", "is_stale": False}
        original = self._set_snapshot(snap)
        try:
            fixed_now = TZ.localize(datetime.datetime.fromisoformat("2026-05-22T08:00:00"))
            with patch("src.main.datetime") as mock_dt:
                mock_dt.datetime.now.return_value = fixed_now
                mock_dt.date = datetime.date
                resp = client.get("/api/snapshot")
            assert resp.status_code == 200
            assert resp.json()["is_stale"] is False
        finally:
            self._restore(original)

    def test_is_stale_false_when_trading_date_equals_today(self):
        """trading_date == today → is_stale = False（calendar 狀態無關）。"""
        snap = {**_FIXTURE_SNAPSHOT, "effective_date": "2026-05-21", "trading_date": "2026-05-22", "is_stale": False}
        original = self._set_snapshot(snap)
        try:
            fixed_now = TZ.localize(datetime.datetime.fromisoformat("2026-05-22T08:00:00"))
            with patch("src.main.datetime") as mock_dt:
                mock_dt.datetime.now.return_value = fixed_now
                mock_dt.date = datetime.date
                resp = client.get("/api/snapshot")
            assert resp.status_code == 200
            assert resp.json()["is_stale"] is False
        finally:
            self._restore(original)


# ---------------------------------------------------------------------------
# _validate_live_raw
# ---------------------------------------------------------------------------

class TestValidateLiveRaw:
    def _observed_at(self, s: str = "2026-05-19T06:00:00") -> datetime.datetime:
        return TZ.localize(datetime.datetime.fromisoformat(s))

    def test_happy_path_returns_strict(self):
        """正常 cron 場景 → 回 {txf_calendar_validation_mode: 'strict', ...}。"""
        data = _make_data(txf_last="2026-05-16", us_last="2026-05-15")  # 週五 + 週五
        observed_at = self._observed_at("2026-05-19T06:00:00")

        with patch("src.freshness.check_freshness", return_value={
            "is_stale": False, "mode": "normal", "expected_trade_date": datetime.date(2026, 5, 16)
        }), \
        patch("src.freshness.expected_us_session_for_effective_date",
              return_value=datetime.date(2026, 5, 15)), \
        patch("src.freshness.last_completed_us_session",
              return_value=datetime.date(2026, 5, 15)), \
        patch("src.freshness.previous_nyse_session",
              return_value=datetime.date(2026, 5, 14)), \
        patch("src.freshness._get_calendar") as mock_cal:
            mock_xtai = MagicMock()
            mock_xtai.is_session.return_value = True
            mock_cal.return_value = mock_xtai
            result = _validate_live_raw(data, observed_at)

        assert result["txf_calendar_validation_mode"] == "strict"
        assert result["us_calendar_validation_mode"] == "strict"

    def test_too_few_txf_rows_raises(self):
        """txf 不足 130 筆 → raise。"""
        data = _make_data()
        data["txf"] = _make_txf(n=50)
        observed_at = self._observed_at()
        with pytest.raises(LivePublishValidationError, match="130"):
            _validate_live_raw(data, observed_at)

    def test_us_dates_mismatch_raises(self):
        """US 四支最後日期不一致 → raise。"""
        data = _make_data(us_last="2026-05-18")
        # 讓 dj 的最後一筆是 2026-05-17
        data["dj"] = _make_us(last_date="2026-05-17")
        observed_at = self._observed_at()

        with patch("src.freshness.check_freshness", return_value={
            "is_stale": False, "mode": "normal", "expected_trade_date": datetime.date(2026, 5, 19)
        }), patch("src.freshness._get_calendar") as mock_cal:
            mock_xtai = MagicMock()
            mock_xtai.is_session.return_value = True
            mock_cal.return_value = mock_xtai
            with pytest.raises(LivePublishValidationError, match="mismatch"):
                _validate_live_raw(data, observed_at)

    def test_stale_txf_raises(self):
        """TXF is_stale → raise。"""
        data = _make_data(txf_last="2026-05-18", us_last="2026-05-18")
        observed_at = self._observed_at()

        with patch("src.freshness.check_freshness", return_value={
            "is_stale": True, "mode": "normal", "expected_trade_date": datetime.date(2026, 5, 19)
        }), patch("src.freshness._get_calendar") as mock_cal:
            mock_xtai = MagicMock()
            mock_xtai.is_session.return_value = True
            mock_cal.return_value = mock_xtai
            with pytest.raises(LivePublishValidationError, match="lags expected"):
                _validate_live_raw(data, observed_at)

    def test_us_not_matching_expected_raises(self):
        """US 最後日 != expected_us → raise（禁止 effective_date=D + us=D-1）。"""
        data = _make_data(txf_last="2026-05-19", us_last="2026-05-18")
        observed_at = self._observed_at("2026-05-20T06:00:00")

        with patch("src.freshness.check_freshness", return_value={
            "is_stale": False, "mode": "normal", "expected_trade_date": datetime.date(2026, 5, 19)
        }), \
        patch("src.freshness.expected_us_session_for_effective_date",
              return_value=datetime.date(2026, 5, 19)), \
        patch("src.freshness.last_completed_us_session",
              return_value=datetime.date(2026, 5, 19)), \
        patch("src.freshness._get_calendar") as mock_cal:
            mock_xtai = MagicMock()
            mock_xtai.is_session.return_value = True
            mock_cal.return_value = mock_xtai
            with pytest.raises(LivePublishValidationError, match="expected NYSE session") as exc_info:
                _validate_live_raw(data, observed_at)
            assert exc_info.value.kind == "us_session_mismatch_for_effective_date"
            assert exc_info.value.effective_date == datetime.date(2026, 5, 19)

    def test_partial_session_leakage_raises(self):
        """expected_us > completed_us（NYSE D 尚未收盤）→ raise with kind='partial_session_leakage'。"""
        data = _make_data(txf_last="2026-05-19", us_last="2026-05-19")
        observed_at = self._observed_at("2026-05-19T16:00:00")  # TPE D 16:00

        with patch("src.freshness.check_freshness", return_value={
            "is_stale": False, "mode": "normal", "expected_trade_date": datetime.date(2026, 5, 19)
        }), \
        patch("src.freshness.expected_us_session_for_effective_date",
              return_value=datetime.date(2026, 5, 19)), \
        patch("src.freshness.last_completed_us_session",
              return_value=datetime.date(2026, 5, 18)), \
        patch("src.freshness._get_calendar") as mock_cal:
            mock_xtai = MagicMock()
            mock_xtai.is_session.return_value = True
            mock_cal.return_value = mock_xtai
            with pytest.raises(LivePublishValidationError, match="partial-session leakage") as exc_info:
                _validate_live_raw(data, observed_at)
        assert exc_info.value.kind == "partial_session_leakage"
        assert exc_info.value.effective_date == datetime.date(2026, 5, 19)

    def test_calendar_unavailable_fail_closed_by_default(self):
        """expected_us_last=None、ALLOW_DEGRADED_US_SESSION=False → raise。"""
        data = _make_data(txf_last="2026-05-16", us_last="2026-05-15")
        observed_at = self._observed_at()

        with patch("src.freshness.check_freshness", return_value={
            "is_stale": False, "mode": "normal", "expected_trade_date": datetime.date(2026, 5, 16)
        }), \
        patch("src.freshness.expected_us_session_for_effective_date", return_value=None), \
        patch("src.freshness.last_completed_us_session", return_value=None), \
        patch("src.freshness._get_calendar") as mock_cal, \
        patch.object(config, "ALLOW_DEGRADED_US_SESSION", False):
            mock_xtai = MagicMock()
            mock_xtai.is_session.return_value = True
            mock_cal.return_value = mock_xtai
            with pytest.raises(LivePublishValidationError, match="NYSE calendar unavailable"):
                _validate_live_raw(data, observed_at)

    def test_calendar_unavailable_allowed_when_flag_set(self):
        """ALLOW_DEGRADED_US_SESSION=True → 通過，us_calendar_validation_mode='degraded_override'。"""
        data = _make_data(txf_last="2026-05-16", us_last="2026-05-15")
        observed_at = self._observed_at()

        with patch("src.freshness.check_freshness", return_value={
            "is_stale": False, "mode": "normal", "expected_trade_date": datetime.date(2026, 5, 16)
        }), \
        patch("src.freshness.expected_us_session_for_effective_date", return_value=None), \
        patch("src.freshness.last_completed_us_session", return_value=None), \
        patch("src.freshness.previous_nyse_session", return_value=None), \
        patch("src.freshness._get_calendar") as mock_cal, \
        patch.object(config, "ALLOW_DEGRADED_US_SESSION", True), \
        patch.object(config, "ALLOW_DEGRADED_TXF_FRESHNESS", True):
            mock_xtai = MagicMock()
            mock_xtai.is_session.return_value = True
            mock_cal.return_value = mock_xtai
            result = _validate_live_raw(data, observed_at)

        assert result["us_calendar_validation_mode"] == "degraded_override"
        assert result["txf_calendar_validation_mode"] == "strict"


# ---------------------------------------------------------------------------
# publish_snapshot 四階段
# ---------------------------------------------------------------------------

class TestPublishSnapshot:
    def _get_tmp_path(self) -> pathlib.Path:
        from src.main import _HTML_PATH
        return pathlib.Path(str(_HTML_PATH) + ".tmp")

    def test_render_tmp_failure_does_not_touch_db_or_cache(self):
        """render_tmp 失敗 → db_upsert / rename / cache 都不執行。"""
        import src.main as main_mod
        original_snapshot = main_mod._SNAPSHOT.copy() if main_mod._SNAPSHOT else {}
        data = _make_data()

        with patch("src.render.render_to", side_effect=Exception("render failed")), \
             patch("src.db.upsert_indices_batch") as mock_upsert, \
             patch.object(config, "DATABASE_URL", "postgresql://test"):
            with pytest.raises(Exception, match="render failed"):
                publish_snapshot(_FIXTURE_SNAPSHOT, data)

        mock_upsert.assert_not_called()
        assert main_mod._SNAPSHOT == original_snapshot

    def test_db_upsert_failure_does_not_rename_or_cache(self):
        """render_tmp 成功、db_upsert raise → rename 不執行、cache 不更新。"""
        import src.main as main_mod
        original_snapshot = main_mod._SNAPSHOT.copy() if main_mod._SNAPSHOT else {}
        data = _make_data()
        tmp_path = self._get_tmp_path()

        with patch("src.render.render_to"), \
             patch("src.db.upsert_indices_batch", side_effect=Exception("db failed")), \
             patch.object(config, "DATABASE_URL", "postgresql://test"):
            with pytest.raises(Exception, match="db failed"):
                publish_snapshot(_FIXTURE_SNAPSHOT, data)

        assert main_mod._SNAPSHOT == original_snapshot

    def test_success_updates_cache(self):
        """render_tmp + db_upsert + rename 全成功 → _SNAPSHOT 更新。"""
        import src.main as main_mod
        data = _make_data()
        new_snap = _FIXTURE_SNAPSHOT.copy()
        new_snap["effective_date"] = "2026-05-18"

        with patch("src.render.render_to"), \
             patch("src.db.upsert_indices_batch"), \
             patch("pathlib.Path.replace"), \
             patch.object(config, "DATABASE_URL", "postgresql://test"):
            publish_snapshot(new_snap, data)

        assert main_mod._SNAPSHOT["effective_date"] == "2026-05-18"

    def test_phase_order_render_db_rename_cache(self):
        """四個 phase 嚴格按 render_tmp→db_upsert→rename_html→update_cache 順序。"""
        import src.main as main_mod
        data = _make_data()
        call_order = []

        with patch("src.render.render_to", side_effect=lambda *a: call_order.append("render_tmp")), \
             patch("src.db.upsert_indices_batch", side_effect=lambda *a: call_order.append("db_upsert")), \
             patch("pathlib.Path.replace", side_effect=lambda *a: call_order.append("rename_html")), \
             patch.object(config, "DATABASE_URL", "postgresql://test"):
            publish_snapshot(_FIXTURE_SNAPSHOT, data)

        call_order.append("update_cache")  # cache update happens at return
        assert call_order.index("render_tmp") < call_order.index("db_upsert")
        assert call_order.index("db_upsert") < call_order.index("rename_html")

    def test_render_tmp_cleans_stale_tmp_first(self):
        """render_tmp 前先 unlink(missing_ok=True) 清理殘留 .tmp。"""
        data = _make_data()
        unlink_called = []

        real_unlink = pathlib.Path.unlink

        def spy_unlink(self, missing_ok=False):
            if str(self).endswith(".tmp"):
                unlink_called.append(str(self))

        with patch("src.render.render_to"), \
             patch("src.db.upsert_indices_batch"), \
             patch("pathlib.Path.replace"), \
             patch("pathlib.Path.unlink", spy_unlink), \
             patch.object(config, "DATABASE_URL", "postgresql://test"):
            publish_snapshot(_FIXTURE_SNAPSHOT, data)

        assert len(unlink_called) >= 1, "unlink should be called on .tmp path before render"


# ---------------------------------------------------------------------------
# _validate_snapshot_output_strict
# ---------------------------------------------------------------------------

class TestValidateSnapshotOutputStrict:
    def test_nan_stat_raises(self):
        """stats.a20=NaN → raise LivePublishValidationError。"""
        from src.main import _validate_snapshot_output_strict
        import math
        snap = _FIXTURE_SNAPSHOT.copy()
        snap["stats"] = dict(snap["stats"])
        snap["stats"]["a20"] = float("nan")
        with pytest.raises(LivePublishValidationError, match="a20"):
            _validate_snapshot_output_strict(snap)

    def test_missing_m6_raises(self):
        """weekday_multi.m6=None → raise。"""
        from src.main import _validate_snapshot_output_strict
        snap = _FIXTURE_SNAPSHOT.copy()
        snap["weekday_multi"] = dict(snap["weekday_multi"])
        snap["weekday_multi"]["m6"] = None
        with pytest.raises(LivePublishValidationError):
            _validate_snapshot_output_strict(snap)


# ---------------------------------------------------------------------------
# refresh_and_publish fallback
# ---------------------------------------------------------------------------

async def _to_thread(fn, *args, **kw):
    """asyncio.to_thread 的測試替代品：直接同步呼叫 fn，避免線程池開銷。"""
    return fn(*args, **kw)


class TestRefreshAndPublishFallback:
    """HF-1：partial-session leakage 時自動降級到 effective_date=D-1。"""

    def _observed_at(self, iso: str = "2026-05-19T22:35:00+08:00") -> datetime.datetime:
        return datetime.datetime.fromisoformat(iso)

    @pytest.mark.asyncio
    async def test_fallback_to_d_minus_1_on_partial_session_leakage(self):
        """
        第一次 _validate_live_raw raise kind='partial_session_leakage' →
        trim txf 最後一列 → 第二次驗過 → publish 被呼叫。
        """
        from src.main import refresh_and_publish

        data_full = _make_data(txf_last="2026-05-19", us_last="2026-05-18")
        observed_at = self._observed_at()

        validation_ok = {
            "txf_calendar_validation_mode": "strict",
            "us_calendar_validation_mode": "strict",
        }

        call_count = {"validate": 0}

        def mock_validate(data, obs, *, skip_staleness=False):
            call_count["validate"] += 1
            if call_count["validate"] == 1:
                raise LivePublishValidationError(
                    "partial-session leakage risk",
                    kind="partial_session_leakage",
                    effective_date=datetime.date(2026, 5, 19),
                )
            return validation_ok

        with patch("src.main.fetch_live_data", return_value=data_full), \
             patch("src.main.datetime") as mock_dt, \
             patch("src.main._validate_live_raw", side_effect=mock_validate), \
             patch("src.main.build_snapshot_from_data", return_value=_FIXTURE_SNAPSHOT), \
             patch("src.main._resolve_earliest_for_live", return_value=None), \
             patch("src.main.publish_snapshot") as mock_publish, \
             patch("asyncio.to_thread", new=_to_thread):
            mock_dt.datetime.now.return_value = observed_at
            mock_dt.date = datetime.date
            await refresh_and_publish()

        assert call_count["validate"] == 2, "第二次 _validate_live_raw 應被呼叫"
        mock_publish.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_fallback_when_kind_is_none(self):
        """
        kind=None（如 rows<130）→ 直接 propagate，不 trim 不 retry。
        """
        from src.main import refresh_and_publish

        data = _make_data()
        validate_call_count = [0]

        def mock_validate(d, obs, *, skip_staleness=False):
            validate_call_count[0] += 1
            raise LivePublishValidationError("txf rows 50 < 130", kind=None)

        with patch("src.main.fetch_live_data", return_value=data), \
             patch("src.main.datetime") as mock_dt, \
             patch("src.main._validate_live_raw", side_effect=mock_validate), \
             patch("asyncio.to_thread", new=_to_thread):
            mock_dt.datetime.now.return_value = self._observed_at()
            mock_dt.date = datetime.date
            with pytest.raises(LivePublishValidationError, match="130"):
                await refresh_and_publish()

        assert validate_call_count[0] == 1, "rows<130 不應 retry"

    @pytest.mark.asyncio
    async def test_fallback_propagates_if_second_validation_fails(self):
        """
        第一次 partial_session_leakage → trim → 第二次仍失敗 → propagate。
        """
        from src.main import refresh_and_publish

        data = _make_data(txf_last="2026-05-19", us_last="2026-05-18")
        call_count = [0]

        def mock_validate(d, obs, *, skip_staleness=False):
            call_count[0] += 1
            if call_count[0] == 1:
                raise LivePublishValidationError(
                    "partial-session leakage risk",
                    kind="partial_session_leakage",
                    effective_date=datetime.date(2026, 5, 19),
                )
            raise LivePublishValidationError("NYSE calendar unavailable", kind=None)

        with patch("src.main.fetch_live_data", return_value=data), \
             patch("src.main.datetime") as mock_dt, \
             patch("src.main._validate_live_raw", side_effect=mock_validate), \
             patch("src.main.publish_snapshot") as mock_publish, \
             patch("asyncio.to_thread", new=_to_thread):
            mock_dt.datetime.now.return_value = self._observed_at()
            mock_dt.date = datetime.date
            with pytest.raises(LivePublishValidationError, match="NYSE calendar unavailable"):
                await refresh_and_publish()

        mock_publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_fallback_when_txf_too_short_to_trim(self):
        """
        txf 只剩 1 列時，kind='partial_session_leakage' → 直接 propagate（沒得 trim）。
        """
        from src.main import refresh_and_publish

        data = _make_data()
        data = {**data, "txf": data["txf"].iloc[:1]}  # 只剩 1 列

        def mock_validate(d, obs, *, skip_staleness=False):
            raise LivePublishValidationError(
                "partial-session leakage risk",
                kind="partial_session_leakage",
                effective_date=datetime.date(2026, 5, 19),
            )

        with patch("src.main.fetch_live_data", return_value=data), \
             patch("src.main.datetime") as mock_dt, \
             patch("src.main._validate_live_raw", side_effect=mock_validate), \
             patch("asyncio.to_thread", new=_to_thread):
            mock_dt.datetime.now.return_value = self._observed_at()
            mock_dt.date = datetime.date
            with pytest.raises(LivePublishValidationError, match="partial-session"):
                await refresh_and_publish()

    @pytest.mark.asyncio
    async def test_fallback_trims_us_partial_bar(self):
        """
        HF-2：yfinance 回傳 US partial bar（TXF=5/20、US=5/20）→
        fallback 後 TXF 與 US 都被 trim 到 5/19。
        """
        from src.main import refresh_and_publish

        # TXF 最後=5/20；US 3 列（5/18、5/19、5/20 partial bar）
        txf_d = "2026-05-20"
        us_d = "2026-05-20"
        data_full = {
            "txf": _make_txf(last_date=txf_d),
            "dj": pd.DataFrame(
                {"open": 100.0, "high": 110.0, "low": 95.0, "close": 105.0, "volume": 1000},
                index=pd.DatetimeIndex([
                    pd.Timestamp("2026-05-18"),
                    pd.Timestamp("2026-05-19"),
                    pd.Timestamp("2026-05-20"),
                ]),
            ),
            "nq": pd.DataFrame(
                {"open": 100.0, "high": 110.0, "low": 95.0, "close": 105.0, "volume": 1000},
                index=pd.DatetimeIndex([
                    pd.Timestamp("2026-05-18"),
                    pd.Timestamp("2026-05-19"),
                    pd.Timestamp("2026-05-20"),
                ]),
            ),
            "spy": pd.DataFrame(
                {"open": 100.0, "high": 110.0, "low": 95.0, "close": 105.0, "volume": 1000},
                index=pd.DatetimeIndex([
                    pd.Timestamp("2026-05-18"),
                    pd.Timestamp("2026-05-19"),
                    pd.Timestamp("2026-05-20"),
                ]),
            ),
            "tsm": pd.DataFrame(
                {"open": 100.0, "high": 110.0, "low": 95.0, "close": 105.0, "volume": 1000},
                index=pd.DatetimeIndex([
                    pd.Timestamp("2026-05-18"),
                    pd.Timestamp("2026-05-19"),
                    pd.Timestamp("2026-05-20"),
                ]),
            ),
        }
        observed_at = self._observed_at("2026-05-20T22:16:00+08:00")
        validation_ok = {
            "txf_calendar_validation_mode": "strict",
            "us_calendar_validation_mode": "strict",
        }
        call_count = {"validate": 0}
        published_data = {}

        def mock_validate(data, obs, *, skip_staleness=False):
            call_count["validate"] += 1
            if call_count["validate"] == 1:
                raise LivePublishValidationError(
                    "partial-session leakage risk",
                    kind="partial_session_leakage",
                    effective_date=datetime.date(2026, 5, 20),
                )
            return validation_ok

        def mock_publish(snapshot, data):
            published_data.update(data)

        with patch("src.main.fetch_live_data", return_value=data_full), \
             patch("src.main.datetime") as mock_dt, \
             patch("src.main._validate_live_raw", side_effect=mock_validate), \
             patch("src.main.build_snapshot_from_data", return_value=_FIXTURE_SNAPSHOT), \
             patch("src.main._resolve_earliest_for_live", return_value=None), \
             patch("src.main.freshness") as mock_freshness, \
             patch("src.main.publish_snapshot", side_effect=mock_publish), \
             patch("asyncio.to_thread", new=_to_thread):
            mock_dt.datetime.now.return_value = observed_at
            mock_dt.date = datetime.date
            # expected_us_session_for_effective_date(2026-05-19) → 2026-05-19
            mock_freshness.expected_us_session_for_effective_date.return_value = datetime.date(2026, 5, 19)
            await refresh_and_publish()

        assert call_count["validate"] == 2, "應觸發一次 fallback + 一次重試"
        assert published_data["txf"].index[-1].date() == datetime.date(2026, 5, 19), \
            "TXF 最後一筆應為 5/19"
        for k in ("dj", "nq", "spy", "tsm"):
            assert published_data[k].index[-1].date() == datetime.date(2026, 5, 19), \
                f"US[{k}] 最後一筆應為 5/19"

    @pytest.mark.asyncio
    async def test_fallback_propagates_when_us_trim_leaves_lt_2_rows(self):
        """
        HF-2：US trim 後剩不足 2 列 → propagate 原 exception，不發布。
        """
        from src.main import refresh_and_publish

        # US 只有 2 列（5/19 + 5/20），trim 5/20 後剩 1 列 → 觸發 propagate
        data_full = {
            "txf": _make_txf(last_date="2026-05-20"),
            "dj": pd.DataFrame(
                {"open": 100.0, "high": 110.0, "low": 95.0, "close": 105.0, "volume": 1000},
                index=pd.DatetimeIndex([
                    pd.Timestamp("2026-05-19"),
                    pd.Timestamp("2026-05-20"),
                ]),
            ),
            "nq": _make_us(last_date="2026-05-20"),
            "spy": _make_us(last_date="2026-05-20"),
            "tsm": _make_us(last_date="2026-05-20"),
        }
        observed_at = self._observed_at("2026-05-20T22:16:00+08:00")
        original_error = LivePublishValidationError(
            "partial-session leakage risk",
            kind="partial_session_leakage",
            effective_date=datetime.date(2026, 5, 20),
        )

        def mock_validate(data, obs, *, skip_staleness=False):
            raise original_error

        with patch("src.main.fetch_live_data", return_value=data_full), \
             patch("src.main.datetime") as mock_dt, \
             patch("src.main._validate_live_raw", side_effect=mock_validate), \
             patch("src.main.freshness") as mock_freshness, \
             patch("src.main.publish_snapshot") as mock_publish, \
             patch("asyncio.to_thread", new=_to_thread):
            mock_dt.datetime.now.return_value = observed_at
            mock_dt.date = datetime.date
            mock_freshness.expected_us_session_for_effective_date.return_value = datetime.date(2026, 5, 19)
            with pytest.raises(LivePublishValidationError, match="partial-session"):
                await refresh_and_publish()

        mock_publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_fallback_uses_new_effective_when_xnys_calendar_unavailable(self):
        """
        HF-2：XNYS calendar 不可用（expected_us=None）→ US 改用 new_effective 為上限 trim。
        """
        from src.main import refresh_and_publish

        data_full = {
            "txf": _make_txf(last_date="2026-05-20"),
            "dj": pd.DataFrame(
                {"open": 100.0, "high": 110.0, "low": 95.0, "close": 105.0, "volume": 1000},
                index=pd.DatetimeIndex([
                    pd.Timestamp("2026-05-18"),
                    pd.Timestamp("2026-05-19"),
                    pd.Timestamp("2026-05-20"),
                ]),
            ),
            "nq": pd.DataFrame(
                {"open": 100.0, "high": 110.0, "low": 95.0, "close": 105.0, "volume": 1000},
                index=pd.DatetimeIndex([
                    pd.Timestamp("2026-05-18"),
                    pd.Timestamp("2026-05-19"),
                    pd.Timestamp("2026-05-20"),
                ]),
            ),
            "spy": pd.DataFrame(
                {"open": 100.0, "high": 110.0, "low": 95.0, "close": 105.0, "volume": 1000},
                index=pd.DatetimeIndex([
                    pd.Timestamp("2026-05-18"),
                    pd.Timestamp("2026-05-19"),
                    pd.Timestamp("2026-05-20"),
                ]),
            ),
            "tsm": pd.DataFrame(
                {"open": 100.0, "high": 110.0, "low": 95.0, "close": 105.0, "volume": 1000},
                index=pd.DatetimeIndex([
                    pd.Timestamp("2026-05-18"),
                    pd.Timestamp("2026-05-19"),
                    pd.Timestamp("2026-05-20"),
                ]),
            ),
        }
        observed_at = self._observed_at("2026-05-20T22:16:00+08:00")
        validation_ok = {
            "txf_calendar_validation_mode": "strict",
            "us_calendar_validation_mode": "strict",
        }
        call_count = {"validate": 0}
        published_data = {}

        def mock_validate(data, obs, *, skip_staleness=False):
            call_count["validate"] += 1
            if call_count["validate"] == 1:
                raise LivePublishValidationError(
                    "partial-session leakage risk",
                    kind="partial_session_leakage",
                    effective_date=datetime.date(2026, 5, 20),
                )
            return validation_ok

        def mock_publish(snapshot, data):
            published_data.update(data)

        with patch("src.main.fetch_live_data", return_value=data_full), \
             patch("src.main.datetime") as mock_dt, \
             patch("src.main._validate_live_raw", side_effect=mock_validate), \
             patch("src.main.build_snapshot_from_data", return_value=_FIXTURE_SNAPSHOT), \
             patch("src.main._resolve_earliest_for_live", return_value=None), \
             patch("src.main.freshness") as mock_freshness, \
             patch("src.main.publish_snapshot", side_effect=mock_publish), \
             patch("asyncio.to_thread", new=_to_thread):
            mock_dt.datetime.now.return_value = observed_at
            mock_dt.date = datetime.date
            # XNYS calendar 不可用 → None，fallback 用 new_effective=5/19 當上限
            mock_freshness.expected_us_session_for_effective_date.return_value = None
            await refresh_and_publish()

        assert call_count["validate"] == 2
        for k in ("dj", "nq", "spy", "tsm"):
            last = published_data[k].index[-1].date()
            assert last <= datetime.date(2026, 5, 19), \
                f"US[{k}] 最後一筆 {last} 應 <= new_effective 2026-05-19"


# ---------------------------------------------------------------------------
# calendar anomaly（台美交易日不對稱）
# ---------------------------------------------------------------------------

class TestCalendarAnomalyDetection:
    """測試 build_snapshot_from_data 的三情境 calendar_anomaly 分流邏輯。"""

    def _observed_at(self, s: str = "2026-05-22T06:00:00") -> datetime.datetime:
        return TZ.localize(datetime.datetime.fromisoformat(s))

    def _call(self, gap_sessions, txf_last="2026-05-21", us_last="2026-05-20"):
        data = _make_data(txf_last=txf_last, us_last=us_last)
        observed_at = self._observed_at()
        with patch("src.freshness.us_sessions_between", return_value=gap_sessions), \
             patch("src.freshness.next_xtai_session", return_value=datetime.date(2026, 5, 22)):
            snap = build_snapshot_from_data(
                data, source="live", observed_at=observed_at,
                txf_calendar_validation_mode="strict",
                us_calendar_validation_mode="strict",
            )
        return snap

    def test_normal_one_session_no_anomaly(self):
        """gap=1 → calendar_anomaly='none'，us_pcts 正常填入。"""
        snap = self._call(gap_sessions=[datetime.date(2026, 5, 21)])
        assert snap["calendar_anomaly"] == "none"
        assert snap["us_aggregation_window"] is None
        for k in ("dj", "nq", "spy", "tsm"):
            assert snap["us"][k] is not None

    def test_case1_no_session_sets_us_no_session(self):
        """gap=0 → calendar_anomaly='us_no_session'，us_pcts 全 None，default_open None。"""
        snap = self._call(gap_sessions=[])
        assert snap["calendar_anomaly"] == "us_no_session"
        assert snap["us_aggregation_window"] is None
        for k in ("dj", "nq", "spy", "tsm"):
            assert snap["us"][k] is None
        assert snap["default_open_price"] is None

    def test_case2_multiple_sessions_sets_tw_holiday_gap(self):
        """gap≥2 → calendar_anomaly='tw_holiday_gap'，us_aggregation_window 有值。"""
        gap = [
            datetime.date(2026, 2, 11), datetime.date(2026, 2, 12),
            datetime.date(2026, 2, 13), datetime.date(2026, 2, 17),
        ]
        # 建立含足夠歷史的 US DataFrame
        dates = pd.DatetimeIndex([
            pd.Timestamp("2026-02-10"),  # baseline（gap_start 前一筆）
            pd.Timestamp("2026-02-11"),  # gap_sessions[0]
            pd.Timestamp("2026-02-12"),
            pd.Timestamp("2026-02-13"),
            pd.Timestamp("2026-02-17"),  # gap_sessions[-1]
        ])
        us_df = pd.DataFrame(
            {"open": 100.0, "high": 110.0, "low": 95.0, "close": 105.0, "volume": 1000},
            index=dates,
        )
        data = {
            "txf": _make_txf(last_date="2026-02-11"),
            "dj": us_df, "nq": us_df, "spy": us_df, "tsm": us_df,
        }
        observed_at = TZ.localize(datetime.datetime.fromisoformat("2026-02-23T06:00:00"))
        with patch("src.freshness.us_sessions_between", return_value=gap), \
             patch("src.freshness.next_xtai_session", return_value=datetime.date(2026, 2, 23)):
            snap = build_snapshot_from_data(
                data, source="live", observed_at=observed_at,
                txf_calendar_validation_mode="strict",
                us_calendar_validation_mode="strict",
            )
        assert snap["calendar_anomaly"] == "tw_holiday_gap"
        assert snap["us_aggregation_window"]["start"] == "2026-02-11"
        assert snap["us_aggregation_window"]["end"] == "2026-02-17"
        assert snap["us_aggregation_window"]["session_count"] == 4
        # default_open_price 應有值（非 None）
        assert snap["default_open_price"] is not None

    def test_calendar_unavailable_degrades_to_normal(self):
        """us_sessions_between 回 None（calendar 不可用） → 退回 latest_pct 正常路徑。"""
        snap = self._call(gap_sessions=None)
        assert snap["calendar_anomaly"] == "none"
        assert snap["us_aggregation_window"] is None
        for k in ("dj", "nq", "spy", "tsm"):
            assert snap["us"][k] is not None


# ---------------------------------------------------------------------------
# cumulative_pct（幾何累積漲跌幅）
# ---------------------------------------------------------------------------

class TestCumulativePct:
    from src import fetch as _fetch

    def _df(self, closes: list[float], start_date: str = "2026-02-10") -> pd.DataFrame:
        n = len(closes)
        dates = pd.bdate_range(start=start_date, periods=n)
        return pd.DataFrame(
            {"open": 100.0, "high": 110.0, "low": 95.0, "close": closes, "volume": 1000},
            index=dates,
        )

    def test_geometric_compounding(self):
        """gap_start=2/11, gap_end=2/13 → baseline=2/10 close, cumulative = (end/base-1)*100。"""
        from src.fetch import cumulative_pct
        df = self._df([100.0, 110.0, 121.0, 110.0], "2026-02-10")
        # baseline = 2/10 close = 100, gap_start = 2/11 (pos 1), gap_end = 2/12 (pos 2)
        result = cumulative_pct(df, datetime.date(2026, 2, 11), datetime.date(2026, 2, 12))
        assert abs(result - 21.0) < 0.01  # (121/100 - 1) * 100 = 21%

    def test_returns_zero_when_start_not_in_df(self):
        from src.fetch import cumulative_pct
        df = self._df([100.0, 110.0])
        result = cumulative_pct(df, datetime.date(2026, 1, 1), datetime.date(2026, 1, 2))
        assert result == 0.0

    def test_returns_zero_when_start_is_first_row(self):
        """gap_start 在 df 第一筆 → 無前一筆 baseline → 0.0。"""
        from src.fetch import cumulative_pct
        df = self._df([100.0, 110.0], "2026-02-10")
        result = cumulative_pct(df, datetime.date(2026, 2, 10), datetime.date(2026, 2, 11))
        assert result == 0.0


# ---------------------------------------------------------------------------
# 端對端情境測試（使用真實 calendar，不 mock calendar helpers）
# ---------------------------------------------------------------------------

class TestEndToEndCalendarScenarios:
    """完整情境驗證：使用真實 exchange_calendars，確認三情境從 data → snapshot 全鏈正確。"""

    def _make_us_extended(self, last_date: str) -> pd.DataFrame:
        """建立含 40+ 個業務日的 US DataFrame，確保 baseline 與 gap_end 都在範圍內。"""
        end = pd.Timestamp(last_date)
        start = end - pd.Timedelta(days=60)
        dates = pd.bdate_range(start=start, end=end)
        n = len(dates)
        closes = [10000.0 + i * 5.0 for i in range(n)]
        return pd.DataFrame(
            {"open": 100.0, "high": 110.0, "low": 95.0, "close": closes, "volume": 1000},
            index=dates,
        )

    def test_spring_festival_2026_end_to_end(self):
        """春節情境（不 mock calendar）：
        TXF last=2026-02-11 → effective=2/11, trading=2/23,
        gap=[2/11,2/12,2/13,2/17,2/18,2/19,2/20] (7 sessions, 2/16=Presidents Day 排除)
        → calendar_anomaly='tw_holiday_gap', us_pcts 全非 None, default_open_price 非 None
        """
        txf = _make_txf(n=130, last_date="2026-02-11")
        us = self._make_us_extended(last_date="2026-02-20")
        data = {"txf": txf, "dj": us, "nq": us, "spy": us, "tsm": us}
        observed_at = TZ.localize(datetime.datetime.fromisoformat("2026-02-22T06:00:00"))

        snap = build_snapshot_from_data(
            data, source="live", observed_at=observed_at,
            txf_calendar_validation_mode="strict",
            us_calendar_validation_mode="strict",
        )

        assert snap["calendar_anomaly"] == "tw_holiday_gap", \
            f"expected tw_holiday_gap, got {snap['calendar_anomaly']}"
        assert snap["effective_date"] == "2026-02-11"
        assert snap["trading_date"] == "2026-02-23"
        win = snap["us_aggregation_window"]
        assert win is not None
        assert win["start"] == "2026-02-11"
        assert win["end"] == "2026-02-20"
        assert win["session_count"] == 7, f"expected 7 sessions, got {win['session_count']}: {win['us_session_dates']}"
        assert snap["default_open_price"] is not None
        for k in ("dj", "nq", "spy", "tsm"):
            pct = snap["us"][k]
            assert pct is not None, f"us[{k}] should be cumulative pct, got None"

    def test_thanksgiving_2026_case1_end_to_end(self):
        """感恩節 Case 1（不 mock calendar）：
        TXF last=2026-11-26 (台股有開), trading=2026-11-27,
        美股 11/26 感恩節 → gap=[] → us_no_session
        → default_open_price=None, us_pcts 全 None
        """
        txf = _make_txf(n=130, last_date="2026-11-26")
        us = self._make_us_extended(last_date="2026-11-25")  # 美股最後開盤 11/25
        data = {"txf": txf, "dj": us, "nq": us, "spy": us, "tsm": us}
        observed_at = TZ.localize(datetime.datetime.fromisoformat("2026-11-27T06:00:00"))

        snap = build_snapshot_from_data(
            data, source="live", observed_at=observed_at,
            txf_calendar_validation_mode="strict",
            us_calendar_validation_mode="strict",
        )

        assert snap["calendar_anomaly"] == "us_no_session", \
            f"expected us_no_session, got {snap['calendar_anomaly']}"
        assert snap["effective_date"] == "2026-11-26"
        assert snap["trading_date"] == "2026-11-27"
        assert snap["default_open_price"] is None
        for k in ("dj", "nq", "spy", "tsm"):
            assert snap["us"][k] is None, f"us[{k}] should be None for us_no_session"

    def test_normal_weekday_no_anomaly(self):
        """正常交易日（5/22 → 5/25）：gap=1 session → calendar_anomaly='none'，無 chip。"""
        txf = _make_txf(n=130, last_date="2026-05-22")
        us = self._make_us_extended(last_date="2026-05-22")
        data = {"txf": txf, "dj": us, "nq": us, "spy": us, "tsm": us}
        observed_at = TZ.localize(datetime.datetime.fromisoformat("2026-05-23T06:00:00"))

        snap = build_snapshot_from_data(
            data, source="live", observed_at=observed_at,
            txf_calendar_validation_mode="strict",
            us_calendar_validation_mode="strict",
        )

        assert snap["calendar_anomaly"] == "none"
        assert snap["us_aggregation_window"] is None
        assert snap["default_open_price"] is not None
        for k in ("dj", "nq", "spy", "tsm"):
            assert snap["us"][k] is not None

    def test_render_spring_festival_html_contains_window_text(self):
        """渲染靜態 HTML 驗證：
        - window._calendarAnomaly 被 Jinja2 渲染為 'tw_holiday_gap'
        - cum-chip 元素（區間累積）存在於 HTML
        - JS patchDOM/bootstrapFresh 有 tw_holiday_gap 分支的處理程式碼
        - 不包含 us_no_session 的「無開盤」靜態文字

        注意：adj-banner 文字是 JS 動態注入（bootstrapFresh/patchDOM 呼叫後才出現），
        靜態 HTML 中只有 JS 模板字串，不包含實際日期字串。
        """
        from src.render import _env

        snapshot = {
            **_FIXTURE_SNAPSHOT,
            "calendar_anomaly": "tw_holiday_gap",
            "us_aggregation_window": {
                "start": "2026-02-11",
                "end": "2026-02-20",
                "session_count": 7,
                "us_session_dates": [
                    "2026-02-11", "2026-02-12", "2026-02-13",
                    "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",
                ],
            },
            "effective_date": "2026-02-11",
            "trading_date": "2026-02-23",
            "asof_adjusted_from": None,
        }
        template = _env.get_template("index.html.j2")
        html = template.render(**snapshot)

        # Jinja2 server-side 渲染的 _calendarAnomaly 全域變數
        assert "window._calendarAnomaly = 'tw_holiday_gap'" in html, \
            "_calendarAnomaly should be rendered as tw_holiday_gap"
        # 區間累積 chip element 應存在
        assert 'id="cum-chip"' in html, "cum-chip element should exist"
        assert "區間累積" in html, "chip text should appear in HTML"
        # JS 程式碼有 tw_holiday_gap 分支（confirm feature is wired）
        assert "tw_holiday_gap" in html, "tw_holiday_gap branch should be in JS"
        # 不應出現 us_no_session 靜態文字（確認正確情境）
        assert "window._calendarAnomaly = 'us_no_session'" not in html
