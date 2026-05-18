import datetime
import logging
import os
import pathlib

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from src import config, db, fetch, compute, render, freshness

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="taifx-volatility")

_HTML_PATH = pathlib.Path(__file__).parent.parent / "data" / "index.html"
_SNAPSHOT: dict = {}


async def _refresh(dry_run: bool = False) -> dict:
    asof = datetime.date.today()
    log.info("refresh start asof=%s dry_run=%s", asof, dry_run)

    data = fetch.fetch_all(lookback_days=config.LOOKBACK_DAYS)

    use_db = bool(config.DATABASE_URL) and not dry_run
    if use_db:
        # 美股四指數
        for key, sym in config.US_SYMBOLS.items():
            db.upsert_indices(sym, data[key])
        # 台指期
        db.upsert_indices(config.TXF_SYMBOL, data["txf"])
        txf_df = db.read_indices(config.TXF_SYMBOL, config.LOOKBACK_DAYS)
    else:
        txf_df = data["txf"]

    from src.compute import (
        simple_range, true_range, rolling_stats, atr_rolling_stats,
        sigma_bands, weekday_avg, daytrade_targets, calc_r,
    )
    hl = simple_range(txf_df)           # 主要指標：H-L 簡單振幅（台指期）
    tr = true_range(txf_df)             # 補充指標：ATR True Range
    stats = rolling_stats(hl)           # a20/a10/a5/s20/s10/s5（基於 H-L）
    atr = atr_rolling_stats(tr)         # atr20/atr10/atr5（SMA + Wilder）
    sig1, sig2, sig3 = sigma_bands(stats["a10"], stats["s10"])
    wd = weekday_avg(hl, config.WEEKDAY_MONTHS)
    targets = daytrade_targets(hl)
    r_val = calc_r(stats["a10"])

    us_pcts = {k: fetch.latest_pct(data[k]) for k in ("dj", "nq", "spy", "tsm")}
    prev_close = float(txf_df["close"].iloc[-1])

    stale = freshness.is_stale(txf_df, asof)
    generated_at = datetime.datetime.now(config.TZ).strftime("%Y-%m-%d %H:%M %Z")

    snapshot = {
        "asof_iso": asof.isoformat(),
        "us": us_pcts,
        "prev_close": int(prev_close),
        "stats": {k: round(v, 2) for k, v in stats.items()},
        "atr": {k: round(v, 2) for k, v in atr.items()},
        "sig1": round(sig1, 2),
        "sig2": round(sig2, 2),
        "sig3": round(sig3, 2),
        "weekday": wd,
        "weekday_months": config.WEEKDAY_MONTHS,
        "targets": targets,
        "r": r_val,
        "is_stale": stale,
        "generated_at": generated_at,
    }

    if not dry_run:
        render.render(snapshot)
    global _SNAPSHOT
    _SNAPSHOT = snapshot
    log.info("refresh done is_stale=%s", stale)
    return snapshot


async def _startup_refresh():
    try:
        await _refresh()
    except Exception as e:
        log.warning("startup refresh failed: %s", e)


@app.on_event("startup")
async def startup():
    if config.DATABASE_URL:
        try:
            db.init_tvol_tables()
        except Exception as e:
            log.warning("DB init failed (continuing without DB): %s", e)
    import asyncio
    asyncio.create_task(_startup_refresh())


@app.get("/")
async def root():
    if _HTML_PATH.exists():
        return FileResponse(str(_HTML_PATH), media_type="text/html")
    raise HTTPException(503, "HTML not yet generated, try POST /refresh first")


@app.get("/api/snapshot")
async def api_snapshot():
    if not _SNAPSHOT:
        raise HTTPException(503, "snapshot not ready")
    return JSONResponse(_SNAPSHOT)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/refresh")
async def refresh_endpoint(token: str, dry: bool = False):
    if token != config.REFRESH_TOKEN:
        raise HTTPException(403, "invalid token")
    snapshot = await _refresh(dry_run=dry)
    return {
        "ok": True,
        "generated_at": snapshot["generated_at"],
        "is_stale": snapshot["is_stale"],
    }
