import os
from datetime import datetime, timezone, timedelta
import requests
import yfinance as yf

FRED_API_KEY = os.getenv("FRED_API_KEY", "").strip()
CMC_API_KEY = os.getenv("CMC_API_KEY", "").strip()


# ==============================================================================
# 1. DATA MAKRO INTERMARKET (YAHOO FINANCE + FRED US02Y & REAL YIELD)
# ==============================================================================
def _ambil_fred_series(series_id: str) -> dict:
    if not FRED_API_KEY:
        return {"last": 0.0, "chg_pct": 0.0, "source": "BELUM_DISET"}
    try:
        url = (
            f"https://api.stlouisfed.org/fred/series/observations"
            f"?series_id={series_id}&api_key={FRED_API_KEY}&file_type=json&sort_order=desc&limit=5"
        )
        r = requests.get(url, timeout=10).json()
        obs = [o for o in r.get("observations", []) if o.get("value") not in (".", None, "")]
        if len(obs) >= 2:
            last_v = float(obs[0]["value"])
            prev_v = float(obs[1]["value"])
            chg = ((last_v - prev_v) / abs(prev_v) * 100.0) if prev_v != 0 else 0.0
            return {"last": round(last_v, 3), "chg_pct": round(chg, 2), "source": "FRED_API"}
    except Exception:
        pass
    return {"last": 0.0, "chg_pct": 0.0, "source": "ERROR"}


def ambil_data_makro() -> dict:
    tickers = {
        "DXY": "DX-Y.NYB",
        "US10Y": "^TNX",
        "USOIL": "CL=F",
        "SILVER": "SI=F",
        "VIX": "^VIX",
        "NASDAQ": "^IXIC",
        "SP500": "^GSPC",
    }
    hasil = {}
    for nama, kode in tickers.items():
        try:
            hist = yf.Ticker(kode).history(period="5d", interval="1d")
            if len(hist) >= 2:
                last_px = float(hist["Close"].iloc[-1])
                prev_px = float(hist["Close"].iloc[-2])
                chg_pct = ((last_px - prev_px) / prev_px * 100.0) if prev_px != 0 else 0.0
                hasil[nama] = {"last": round(last_px, 3), "chg_pct": round(chg_pct, 2)}
            elif len(hist) == 1:
                hasil[nama] = {"last": round(float(hist["Close"].iloc[-1]), 3), "chg_pct": 0.0}
            else:
                hasil[nama] = {"last": 0.0, "chg_pct": 0.0}
        except Exception:
            hasil[nama] = {"last": 0.0, "chg_pct": 0.0}

    # Tambahan data resmi Federal Reserve (FRED): US02Y & 10Y Real Yield
    hasil["US02Y"] = _ambil_fred_series("DGS2")
    hasil["REAL_YIELD_10Y"] = _ambil_fred_series("DFII10")
    return hasil


# ==============================================================================
# 2. DERIVATIF BTC (OKX PUBLIC ORDERFLOW), CFTC COT & LIKUIDITAS KRIPTO
# ==============================================================================
def _ambil_btc_orderflow_okx() -> dict:
    """Menarik OI Change % dan Taker Buy/Sell Ratio BTC dari OKX Public API (bebas geoblock US)."""
    oi_chg_pct = 0.0
    taker_ratio = 1.0
    sumber = "DEFAULT"

    try:
        # 1. Open Interest & Volume 1H
        url_oi = "https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume?ccy=BTC&period=1H"
        r_oi = requests.get(url_oi, timeout=10).json()
        data_oi = r_oi.get("data", [])
        if len(data_oi) >= 5:
            oi_now = float(data_oi[0][1])
            oi_4h_ago = float(data_oi[4][1])
            if oi_4h_ago > 0:
                oi_chg_pct = round(((oi_now - oi_4h_ago) / oi_4h_ago) * 100.0, 2)
                sumber = "OKX_PUBLIC_API"
    except Exception:
        pass

    try:
        # 2. Taker Buy/Sell Volume 1H
        url_tk = "https://www.okx.com/api/v5/rubik/stat/taker-volume?ccy=BTC&instType=CONTRACTS&period=1H"
        r_tk = requests.get(url_tk, timeout=10).json()
        data_tk = r_tk.get("data", [])
        if data_tk:
            sell_vol = float(data_tk[0][1])
            buy_vol = float(data_tk[0][2])
            if sell_vol > 0:
                taker_ratio = round(buy_vol / sell_vol, 3)
                sumber = "OKX_PUBLIC_API"
    except Exception:
        pass

    if taker_ratio >= 1.08 and oi_chg_pct > 0:
        bias = "AGGRESSIVE_BUY_BUILDUP"
    elif taker_ratio <= 0.92 and oi_chg_pct > 0:
        bias = "AGGRESSIVE_SELL_BUILDUP"
    elif taker_ratio > 1.03:
        bias = "MODERATE_BUY_FLOW"
    elif taker_ratio < 0.97:
        bias = "MODERATE_SELL_FLOW"
    else:
        bias = "NEUTRAL"

    return {
        "orderflow_bias": bias,
        "oi_change_pct": oi_chg_pct,
        "taker_buy_sell_ratio": taker_ratio,
        "source": sumber,
    }


def _ambil_cftc_gold_cot() -> dict:
    try:
        url = (
            "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"
            "?$where=cftc_contract_market_code='088691'"
            "&$order=report_date_as_yyyy_mm_dd DESC&$limit=2"
        )
        rows = requests.get(url, timeout=12).json()
        if rows:
            latest = rows[0]
            long_mm = int(float(latest.get("m_money_positions_long_all", 0)))
            short_mm = int(float(latest.get("m_money_positions_short_all", 0)))
            net_now = long_mm - short_mm

            net_prev = net_now
            if len(rows) >= 2:
                prev = rows[1]
                net_prev = int(float(prev.get("m_money_positions_long_all", 0))) - int(
                    float(prev.get("m_money_positions_short_all", 0))
                )

            if net_now > 0 and net_now >= net_prev:
                bias = "BULLISH_ACCUMULATION"
            elif net_now > 0:
                bias = "BULLISH_TRIMMING"
            elif net_now < 0 and net_now <= net_prev:
                bias = "BEARISH_EXPANSION"
            else:
                bias = "BEARISH_COVERING"

            return {
                "gold_cot_bias": bias,
                "gold_managed_money_net": net_now,
                "report_date": latest.get("report_date_as_yyyy_mm_dd", ""),
            }
    except Exception:
        pass
    return {"gold_cot_bias": "NEUTRAL", "gold_managed_money_net": 0, "report_date": ""}


def _ambil_crypto_liquidity_dan_fng() -> dict:
    btc_d, usdt_d, total3_b = 0.0, 0.0, 0.0
    fng_val, fng_label = 50, "Neutral"

    try:
        g = requests.get("https://api.coingecko.com/api/v3/global", timeout=10).json().get("data", {})
        mcap_pct = g.get("market_cap_percentage", {})
        total_mcap = float(g.get("total_market_cap", {}).get("usd", 0.0))
        btc_d = round(float(mcap_pct.get("btc", 0.0)), 2)
        eth_d = float(mcap_pct.get("eth", 0.0))
        usdt_d = round(float(mcap_pct.get("usdt", 0.0)), 2)
        if total_mcap > 0:
            total3_usd = total_mcap * max(0.0, (100.0 - btc_d - eth_d) / 100.0)
            total3_b = round(total3_usd / 1e9, 2)
    except Exception:
        pass

    # Crypto Fear & Greed (CoinMarketCap API -> cadangan Alternative.me)
    fng_ok = False
    if CMC_API_KEY:
        try:
            r_cmc = requests.get(
                "https://pro-api.coinmarketcap.com/v3/fear-and-greed/latest",
                headers={"X-CMC_PRO_API_KEY": CMC_API_KEY},
                timeout=8,
            ).json()
            d_cmc = r_cmc.get("data", {})
            if "value" in d_cmc:
                fng_val = int(d_cmc["value"])
                fng_label = d_cmc.get("value_classification", "Neutral")
                fng_ok = True
        except Exception:
            pass

    if not fng_ok:
        try:
            r_alt = requests.get("https://api.alternative.me/fng/?limit=1", timeout=8).json()
            d_alt = r_alt.get("data", [{}])[0]
            if "value" in d_alt:
                fng_val = int(d_alt["value"])
                fng_label = d_alt.get("value_classification", "Neutral")
        except Exception:
            pass

    return {
        "btc_dominance": btc_d,
        "usdt_dominance": usdt_d,
        "total3_usd_b": total3_b,
        "fear_greed_index": fng_val,
        "fear_greed_label": fng_label,
    }


def ambil_metrik_derivatif_dan_sentimen() -> dict:
    return {
        "btc_orderflow": _ambil_btc_orderflow_okx(),
        "cftc_cot": _ambil_cftc_gold_cot(),
        "crypto_liquidity": _ambil_crypto_liquidity_dan_fng(),
    }


# ==============================================================================
# 3. KALENDER EKONOMI HIGH IMPACT (FOREXFACTORY JSON FEED)
# ==============================================================================
def cek_kalender_ekonomi_high_impact(window_menit: int = 60) -> dict:
    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        events = requests.get(url, timeout=10).json()
        now_utc = datetime.now(timezone.utc)
        high_events = []

        for ev in events:
            if ev.get("impact") != "High":
                continue
            if ev.get("country") not in ("USD", "EUR", "GBP", "JPY"):
                continue
            dt_str = ev.get("date", "")
            if not dt_str:
                continue
            ev_dt = datetime.fromisoformat(dt_str).astimezone(timezone.utc)
            selisih_menit = abs((ev_dt - now_utc).total_seconds()) / 60.0
            if selisih_menit <= window_menit:
                high_events.append(f"{ev.get('country')} - {ev.get('title')}")

        return {
            "news_blackout": len(high_events) > 0,
            "high_impact_events": high_events,
        }
    except Exception:
        return {"news_blackout": False, "high_impact_events": []}
