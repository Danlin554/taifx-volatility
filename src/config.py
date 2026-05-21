import os
import pytz

TZ = pytz.timezone("Asia/Taipei")


def _truthy(s: str) -> bool:
    return s.lower() in {"1", "true", "yes", "on"}

# 美股指數（yfinance symbols），用於計算預估開盤
US_SYMBOLS = {
    "dj": "^DJI",
    "nq": "^IXIC",
    "spy": "SPY",
    "tsm": "TSM",
}

# 台指期近月合約（富邦 API symbol）
TXF_SYMBOL = "TXFR1"

# 取回幾天的歷史（130 交易日 ÷ ~0.69 ≈ 189 日曆天，加長假 buffer → 220）
LOOKBACK_DAYS = 220

WEEKDAY_MONTHS = 6

# 富邦 API 憑證（設定於 Zeabur 環境變數）
FUBON_ID       = os.getenv("FUBON_ID", "")
FUBON_PWD      = os.getenv("FUBON_PWD", "")
FUBON_CERT_PATH = os.getenv("FUBON_CERT_PATH", "")
FUBON_CERT_PWD  = os.getenv("FUBON_CERT_PWD", "")

# FinMind API（期交所資料備援，不需帳號；設定 token 可解鎖更高 rate limit）
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")

REFRESH_TOKEN = os.getenv("REFRESH_TOKEN", "dev-token")
DATABASE_URL  = os.getenv("DATABASE_URL", "")

# Live publish 降級旗標（預設 fail-closed）
ALLOW_DEGRADED_TXF_FRESHNESS: bool = bool(os.getenv("ALLOW_DEGRADED_TXF_FRESHNESS", ""))
ALLOW_DEGRADED_US_SESSION: bool = bool(os.getenv("ALLOW_DEGRADED_US_SESSION", ""))

# TXF 資料源旗標（B5.1）
# FORCE_FINMIND_TXF=1 → 直接走 FinMind，跳過富邦（緊急止血用）
# FUBON_TXF_VERIFIED=1 → 富邦 SDK session 參數已驗證可用，啟用富邦路徑（B5.1 步驟 4 完成後才設）
FORCE_FINMIND_TXF: bool = _truthy(os.getenv("FORCE_FINMIND_TXF", ""))
FUBON_TXF_VERIFIED: bool = _truthy(os.getenv("FUBON_TXF_VERIFIED", ""))
