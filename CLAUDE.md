# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# 開發伺服器
uv run uvicorn src.main:app --host 0.0.0.0 --port 8080 --reload

# 測試
uv run pytest
uv run pytest tests/test_compute.py::TestDaytradeTargets  # 單一測試類別

# 驗證算法與試算表期望值
uv run python -m src.verify --asof 2026-05-18

# 安裝依賴
uv sync
uv sync --extra dev  # 含 pytest/httpx
```

## Architecture

### 資料流

```
fetch_all() → compute.*() → render() → data/index.html → FastAPI GET /
                ↓
            db.upsert()  ← 有 DATABASE_URL 時寫入 PostgreSQL
```

每次 `POST /refresh` 或啟動時執行 `_refresh()`：
1. `fetch.fetch_all()` — 抓美股四指數（yfinance）+ 台指期 OHLC
2. 若有 DB：寫入 `tvol_indices_daily`，再從 DB 讀台指期
3. `compute.*()` — 計算全部指標，組成 `snapshot` dict
4. `render.render(snapshot)` — Jinja2 渲染寫出 `data/index.html`
5. 快取進 `_SNAPSHOT` 供 `GET /api/snapshot` 用

### 模組職責

| 模組 | 職責 |
|------|------|
| `main.py` | FastAPI app、路由、啟動流程 |
| `config.py` | 所有環境變數與常數 |
| `fetch.py` | yfinance（美股）+ FinMind/富邦 API（台指期，富邦優先） |
| `compute.py` | 振幅計算（H-L、ATR、sigma bands、weekday avg、daytrade targets、forecast） |
| `db.py` | PostgreSQL upsert/read（psycopg2，`tvol_indices_daily` 表） |
| `render.py` | Jinja2 渲染 `templates/index.html.j2` → `data/index.html` |
| `freshness.py` | 判斷資料是否過期（對比台股交易日曆） |
| `verify.py` | 獨立驗證腳本，對比 docs/2-台指平均震幅.xlsx 期望值 |

### 台指期資料來源優先順序

1. **富邦 API**（`fubon_neo.sdk`）— 需設定 `FUBON_ID/PWD/CERT_PATH/CERT_PWD`
2. **FinMind 公開 API** — 不需憑證；設定 `FINMIND_TOKEN` 可提升 rate limit

### 主要計算指標

- **H-L 簡單振幅**（`simple_range`）— 主指標，對應試算表公式
- **ATR True Range**（`true_range`）— 含跳空的補充指標
- **rolling_stats**：a5/a10/a20（SMA）+ s5/s10/s20（std ddof=1）
- **sigma_bands**：a10 ± N×s10（1/2/3 sigma）
- **daytrade_targets**（b1~b5）：MIN / AVERAGEIF<avg / AVG / AVERAGEIF>avg / MAX
- **weekday_avg**：近 6 個月各週別平均振幅
- **forecast/build_forecast**：美股加權漲幅 → 台指期預估開盤（前端用）

### 環境變數

| 變數 | 用途 |
|------|------|
| `DATABASE_URL` | PostgreSQL 連線字串；空值則不使用 DB |
| `REFRESH_TOKEN` | `POST /refresh` 驗證 token（預設 `dev-token`） |
| `FUBON_ID/PWD/CERT_PATH/CERT_PWD` | 富邦 API 憑證 |
| `FINMIND_TOKEN` | FinMind API token（可選） |

### 部署

部署於 Zeabur，啟動指令定義在 `zbpack.json`：
```
uv run uvicorn src.main:app --host 0.0.0.0 --port 8080
```

啟動時若 DB 初始化或 refresh 失敗，會 log warning 後繼續（不 crash），refresh 在 background task 非同步執行以防 OOM。
