import os
import pytz

TZ = pytz.timezone("Asia/Taipei")

# 美股指數（yfinance symbols），用於計算預估開盤
US_SYMBOLS = {
    "dj": "^DJI",
    "nq": "^IXIC",
    "spy": "SPY",
    "tsm": "TSM",
}

# 台指期近月合約（富邦 API symbol）
TXF_SYMBOL = "TXFR1"

# 取回幾天的歷史（台指期 weekday avg 需要 6 個月 ≈ 180 天）
LOOKBACK_DAYS = 200

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
