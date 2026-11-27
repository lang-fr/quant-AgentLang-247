import os
import html
import json
import time
import threading
import sqlite3
from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd
import yfinance as yf
import ccxt
import feedparser
import requests
import cot_reports as cot
from arch import arch_model
from backtesting import Backtest, Strategy
from dotenv import load_dotenv
from fredapi import Fred
from flask import Flask, request, jsonify
from google import genai
from google.genai import types

# ==============================================================================
# 1. KONFIGURASI & ENVIRONMENT VARIABLES (AUTO-STRIP KARAKTER TERSEMBUNYI)
# ==============================================================================
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
FRED_API_KEY = os.getenv("FRED_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
BLOCKWORKS_API_KEY = os.getenv("BLOCKWORKS_API_KEY", "").strip()
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
BINANCE_SECRET = os.getenv("BINANCE_SECRET", "").strip()
METAAPI_TOKEN = os.getenv("METAAPI_TOKEN", "").strip()
METAAPI_ACCOUNT_ID = os.getenv("METAAPI_ACCOUNT_ID", "").strip()

# Prioritas Model AI: Gemini 4 Hydrogen -> Fallback otomatis ke Gemini 3.8 Flash
GEMINI_MODEL_CHAIN = [
    "gemini-4-hydrogen",
    "gemini-3.8-flash",
    "gemini-4-argon",
]

MAX_RISK_PCT = float(os.getenv("MAX_RISK_PER_SETUP_PCT", "1.0"))
MAX_ENTRIES_SESSION = int(os.getenv("MAX_ENTRIES_PER_SESSION", "3"))
MAX_LAYERS_SETUP = int(os.getenv("MAX_LAYERS_PER_SETUP", "3"))
MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", "2.5"))
MAX_SPREAD_PIPS = float(os.getenv("MAX_ALLOWED_SPREAD_PIPS", "3.0"))
AUTO_SCAN_INTERVAL_SEC = int(os.getenv("AUTO_SCAN_INTERVAL_SEC", "14400"))

app = Flask(__name__)
DB_PATH = "quant_cloud_audit.db"
WIB = timezone(timedelta(hours=7))


def waktu_wib_str() -> str:
    return datetime.now(WIB).strftime("%d %b %Y %H:%M WIB")


def clean_yf_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df.loc[:, ~df.columns.duplicated()].copy()


def panggil_gemini_prioritas(prompt: str, json_mode: bool = False) -> tuple:
    """
    Memanggil Gemini 4 Hydrogen sebagai prioritas #1.
    Jika terjadi limit/404/gangguan, otomatis fallback ke Gemini 3.8 Flash.
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY kosong atau belum terbaca.")

    client = genai.Client(api_key=GEMINI_API_KEY)
    last_err = None

    for model_name in GEMINI_MODEL_CHAIN:
        try:
            if json_mode:
                resp = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(response_mime_type="application/json"),
                )
            else:
                resp = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                )
            if resp and resp.text:
                return resp.text, model_name
        except Exception as e:
            last_err = e
            print(f"[AI Fallback] Model {model_name} gagal ({e}), beralih ke model berikutnya...")
            continue

    raise RuntimeError(f"Semua model Gemini gagal diakses. Error terakhir: {last_err}")


# ==============================================================================
# 2. DATABASE SQLITE & PELACAK SESI / KILLZONE
# ==============================================================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp_utc TEXT,
            date_utc TEXT,
            session_name TEXT,
            asset TEXT,
            bias TEXT,
            score INTEGER,
            spread_pips REAL,
            macro_snapshot TEXT,
            user_decision TEXT,
            summary TEXT
        )
    """)
    conn.commit()
    conn.close()


def deteksi_sesi_trading() -> str:
    jam_utc = datetime.now(timezone.utc).hour
    if 0 <= jam_utc < 7:
        return "ASIA_RANGE (07:00-14:00 WIB)"
    elif 7 <= jam_utc < 10:
        return "LONDON_KILLZONE (14:00-17:00 WIB)"
    elif 10 <= jam_utc < 12:
        return "LONDON_SESSION (17:00-19:00 WIB)"
    elif 12 <= jam_utc < 16:
        return "NY_AM_KILLZONE (19:00-23:00 WIB)"
    elif 16 <= jam_utc < 21:
        return "NY_PM_SESSION (23:00-04:00 WIB)"
    return "DEAD_ZONE (04:00-07:00 WIB)"


def hitung_entry_terpakai_sesi_ini() -> int:
    init_db()
    hari_ini = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sesi_ini = deteksi_sesi_trading()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        SELECT COUNT(*) FROM audit_log
        WHERE date_utc = ? AND session_name = ? AND user_decision = 'APPROVED_SIMULATION'
        """,
        (hari_ini, sesi_ini),
    )
    jumlah = c.fetchone()[0]
    conn.close()
    return jumlah


def simpan_log(asset: str, bias: str, score: int, spread_pips: float, macro_snapshot: str, decision: str, summary: str) -> int:
    init_db()
    now_utc = datetime.now(timezone.utc)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO audit_log
        (timestamp_utc, date_utc, session_name, asset, bias, score, spread_pips, macro_snapshot, user_decision, summary)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now_utc.isoformat(),
            now_utc.strftime("%Y-%m-%d"),
            deteksi_sesi_trading(),
            asset,
            bias,
            score,
            spread_pips,
            macro_snapshot,
            decision,
            summary,
        ),
    )
    row_id = c.lastrowid
    conn.commit()
    conn.close()
    return row_id


def update_keputusan_user(log_id: int, decision: str):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE audit_log SET user_decision = ? WHERE id = ?", (decision, log_id))
    conn.commit()
    conn.close()


def ambil_ringkasan_jurnal_dan_evaluasi_mingguan() -> str:
    init_db()
    batas_7_hari = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("SELECT COUNT(*) FROM audit_log WHERE timestamp_utc >= ?", (batas_7_hari,))
    scan_7d = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM audit_log WHERE timestamp_utc >= ? AND user_decision = 'APPROVED_SIMULATION'", (batas_7_hari,))
    approved_7d = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM audit_log WHERE timestamp_utc >= ? AND user_decision = 'REJECTED_BY_USER'", (batas_7_hari,))
    rejected_7d = c.fetchone()[0]

    c.execute("SELECT AVG(score) FROM audit_log WHERE timestamp_utc >= ? AND score > 0", (batas_7_hari,))
    avg_score = c.fetchone()[0] or 0.0

    c.execute("SELECT session_name, asset, bias, score, user_decision FROM audit_log ORDER BY id DESC LIMIT 5")
    rows = c.fetchall()
    conn.close()

    sesi_aktif = deteksi_sesi_trading()
    sisa_kuota = max(0, MAX_ENTRIES_SESSION - hitung_entry_terpakai_sesi_ini())

    teks = (
        f"📊 <b>JURNAL AUDIT & EVALUASI 7 HARI TERAKHIR</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"• <b>Total Scan Sistem:</b> {scan_7d}x\n"
        f"• <b>Sinyal Disetujui:</b> {approved_7d}\n"
        f"• <b>Sinyal Ditolak:</b> {rejected_7d}\n"
        f"• <b>Rata-rata Skor Konfirmasi:</b> {round(float(avg_score), 1)}/100\n"
        f"• <b>Sesi Aktif:</b> {html.escape(sesi_aktif)} (Sisa Kuota: {sisa_kuota}/{MAX_ENTRIES_SESSION})\n\n"
        f"🗂 <b>5 Log Terakhir:</b>\n\n"
    )
    for r in rows:
        teks += f"• <b>[{html.escape(str(r[0]))}]</b> {html.escape(str(r[1]))} | {html.escape(str(r[2]))} | Skor: <b>{r[3]}</b> ({html.escape(str(r[4]))})\n\n"
    return teks


# ==============================================================================
# 3. KALENDER BERITA TIER-1 & STRUKTUR PRESISI MULTI-TIMEFRAME (M15 s.d. MONTHLY)
# ==============================================================================
def cek_kalender_berita_tier1_otomatis() -> dict:
    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        events = requests.get(url, timeout=10).json()
        now_utc = datetime.now(timezone.utc)
        active_warnings = []

        for ev in events:
            if ev.get("country") == "USD" and ev.get("impact") == "High":
                ev_date_str = ev.get("date", "")
                if ev_date_str:
                    ev_dt = datetime.fromisoformat(ev_date_str).astimezone(timezone.utc)
                    diff_min = abs((ev_dt - now_utc).total_seconds()) / 60.0
                    if diff_min <= 45:
                        active_warnings.append(f"{ev.get('title')} ({int(diff_min)}m)")

        return {"is_high_impact_active": len(active_warnings) > 0, "upcoming_events": active_warnings}
    except Exception as e:
        return {"is_high_impact_active": False, "error": str(e)}


def hitung_level_sniper_historis(ticker: str = "GC=F") -> dict:
    try:
        df_m15 = clean_yf_df(yf.download(ticker, period="5d", interval="15m", progress=False))
        df_h1 = clean_yf_df(yf.download(ticker, period="30d", interval="1h", progress=False))
        df_d1 = clean_yf_df(yf.download(ticker, period="1y", interval="1d", progress=False))
        df_mo = clean_yf_df(yf.download(ticker, period="5y", interval="1mo", progress=False))

        if df_h1.empty or df_d1.empty:
            return {"error": f"Data historis {ticker} gagal dimuat."}

        harga_live = round(float(df_m15["Close"].iloc[-1] if not df_m15.empty else df_h1["Close"].iloc[-1]), 2)

        prev_d1 = df_d1.iloc[-2]
        p_open, p_high, p_low, p_close = float(prev_d1["Open"]), float(prev_d1["High"]), float(prev_d1["Low"]), float(prev_d1["Close"])
        pivot = round(((2 * p_close) + p_high + p_low) / 4.0, 2)
        x_val = ((2 * p_high) + p_low + p_close) / 2.0 if p_close >= p_open else ((2 * p_low) + p_high + p_close) / 2.0
        ohlc_nh = round(x_val - p_low, 2)
        ohlc_nl = round(x_val - p_high, 2)

        atr_m15 = round(float((df_m15["High"] - df_m15["Low"]).tail(14).mean()), 2) if not df_m15.empty else 2.5
        atr_h1 = round(float((df_h1["High"] - df_h1["Low"]).tail(14).mean()), 2)

        fvg_zone = "Tidak ada FVG baru"
        if len(df_m15) >= 5:
            for i in range(len(df_m15) - 3, max(0, len(df_m15) - 25), -1):
                c1_high = float(df_m15["High"].iloc[i])
                c3_low = float(df_m15["Low"].iloc[i + 2])
                c1_low = float(df_m15["Low"].iloc[i])
                c3_high = float(df_m15["High"].iloc[i + 2])
                if c3_low > c1_high:
                    ce_mid = round((c1_high + c3_low) / 2.0, 2)
                    fvg_zone = f"Bullish FVG M15 [{round(c1_high, 2)} - {round(c3_low, 2)}] | CE 50%: {ce_mid}"
                    break
                elif c3_high < c1_low:
                    ce_mid = round((c3_high + c1_low) / 2.0, 2)
                    fvg_zone = f"Bearish FVG M15 [{round(c3_high, 2)} - {round(c1_low, 2)}] | CE 50%: {ce_mid}"
                    break

        h1_trend = "BULLISH" if df_h1["Close"].ewm(span=20).mean().iloc[-1] > df_h1["Close"].ewm(span=50).mean().iloc[-1] else "BEARISH"
        df_h4 = df_h1.resample("4h").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
        h4_trend = "BULLISH" if df_h4["Close"].ewm(span=20).mean().iloc[-1] > df_h4["Close"].ewm(span=50).mean().iloc[-1] else "BEARISH"
        d1_trend = "BULLISH" if df_d1["Close"].ewm(span=20).mean().iloc[-1] > df_d1["Close"].ewm(span=50).mean().iloc[-1] else "BEARISH"

        high, low, close = df_d1["High"], df_d1["Low"], df_d1["Close"]
        plus_dm = high.diff().clip(lower=0)
        minus_dm = (-low.diff()).clip(lower=0)
        tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
        atr_d1 = tr.rolling(14).mean()
        plus_di = 100 * (plus_dm.rolling(14).mean() / atr_d1)
        minus_di = 100 * (minus_dm.rolling(14).mean() / atr_d1)
        dx = (abs(plus_di - minus_di) / (plus_di + minus_di)) * 100
        adx_val = round(float(dx.rolling(14).mean().iloc[-1]), 1)

        h1_ret = np.log(df_h1["Close"] / df_h1["Close"].shift(1)).dropna()
        z_score_h1 = round(float((h1_ret.iloc[-1] - h1_ret.mean()) / h1_ret.std()), 2)

        swing_high_60d = round(float(df_d1["High"].tail(60).max()), 2)
        swing_low_60d = round(float(df_d1["Low"].tail(60).min()), 2)
        ote_bull_swing = round(swing_high_60d - 0.705 * (swing_high_60d - swing_low_60d), 2)
        ote_bear_swing = round(swing_low_60d + 0.705 * (swing_high_60d - swing_low_60d), 2)

        hold_high_5y = round(float(df_mo["High"].max()), 2) if not df_mo.empty else swing_high_60d
        hold_low_5y = round(float(df_mo["Low"].min()), 2) if not df_mo.empty else swing_low_60d
        hold_eq_5y = round((hold_high_5y + hold_low_5y) / 2.0, 2)
        hold_discount_zone = round(hold_low_5y + 0.30 * (hold_high_5y - hold_low_5y), 2)

        return {
            "ticker": ticker,
            "harga_terakhir": harga_live,
            "trend_MTF": f"H1:{h1_trend} | H4:{h4_trend} | D1:{d1_trend}",
            "ADX_14": adx_val,
            "regime": "TRENDING" if adx_val >= 20 else "CHOPPY_SIDEWAYS",
            "Z_score_H1": z_score_h1,
            "ATR_M15": atr_m15,
            "ATR_H1": atr_h1,
            "OHLC_Pivot": pivot,
            "OHLC_Resisten_NH": ohlc_nh,
            "OHLC_Support_NL": ohlc_nl,
            "FVG_M15_Terbaru": fvg_zone,
            "Swing_60D": {"High": swing_high_60d, "Low": swing_low_60d, "OTE_Buy_0705": ote_bull_swing, "OTE_Sell_0705": ote_bear_swing},
            "Hold_5Y_Monthly": {"ATH_5Y": hold_high_5y, "Low_5Y": hold_low_5y, "Equilibrium": hold_eq_5y, "Deep_Discount_Accumulation": hold_discount_zone},
        }
    except Exception as e:
        return {"error": str(e)}


# ==============================================================================
# 4. HARDCODED RISK ENGINE & CIRCUIT BREAKER
# ==============================================================================
def validasi_hardcoded_risk(
    balance: float,
    sl_distance: float,
    pip_value: float,
    layers: int = 1,
    daily_loss_pct: float = 0.0,
    spread_pips: float = 1.5,
    is_high_impact_news: bool = False,
    z_score_h1: float = 0.0,
) -> dict:
    sesi = deteksi_sesi_trading()
    terpakai = hitung_entry_terpakai_sesi_ini()

    if is_high_impact_news:
        return {"approved": False, "reason": "CIRCUIT BREAKER: Rilis berita USD Tier-1 aktif (±45 menit)."}
    if abs(z_score_h1) >= 2.5:
        return {"approved": False, "reason": f"SPIKE GUARD: Lonjakan ekstrem Z-Score H1 ({z_score_h1})."}
    if daily_loss_pct >= MAX_DAILY_LOSS_PCT:
        return {"approved": False, "reason": f"CIRCUIT BREAKER: Batas rugi harian {MAX_DAILY_LOSS_PCT}% tercapai."}
    if terpakai >= MAX_ENTRIES_SESSION:
        return {"approved": False, "reason": f"BATAS SESI ({sesi}): Kuota {MAX_ENTRIES_SESSION} entry habis."}
    if spread_pips > MAX_SPREAD_PIPS:
        return {"approved": False, "reason": f"FILTER SPREAD: Spread ({spread_pips}) > {MAX_SPREAD_PIPS} pips."}

    risk_usd = balance * (MAX_RISK_PCT / 100.0)
    risk_per_layer = risk_usd / max(1, min(layers, MAX_LAYERS_SETUP))
    denom = max(0.1, sl_distance * pip_value)
    lot = max(0.01, round(risk_per_layer / denom, 2))
    return {
        "approved": True,
        "sesi_aktif": sesi,
        "sisa_kuota_sesi": MAX_ENTRIES_SESSION - terpakai,
        "lot_per_layer": lot,
        "total_risk_usd": round(risk_usd, 2),
    }


# ==============================================================================
# 5. PENARIK DATA MAKRO, BLOCKWORKS INTEL, BINANCE DEMO & TWELVEDATA
# ==============================================================================
def ambil_statistik_dan_makro() -> dict:
    tickers = {
        "GOLD": "GC=F",
        "NASDAQ": "^IXIC",
        "SP500": "^GSPC",
        "DXY": "DX-Y.NYB",
        "US10Y": "^TNX",
        "USOIL": "CL=F",
        "BTC": "BTC-USD",
        "VIX": "^VIX",
    }
    series_dict = {}
    for nama, kode in tickers.items():
        try:
            df = clean_yf_df(yf.download(kode, period="6mo", interval="1d", progress=False))
            if not df.empty and "Close" in df.columns:
                series_dict[nama] = df["Close"]
        except Exception:
            pass

    prices = pd.DataFrame(series_dict).ffill().dropna()
    hasil = {"harga_terakhir": {}, "korelasi": {}, "garch_risk": {}, "fred_macro": {}}

    if not prices.empty:
        hasil["harga_terakhir"] = prices.iloc[-1].round(2).to_dict()
        log_ret = np.log(prices / prices.shift(1)).dropna()
        hasil["korelasi"] = log_ret.corr().round(3).to_dict()

        for aset in ["GOLD", "BTC"]:
            if aset in log_ret.columns and len(log_ret[aset]) > 30:
                ret_pct = log_ret[aset] * 100
                try:
                    model = arch_model(ret_pct, vol="Garch", p=1, q=1, dist="t")
                    fit_res = model.fit(disp="off")
                    pred_vol = float(np.sqrt(fit_res.forecast(horizon=1).variance.values[-1, 0]))
                except Exception:
                    pred_vol = float(ret_pct.std())

                var_95 = float(np.percentile(ret_pct, 5))
                cvar_95 = float(ret_pct[ret_pct <= var_95].mean())
                hasil["garch_risk"][aset] = {
                    "prediksi_vol_besok_pct": round(pred_vol, 3),
                    "CVaR_95_ekstrem_pct": round(cvar_95, 3),
                }

    if FRED_API_KEY:
        try:
            fred = Fred(api_key=FRED_API_KEY)
            hasil["fred_macro"]["FEDFUNDS"] = round(float(fred.get_series("FEDFUNDS").iloc[-1]), 2)
            hasil["fred_macro"]["US02Y_DGS2"] = round(float(fred.get_series("DGS2").iloc[-1]), 2)
            hasil["fred_macro"]["REAL_YIELD_DFII10"] = round(float(fred.get_series("DFII10").iloc[-1]), 2)
            walcl = float(fred.get_series("WALCL").iloc[-1]) / 1000.0
            tga = float(fred.get_series("WTREGEN").iloc[-1])
            rrp = float(fred.get_series("RRPONTSYD").iloc[-1])
            hasil["fred_macro"]["FED_NET_LIQUIDITY_B_USD"] = round(walcl - tga - rrp, 2)
        except Exception as e:
            hasil["fred_macro"]["error"] = str(e)

    return hasil


def ambil_data_institusi_dan_berita() -> dict:
    info = {"kripto_derivatif": {}, "deribit_options": {}, "stablecoin_flow": {}, "cftc_cot": {}, "berita": []}

    try:
        ex = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "future"}})
        ticker = ex.fetch_ticker("BTC/USDT")
        funding = ex.fetch_funding_rate("BTC/USDT")
        oi = ex.fetch_open_interest("BTC/USDT")
        info["kripto_derivatif"] = {
            "BTC_last": ticker["last"],
            "funding_rate_pct": round(float(funding["fundingRate"]) * 100, 4),
            "open_interest_btc": round(float(oi.get("openInterestAmount", 0)), 2),
        }
    except Exception as e:
        info["kripto_derivatif"]["error"] = str(e)

    try:
        dvol_resp = requests.get(
            "https://www.deribit.com/api/v2/public/get_index_price",
            params={"index_name": "btcdvol_usdc"},
            timeout=10,
        ).json()
        info["deribit_options"]["BTC_DVOL"] = dvol_resp.get("result", {}).get("index_price")
    except Exception:
        pass

    try:
        sc_data = requests.get("https://stablecoins.llama.fi/stablecoincharts/all", timeout=10).json()
        if len(sc_data) >= 8:
            mcap_now = float(sc_data[-1]["totalCirculatingUSD"]["peggedUSD"])
            mcap_7d = float(sc_data[-8]["totalCirculatingUSD"]["peggedUSD"])
            info["stablecoin_flow"] = {
                "total_stablecoin_B_USD": round(mcap_now / 1e9, 2),
                "perubahan_7hari_B_USD": round((mcap_now - mcap_7d) / 1e9, 2),
            }
    except Exception as e:
        info["stablecoin_flow"]["error"] = str(e)

    try:
        df_cot = cot.cot_year(year=datetime.now().year, cot_report_type="legacy_fut")
        gold_row = df_cot[df_cot["Market and Exchange Names"].str.contains("GOLD - COMMODITY EXCHANGE", na=False)].iloc[0]
        net_pos = int(gold_row["Noncommercial Positions-Long (All)"]) - int(gold_row["Noncommercial Positions-Short (All)"])
        info["cftc_cot"]["GOLD_Net_HedgeFund_Contracts"] = net_pos
    except Exception as e:
        info["cftc_cot"]["error"] = str(e)

    for url in ["https://www.investing.com/rss/news_25.rss", "https://www.investing.com/rss/news_301.rss"]:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:3]:
                info["berita"].append(entry.title)
        except Exception:
            pass

    return info


def ambil_intelijen_blockworks() -> dict:
    if not BLOCKWORKS_API_KEY:
        return {"status": "opsional_belum_diisi"}
    try:
        headers = {
            "X-Blockworks-API-Key": BLOCKWORKS_API_KEY,
            "Accept": "application/json",
        }
        resp = requests.get(
            "https://api.blockworks.com/query/tabular/assets",
            headers=headers,
            params={"pageSize": 5},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            rows = data.get("data", []) if isinstance(data, dict) else data
            ringkasan = []
            if isinstance(rows, list):
                for item in rows[:5]:
                    if isinstance(item, dict):
                        ringkasan.append({
                            "symbol": item.get("symbol") or item.get("ticker"),
                            "name": item.get("name"),
                        })
            return {"status": "aktif_terhubung", "assets_sample": ringkasan}
        return {"status": f"http_{resp.status_code}", "detail": resp.text[:180]}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def ambil_data_twelvedata_intraday() -> dict:
    if not TWELVEDATA_API_KEY:
        return {"status": "opsional_belum_diisi"}
    hasil = {"status": "aktif"}
    for label, sym in [("XAUUSD", "XAU/USD"), ("QQQ_NASDAQ", "QQQ")]:
        try:
            resp = requests.get(
                "https://api.twelvedata.com/quote",
                params={"symbol": sym, "apikey": TWELVEDATA_API_KEY},
                timeout=10,
            ).json()
            if "close" in resp:
                hasil[label] = {
                    "close": resp.get("close"),
                    "percent_change": resp.get("percent_change"),
                    "high": resp.get("high"),
                    "low": resp.get("low"),
                }
            else:
                hasil[label] = {"error": resp.get("message", "gagal_quote")}
        except Exception as e:
            hasil[label] = {"error": str(e)}
    return hasil


def cek_spread_mt5_cloud(symbol: str = "XAUUSD") -> float:
    if not METAAPI_TOKEN or not METAAPI_ACCOUNT_ID:
        return 1.5
    try:
        headers = {"auth-token": METAAPI_TOKEN}
        url = f"https://mt-client-api-v1.agiliumtrade.agiliumtrade.ai/users/current/accounts/{METAAPI_ACCOUNT_ID}/symbols/{symbol}/current-price"
        resp = requests.get(url, headers=headers, timeout=10).json()
        bid, ask = float(resp.get("bid", 0)), float(resp.get("ask", 0))
        if bid > 0 and ask > 0:
            return round((ask - bid) * 10, 2)
    except Exception:
        pass
    return 1.5


# ==============================================================================
# 6. PENGIRIM TELEGRAM HTML RAPI (SPASI LEGA & ANTI-ERROR ENTITAS)
# ==============================================================================
def kirim_telegram(pesan_html: str, log_id: int = None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(pesan_html)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    reply_markup = None
    if log_id is not None:
        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve Sniper Setup", "callback_data": f"approve_{log_id}"},
                    {"text": "❌ Reject / Skip", "callback_data": f"reject_{log_id}"},
                ]
            ]
        }

    chunks = [pesan_html[i : i + 3800] for i in range(0, len(pesan_html), 3800)]
    for idx, chunk in enumerate(chunks):
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup and idx == len(chunks) - 1:
            payload["reply_markup"] = json.dumps(reply_markup)
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code != 200:
            requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk}, timeout=15)


# ==============================================================================
# 7. DEWAN JURI 4-AGEN: SNIPER ZERO FLOATING (GEMINI 4 HYDROGEN PRIORITY)
# ==============================================================================
def format_setup_horizon(judul: str, data: dict) -> str:
    if not isinstance(data, dict):
        return ""
    arah = html.escape(str(data.get("action", "WAIT")))
    entry = html.escape(str(data.get("entry_limit", "-")))
    sl = html.escape(str(data.get("sl", "-")))
    tp1 = html.escape(str(data.get("tp1_bep", "-")))
    tp2 = html.escape(str(data.get("tp2", "-")))
    tp3 = html.escape(str(data.get("tp3_runner", "-")))
    rr = html.escape(str(data.get("rr", "-")))
    alasan = html.escape(str(data.get("reason", "-")))
    return (
        f"🎯 <b>{judul}</b> — <b>[{arah}]</b> (RR: <b>{rr}</b>)\n"
        f"• <b>Sniper Limit Entry:</b> <code>{entry}</code>\n"
        f"• <b>Tight Stop Loss:</b> <code>{sl}</code>\n"
        f"• <b>TP1 (Geser SL ke BEP+1):</b> <code>{tp1}</code>\n"
        f"• <b>TP2 / TP3 Runner:</b> <code>{tp2}</code> | <code>{tp3}</code>\n"
        f"• <i>Alasan Presisi:</i> {alasan}\n\n"
    )


def jalankan_analisa_lengkap(pemicu: str = "Auto-Scan 24/7") -> str:
    data_makro = ambil_statistik_dan_makro()
    data_institusi = ambil_data_institusi_dan_berita()
    blockworks_intel = ambil_intelijen_blockworks()
    twelvedata_quote = ambil_data_twelvedata_intraday()
    level_gold = hitung_level_sniper_historis("GC=F")
    level_btc = hitung_level_sniper_historis("BTC-USD")
    kalender_news = cek_kalender_berita_tier1_otomatis()
    spread_aktif = cek_spread_mt5_cloud("XAUUSD")

    sl_dinamis_gold = max(2.0, round(float(level_gold.get("ATR_M15", 2.5)) * 1.2, 2))
    risk_status = validasi_hardcoded_risk(
        balance=1000.0,
        sl_distance=sl_dinamis_gold,
        pip_value=1.0,
        layers=2,
        spread_pips=spread_aktif,
        is_high_impact_news=kalender_news.get("is_high_impact_active", False),
        z_score_h1=level_gold.get("Z_score_H1", 0.0),
    )

    macro_snap = json.dumps(data_makro.get("harga_terakhir", {}))

    prompt = f"""
Kamu adalah Dewan Juri 4-Agen Kuantitatif Institusional (SMC, ICT, Orderflow, Makro, On-Chain).
Analisa seluruh data historis s.d. harga terbaru berikut untuk XAUUSD (GOLD) dan BTCUSDT:

Waktu: {waktu_wib_str()} | Sesi: {deteksi_sesi_trading()} | Spread XAU: {spread_aktif} pips
1. Makro, Korelasi, GARCH & FRED: {data_makro}
2. Level Sniper Historis XAUUSD (M15 FVG, OHLC Pivot/NH/NL, Swing 60D, Hold 5Y): {level_gold}
3. Level Sniper Historis BTCUSDT (M15 FVG, OHLC Pivot/NH/NL, Swing 60D, Hold 5Y): {level_btc}
4. CFTC COT, Derivatif OI/Funding, Deribit DVOL, Stablecoin Flow & Berita: {data_institusi}
5. Blockworks Intel & Twelve Data Real-Time: {blockworks_intel} | {twelvedata_quote}
6. Kalender Berita Tier-1 & Hardcoded Risk: {kalender_news} | {risk_status}

ATURAN MUTLAK SNIPER ENTRY (ZERO FLOATING & HIGH RR):
- DILARANG memberi entry market di tengah rentang! Wajib tentukan titik LIMIT ORDER presisi di ujung zona PD Array (FVG M15 CE 50%, OHLC NL/NH, Order Block, atau Fibonacci OTE 0.705) agar potensi floating minus minimal.
- Stop Loss (SL) wajib KETAT tepat di luar sumbu struktur invalidasi (gunakan referensi ATR M15/H1, bukan SL lebar).
- Rasio Risk-to-Reward (RR) minimal 1:3 untuk Sesi/Intraday, 1:5 untuk Swing, dan 1:10+ untuk Hold Position jangka panjang.
- Jika skor konfirmasi < 75 atau tidak memenuhi syarat, isi action dengan "WAIT_LIMIT".

Kembalikan HANYA JSON valid dengan struktur persis berikut:
{{
  "xau_score": integer (0-100),
  "xau_bias": "BULLISH" | "BEARISH" | "WAIT",
  "btc_score": integer (0-100),
  "btc_bias": "BULLISH" | "BEARISH" | "WAIT",
  "macro_reason": "penjelasan makro & likuiditas 2 kalimat jelas",
  "smc_orderflow_reason": "penjelasan struktur MTF, FVG, liquidity sweep & COT/OI 2 kalimat jelas",
  "skeptic_risk_reason": "risiko pembatal setup & kondisi berita 1-2 kalimat jelas",
  "xau_setups": {{
    "sesi_scalp": {{"action": "BUY LIMIT/SELL LIMIT/WAIT", "entry_limit": "angka", "sl": "angka", "tp1_bep": "angka", "tp2": "angka", "tp3_runner": "angka", "rr": "1:3.5", "reason": "alasan singkat"}},
    "intraday": {{"action": "...", "entry_limit": "...", "sl": "...", "tp1_bep": "...", "tp2": "...", "tp3_runner": "...", "rr": "...", "reason": "..."}},
    "swing": {{"action": "...", "entry_limit": "...", "sl": "...", "tp1_bep": "...", "tp2": "...", "tp3_runner": "...", "rr": "...", "reason": "..."}},
    "hold_longterm": {{"action": "...", "entry_limit": "...", "sl": "...", "tp1_bep": "...", "tp2": "...", "tp3_runner": "...", "rr": "1:12", "reason": "zona akumulasi multi-bulan s.d. tahunan"}}
  }},
  "btc_setups": {{
    "sesi_scalp": {{"action": "...", "entry_limit": "...", "sl": "...", "tp1_bep": "...", "tp2": "...", "tp3_runner": "...", "rr": "...", "reason": "..."}},
    "intraday": {{"action": "...", "entry_limit": "...", "sl": "...", "tp1_bep": "...", "tp2": "...", "tp3_runner": "...", "rr": "...", "reason": "..."}},
    "swing": {{"action": "...", "entry_limit": "...", "sl": "...", "tp1_bep": "...", "tp2": "...", "tp3_runner": "...", "rr": "...", "reason": "..."}},
    "hold_longterm": {{"action": "...", "entry_limit": "...", "sl": "...", "tp1_bep": "...", "tp2": "...", "tp3_runner": "...", "rr": "1:15", "reason": "zona akumulasi multi-bulan s.d. tahunan"}}
  }}
}}
"""
    try:
        raw_json, active_model = panggil_gemini_prioritas(prompt, json_mode=True)
        v = json.loads(raw_json)
        xau_score = int(v.get("xau_score", 0))
        btc_score = int(v.get("btc_score", 0))
        top_score = max(xau_score, btc_score)
        bias_summary = f"XAU:{v.get('xau_bias')}({xau_score}) | BTC:{v.get('btc_bias')}({btc_score})"
        lolos_final = risk_status.get("approved") and (top_score >= 75)

        xau_s = v.get("xau_setups", {})
        btc_s = v.get("btc_setups", {})

        pesan_html = (
            f"🏛 <b>QUANT AGENT v3.0 — SNIPER INSTITUTIONAL REPORT</b>\n"
            f"🕒 <i>{waktu_wib_str()}</i>\n"
            f"🧠 <b>AI Engine:</b> <code>{html.escape(active_model)}</code>\n"
            f"🌐 <b>Sesi:</b> {html.escape(deteksi_sesi_trading())}\n"
            f"📡 <b>Blockworks API:</b> {html.escape(str(blockworks_intel.get('status')))}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📌 <b>RINGKASAN KONFIRMASI</b>\n\n"
            f"🥇 <b>XAUUSD (Live: {level_gold.get('harga_terakhir')})</b>\n"
            f"Bias: <b>{html.escape(str(v.get('xau_bias')))}</b> | Skor: <b>{xau_score}/100</b> | {html.escape(str(level_gold.get('regime')))}\n\n"
            f"₿ <b>BTCUSDT (Live: {level_btc.get('harga_terakhir')})</b>\n"
            f"Bias: <b>{html.escape(str(v.get('btc_bias')))}</b> | Skor: <b>{btc_score}/100</b> | {html.escape(str(level_btc.get('regime')))}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🧠 <b>1. ANALISA MAKRO & LIKUIDITAS</b>\n"
            f"{html.escape(str(v.get('macro_reason')))}\n\n\n"
            f"📐 <b>2. KONFLUENSI SMC, ORDERFLOW & ON-CHAIN</b>\n"
            f"{html.escape(str(v.get('smc_orderflow_reason')))}\n\n\n"
            f"🛡 <b>3. SKEPTIC VETO & FILTER RISIKO</b>\n"
            f"{html.escape(str(v.get('skeptic_risk_reason')))}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🥇 <b>MAPPING SNIPER XAUUSD (ZERO FLOATING)</b>\n\n"
            f"{format_setup_horizon('SESI / SCALP (M5-M15)', xau_s.get('sesi_scalp'))}"
            f"{format_setup_horizon('INTRADAY (H1-H4)', xau_s.get('intraday'))}"
            f"{format_setup_horizon('SWING (D1-W1)', xau_s.get('swing'))}"
            f"{format_setup_horizon('HOLD POSITION (BULANAN-TAHUNAN)', xau_s.get('hold_longterm'))}"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"₿ <b>MAPPING SNIPER BTCUSDT (ZERO FLOATING)</b>\n\n"
            f"{format_setup_horizon('SESI / SCALP (M5-M15)', btc_s.get('sesi_scalp'))}"
            f"{format_setup_horizon('INTRADAY (H1-H4)', btc_s.get('intraday'))}"
            f"{format_setup_horizon('SWING (D1-W1)', btc_s.get('swing'))}"
            f"{format_setup_horizon('HOLD POSITION (BULANAN-TAHUNAN)', btc_s.get('hold_longterm'))}"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚖️ <b>Status Guardrail:</b> {'✅ LOLOS EKSEKUSI (>=75)' if lolos_final else '⏳ WAIT LIMIT / TUNGGU KONFIRMASI'}"
        )
    except Exception as e:
        top_score = 0
        bias_summary = "ERROR"
        lolos_final = False
        pesan_html = f"❌ <b>Gagal memproses analisa AI:</b>\n<code>{html.escape(str(e))}</code>"

    log_id = simpan_log(
        "XAUUSD/BTC",
        bias_summary,
        top_score,
        spread_aktif,
        macro_snap,
        "PENDING_REVIEW" if lolos_final else "AUTO_WAIT",
        pesan_html[:600],
    )
    kirim_telegram(pesan_html, log_id=log_id if lolos_final else None)
    return pesan_html


# ==============================================================================
# 8. LIVE CHAT INTERAKTIF TELEGRAM (PRIORITAS GEMINI 4 HYDROGEN)
# ==============================================================================
def jawab_live_chat_telegram(pertanyaan_user: str) -> str:
    try:
        level_gold = hitung_level_sniper_historis("GC=F")
        level_btc = hitung_level_sniper_historis("BTC-USD")
        jurnal_terakhir = ambil_ringkasan_jurnal_dan_evaluasi_mingguan()

        prompt = f"""
Kamu adalah Co-Pilot Quant Trader pribadi pengguna di Telegram.
Jawab pertanyaan pengguna secara langsung, padat, tajam, dan berbasis data live + historis berikut:

Waktu Sekarang: {waktu_wib_str()} | Sesi Aktif: {deteksi_sesi_trading()}
Data Sniper XAUUSD Terbaru: {level_gold}
Data Sniper BTCUSDT Terbaru: {level_btc}
Log Audit Terakhir: {jurnal_terakhir}

Pertanyaan / Pesan Pengguna:
"{pertanyaan_user}"

Aturan Jawaban:
- Gunakan bahasa Indonesia yang santai tapi profesional ala meja institusional.
- Jika pengguna bertanya setup (sesi, intraday, swing, atau hold jangka panjang), berikan angka Entry Limit presisi, SL ketat, TP1 (BEP), TP2, TP3, dan RR.
- Pisahkan setiap poin dengan baris kosong agar rapi dibaca di HP. Jangan gunakan tanda bintang (*).
"""
        teks_jawaban, active_model = panggil_gemini_prioritas(prompt, json_mode=False)
        return (
            f"💬 <b>QUANT CO-PILOT LIVE</b> (<code>{html.escape(active_model)}</code>)\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{html.escape(teks_jawaban)}"
        )
    except Exception as e:
        return f"❌ Error Live Chat: <code>{html.escape(str(e))}</code>"


# ==============================================================================
# 9. BACKTESTING & MONTE CARLO
# ==============================================================================
class TrendQuantStrategy(Strategy):
    fast_period = 20
    slow_period = 50

    def init(self):
        close = pd.Series(self.data.Close)
        self.sma_fast = self.I(lambda x: pd.Series(x).rolling(self.fast_period).mean(), close)
        self.sma_slow = self.I(lambda x: pd.Series(x).rolling(self.slow_period).mean(), close)

    def next(self):
        if self.sma_fast[-1] > self.sma_slow[-1] and not self.position:
            self.buy(size=0.1)
        elif self.sma_fast[-1] < self.sma_slow[-1] and self.position:
            self.position.close()


def jalankan_backtest_dan_montecarlo(ticker: str = "GC=F", period: str = "2y") -> str:
    df = clean_yf_df(yf.download(ticker, period=period, interval="1d", progress=False))
    if df.empty or len(df) < 120:
        return f"Data historis {ticker} tidak cukup."

    ohlc = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    split_idx = int(len(ohlc) * 0.7)
    oos_data = ohlc.iloc[split_idx:]

    bt = Backtest(oos_data, TrendQuantStrategy, cash=10000, commission=0.0005, finalize_trades=True)
    stats = bt.run()
    trades_df = stats["_trades"]

    if trades_df is None or len(trades_df) < 5:
        return f"Sampel trade OOS {ticker} terlalu sedikit."

    trade_returns = trades_df["ReturnPct"].values
    np.random.seed(42)
    mc_final_returns, mc_max_drawdowns = [], []
    for _ in range(1000):
        sim_seq = np.random.choice(trade_returns, size=len(trade_returns), replace=True)
        equity_curve = np.cumprod(1 + sim_seq)
        running_max = np.maximum.accumulate(equity_curve)
        dd = (running_max - equity_curve) / running_max
        mc_final_returns.append((equity_curve[-1] - 1) * 100)
        mc_max_drawdowns.append(np.max(dd) * 100)

    return (
        f"📈 <b>BACKTEST & MONTE CARLO ({ticker})</b>\n"
        f"• Trade OOS: {len(trades_df)} | Win Rate: {round(float(stats['Win Rate [%]']), 1)}%\n"
        f"• Median Return (1.000x MC): {round(float(np.percentile(mc_final_returns, 50)), 2)}%\n"
        f"• Max Drawdown 95%: -{round(float(np.percentile(mc_max_drawdowns, 95)), 2)}%"
    )


# ==============================================================================
# 10. BACKGROUND SCHEDULER & TELEGRAM LISTENER (NON-BLOCKING MULTI-THREAD)
# ==============================================================================
def loop_scheduler_otomatis():
    time.sleep(15)
    while True:
        try:
            jalankan_analisa_lengkap(pemicu="Auto-Scan Cloud 24/7")
        except Exception as e:
            print("Error scheduler:", e)
        time.sleep(AUTO_SCAN_INTERVAL_SEC)


def loop_listener_telegram():
    if not TELEGRAM_BOT_TOKEN:
        return
    offset = 0
    base_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    while True:
        try:
            resp = requests.get(f"{base_url}/getUpdates", params={"offset": offset, "timeout": 30}, timeout=35).json()
            if not resp.get("ok"):
                print("Telegram getUpdates warning:", resp)
                time.sleep(5)
                continue

            for item in resp.get("result", []):
                offset = item["update_id"] + 1

                if "callback_query" in item:
                    cb = item["callback_query"]
                    cb_id = cb["id"]
                    data_cb = cb.get("data", "")
                    from_id = str(cb.get("from", {}).get("id", ""))
                    if from_id == TELEGRAM_CHAT_ID and "_" in data_cb:
                        aksi, id_str = data_cb.split("_", 1)
                        log_id = int(id_str)
                        if aksi == "approve":
                            update_keputusan_user(log_id, "APPROVED_SIMULATION")
                            requests.post(f"{base_url}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "Approved!"})
                            kirim_telegram(f"✅ <b>Sinyal #{log_id} DISETUJUI.</b>\nDisiplin risiko & aturan BEP TP1 aktif.")
                        elif aksi == "reject":
                            update_keputusan_user(log_id, "REJECTED_BY_USER")
                            requests.post(f"{base_url}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "Rejected!"})
                            kirim_telegram(f"❌ <b>Sinyal #{log_id} DITOLAK.</b>\nModal aman terjaga.")
                    continue

                msg = item.get("message", {})
                raw_text = msg.get("text", "").strip()
                text_lower = raw_text.lower()
                chat_id = str(msg.get("chat", {}).get("id", ""))

                if chat_id == TELEGRAM_CHAT_ID and raw_text:
                    if text_lower in ["/start", "/help"]:
                        kirim_telegram(
                            "🤖 <b>QUANT AGENT v3.0 (GEMINI 4 HYDROGEN + LIVE CHAT)</b>\n"
                            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                            "• <code>/scan</code> atau <code>/cek</code> — Analisa lengkap 4 Horizon (Sesi, Intraday, Swing, Hold)\n\n"
                            "• <code>/backtest</code> — Uji Backtest & 1.000x Monte Carlo\n\n"
                            "• <code>/jurnal</code> — Lihat log audit & kuota sesi\n\n"
                            "• <b>Live Chat Bebas:</b> Ketik pertanyaan apa saja langsung di chat ini."
                        )
                    elif text_lower in ["/scan", "/cek", "/analisa"]:
                        kirim_telegram("⏳ <i>Gemini 4 Hydrogen sedang menarik data historis s.d. tick terbaru & menghitung zona Sniper Limit...</i>")
                        threading.Thread(target=jalankan_analisa_lengkap, args=("Manual /scan Telegram",), daemon=True).start()
                    elif text_lower == "/backtest":
                        kirim_telegram("⏳ <i>Menjalankan Backtest Out-of-Sample & 1.000x Monte Carlo...</i>")
                        threading.Thread(
                            target=lambda: kirim_telegram(
                                f"{jalankan_backtest_dan_montecarlo('GC=F', '2y')}\n\n{jalankan_backtest_dan_montecarlo('BTC-USD', '2y')}"
                            ),
                            daemon=True,
                        ).start()
                    elif text_lower in ["/jurnal", "/evaluasi"]:
                        kirim_telegram(ambil_ringkasan_jurnal_dan_evaluasi_mingguan())
                    else:
                        threading.Thread(
                            target=lambda q=raw_text: kirim_telegram(jawab_live_chat_telegram(q)),
                            daemon=True,
                        ).start()
        except Exception as e:
            print("Listener error:", e)
            time.sleep(5)


@app.route("/", methods=["GET"])
@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({
        "status": "alive",
        "version": "3.0-gemini4-hydrogen",
        "primary_model": GEMINI_MODEL_CHAIN[0],
        "session": deteksi_sesi_trading(),
        "wib": waktu_wib_str(),
    })


@app.route("/webhook", methods=["POST"])
def tradingview_webhook():
    payload = request.get_json(silent=True) or {"raw": request.data.decode("utf-8", errors="ignore")}
    threading.Thread(target=jalankan_analisa_lengkap, args=(f"TradingView Alert: {payload}",), daemon=True).start()
    return jsonify({"received": True})


if __name__ == "__main__":
    init_db()
    threading.Thread(target=loop_scheduler_otomatis, daemon=True).start()
    threading.Thread(target=loop_listener_telegram, daemon=True).start()
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
