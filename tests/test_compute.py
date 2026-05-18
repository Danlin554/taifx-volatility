"""
驗證計算邏輯與試算表期望值的吻合度。
期望值來源：docs/2-台指平均震幅.xlsx（DATA 工作表）
  a20=296.85, a10=368.4, a5=409.4（簡單 H-L 的 SMA）
  s20=153.18, s10=184.29, s5=233.67（H-L 的 std ddof=1）
  sig1=552.69, sig2=736.98, sig3=921.27（a10 + N×s10）
  b1=116, b2=201.6, b3=296.85, b4=439.8, b5=734（MIN/AVERAGEIF/AVG/AVERAGEIF/MAX）
  注意：b5=734 是 MAX of HL，非 sig2=736.98
"""

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
