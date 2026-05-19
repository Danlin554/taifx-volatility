"""
驗證計算邏輯與試算表期望值的吻合度。
期望值來源：docs/2-台指平均震幅.xlsx（DATA 工作表）
  a20=296.85, a10=368.4, a5=409.4（簡單 H-L 的 SMA）
  s20=153.18, s10=184.29, s5=233.67（H-L 的 std ddof=1）
  sig1=552.69, sig2=736.98, sig3=921.27（a10 + N×s10）
  b1=116, b2=201.6, b3=296.85, b4=439.8, b5=734（MIN/AVERAGEIF/AVG/AVERAGEIF/MAX）
  注意：b5=734 是 MAX of HL，非 sig2=736.98
"""

import math

import pytest
import pandas as pd
import numpy as np
from src.compute import (
    true_range,
    atr_sma,
    sigma_bands,
    daytrade_targets,
    forecast,
    build_forecast,
    calc_r,
    _round_half_up,
    weekday_avg_multi,
    default_open_price,
    rolling_stats,
)


def _dummy_df(highs, lows, closes):
    """製造一個 DataFrame 方便測試。"""
    return pd.DataFrame({
        "high": highs,
        "low": lows,
        "close": closes,
    })


class TestTrueRange:
    def test_first_row_uses_hl(self):
        df = _dummy_df([110], [100], [105])
        tr = true_range(df)
        assert tr.iloc[0] == pytest.approx(10.0)

    def test_gap_up_captured(self):
        # 昨收 100，今 high=115, low=108 → TR = max(7, 15, 8) = 15
        df = _dummy_df([115, 115], [100, 108], [100, 112])
        tr = true_range(df)
        assert tr.iloc[1] == pytest.approx(15.0)

    def test_gap_down_captured(self):
        # 昨收 110，今 high=106, low=98 → TR = max(8, 4, 12) = 12
        df = _dummy_df([110, 106], [100, 98], [110, 102])
        tr = true_range(df)
        assert tr.iloc[1] == pytest.approx(12.0)


class TestAtrSma:
    def test_sma_5(self):
        trs = pd.Series([100.0, 200.0, 300.0, 400.0, 500.0])
        result = atr_sma(trs, 5)
        assert result == pytest.approx(300.0)

    def test_longer_window_trims_to_last(self):
        trs = pd.Series([100.0] * 20 + [200.0] * 20)
        result = atr_sma(trs, 20)
        assert result == pytest.approx(200.0)


class TestSigmaBands:
    def test_bands_match_csv(self):
        a10 = 368.4
        s10 = 184.29
        sig1, sig2, sig3 = sigma_bands(a10, s10)
        assert sig1 == pytest.approx(552.69, abs=0.02)
        assert sig2 == pytest.approx(736.98, abs=0.02)
        assert sig3 == pytest.approx(921.27, abs=0.02)


class TestDaytradeTargets:
    def test_targets_formula(self):
        # 5 值：[100, 200, 300, 400, 500]，avg=300
        # below avg: [100, 200] → mean=150
        # above avg: [400, 500] → mean=450
        hl = pd.Series([100.0, 200.0, 300.0, 400.0, 500.0])
        t = daytrade_targets(hl, window=5)
        assert t["b1"] == pytest.approx(100.0)   # MIN
        assert t["b2"] == pytest.approx(150.0)   # AVERAGEIF < avg
        assert t["b3"] == pytest.approx(300.0)   # AVERAGE
        assert t["b4"] == pytest.approx(450.0)   # AVERAGEIF > avg
        assert t["b5"] == pytest.approx(500.0)   # MAX

    def test_b5_is_max_not_sig2(self):
        # b5 是 MAX of HL，不是 sig2（736.98）
        hl = pd.Series([200.0, 300.0, 734.0, 250.0, 180.0])
        t = daytrade_targets(hl, window=5)
        assert t["b5"] == pytest.approx(734.0)


class TestCalcR:
    def test_floor_a10_div5(self):
        # FLOOR(368.4 / 5, 1) = FLOOR(73.68, 1) = 73
        assert calc_r(368.4) == 73
        # FLOOR(374.9 / 5, 1) = FLOOR(74.98, 1) = 74
        assert calc_r(374.9) == 74


class TestForecast:
    def test_weighted_avg_50_50(self):
        # DJ 7.51% × 50 + NQ 15.56% × 50 = 11.535%
        pcts = {"dj": 7.51, "nq": 15.56, "spy": 11.33, "tsm": 33.5}
        weights = {"dj": 50, "nq": 50, "spy": 0, "tsm": 0}
        result = forecast(26740, pcts, weights)
        assert result["weighted_pct"] == pytest.approx(11.535, abs=0.01)
        assert result["fut_est"] > 26740  # 美股漲，預估應高於前收

    def test_zero_weight_guard(self):
        pcts = {"dj": 5.0, "nq": 5.0, "spy": 5.0, "tsm": 5.0}
        weights = {"dj": 0, "nq": 0, "spy": 0, "tsm": 0}
        result = forecast(29000, pcts, weights)
        assert result["weighted_pct"] == 0.0
        assert result["fut_est"] == 29000

    def test_range_is_05pct(self):
        # 合理範圍 = 期貨預估 × ±0.5%（非 ±a20/2）
        pcts = {"dj": 0.0, "nq": 0.0, "spy": 0.0, "tsm": 0.0}
        weights = {"dj": 25, "nq": 25, "spy": 25, "tsm": 25}
        result = build_forecast(29800, pcts, weights)
        assert result["fut_est"] == 29800
        assert result["range_low"] == round(29800 * 0.995)    # 29651
        assert result["range_high"] == round(29800 * 1.005)   # 29949


class TestRoundHalfUp:
    def test_half_boundary_rounds_up(self):
        # 23114.5 → 23115（Python round() 銀行家捨入會給 23114，這裡必須 23115）
        assert _round_half_up(23114.5) == 23115
        assert _round_half_up(23115.5) == 23116
        assert _round_half_up(23116.5) == 23117

    def test_negative_half_boundary(self):
        # _round_half_up(-0.5) == 0（與 JS Math.floor(-0.5 + 0.5) = 0 一致）
        assert _round_half_up(-0.5) == 0
        # _round_half_up(-1.5) == -1（與 JS Math.floor(-1.5 + 0.5) = -1 一致）
        assert _round_half_up(-1.5) == -1
        # 驗 Python round(-1.5) == -2（銀行家捨入，與我們的 helper 不同）
        assert round(-1.5) == -2

    def test_integer_unchanged(self):
        assert _round_half_up(100.0) == 100
        assert _round_half_up(0.0) == 0

    def test_normal_rounding(self):
        assert _round_half_up(23100.3) == 23100
        assert _round_half_up(23100.7) == 23101


class TestDefaultOpenPrice:
    def test_basic_dj50_nq50(self):
        # DJ +1%, NQ +1% → weighted +1% → prev_close=20000 → 20200
        pcts = {"dj": 1.0, "nq": 1.0, "spy": 0.0, "tsm": 0.0}
        result = default_open_price(20000, pcts)
        assert result == 20200

    def test_empty_weights_raises(self):
        # 傳 {} 應 raise ValueError，不靜默替換成預設
        pcts = {"dj": 1.0, "nq": 1.0, "spy": 0.0, "tsm": 0.0}
        with pytest.raises(ValueError, match="missing keys"):
            default_open_price(20000, pcts, weights={})

    def test_custom_weights(self):
        # 100% DJ，DJ +2% → 結果應是 prev_close * 1.02
        pcts = {"dj": 2.0, "nq": 0.0, "spy": 0.0, "tsm": 0.0}
        result = default_open_price(10000, pcts, weights={"dj": 100, "nq": 0, "spy": 0, "tsm": 0})
        assert result == 10200

    def test_half_boundary_uses_half_up(self):
        # 構造一個結果為 .5 的情境：prev_close=23000, 加權漲幅讓結果 = 23114.5
        # weighted_pct = (23114.5 / 23000 - 1) * 100 ≈ 0.497826...%
        # 用 DJ=100% 讓計算簡單：DJ pct = 0.497826...
        import math as _math
        target = 23114.5
        pct = (target / 23000 - 1) * 100
        pcts = {"dj": pct, "nq": 0.0, "spy": 0.0, "tsm": 0.0}
        result = default_open_price(23000, pcts, weights={"dj": 100, "nq": 0, "spy": 0, "tsm": 0})
        # 預期走 half-up → 23115，不是 Python round() 可能的 23114
        assert result == 23115


class TestWeekdayAvgMulti:
    def _make_hl(self, n_months=7):
        """製造 n_months 個月的週一~週五資料，每日振幅 = 100。"""
        import datetime
        dates = pd.bdate_range(
            end=pd.Timestamp("2026-05-16"),
            periods=n_months * 22,  # 約 22 個交易日/月
        )
        return pd.Series(100.0, index=dates, name="hl")

    def test_m6_available_when_enough_data(self):
        hl = self._make_hl(n_months=7)
        result = weekday_avg_multi(hl)
        assert result["m6"] is not None
        assert isinstance(result["m6"], dict)
        assert set(result["m6"].keys()) == {"mon", "tue", "wed", "thu", "fri"}

    def test_m1_available_when_enough_data(self):
        hl = self._make_hl(n_months=7)
        result = weekday_avg_multi(hl)
        assert result["m1"] is not None

    def test_empty_series_returns_none_for_all(self):
        hl = pd.Series([], dtype=float)
        result = weekday_avg_multi(hl)
        assert result["m1"] is None
        assert result["m6"] is None

    def test_missing_weekday_within_dict_is_none(self):
        # 3 天都是同一周的 Wed/Thu/Fri → Mon、Tue 的平均應為 None（dict 存在但值為 None）
        dates = pd.DatetimeIndex([
            pd.Timestamp("2026-05-13"),  # Wed
            pd.Timestamp("2026-05-14"),  # Thu
            pd.Timestamp("2026-05-15"),  # Fri
        ])
        hl = pd.Series([100.0, 200.0, 150.0], index=dates)
        result = weekday_avg_multi(hl)
        # 不論哪個月份，dict 都應存在
        assert isinstance(result["m1"], dict)
        # Mon 和 Tue 沒有資料 → 值為 None
        assert result["m1"]["mon"] is None
        assert result["m1"]["tue"] is None
        # Wed/Thu/Fri 有資料 → 值不為 None
        assert result["m1"]["wed"] is not None
        assert result["m1"]["thu"] is not None
        assert result["m1"]["fri"] is not None


class TestRollingStatsInsufficient:
    def test_a20_nan_when_only_5_rows(self):
        hl = pd.Series([100.0, 200.0, 150.0, 180.0, 120.0])
        stats = rolling_stats(hl)
        assert math.isnan(stats["a20"])  # 5 筆不夠 20 日窗口

    def test_a5_valid_when_5_rows(self):
        hl = pd.Series([100.0, 200.0, 150.0, 180.0, 120.0])
        stats = rolling_stats(hl)
        assert not math.isnan(stats["a5"])
        assert stats["a5"] == pytest.approx(150.0, abs=0.01)
