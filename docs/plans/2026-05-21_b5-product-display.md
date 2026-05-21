# taifx-volatility 商品顯示與資料揭露計畫（B5）

> 本計畫聚焦使用者本次提出的「商品顯示與資料揭露」需求（B5），並把 v19 全面升級計畫剩餘的待辦項目整理在最後。

---

## Context

使用者瀏覽 https://taifx-volatility.zeabur.app 後反映：「光看前日台指期收盤這個數字就是異常的」。經程式碼探索證實：

- `src/fetch.py:_fetch_txf_fubon`（**主資料源，富邦 API**）**完全沒有 session 過濾**，只傳 `id/from_date/to_date`（fetch.py:90-94），volume 還硬寫 0（fetch.py:101）。若富邦 SDK 預設回「全時段合併 OHLC」，high/low/close 都會被夜盤拉寬／污染。
- FinMind 備援路徑明確過濾 `trading_session == "position"`（fetch.py:147）→ 純日盤，安全。
- **這就是台指收盤異常最可能的根因**。

同時，前端在資料揭露上嚴重不足：
- 「前日台指收盤」只有單一 close input、**無日期、無 OHL**（templates/index.html.j2:273-274）。
- 美股表頭只有「指數 / 漲幅(%) / 權重(%)」，**沒有 close 數值、沒有 NYSE 收盤日**（templates/index.html.j2:247）。
- snapshot 內 `us` 只有 pct，**沒有原始 close**（main.py:445）。
- 「前日」具體是哪一天、美股收盤日對應台北哪一天的凌晨，使用者無從判斷。

**本次目標**：（1）修復富邦 fetch 的日盤過濾以解決異常根因；（2）後端 schema 加 OHLC 與個別 close；（3）前端清楚揭露所有商品的收盤價、收盤日、與台北時間對應；（4）統一商品命名規範（大臺近月 + 商品中英文）。

---

## B5：本次實作範圍

### B5.1 富邦 fetch 日盤過濾（後端，HF）

**檔案**：`src/fetch.py:_fetch_txf_fubon`（69-103）、`src/config.py`、`src/verify_sources.py`（新）

**核心策略**（R1 修訂）：**預設 FinMind 為安全路徑**，富邦只在「日盤 session 參數已驗證」或「sanity check 通過」時才使用；任何不確定狀態都自動 fallback FinMind，不依賴人工設 env。

**步驟**：

1. **寫獨立 verify 腳本** `src/verify_sources.py`（R1 #3 + R2 #2/#3/#6 強化）：
   - **直接呼叫** `fetch._fetch_txf_fubon()` 與 `fetch._fetch_txf_finmind()`（**不**經過 `fetch_txf()` 或 `FORCE_FINMIND_TXF` 旗標，避免假陰性）
   - **Timezone 正規化**（R2 #6）：兩來源 index 不論 UTC/naive 一律先轉成 `Asia/Taipei` date
   - **排除 partial bar**：過濾 `tpe_date >= today_tpe`（即「今日台北日界線之後尚未收盤的 bar」）
   - 用兩來源 TPE date **inner join** 比對近 30 日
   - **失敗閾值（R2 #2 close 明列）**：任一交易日 `|fubon.close - finmind.close| / finmind.close > 0.5%` **OR** `|fubon.high - finmind.high| / finmind.high > 0.5%` **OR** `|fubon.low - finmind.low| / finmind.low > 0.5%` → 視為污染
   - **額外 OHLC 關係檢查**：`high >= max(open, close)` / `low <= min(open, close)` 違反 → 列為高優先污染
   - **non-zero exit code**：污染確認 → exit 1（讓 CI / pre-deploy gate 可攔截）；輸出 CSV 列出每日 diff（debug 用）
   - **CLI 介面**：
     - `uv run python -m src.verify_sources --days 30`（兩來源 diff，B5.1 用）
     - `uv run python -m src.verify_sources --db-check --days 30`（R2 #3：DB vs FinMind diff，B5.4 用；讀 `DATABASE_URL`，SQL `SELECT trade_date, open_px, high_px, low_px, close_px FROM tvol_indices_daily WHERE symbol='TXFR1' ORDER BY trade_date DESC LIMIT N`，再與 FinMind inner join）
   - **缺資料防呆（R4 #2）**：
     - `min_joined_rows` 預設 20（30 日窗口內預期至少 20 交易日）— 不足 → exit 2（資料不完整，非污染）
     - 輸出 `left_only_dates`（FinMind 有富邦/DB 沒）與 `right_only_dates`（反之）
     - **latest date 一致性**：富邦/DB 最新日 vs FinMind 最新日差 > 1 trading day → exit 1（資料落後也視為失敗）
     - `--db-check` 模式下，若 DB 缺最新交易日，視為「資料未更新」exit 1，不可掩蓋
   - **OHLC 完整性比對（R4 #4）**：`--db-check` 與 `--days 30` 都要比對 **open**（不只 close/high/low）+ 檢查 OHLC 關係 `high >= max(o,c) and low <= min(o,c)`

2. **保險閘 `FORCE_FINMIND_TXF`**（R1 #1 修訂使用語意 + R3 #4 旗標解析）：
   - `src/config.py` 加：
     ```python
     def _truthy(s: str) -> bool:
         return s.lower() in {"1", "true", "yes", "on"}
     FORCE_FINMIND_TXF: bool = _truthy(os.getenv("FORCE_FINMIND_TXF", ""))
     FUBON_TXF_VERIFIED: bool = _truthy(os.getenv("FUBON_TXF_VERIFIED", ""))  # 預設 False
     ```
   - **不要用** `bool(os.getenv(...))` — 對 `"0"` / `"false"` 字串會回 True（Python 陷阱）
   - `fetch.fetch_txf()` 開頭檢查此旗標，True → 直接 FinMind
   - **重要操作步驟**（plan 內必須明文寫出）：
     1. Zeabur 設 env `FORCE_FINMIND_TXF=1`
     2. **必須觸發 service restart**（`npx zeabur@latest service restart`），env 才會被 process 讀取
     3. 等啟動 log 出現 `startup refresh succeeded`
     4. 驗證 `curl /api/snapshot | jq .txf_ohlc.close` 與 `curl / | grep txfClose` 兩處都已更新為 FinMind 日盤值
     5. 若 HTML 仍是舊值 → 手動 `POST /refresh` token 強制重生 `data/index.html`

3. **Auto-fallback 機制**（R1 #2 + R2 #1 + R4 #3 重設 detection 策略）：
   - **預設安全模式**：`fetch_txf()` 預設走 FinMind；富邦僅在 `FUBON_TXF_VERIFIED=True`（B5.1 步驟 4 完成才設）時才會被使用
   - 富邦路徑被使用時，fetch 完成後跑 **inline sanity check**（**外部 baseline，不用富邦自己歷史**）：
     - **跨來源比對最新 5 日**：富邦最新 5 日 close/high/low vs FinMind 同期；任一日差 > 0.5% → 視為污染
     - **理由（R4 #3）**：若富邦近 60 日全被夜盤污染，內部 H-L 中位數本身已被拉高，detection 失效。必須用外部可信 baseline（FinMind 純日盤）做即時比對
     - **volume 規則**：因現況 `_fetch_txf_fubon` 硬寫 `volume=0`（fetch.py:101），volume=0 不能單獨作為污染判定。**B5.1 步驟 4 完成時必須一併修掉 volume 硬寫**；屆時規則改為「volume < 60 日中位數 × 0.1」附加判定
   - 污染判定 → **自動 fallback FinMind**（log error 並回 FinMind 結果），不阻擋 publish 但確保資料正確
   - **效能取捨**：每次富邦 fetch 都觸發 FinMind 5 日 query 會增加延遲；可用 5 分鐘 in-process cache 攤平

4. **研究富邦 SDK 真正解法**（本批次必做，R2 #4 統一立場）：
   - 查 `fubon_neo.sdk` 的 `marketdata.rest_client.future.history.ohlc` 是否支援 session 參數（如 `session="day"` / `data_type="day"`）
   - **同時修 volume 硬寫**：從富邦回傳結構取真實 volume，不再寫死 0
   - 若 session 參數可用 → 修 `_fetch_txf_fubon` 加參數，跑 `verify_sources.py` 驗證通過 → 在 `config.py` 把 `FUBON_TXF_VERIFIED` 預設改為 True
   - 若 session 參數不可用 → 永久走 FinMind；`fetch_txf()` 富邦路徑可保留為 dev-only debug，但生產不啟用
   - **本批次最低可接受狀態**：預設 FinMind + `verify_sources.py` 可證明富邦不可信 + `FUBON_TXF_VERIFIED=False` 為預設（即使富邦研究卡關，止血仍然完成）

**驗收標準**（R3 #2 拆分）：
- **A1**：`verify_sources.py --days 30` 跑完輸出 diff 報告；若富邦確實污染 → exit 1（**這是正確結果**，證實根因，不是失敗）；diff CSV 內可看到逐日 close/high/low 差異
- **A2**：`verify_sources.py --db-check --days 30` 必須 exit 0（DB 內現存 TXFR1 與 FinMind 一致）；若 exit 1 → 跑 B5.4 修復步驟
- **A3**：線上 `/api/snapshot` 與首頁 HTML 顯示 TXF close 一致
- **A4**（R4 #3 修正）：手動測試 — **設定 `FUBON_TXF_VERIFIED=1`** 強制走富邦路徑 + 用污染 fixture / monkey-patch `_fetch_txf_fubon` 回傳被夜盤污染的資料，驗證 inline sanity check（跨來源比對 FinMind 最新 5 日差 > 0.5%）能自動 fallback FinMind 並 log error。不能光依賴「移除 FORCE_FINMIND_TXF」，因為 `FUBON_TXF_VERIFIED=False` 預設就不會走富邦
- **A5**（B5.1 步驟 4 完成時）：`verify_sources.py --days 30` exit 0（富邦 session 參數修好，與 FinMind 一致），再把 `FUBON_TXF_VERIFIED=True` 推上線

### B5.2 後端 snapshot schema 擴充 + OHLC 完整性驗證

**檔案**：`src/main.py:build_snapshot_from_data`（440-491 附近）、`src/main.py:_validate_live_raw`、snapshot validator

**B5.2.a 新增 snapshot 欄位**（additive，舊欄位全保留）：

```python
# 台指期完整 OHLC + 商品標籤
snapshot["txf_ohlc"] = {
    "date": effective.isoformat(),
    "open":  float(txf["open"].iloc[-1]),
    "high":  float(txf["high"].iloc[-1]),
    "low":   float(txf["low"].iloc[-1]),
    "close": float(txf["close"].iloc[-1]),
}
snapshot["txf_symbol_label"] = "大臺近月（TXFR1）日盤"

# 美股個別 close + 日期 + pct + NYSE close 真實台北時間（R1 #4）
snapshot["us_data"] = {
    k: {
        "date":  data[k].index[-1].date().isoformat(),
        "close": float(data[k]["close"].iloc[-1]),
        "pct":   fetch.latest_pct(data[k]),
    }
    for k in config.US_SYMBOLS
}

# NYSE session close 對應台北實際時間（DST + 半日交易日自動處理）
us_session_close_tpe = compute_us_session_close_tpe(us_session_date)  # 新 helper
snapshot["us_session_close_tpe"] = us_session_close_tpe.isoformat() if us_session_close_tpe else None
# 既有 snapshot["us"] / snapshot["prev_close"] 全保留
```

**B5.2.b OHLC 完整性驗證**（R1 #5 + R4 #4：shared validator，live + DB 路徑都跑）：

抽出 shared helper `validate_txf_ohlc_frame(df)` 於 `src/compute.py` 或 `src/main.py`：
- `open / high / low / close` 必須全部 `finite`（非 NaN/inf）
- `open > 0 and high > 0 and low > 0 and close > 0`
- `high >= max(open, close)` 且 `high >= low`
- `low <= min(open, close)`
- 任一條違反 → raise `LivePublishValidationError(kind="ohlc_integrity")` 或對應 DB 路徑例外，**不寫 DB、不更新 cache、不回傳 snapshot**

**呼叫點**（R4 #4，兩條路徑都要跑）：
- **Live publish 路徑**：`_validate_live_raw` 內加 `validate_txf_ohlc_frame(txf)`
- **DB 路徑**：`read_db_data` 取出 TXF DataFrame 後立即跑 `validate_txf_ohlc_frame()`，違反 → 視為 DB 資料污染，拋例外讓 `/api/snapshot?asof=` 回 422，不可悄悄 publish

snapshot output validator 同步檢查 `txf_ohlc.{o,h,l,c}` 與 `us_data.<k>.close` 全為 finite 正數 + OHLC 關係（h>=max(o,c), l<=min(o,c)）。

**B5.2.c NYSE close 台北時間 helper**（R1 #4，新增至 `src/freshness.py` 或 `src/compute.py`）：

```python
def compute_us_session_close_tpe(session_date: datetime.date) -> datetime.datetime | None:
    """回傳 NYSE 該 session 的收盤對應台北時間（DST + 半日交易日自動處理）。
    
    XNYS.session_close() 回傳 UTC-aware Timestamp，astimezone(config.TZ) 轉台北。
    Black Friday / Day-after-Thanksgiving 半日交易（13:00 ET = 02:00/03:00 TPE）。
    """
    try:
        xnys = _get_calendar("XNYS")
        close_utc = xnys.session_close(pd.Timestamp(session_date))
        return close_utc.astimezone(config.TZ).to_pydatetime()
    except Exception:
        return None
```

測試覆蓋：DST 切換前後（每年 3 月 / 11 月）+ Black Friday 半日交易 + 一般日。

### B5.3 前端商品顯示與資料揭露（templates/index.html.j2）

**A. 「前日台指期收盤」卡片重設計**（替換現有 line 273-274 單一 input）

```html
<div class="card">
  <div class="card-title">
    <span class="ct-icon">📊</span>前日台指期收盤
    <span class="meta-badge" id="txf-meta">
      {{ txf_symbol_label }} · {{ txf_ohlc.date }}
    </span>
  </div>
  <div class="ohlc-row">
    <div class="ohlc-box"><div class="ol">開</div><div class="ov" id="txfOpen" data-readonly>{{ txf_ohlc.open }}</div></div>
    <div class="ohlc-box"><div class="ol">高</div><div class="ov" id="txfHigh" data-readonly>{{ txf_ohlc.high }}</div></div>
    <div class="ohlc-box"><div class="ol">低</div><div class="ov" id="txfLow"  data-readonly>{{ txf_ohlc.low }}</div></div>
    <div class="ohlc-box highlight"><div class="ol">收</div><div class="ov" id="txfClose" data-readonly>{{ txf_ohlc.close }}</div></div>
  </div>
  <!-- 保留 hidden input 維持 calculate() / prevClose 既有契約（R4 #1）-->
  <input type="hidden" id="prevClose" value="{{ txf_ohlc.close }}">
</div>
```

OHL+C 四欄全 readonly（`<div>` 顯示而非 input）。

**R4 #1 修正**：原本 `<input id="prevClose">` 是 `calculate()`（main 加權預估邏輯）的資料來源；若直接刪掉，預估會用 `prev=0` 全壞。**保留 hidden input `prevClose`** 同步 `txf_ohlc.close` 值，`patchDOM` 也要同步 `setVal('prevClose', data.txf_ohlc.close)` 並觸發 `calculate()`。

**B. 美股表格改造**（替換現有 line 240-279）

表頭：`商品 / 收盤 / 漲幅 (%) / 權重 (%)`
卡片標題加 NYSE 收盤日 meta badge（R1 #4 — 用實際 TPE 時間，**不**硬寫 04:00）：
```
📈 美股漲跌幅 → 台指開盤預估
NYSE 收盤 {{ us_session_date }}（台北 {{ us_session_close_tpe | format_tpe }}）
```

`format_tpe` 顯示「MM-DD HH:MM」，DST 期間自動顯示 04:00，冬令 05:00。半日交易日（Black Friday、Christmas Eve、Independence Day eve）由 `XNYS.session_close()` 計算 — **不要寫死小時數**（R4 #5）。實際 TPE 時間由 calendar 推導，前端只負責顯示，避免 EST/EDT 1 小時差誤導使用者。`us_session_close_tpe` 為 None 時 fallback 顯示「NYSE 收盤後」。

每列：
| 商品 | 收盤 | 漲幅(%) | 權重(%) |
|------|------|---------|---------|
| DJ（道瓊工業） | `{{ us_data.dj.close }}` readonly | `{{ us_data.dj.pct \| round(2) }}` input | `djW` input |
| NQ（那斯達克綜合） | `{{ us_data.nq.close }}` readonly | … | … |
| SPY（標普500） | … | … | … |
| TSM（台積電ADR） | … | … | … |

> **NQ 中文標註待確認**：使用者明確說「NASDAQ 部分晚一點處理」，本批次保持顯示 `^IXIC`（那斯達克綜合）對應的中文「那斯達克綜合」，並在計畫附錄 todo F 記錄需確認是否改 `^NDX`（那斯達克100）。

**C. patchDOM 同步擴充**（line 530-561 附近）

新增節點更新：
- `setText('txf-meta', `${data.txf_symbol_label} · ${data.txf_ohlc.date}`)`
- `setText('txfOpen' / 'txfHigh' / 'txfLow' / 'txfClose', data.txf_ohlc.*)`（用 `roundHalfUp` 或 `.toLocaleString()`）
- **`setVal('prevClose', data.txf_ohlc.close)`**（R4 #1：hidden input 同步，確保 `calculate()` 仍能讀到正確值）
- **call `calculate()`** 於 patchDOM 末尾觸發加權預估重算
- `setText('us-meta', `NYSE 收盤 ${data.us_session_date}（台北 ${formatTpe(data.us_session_close_tpe)}）`)` — `formatTpe` 解析 ISO datetime → MM-DD HH:MM；null → 「收盤後」
- US close 四欄 readonly 顯示 `data.us_data.<k>.close`（`toLocaleString()` 加千分位）
- US 漲幅 input 統一 `(+pct).toFixed(2)` 顯示到小數第二位

**D. 漲幅顯示精度修正**

`patchDOM` 內：
```js
setVal('djC',  (+data.us_data.dj.pct).toFixed(2));
setVal('nqC',  (+data.us_data.nq.pct).toFixed(2));
setVal('spyC', (+data.us_data.spy.pct).toFixed(2));
setVal('tsmC', (+data.us_data.tsm.pct).toFixed(2));
```
Jinja2 初值同步用 `{{ us_data.dj.pct | round(2) }}`。

---

## 修改檔案清單

| 檔案 | 動作 | 說明 |
|------|------|------|
| `src/verify_sources.py` | **新檔** | 直接呼叫 `_fetch_txf_fubon`/`_fetch_txf_finmind`，排除 partial bar，inner join 交易日，>0.5% 差異 non-zero exit |
| `src/config.py` | 加旗標 | `FORCE_FINMIND_TXF: bool`、`FUBON_TXF_VERIFIED: bool`（B5.1 步驟 4 驗證通過才 True） |
| `src/fetch.py` | 修改 | `fetch_txf()` 預設 FinMind（除非 `FUBON_TXF_VERIFIED=True`）；走富邦時 inline sanity check（**跨來源比對 FinMind 最新 5 日**，任一日 close/high/low 差 > 0.5% → 自動 fallback FinMind）。**B5.1 步驟 4 修 volume 硬寫之後**，再啟用「volume < 60 日中位數 × 0.1」附加規則 |
| `src/main.py` | 加欄位+驗證 | `build_snapshot_from_data` 加 `txf_ohlc`、`txf_symbol_label`、`us_data`、`us_session_close_tpe`；`_validate_live_raw` 加 OHLC 完整性檢查（open finite、h>=max(o,c)、l<=min(o,c)） |
| `src/freshness.py` 或 `src/compute.py` | 加 helper | `compute_us_session_close_tpe(session_date)` 用 `XNYS.session_close()` + DST/半日交易日測試 |
| `templates/index.html.j2` | 重構 | 前日台指卡片改 OHLC 顯示；美股表頭與資料列重構；卡片標題用 `us_session_close_tpe`（**不**硬寫 04:00）；`patchDOM` 擴充；漲幅 toFixed(2) |
| `tests/test_freshness.py` 或新 test 檔 | 新測試 | `compute_us_session_close_tpe` DST 切換前後 + Black Friday 半日 + 一般日 |

**B5.4 DB 既有污染資料處理**（R1 #6 + R2 #3 強化）：

- 上線 B5.1 後，跑 `uv run python -m src.verify_sources --db-check --days 30`（B5.1 步驟 1 已實作此 mode）
- DB vs FinMind inner join，任一日 close/high/low 差 > 0.5% → 視為污染殘留
- **修復選項**（依污染範圍）：
  - 污染 ≤ 5 日：手動跑 `backfill` script 從 FinMind 重抓並 upsert 覆寫該日
  - 污染 > 5 日：`DELETE FROM tvol_indices_daily WHERE symbol='TXFR1' AND trade_date >= NOW() - INTERVAL '30 days'`，next refresh 自動 backfill
- **驗收**：B5.1 部署後 24 小時內，`--db-check` 回 exit 0（0 筆差異 > 0.5%）

---

## 驗證方式

```bash
# 1. 資料源 diff 驗證
uv run python -m src.verify_sources --days 30
# 預期輸出：每日 close/high/low 兩來源差異；若富邦 H/L 顯著大於 FinMind → 證實夜盤污染

# 2. 後端測試（本批次新加的 schema 欄位）
uv run pytest -v tests/test_main.py -k "txf_ohlc or us_data"

# 3. 本機跑 server
FORCE_FINMIND_TXF=1 uv run uvicorn src.main:app --host 0.0.0.0 --port 8080 --reload
curl http://localhost:8080/api/snapshot | jq '.txf_ohlc, .us_data, .us_session_date'

# 4. 開瀏覽器確認
explorer.exe "http://localhost:8080"
# 檢查項目：
#   - 前日台指卡片顯示「大臺近月（TXFR1）日盤 · 2026-05-XX」+ 開高低收四數字
#   - 美股表頭為「商品 / 收盤 / 漲幅 / 權重」
#   - 商品列：「DJ（道瓊工業）」「NQ（那斯達克綜合）」「SPY（標普500）」「TSM（台積電ADR）」
#   - 美股卡片標題顯示「NYSE 收盤 YYYY-MM-DD（台北 MM-DD HH:MM）」— 實際時間由 snapshot.us_session_close_tpe（XNYS.session_close 推導）決定；夏令約 04:00、冬令約 05:00、半日交易日由 calendar 推導（不寫死小時數）
#   - 漲幅顯示到小數第二位
#   - 切換日期時所有上述欄位都同步更新（patchDOM 完整）

# 5. 線上部署後驗證
# 設定 Zeabur env: FORCE_FINMIND_TXF=1（暫時切 FinMind 止血）
npx zeabur@latest deployment log --service-id 6a0ab5cacae74f9c179b52ce -t runtime -i=false | tail -30
curl https://taifx-volatility.zeabur.app/api/snapshot | jq '.txf_ohlc.close, .us_data.dj.close'
```

---

## 完整待辦清單（本次完成後仍剩餘）

| 編號 | 項目 | 狀態 | 預估批次 |
|------|------|------|----------|
| **B5** | **本次** — 商品顯示 + 資料揭露 + 富邦日盤過濾（含富邦 SDK session 參數研究） | 🟡 進行中 | 本批次 |
| C1 | Readonly + 編輯模式 toggle（頁首「✏️ 手動調整」按鈕） | ⚪ 待做 | 下批 |
| C2 | 週別月份 chip 切換（1/2/3/6 月，後端 `weekday_avg_multi` 已實作） | ⚪ 待做 | 下批 |
| D1 | `GET /api/snapshot?asof=` 端點實作（日期切換目前會 404） | ⚪ 待做 | 後端批次 |
| D2 | `read_db_data` / `build_snapshot` v19 三層分離重構 | ⚪ 待做 | 後端批次 |
| E1 | （已併入 B5.3.D）US 漲幅 input 顯示 toFixed(2) | 🟡 進行中 | 本批次 |
| F1 | NASDAQ symbol 確認：`^IXIC`（綜合）vs `^NDX`（那斯達克100）vs `NQ=F`（期貨） | ⚪ 待做 | 使用者明說晚點處理 |
| G1 | Batch 3 mockup：5 組多方/空方卡片設計 | ⚪ 待做 | 設計批次 |
| G2 | Batch 4 mockup：10 組整頁風格 | ⚪ 待做 | 設計批次 |
| H1 | 註冊 FinMind 帳號拿 token（finmindtrade.com）→ Zeabur 設 `FINMIND_TOKEN` env | ⚪ 待做 | 營運維護 |

> R2 #4：原 F2「富邦 SDK 永久解法」已併入 B5.1 步驟 4，本批次必做，不再列為獨立待辦。

> **本次優先順序**：B5（含 E1）→ C1+C2（編輯模式 + chip）→ D1+D2（後端重構）→ F1（NASDAQ symbol 確認）→ G1+G2（設計探索）

---

## 風險與停損

- **B5.1 步驟 4（富邦 SDK 研究）若卡關**：本批次最低可接受狀態 = 預設 FinMind（`FUBON_TXF_VERIFIED=False`）+ `verify_sources.py` 已證明富邦不可信。富邦 session 參數可作為條件式 hotfix 之後解除 `FUBON_TXF_VERIFIED`，**不會** block 本批次完成（R2 #4 統一立場）。
- **B5.2 schema 擴充屬 additive**：不影響舊前端，可獨立部署。但 `_validate_live_raw` 加 OHLC integrity 檢查屬 breaking — 若歷史資料含 NaN/異常，refresh 會失敗。部署前先跑 `--db-check` 確認 DB 乾淨。
- **B5.3 前端改動範圍**：限 templates/index.html.j2 + patchDOM，未動計算邏輯；最壞情況 `git revert` 即可。
- **不動到 v19 既有契約**：本批次不改 `expected_us_session_for_effective_date`、HF-1/HF-2 fallback 等架構決策（但 `_validate_live_raw` 屬可擴充契約，加新欄位檢查不算破壞）。

---

## Codex Review 軌跡

| Round | 模型/深度 | Verdict | B/Ma/Mi/S | Claude A:R | 死結升級 | Plan 版本 |
|---|---|---|---|---|---|---|
| 1 | gpt-5.5/high | NEEDS_REVISION | B:0/Ma:5/Mi:1/S:0 | A:6/R:0 | — | v2 |
| 2 | gpt-5.5/high | NEEDS_REVISION | B:0/Ma:4/Mi:2/S:0 | A:6/R:0 | — | v3 |
| 3 | gpt-5.5/high | NEEDS_REVISION | B:0/Ma:2/Mi:2/S:0 | A:4/R:0 | — | v4 |
| 4 | gpt-5.5/high | NEEDS_REVISION | B:0/Ma:4/Mi:1/S:0 | A:5/R:0 | — | v5 |
| 5 | gpt-5.5/high | **PASS** | — | — | — | v5（最終） |
