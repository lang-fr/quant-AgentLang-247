import os
from datetime import datetime, timezone, timedelta
import requests
import pandas as pd
import yfinance as yf

FRED_API_KEY = os.getenv("FRED_API_KEY", "").strip()
CMC_API_KEY = os.getenv("CMC_API_KEY", "").strip()
FCS_API_KEY = os.getenv("FCS_API_KEY", "").strip()

WIB = timezone(timedelta(hours=7))


# ==============================================================================
# 1. MAKRO & INTERMARKET (DXY, US10Y, S&P500, NASDAQ, VIX, SILVER, USOIL, FRED)
# ==============================================================================
def ambil_data_makro() -> dict:
    """Menarik DXY, US10Y, S&P500, NASDAQ, VIX, Silver, dan USOIL (WTI) secara real-time."""
    tickers = {
        "DXY": "DX-Y.NYB",
        "US10Y": "^TNX",
        "SP500": "^GSPC",
        "NASDAQ": "^IXIC",
        "VIX": "^VIX",
        "SILVER": "SI=F",
        "USOIL": "CL=F",
    }
    hasil = {}
    for nama, kode in tickers.items():
        try:
            df = yf.download(kode, period="5d", interval="1d", progress=False)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df.dropna(subset=["Close"])
            if len(df) >= 2:
                prev_c = float(df["Close"].iloc[-2])
                last_c = float(df["Close"].iloc[-1])
                chg_pct = ((last_c - prev_c) / prev_c) * 100.0
                hasil[nama] = {"last": round(last_c, 3), "chg_pct": round(chg_pct, 2)}
            elif len(df) == 1:
                hasil[nama] = {"last": round(float(df["Close"].iloc[-1]), 3), "chg_pct": 0.0}
            else:
                hasil[nama] = {"last": 0.0, "chg_pct": 0.0}
        except Exception:
            hasil[nama] = {"last": 0.0, "chg_pct": 0.0}

    # Spread Yield Obligasi AS 10Y - 2Y (T10Y2Y) dari FRED API
    if FRED_API_KEY:
        try:
            url = (
                f"https://api.stlouisfed.org/fred/series/observations"
                f"?series_id=T10Y2Y&api_key={FRED_API_KEY}&file_type=json&sort_order=desc&limit=1"
            )
            r = requests.get(url, timeout=8).json()
            obs = r.get("observations", [])
            if obs and obs[0].get("value") != ".":
                hasil["FRED_T10Y2Y"] = float(obs[0]["value"])
        except Exception:
            pass

    return hasil


# ==============================================================================
# 2. FILTER KALENDER EKONOMI (RED FOLDER NEWS BLACKOUT +/- 60 MENIT)
# ==============================================================================
def cek_kalender_ekonomi_high_impact(window_menit: int = 60) -> dict:
    """
    Mendeteksi jadwal berita ekonomi High-Impact (NFP, CPI, FOMC, PCE, Rate Decision)
    dalam jendela +/- 60 menit untuk mencegah entry saat volatilitas liar.
    """
    now_utc = datetime.now(timezone.utc)
    event_aktif = []

    # Jalur 1: FCS API Economic Calendar (jika FCS_API_KEY diisi)
    if FCS_API_KEY:
        try:
            tgl = now_utc.strftime("%Y-%m-%d")
            url = f"https://fcsapi.com/api-v3/forex/economy_cal?symbol=USD&from={tgl}&to={tgl}&access_key={FCS_API_KEY}"
            resp = requests.get(url, timeout=8).json()
            for item in resp.get("response", []):
                imp = str(item.get("importance", "")).lower()
                if imp in ("high", "3"):
                    dt_str = item.get("date", "")
                    try:
                        ev_dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                        selisih_menit = abs((ev_dt - now_utc).total_seconds()) / 60.0
                        if selisih_menit <= window_menit:
                            event_aktif.append(f"{item.get('title')} ({int(selisih_menit)}m)")
                    except Exception:
                        continue
        except Exception:
            pass

    # Jalur 2: Public ForexFactory JSON Mirror (Gratis tanpa API key)
    if not event_aktif:
        try:
            ff = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=8).json()
            if isinstance(ff, list):
                for ev in ff:
                    if str(ev.get("country", "")).upper() == "USD" and str(ev.get("impact", "")).lower() == "high":
                        date_iso = ev.get("date", "")
                        try:
                            ev_dt = datetime.fromisoformat(date_iso).astimezone(timezone.utc)
                            selisih_menit = abs((ev_dt - now_utc).total_seconds()) / 60.0
                            if selisih_menit <= window_menit:
                                event_aktif.append(f"{ev.get('title')} ({int(selisih_menit)}m)")
                        except Exception:
                            continue
        except Exception:
            pass

    return {
        "news_blackout": len(event_aktif) > 0,
        "high_impact_events": event_aktif,
        "window_minutes": window_menit,
    }


# ==============================================================================
# 3. ORDERFLOW DERIVATIF PUBLIK BINANCE (GRATIS TANPA API KEY)
# ==============================================================================
def ambil_orderflow_binance_publik(symbol: str = "BTCUSDT") -> dict:
    """
    Menarik Open Interest, Top Trader Long/Short Ratio, dan Taker Buy/Sell Volume
    langsung dari endpoint publik Binance Futures.
    """
    base = "https://fapi.binance.com/futures/data"
    hasil = {
        "symbol": symbol,
        "oi_change_pct": 0.0,
        "top_trader_ls_ratio": 1.0,
        "taker_buy_sell_ratio": 1.0,
        "orderflow_bias": "NEUTRAL",
    }
    try:
        r_oi = requests.get(f"{base}/openInterestHist?symbol={symbol}&period=1h&limit=2", timeout=8).json()
        if isinstance(r_oi, list) and len(r_oi) >= 2:
            oi_prev = float(r_oi[-2]["sumOpenInterestValue"])
            oi_curr = float(r_oi[-1]["sumOpenInterestValue"])
            if oi_prev > 0:
                hasil["oi_change_pct"] = round(((oi_curr - oi_prev) / oi_prev) * 100.0, 2)
    except Exception:
        pass

    try:
        r_ls = requests.get(f"{base}/topLongShortPositionRatio?symbol={symbol}&period=1h&limit=1", timeout=8).json()
        if isinstance(r_ls, list) and len(r_ls) >= 1:
            hasil["top_trader_ls_ratio"] = round(float(r_ls[-1]["longShortRatio"]), 3)
    except Exception:
        pass

    try:
        r_tk = requests.get(f"{base}/takerlongshortRatio?symbol={symbol}&period=1h&limit=1", timeout=8).json()
        if isinstance(r_tk, list) and len(r_tk) >= 1:
            hasil["taker_buy_sell_ratio"] = round(float(r_tk[-1]["buySellRatio"]), 3)
    except Exception:
        pass

    # Klasifikasi tekanan Orderflow
    taker = hasil["taker_buy_sell_ratio"]
    oi_chg = hasil["oi_change_pct"]
    if taker >= 1.12 and oi_chg > 0:
        hasil["orderflow_bias"] = "STRONG_BUY_FLOW"
    elif taker <= 0.88 and oi_chg > 0:
        hasil["orderflow_bias"] = "STRONG_SELL_FLOW"
    elif taker >= 1.05:
        hasil["orderflow_bias"] = "BUY_LEAN"
    elif taker <= 0.95:
        hasil["orderflow_bias"] = "SELL_LEAN"

    return hasil


# ==============================================================================
# 4. LAPORAN POSISI INSTITUSI CFTC COT REPORT (GRATIS TANPA API KEY)
# ==============================================================================
def ambil_laporan_cot_cftc() -> dict:
    """
    Menarik data CFTC Commitments of Traders (Disaggregated Futures) untuk Emas COMEX (088691)
    guna membaca arah posisi bersih Hedge Fund / Managed Money mingguan.
    """
    cot = {"gold_managed_money_net": 0, "gold_cot_bias": "NEUTRAL", "report_date": "N/A"}
    try:
        url = (
            "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"
            "?cftc_contract_market_code=088691&$order=report_date_as_yyyy_mm_dd DESC&$limit=2"
        )
        rows = requests.get(url, timeout=10).json()
        if isinstance(rows, list) and len(rows) >= 1:
            latest = rows[0]
            mm_long = float(latest.get("m_money_positions_long_all", 0))
            mm_short = float(latest.get("m_money_positions_short_all", 0))
            net_curr = int(mm_long - mm_short)
            cot["gold_managed_money_net"] = net_curr
            cot["report_date"] = str(latest.get("report_date_as_yyyy_mm_dd", ""))[:10]

            if len(rows) >= 2:
                prev = rows[1]
                net_prev = int(float(prev.get("m_money_positions_long_all", 0)) - float(prev.get("m_money_positions_short_all", 0)))
                if net_curr > 0 and net_curr >= net_prev:
                    cot["gold_cot_bias"] = "BULLISH_ACCUMULATION"
                elif net_curr > 0 and net_curr < net_prev:
                    cot["gold_cot_bias"] = "BULLISH_TRIMMING"
                else:
                    cot["gold_cot_bias"] = "BEARISH_DISTRIBUTION"
    except Exception:
        pass
    return cot


# ==============================================================================
# 5. SENTIMEN, LIKUIDITAS KRIPTO LUAS (BTC.D, USDT.D, TOTAL3) & HYPERLIQUID
# ==============================================================================
def ambil_likuiditas_kripto_global() -> dict:
    """Mengambil BTC Dominance, USDT Dominance, dan estimasi TOTAL3 (Altcoin Cap)."""
    liq = {"btc_dominance": 0.0, "usdt_dominance": 0.0, "total3_usd_b": 0.0}
    try:
        cg = requests.get("https://api.coingecko.com/api/v3/global", timeout=8).json().get("data", {})
        mcap_pct = cg.get("market_cap_percentage", {})
        total_mcap = float(cg.get("total_market_cap", {}).get("usd", 0.0))
        btc_d = float(mcap_pct.get("btc", 0.0))
        eth_d = float(mcap_pct.get("eth", 0.0))
        usdt_d = float(mcap_pct.get("usdt", 0.0))

        liq["btc_dominance"] = round(btc_d, 2)
        liq["usdt_dominance"] = round(usdt_d, 2)
        if total_mcap > 0:
            total3 = total_mcap * max(0.0, (100.0 - btc_d - eth_d)) / 100.0
            liq["total3_usd_b"] = round(total3 / 1e9, 2)
    except Exception:
        pass
    return liq


def ambil_metrik_derivatif_dan_sentimen() -> dict:
    """Menggabungkan Fear & Greed, Funding/OI Hyperliquid xyz:GOLD, Binance Orderflow, COT, dan Kripto Global."""
    info = {
        "fear_greed_value": 50,
        "fear_greed_label": "Neutral",
        "xyz_gold_funding": 0.0,
        "xyz_gold_oi": 0.0,
    }
    try:
        fg = requests.get("https://api.alternative.me/fng/?limit=1", timeout=8).json()
        data_fg = fg.get("data", [{}])[0]
        info["fear_greed_value"] = int(data_fg.get("value", 50))
        info["fear_greed_label"] = data_fg.get("value_classification", "Neutral")
    except Exception:
        pass

    try:
        hl = requests.post(
            "https://api.hyperliquid.xyz/info",
            json={"type": "metaAndAssetCtxs", "dex": "xyz"},
            timeout=8,
        ).json()
        if isinstance(hl, list) and len(hl) >= 2:
            universe = hl[0].get("universe", [])
            ctxs = hl[1]
            for idx, meta in enumerate(universe):
                if meta.get("name") == "xyz:GOLD" and idx < len(ctxs):
                    info["xyz_gold_funding"] = round(float(ctxs[idx].get("funding", 0.0)) * 100.0, 5)
                    info["xyz_gold_oi"] = round(float(ctxs[idx].get("openInterest", 0.0)), 2)
                    break
    except Exception:
        pass

    info["btc_orderflow"] = ambil_orderflow_binance_publik("BTCUSDT")
    info["cftc_cot"] = ambil_laporan_cot_cftc()
    info["crypto_liquidity"] = ambil_likuiditas_kripto_global()
    return info
