import os
from datetime import datetime, timezone
import ccxt
import requests
from fredapi import Fred


def ambil_kalender_ff(mata_uang: tuple = ("USD",)) -> dict:
    """Ambil jadwal berita High-Impact minggu ini dari ForexFactory (FairEconomy JSON)."""
    url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        events = resp.json()

        now_utc = datetime.now(timezone.utc)
        high_impact = []
        zona_bahaya = False

        for ev in events:
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

            # Tampilkan berita hari ini atau yang akan datang dalam 48 jam
            if selisih_menit is None or -180 <= selisih_menit <= 2880:
                high_impact.append({
                    "waktu_utc": date_str,
                    "country": ev.get("country"),
                    "title": ev.get("title"),
                    "forecast": ev.get("forecast", "-"),
                    "previous": ev.get("previous", "-"),
                    "menit_lagi": round(selisih_menit) if selisih_menit is not None else None,
                })

        return {
            "status": "ok",
            "blokir_entry_news": zona_bahaya,
            "jumlah_event": len(high_impact),
            "events": high_impact[:8],
        }
    except Exception as e:
        return {"status": "error", "blokir_entry_news": False, "detail": str(e)}


def ambil_makro_fred() -> dict:
    """Ambil data makro US02Y, US10Y, Fed Rate, CPI YoY, dan NFP dari FRED."""
    api_key = os.getenv("FRED_API_KEY", "").strip()
    if not api_key:
        return {"status": "skip", "detail": "FRED_API_KEY belum terbaca di environment"}

    try:
        fred = Fred(api_key=api_key)

        us02y = float(fred.get_series("DGS2").dropna().iloc[-1])
        us10y = float(fred.get_series("DGS10").dropna().iloc[-1])
        fed_rate = float(fred.get_series("FEDFUNDS").dropna().iloc[-1])

        cpi_series = fred.get_series("CPIAUCSL").dropna()
        cpi_yoy = ((float(cpi_series.iloc[-1]) / float(cpi_series.iloc[-13])) - 1) * 100

        nfp_series = fred.get_series("PAYEMS").dropna()
        nfp_change_k = float(nfp_series.iloc[-1]) - float(nfp_series.iloc[-2])

        spread_10y_2y = round(us10y - us02y, 3)

        return {
            "status": "ok",
            "US02Y_pct": round(us02y, 3),
            "US10Y_pct": round(us10y, 3),
            "yield_curve_10y_2y": spread_10y_2y,
            "fed_funds_rate_pct": round(fed_rate, 2),
            "cpi_yoy_pct": round(cpi_yoy, 2),
            "nfp_mom_ribu": round(nfp_change_k, 1),
        }
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def ambil_orderflow_ccxt(symbol: str = "BTC/USDT") -> dict:
    """Ambil harga, funding rate, dan orderbook imbalance dari bursa publik yang aman di IP cloud."""
    # Hyperliquid & OKX diprioritaskan karena jarang kena blokir IP server cloud AS/Eropa
    bursa_prioritas = ["hyperliquid", "okx", "kucoin", "bybit"]
    last_err = ""

    for ex_id in bursa_prioritas:
        try:
            ex_class = getattr(ccxt, ex_id)
            exchange = ex_class({"enableRateLimit": True, "timeout": 10000})

            pair = "BTC/USDC:USDC" if ex_id == "hyperliquid" and symbol == "BTC/USDT" else symbol
            ticker = exchange.fetch_ticker(pair)

            # Hitung Orderbook Imbalance 20 level teratas
            ob = exchange.fetch_order_book(pair, limit=20)
            bid_vol = sum(b[1] for b in ob.get("bids", [])[:20])
            ask_vol = sum(a[1] for a in ob.get("asks", [])[:20])
            total_vol = bid_vol + ask_vol
            imbalance_pct = round(((bid_vol - ask_vol) / total_vol) * 100, 2) if total_vol > 0 else 0.0

            # Ambil Funding Rate perpetual
            funding_pct = None
            try:
                swap_sym = pair if ":" in pair else f"{pair}:USDT"
                fr = exchange.fetch_funding_rate(swap_sym)
                if fr and fr.get("fundingRate") is not None:
                    funding_pct = round(float(fr["fundingRate"]) * 100, 4)
            except Exception:
                pass

            tekanan = "BUY_DOMINANT" if imbalance_pct > 10 else ("SELL_DOMINANT" if imbalance_pct < -10 else "NEUTRAL")

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
