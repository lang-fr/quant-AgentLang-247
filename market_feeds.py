import os
import time
from datetime import datetime, timezone
import ccxt
import requests
from fredapi import Fred

_CACHE = {}

HEADERS_BROWSER = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
}


def _get_cache(key: str):
    item = _CACHE.get(key)
    if item and time.time() < item["expires_at"]:
        return item["value"]
    return None


def _set_cache(key: str, value, ttl_seconds: int):
    _CACHE[key] = {"value": value, "expires_at": time.time() + ttl_seconds}


def ambil_kalender_ff(mata_uang: tuple = ("USD",)) -> dict:
    """Ambil jadwal berita High-Impact minggu ini dari ForexFactory (FairEconomy JSON)."""
    cache_key = f"ff_{','.join(mata_uang)}"
    cached = _get_cache(cache_key)
    if cached is not None:
        return cached

    url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    try:
        resp = requests.get(url, headers=HEADERS_BROWSER, timeout=12)
        if resp.status_code != 200:
            return {
                "status": "error",
                "blokir_entry_news": False,
                "detail": f"HTTP {resp.status_code}",
            }

        events = resp.json()
        if not isinstance(events, list):
            return {
                "status": "error",
                "blokir_entry_news": False,
                "detail": "Format respons kalender bukan list",
            }

        now_utc = datetime.now(timezone.utc)
        high_impact = []
        zona_bahaya = False

        for ev in events:
            if not isinstance(ev, dict):
                continue
            if ev.get("impact") != "High":
                continue
            if mata_uang and ev.get("country") not in mata_uang:
                continue

            date_str = ev.get("date", "")
            try:
                ev_time = datetime.fromisoformat(date_str).astimezone(timezone.utc)
                selisih_menit = (ev_time - now_utc).total_seconds() / 60.0
            except Exception:
                selisih_menit = None

            if selisih_menit is not None and -45 <= selisih_menit <= 45:
                zona_bahaya = True

            if selisih_menit is None or -180 <= selisih_menit <= 2880:
                high_impact.append({
                    "waktu_utc": date_str,
                    "country": ev.get("country"),
                    "title": ev.get("title"),
                    "forecast": ev.get("forecast", "-"),
                    "previous": ev.get("previous", "-"),
                    "menit_lagi": round(selisih_menit) if selisih_menit is not None else None,
                })

        hasil = {
            "status": "ok",
            "blokir_entry_news": zona_bahaya,
            "jumlah_event": len(high_impact),
            "events": high_impact[:8],
        }
        _set_cache(cache_key, hasil, ttl_seconds=1800)
        return hasil
    except Exception as e:
        return {"status": "error", "blokir_entry_news": False, "detail": str(e)}


def ambil_makro_fred() -> dict:
    """Ambil data makro US02Y, US10Y, Fed Rate, CPI YoY, dan NFP dari FRED."""
    api_key = os.getenv("FRED_API_KEY", "").strip()
    if not api_key:
        return {"status": "skip", "detail": "FRED_API_KEY belum terbaca di environment"}

    cached = _get_cache("fred_makro_detail")
    if cached is not None:
        return cached

    try:
        fred = Fred(api_key=api_key)

        s_dgs2 = fred.get_series("DGS2").dropna()
        s_dgs10 = fred.get_series("DGS10").dropna()
        s_fed = fred.get_series("FEDFUNDS").dropna()
        s_cpi = fred.get_series("CPIAUCSL").dropna()
        s_nfp = fred.get_series("PAYEMS").dropna()

        us02y = float(s_dgs2.iloc[-1])
        us10y = float(s_dgs10.iloc[-1])
        fed_rate = float(s_fed.iloc[-1])

        cpi_yoy = (
            ((float(s_cpi.iloc[-1]) / float(s_cpi.iloc[-13])) - 1) * 100
            if len(s_cpi) >= 13
            else None
        )
        nfp_change_k = (
            float(s_nfp.iloc[-1]) - float(s_nfp.iloc[-2])
            if len(s_nfp) >= 2
            else None
        )

        hasil = {
            "status": "ok",
            "US02Y_pct": round(us02y, 3),
            "US10Y_pct": round(us10y, 3),
            "yield_curve_10y_2y": round(us10y - us02y, 3),
            "fed_funds_rate_pct": round(fed_rate, 2),
            "cpi_yoy_pct": round(cpi_yoy, 2) if cpi_yoy is not None else None,
            "nfp_mom_ribu": round(nfp_change_k, 1) if nfp_change_k is not None else None,
        }
        _set_cache("fred_makro_detail", hasil, ttl_seconds=1800)
        return hasil
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def ambil_orderflow_ccxt(symbol: str = "BTC/USDT") -> dict:
    """Ambil harga, funding rate, dan orderbook imbalance dari bursa publik yang aman di IP cloud."""
    bursa_prioritas = ["hyperliquid", "okx", "kucoin", "bybit"]
    last_err = ""

    for ex_id in bursa_prioritas:
        try:
            ex_class = getattr(ccxt, ex_id)
            cfg = {"enableRateLimit": True, "timeout": 10000}
            if ex_id in ("okx", "kucoin", "bybit"):
                cfg["options"] = {"defaultType": "swap"}
            exchange = ex_class(cfg)

            pair = "BTC/USDC:USDC" if ex_id == "hyperliquid" and symbol == "BTC/USDT" else symbol
            ticker = exchange.fetch_ticker(pair)

            ob = exchange.fetch_order_book(pair, limit=20)
            bid_vol = sum(float(b[1]) for b in ob.get("bids", [])[:20])
            ask_vol = sum(float(a[1]) for a in ob.get("asks", [])[:20])
            total_vol = bid_vol + ask_vol
            imbalance_pct = round(((bid_vol - ask_vol) / total_vol) * 100, 2) if total_vol > 0 else 0.0

            funding_pct = None
            try:
                swap_sym = pair if ":" in pair else f"{pair}:USDT"
                fr = exchange.fetch_funding_rate(swap_sym)
                if fr and fr.get("fundingRate") is not None:
                    funding_pct = round(float(fr["fundingRate"]) * 100, 4)
            except Exception:
                pass

            tekanan = (
                "BUY_DOMINANT"
                if imbalance_pct > 10
                else ("SELL_DOMINANT" if imbalance_pct < -10 else "NEUTRAL")
            )

            return {
                "status": "ok",
                "exchange": ex_id.upper(),
                "symbol": pair,
                "last_price": ticker.get("last"),
                "change_24h_pct": ticker.get("percentage"),
                "funding_rate_pct": funding_pct,
                "orderbook_imbalance_pct": imbalance_pct,
                "orderbook_bias": tekanan,
            }
        except Exception as e:
            last_err = f"{ex_id}: {e}"
            continue

    return {"status": "error", "detail": last_err}
