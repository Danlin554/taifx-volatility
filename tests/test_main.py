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
        """asof != resolved effective_date → asof_adjusted_from 有值。"""
        data = _make_data(txf_last="2026-05-15", us_last="2026-05-14")
        observed_at = self._observed_at("2026-05-19T06:00:00")

        snap = build_snapshot_from_data(
            data,
            source="db",
            asof=datetime.date(2026, 5, 17),  # 週六，但 resolved=週五
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
