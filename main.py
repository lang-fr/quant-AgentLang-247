import os
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
import socket
import urllib3.util.connection as urllib3_cn

urllib3_cn.allowed_gai_family = lambda: socket.AF_INET
import cot_reports as cot
from arch import arch_model
from backtesting import Backtest, Strategy
from dotenv import load_dotenv
from fredapi import Fred
from flask import Flask, request, jsonify
from google import genai
from google.genai import types
from market_feeds import ambil_kalender_ff, ambil_makro_fred, ambil_orderflow_ccxt

# ==============================================================================
# 1. LAPISAN 5: KEAMANAN KUNCI API (.env LOKAL & ENVIRONMENT VARIABLES CLOUD)
# ==============================================================================
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
FRED_API_KEY = os.getenv("FRED_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
BLOCKWORKS_API_KEY = os.getenv("BLOCKWORKS_API_KEY", "")
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "")
CMC_API_KEY = os.getenv("CMC_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
METAAPI_TOKEN = os.getenv("METAAPI_TOKEN", "")
METAAPI_ACCOUNT_ID = os.getenv("METAAPI_ACCOUNT_ID", "")

MAX_RISK_PCT = float(os.getenv("MAX_RISK_PER_SETUP_PCT", "1.0"))
MAX_ENTRIES_SESSION = int(os.getenv("MAX_ENTRIES_PER_SESSION", "3"))
MAX_LAYERS_SETUP = int(os.getenv("MAX_LAYERS_PER_SETUP", "3"))
MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", "2.5"))
MAX_SPREAD_PIPS = float(os.getenv("MAX_ALLOWED_SPREAD_PIPS", "3.0"))
AUTO_SCAN_INTERVAL_SEC = int(os.getenv("AUTO_SCAN_INTERVAL_SEC", "14400"))  # Tiap 4 jam

app = Flask(__name__)
DB_PATH = "quant_cloud_audit.db"


# ==============================================================================
# 2. LAPISAN 2: DATABASE SQLITE, PELACAK SESI & EVALUASI AKURASI MINGGUAN
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
        return "ASIA"
    elif 7 <= jam_utc < 13:
        return "LONDON"
    elif 13 <= jam_utc < 21:
        return "NEW_YORK"
    return "OFF_SESSION"


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


def simpan_log(
    asset: str,
    bias: str,
    score: int,
    spread_pips: float,
    macro_snapshot: str,
    decision: str,
    summary: str,
) -> int:
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

    c.execute(
        "SELECT COUNT(*) FROM audit_log WHERE timestamp_utc >= ? AND user_decision = 'APPROVED_SIMULATION'",
        (batas_7_hari,),
    )
    approved_7d = c.fetchone()[0]

    c.execute(
        "SELECT COUNT(*) FROM audit_log WHERE timestamp_utc >= ? AND user_decision = 'REJECTED_BY_USER'",
        (batas_7_hari,),
    )
    rejected_7d = c.fetchone()[0]

    c.execute("SELECT AVG(score) FROM audit_log WHERE timestamp_utc >= ? AND score > 0", (batas_7_hari,))
    avg_score = c.fetchone()[0] or 0.0

    c.execute(
        "SELECT session_name, asset, bias, score, spread_pips, user_decision FROM audit_log ORDER BY id DESC LIMIT 5"
    )
    rows = c.fetchall()
    conn.close()

    sesi_aktif = deteksi_sesi_trading()
    sisa_kuota = max(0, MAX_ENTRIES_SESSION - hitung_entry_terpakai_sesi_ini())

    teks = (
        f"JURNAL AUDIT & EVALUASI MINGGUAN (7 HARI TERAKHIR)\n"
        f"- Total Scan Sistem: {scan_7d}\n"
        f"- Sinyal Disetujui (Simulasi): {approved_7d}\n"
        f"- Sinyal Ditolak (Disiplin): {rejected_7d}\n"
        f"- Rata-rata Skor Konfirmasi: {round(float(avg_score), 1)}/100\n"
        f"- Sesi Aktif ({sesi_aktif}): Sisa kuota {sisa_kuota}/{MAX_ENTRIES_SESSION} entry\n\n"
        f"5 Log Terakhir:\n"
    )
    for r in rows:
        teks += f"- [{r[0]}] {r[1]} | {r[2]} | Skor: {r[3]} | Spread: {r[4]} | {r[5]}\n"
    return teks


# ==============================================================================
# 3. MODUL KONFIRMASI TAMBAHAN: KALENDER TIER-1 OTOMATIS & MULTI-TIMEFRAME MTF
# ==============================================================================
def cek_kalender_berita_tier1_otomatis() -> dict:
    """
    Mengecek feed kalender ekonomi mingguan untuk mendeteksi berita High-Impact USD
    dalam jendela ±45 menit dari waktu sekarang (rem darurat otomatis).
    """
    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=10,
        )
        if resp.status_code != 200:
            return {"is_high_impact_active": False, "error": f"HTTP {resp.status_code}"}
        events = resp.json()
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

        return {
            "is_high_impact_active": len(active_warnings) > 0,
            "upcoming_events": active_warnings,
        }
    except Exception as e:
        return {"is_high_impact_active": False, "error": str(e)}


def cek_konfluensi_mtf_dan_regime(ticker: str = "GC=F") -> dict:
    """
    Adaptasi Freqtrade: Mengecek keselarasan tren EMA(20/50) pada H1, H4, dan D1
    serta menghitung ADX(14) untuk mendeteksi pasar trending vs sideways/choppy.
    """
    try:
        df_h1 = yf.download(ticker, period="30d", interval="1h", progress=False)
        df_d1 = yf.download(ticker, period="6mo", interval="1d", progress=False)
        if df_h1.empty or df_d1.empty:
            return {"error": f"Data MTF {ticker} tidak tersedia."}

        if isinstance(df_h1.columns, pd.MultiIndex):
            df_h1.columns = df_h1.columns.get_level_values(0)
        if isinstance(df_d1.columns, pd.MultiIndex):
            df_d1.columns = df_d1.columns.get_level_values(0)

        h1_close = df_h1["Close"]
        h1_trend = "BULLISH" if h1_close.ewm(span=20).mean().iloc[-1] > h1_close.ewm(span=50).mean().iloc[-1] else "BEARISH"

        df_h4 = df_h1.resample("4h").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
        h4_close = df_h4["Close"]
        h4_trend = "BULLISH" if h4_close.ewm(span=20).mean().iloc[-1] > h4_close.ewm(span=50).mean().iloc[-1] else "BEARISH"

        d1_close = df_d1["Close"]
        d1_trend = "BULLISH" if d1_close.ewm(span=20).mean().iloc[-1] > d1_close.ewm(span=50).mean().iloc[-1] else "BEARISH"

        high, low, close = df_d1["High"], df_d1["Low"], df_d1["Close"]
        plus_dm = high.diff().clip(lower=0)
        minus_dm = (-low.diff()).clip(lower=0)
        tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
        atr = tr.rolling(14).mean()
        plus_di = 100 * (plus_dm.rolling(14).mean() / atr)
        minus_di = 100 * (minus_dm.rolling(14).mean() / atr)
        dx = (abs(plus_di - minus_di) / (plus_di + minus_di)) * 100
        adx_val = round(float(dx.rolling(14).mean().iloc[-1]), 1)

        h1_ret = np.log(h1_close / h1_close.shift(1)).dropna()
        z_score_h1 = round(float((h1_ret.iloc[-1] - h1_ret.mean()) / h1_ret.std()), 2)

        aligned = (h1_trend == h4_trend == d1_trend)
        regime = "TRENDING" if adx_val >= 20 else "CHOPPY_SIDEWAYS"

        return {
            "ticker": ticker,
            "H1_trend": h1_trend,
            "H4_trend": h4_trend,
            "D1_trend": d1_trend,
            "MTF_aligned": aligned,
            "ADX_14": adx_val,
            "market_regime": regime,
            "Z_score_H1_latest": z_score_h1,
        }
    except Exception as e:
        return {"error": str(e)}


# ==============================================================================
# 4. LAPISAN 3: HARDCODED RISK ENGINE & CIRCUIT BREAKER
# ==============================================================================
def validasi_hardcoded_risk(
    balance: float,
    sl_pips: float,
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
        return {"approved": False, "reason": "CIRCUIT BREAKER: Jadwal berita USD Tier-1 aktif (±45 menit)."}
    if abs(z_score_h1) >= 2.5:
        return {"approved": False, "reason": f"SPIKE GUARD: Z-Score H1 ekstrem ({z_score_h1}), tunda entry."}
    if daily_loss_pct >= MAX_DAILY_LOSS_PCT:
        return {"approved": False, "reason": f"CIRCUIT BREAKER: Batas rugi harian {MAX_DAILY_LOSS_PCT}% tercapai."}
    if terpakai >= MAX_ENTRIES_SESSION:
        return {"approved": False, "reason": f"BATAS SESI ({sesi}): Kuota {MAX_ENTRIES_SESSION} entry sudah habis."}
    if layers > MAX_LAYERS_SETUP:
        return {"approved": False, "reason": f"BATAS LAYER: Maksimal {MAX_LAYERS_SETUP} layer per setup."}
    if spread_pips > MAX_SPREAD_PIPS:
        return {"approved": False, "reason": f"FILTER SPREAD: Spread ({spread_pips}) melebihi batas {MAX_SPREAD_PIPS}."}
    if sl_pips <= 0 or pip_value <= 0:
        return {"approved": False, "reason": "Stop Loss wajib lebih besar dari 0."}

    risk_usd = balance * (MAX_RISK_PCT / 100.0)
    risk_per_layer = risk_usd / max(1, layers)
    lot = max(0.01, round(risk_per_layer / (sl_pips * pip_value), 2))
    return {
        "approved": True,
        "sesi_aktif": sesi,
        "sisa_kuota_entry_sesi": MAX_ENTRIES_SESSION - terpakai,
        "lot_per_layer": lot,
        "total_risk_usd": round(risk_usd, 2),
    }


# ==============================================================================
# 5. LAPISAN 1: MESIN BACKTESTING (backtesting.py) & SIMULASI MONTE CARLO
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
    df = yf.download(ticker, period=period, interval="1d", progress=False)
    if df.empty or len(df) < 120:
        return f"Data historis {ticker} tidak cukup untuk backtest."

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    ohlc = df[["Open", "High", "Low", "Close", "Volume"]].dropna()

    split_idx = int(len(ohlc) * 0.7)
    oos_data = ohlc.iloc[split_idx:]

    bt = Backtest(oos_data, TrendQuantStrategy, cash=10000, commission=0.0005, finalize_trades=True)
    stats = bt.run()
    trades_df = stats["_trades"]

    if trades_df is None or len(trades_df) < 5:
        return f"Sampel transaksi Out-of-Sample pada {ticker} terlalu sedikit ({len(trades_df) if trades_df is not None else 0})."

    trade_returns = trades_df["ReturnPct"].values

    np.random.seed(42)
    mc_final_returns = []
    mc_max_drawdowns = []
    for _ in range(1000):
        sim_seq = np.random.choice(trade_returns, size=len(trade_returns), replace=True)
        equity_curve = np.cumprod(1 + sim_seq)
        running_max = np.maximum.accumulate(equity_curve)
        dd = (running_max - equity_curve) / running_max
        mc_final_returns.append((equity_curve[-1] - 1) * 100)
        mc_max_drawdowns.append(np.max(dd) * 100)

    median_return = round(float(np.percentile(mc_final_returns, 50)), 2)
    worst_5pct_dd = round(float(np.percentile(mc_max_drawdowns, 95)), 2)
    win_rate = round(float(stats["Win Rate [%]"]), 1)
    profit_factor = round(float(stats["Profit Factor"]), 2) if not np.isnan(stats["Profit Factor"]) else 0.0

    return (
        f"HASIL BACKTEST (backtesting.py) & MONTE CARLO ({ticker} - Out-of-Sample)\n"
        f"- Jumlah Trade OOS: {len(trades_df)}\n"
        f"- Win Rate: {win_rate}%\n"
        f"- Profit Factor: {profit_factor}\n"
        f"- Median Return (1.000x Monte Carlo): {median_return}%\n"
        f"- Max Drawdown Terburuk (Persentil 95): -{worst_5pct_dd}%"
    )


# ==============================================================================
# 6. MODUL PENARIK DATA KUANTITATIF, MAKRO, INSTITUSI & INTELIJEN MULTI-ASET
# ==============================================================================
def ambil_statistik_dan_makro() -> dict:
    tickers = {
        "GOLD": "GC=F",
        "NASDAQ": "^IXIC",
        "DXY": "DX-Y.NYB",
        "US10Y": "^TNX",
        "BTC": "BTC-USD",
        "VIX": "^VIX",
    }
    prices = pd.DataFrame()
    for nama, kode in tickers.items():
        try:
            df = yf.download(kode, period="6mo", interval="1d", progress=False)
            if not df.empty and "Close" in df:
                s = df["Close"].squeeze().dropna()
                if not s.empty:
                    prices[nama] = s
        except Exception:
            pass

    hasil = {"harga_terakhir": {}, "korelasi": {}, "garch_risk": {}, "fred_macro": {}}

    if not prices.empty:
        for col in prices.columns:
            valid_vals = prices[col].dropna()
            if not valid_vals.empty:
                hasil["harga_terakhir"][col] = round(float(valid_vals.iloc[-1]), 2)
        log_ret = np.log(prices / prices.shift(1)).dropna(how="all")
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
            hasil["fred_macro"]["REAL_YIELD_DFII10"] = round(float(fred.get_series("DFII10").iloc[-1]), 2)
            walcl = float(fred.get_series("WALCL").iloc[-1]) / 1000.0
            tga = float(fred.get_series("WTREGEN").iloc[-1]) / 1000.0
            rrp = float(fred.get_series("RRPONTSYD").iloc[-1])
            hasil["fred_macro"]["FED_NET_LIQUIDITY_B_USD"] = round(walcl - tga - rrp, 2)
        except Exception as e:
            hasil["fred_macro"]["error"] = str(e)

    return hasil


def ambil_deribit_options_btc() -> dict:
    try:
        dvol_resp = requests.get(
            "https://www.deribit.com/api/v2/public/get_volatility_index_data",
            params={
                "currency": "BTC",
                "resolution": "60",
                "start_timestamp": int((datetime.now(timezone.utc) - timedelta(hours=6)).timestamp() * 1000),
                "end_timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
            },
            timeout=10,
        ).json()
        dvol_data = dvol_resp.get("result", {}).get("data", [])
        dvol_terakhir = round(float(dvol_data[-1][4]), 2) if dvol_data else None

        books_resp = requests.get(
            "https://www.deribit.com/api/v2/public/get_book_summary_by_currency",
            params={"currency": "BTC", "kind": "option"},
            timeout=12,
        ).json()
        summaries = books_resp.get("result", [])
        call_oi = 0.0
        put_oi = 0.0
        for row in summaries:
            inst = str(row.get("instrument_name", ""))
            oi_val = float(row.get("open_interest") or 0.0)
            if inst.endswith("-C"):
                call_oi += oi_val
            elif inst.endswith("-P"):
                put_oi += oi_val

        put_call_ratio = round(put_oi / call_oi, 3) if call_oi > 0 else None
        return {
            "DVOL_BTC": dvol_terakhir,
            "Put_Call_OI_Ratio": put_call_ratio,
            "Call_OI_BTC": round(call_oi, 1),
            "Put_OI_BTC": round(put_oi, 1),
        }
    except Exception as e:
        return {"error": str(e)}


def ambil_berita_financial_juice(limit: int = 5) -> list:
    urls_fj = [
        "https://www.financialjuice.com/feed.ashx?xy=rss",
        "https://feeds.feedburner.com/financialjuice",
    ]
    headlines = []
    for url in urls_fj:
        try:
            feed = feedparser.parse(url)
            for entry in getattr(feed, "entries", [])[:limit]:
                judul = getattr(entry, "title", "").strip()
                if judul and judul not in headlines:
                    headlines.append(judul)
            if headlines:
                break
        except Exception:
            continue
    return headlines[:limit]


def ambil_data_institusi_dan_berita() -> dict:
    info = {"kripto_derivatif": {}, "stablecoin_flow": {}, "cftc_cot": {}, "berita": []}
    info["kalender_ff"] = ambil_kalender_ff(("USD",))
    info["makro_fred_detail"] = ambil_makro_fred()
    info["orderflow_ccxt"] = ambil_orderflow_ccxt("BTC/USDT")
    info["deribit_options"] = ambil_deribit_options_btc()
    for judul in ambil_berita_financial_juice(limit=5):
        if judul not in info["berita"]:
            info["berita"].append(judul)

    try:
        ex = ccxt.okx({"enableRateLimit": True, "options": {"defaultType": "swap"}})
        ticker = ex.fetch_ticker("BTC/USDT:USDT")
        funding = ex.fetch_funding_rate("BTC/USDT:USDT")
        oi = ex.fetch_open_interest("BTC/USDT:USDT")
        info["kripto_derivatif"] = {
            "BTC_last": ticker["last"],
            "funding_rate_pct": round(float(funding["fundingRate"]) * 100, 4),
            "open_interest_btc": round(float(oi.get("openInterestAmount", 0)), 2),
        }
    except Exception as e:
        info["kripto_derivatif"]["error"] = str(e)

    try:
        sc_data = requests.get("https://stablecoins.llama.fi/stablecoincharts/all", timeout=10).json()
        if len(sc_data) >= 8:
            mcap_now = float(sc_data[-1]["totalCirculatingUSD"]["peggedUSD"])
            mcap_7d = float(sc_data[-8]["totalCirculatingUSD"]["peggedUSD"])
            diff_b = (mcap_now - mcap_7d) / 1e9
            info["stablecoin_flow"] = {
                "total_stablecoin_B_USD": round(mcap_now / 1e9, 2),
                "perubahan_7hari_B_USD": round(diff_b, 2),
            }
    except Exception as e:
        info["stablecoin_flow"]["error"] = str(e)

    try:
        df_cot = cot.cot_year(year=datetime.now().year, cot_report_type="legacy_fut")
        gold_row = df_cot[df_cot["Market and Exchange Names"].str.contains("GOLD - COMMODITY EXCHANGE", na=False)].iloc[0]
        net_pos = int(gold_row["Noncommercial Positions-Long (All)"]) - int(
            gold_row["Noncommercial Positions-Short (All)"]
        )
        info["cftc_cot"]["GOLD_Net_HedgeFund_Contracts"] = net_pos
    except Exception as e:
        info["cftc_cot"]["error"] = str(e)

    for url in ["https://www.investing.com/rss/news_25.rss", "https://www.investing.com/rss/news_301.rss"]:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:4]:
                info["berita"].append(entry.title)
        except Exception:
            pass

    info["kalender_ff"] = ambil_kalender_ff(("USD",))
    info["makro_fred_detail"] = ambil_makro_fred()
    info["orderflow_ccxt"] = ambil_orderflow_ccxt("BTC/USDT")
    return info


def ambil_intelijen_blockworks():
    api_key = os.getenv("BLOCKWORKS_API_KEY", "").strip()
    if not api_key:
        return {"status": "nonaktif_tanpa_key", "catatan": "BLOCKWORKS_API_KEY kosong"}

    headers = {
        "x-blockworks-api-key": api_key,
        "Accept": "application/json",
    }
    kandidat_url = [
        ("https://api.blockworks.com/query/tabular/assets", {"pageSize": 5}),
        ("https://api.blockworks.com/v1/assets", None),
    ]

    for url, params in kandidat_url:
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "status": "aktif",
                    "endpoint": url,
                    "ringkasan": data if isinstance(data, (dict, list)) else str(data)[:300],
                }
            if resp.status_code != 401:
                return {
                    "status": f"http_{resp.status_code}",
                    "endpoint": url,
                    "catatan": resp.text[:200],
                }
        except Exception as e:
            return {"status": "error_koneksi", "endpoint": url, "catatan": str(e)}

    return {
        "status": "http_401",
        "catatan": "Periksa masa aktif/izin endpoint API key di Blockworks Intel",
    }
    
def ambil_data_coinmarketcap() -> dict:
    if not CMC_API_KEY:
        return {"status": "opsional_belum_diisi"}
    headers = {
        "X-CMC_PRO_API_KEY": CMC_API_KEY,
        "Accept": "application/json",
    }
    hasil = {"status": "aktif"}
    try:
        g_resp = requests.get(
            "https://pro-api.coinmarketcap.com/v1/global-metrics/quotes/latest",
            headers=headers,
            timeout=10,
        ).json()
        g_data = g_resp.get("data", {})
        usd_quote = g_data.get("quote", {}).get("USD", {})
        if usd_quote:
            hasil["global"] = {
                "total_mcap_B_USD": round(float(usd_quote.get("total_market_cap", 0)) / 1e9, 2),
                "vol_24h_B_USD": round(float(usd_quote.get("total_volume_24h", 0)) / 1e9, 2),
                "btc_dominance_pct": round(float(g_data.get("btc_dominance", 0)), 2),
                "mcap_change_24h_pct": round(float(usd_quote.get("total_market_cap_yesterday_percentage_change", 0)), 2),
            }

        q_resp = requests.get(
            "https://pro-api.coinmarketcap.com/v2/cryptocurrency/quotes/latest",
            headers=headers,
            params={"symbol": "BTC", "convert": "USD"},
            timeout=10,
        ).json()
        btc_list = q_resp.get("data", {}).get("BTC", [])
        if isinstance(btc_list, list) and btc_list:
            btc_usd = btc_list[0].get("quote", {}).get("USD", {})
            hasil["BTC_USD"] = {
                "price": round(float(btc_usd.get("price", 0)), 2),
                "percent_change_24h": round(float(btc_usd.get("percent_change_24h", 0)), 2),
            }
        return hasil
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def ambil_data_twelvedata_intraday() -> dict:
    if not TWELVEDATA_API_KEY:
        return {"status": "opsional_belum_diisi"}
    hasil = {"status": "aktif"}
    for label, sym in [("XAUUSD", "XAU/USD"), ("BTCUSD", "BTC/USD"), ("QQQ_NASDAQ", "QQQ")]:
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
                    "fifty_two_week_range": resp.get("fifty_two_week", {}).get("range"),
                }
            else:
                hasil[label] = {"error": resp.get("message", "gagal_ambil_quote")}
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
        bid = float(resp.get("bid", 0))
        ask = float(resp.get("ask", 0))
        if bid > 0 and ask > 0:
            return round((ask - bid) * 10, 2)
    except Exception:
        pass
    return 1.5


# ==============================================================================
# 7. LAPISAN 4: DEWAN JURI 4-AGEN GEMINI (JSON) & KONTROL TELEGRAM
# ==============================================================================
def bersihkan_teks_telegram(teks: str) -> str:
    if not teks:
        return ""

    teks_bersih = teks.strip()
    if teks_bersih.startswith("```"):
        lines = teks_bersih.splitlines()
        if len(lines) >= 2 and lines[-1].strip().startswith("```"):
            teks_bersih = "\n".join(lines[1:-1]).strip()

    try:
        data = json.loads(teks_bersih)
    except Exception:
        return teks

    if not isinstance(data, dict):
        return teks

    baris = []
    if data.get("greeting"):
        baris.append(str(data["greeting"]).strip())

    sesi = data.get("session_status")
    if isinstance(sesi, dict):
        ringkas_sesi = []
        if "total_scans" in sesi:
            ringkas_sesi.append(f"Scan: {sesi['total_scans']}")
        if "approved_signals" in sesi:
            ringkas_sesi.append(f"Disetujui: {sesi['approved_signals']}")
        if "rejected_signals" in sesi:
            ringkas_sesi.append(f"Ditolak: {sesi['rejected_signals']}")
        if "average_confirmation_score" in sesi:
            ringkas_sesi.append(f"Rata-rata Skor: {sesi['average_confirmation_score']}")
        if "asia_quota_remaining" in sesi:
            ringkas_sesi.append(f"Sisa Kuota Asia: {sesi['asia_quota_remaining']}")
        if ringkas_sesi:
            baris.append(" | ".join(ringkas_sesi))

        logs = sesi.get("last_logs")
        if isinstance(logs, list) and logs:
            baris.append("\nLog Terakhir:")
            for item in logs[-3:]:
                if isinstance(item, dict):
                    reg = item.get("region", "-")
                    pair = item.get("pair", "-")
                    xau_st = item.get("XAU_status", "-")
                    xau_sc = item.get("XAU_score", "-")
                    btc_st = item.get("BTC_status", "-")
                    btc_sc = item.get("BTC_score", "-")
                    spr = item.get("spread", "-")
                    baris.append(
                        f"• [{reg}] {pair} | XAU: {xau_st} ({xau_sc}) | BTC: {btc_st} ({btc_sc}) | Spread: {spr}"
                    )

    for key, label in [
        ("observations", "\nCatatan Utama:"),
        ("risk_considerations", "\nPertimbangan Risiko:"),
        ("recommendations", "\nRekomendasi:"),
    ]:
        val = data.get(key)
        if isinstance(val, list) and val:
            baris.append(label)
            for idx, poin in enumerate(val, 1):
                baris.append(f"{idx}. {str(poin).strip()}")

    if data.get("recommendation") and isinstance(data.get("recommendation"), str):
        baris.append(f"\nKesimpulan: {data['recommendation'].strip()}")

    return "\n".join(baris).strip() if baris else teks


def kirim_telegram(pesan: str, log_id: int = None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram belum dikonfigurasi:\n", pesan)
        return

    pesan_final = bersihkan_teks_telegram(pesan)
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    reply_markup = None
    if log_id is not None:
        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve (Simulasi)", "callback_data": f"approve_{log_id}"},
                    {"text": "❌ Reject (Abaikan)", "callback_data": f"reject_{log_id}"},
                ]
            ]
        }

    chunks = [pesan_final[i : i + 3900] for i in range(0, len(pesan_final), 3900)]
    for idx, chunk in enumerate(chunks):
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": chunk}
        if reply_markup and idx == len(chunks) - 1:
            payload["reply_markup"] = json.dumps(reply_markup)
        requests.post(url, json=payload, timeout=15)


def panggil_groq_fallback(prompt: str, system_prompt: str = "", model: str = "llama-3.3-70b-versatile") -> str:
    if not GROQ_API_KEY:
        return ""
    try:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": messages,
                "temperature": 0.2,
                "max_tokens": 2200,
            },
            timeout=20,
        )
        data = resp.json()
        konten = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return konten.strip()
    except Exception:
        return ""


def analisa_dengan_retry(client, models: list, prompt: str, config=None, retries: int = 2) -> str:
    if not client:
        return "GEMINI_API_KEY belum diisi."
    last_err = None
    for model_name in models:
        for attempt in range(retries + 1):
            try:
                res = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=config,
                )
                teks = getattr(res, "text", None)
                if teks and teks.strip():
                    return teks.strip()
            except Exception as e:
                last_err = e
                time.sleep(2 * (attempt + 1))
    return f"Maaf, semua percobaan model gagal ({str(last_err)[:120]})."


def jalankan_analisa_lengkap(pemicu: str = "Terjadwal 24/7") -> str:
    data_makro = ambil_statistik_dan_makro()
    data_institusi = ambil_data_institusi_dan_berita()
    blockworks_intel = ambil_intelijen_blockworks()
    cmc_data = ambil_data_coinmarketcap()
    twelvedata_quote = ambil_data_twelvedata_intraday()
    mtf_gold = cek_konfluensi_mtf_dan_regime("GC=F")
    mtf_btc = cek_konfluensi_mtf_dan_regime("BTC-USD")
    kalender_news = cek_kalender_berita_tier1_otomatis()
    spread_aktif = cek_spread_mt5_cloud("XAUUSD")

    xau_live_td = (
        twelvedata_quote.get("XAUUSD", {}).get("close")
        if isinstance(twelvedata_quote, dict) and isinstance(twelvedata_quote.get("XAUUSD"), dict)
        else None
    )
    xau_live_main = xau_live_td
    if not xau_live_main:
        try:
            fast_px = yf.Ticker("GC=F").fast_info.get("lastPrice")
            if fast_px is not None:
                xau_live_main = round(float(fast_px), 5)
        except Exception:
            xau_live_main = None

    btc_live = (
        mtf_btc.get("last_close") or mtf_btc.get("close_terakhir")
        if isinstance(mtf_btc, dict)
        else None
    )
    if not btc_live or btc_live == "TIDAK_TERSEDIA":
        try:
            btc_live = round(float(ccxt.okx({"enableRateLimit": True}).fetch_ticker("BTC/USDT:USDT")["last"]), 2)
        except Exception:
            try:
                btc_live = round(float(yf.Ticker("BTC-USD").fast_info["lastPrice"]), 2)
            except Exception:
                cmc_btc_px = (
                    cmc_data.get("BTC_USD", {}).get("price")
                    if isinstance(cmc_data, dict)
                    else None
                )
                btc_live = round(float(cmc_btc_px), 2) if cmc_btc_px else None

    live_prices_ctx = {
        "XAUUSD_live": xau_live_main or "TIDAK_TERSEDIA",
        "XAUUSD_sumber": "TwelveData" if xau_live_td else ("yfinance_GC=F" if xau_live_main else "TIDAK_TERSEDIA"),
        "BTCUSD_live": btc_live or "TIDAK_TERSEDIA",
    }

    risk_status = validasi_hardcoded_risk(
        balance=1000.0,
        sl_pips=30.0,
        pip_value=1.0,
        layers=1,
        spread_pips=spread_aktif,
        is_high_impact_news=data_institusi.get("kalender_ff", {}).get(
            "blokir_entry_news", kalender_news.get("is_high_impact_active", False)
        ),
        z_score_h1=mtf_gold.get("Z_score_H1_latest", 0.0),
    )

    macro_snap = json.dumps(data_makro.get("harga_terakhir", {}))

    if not GEMINI_API_KEY:
        laporan = (
            f"[MODE DATA MENTAH - {pemicu}]\n"
            f"Live Prices: {live_prices_ctx}\n"
            f"Risk: {risk_status}\n"
            f"MTF Gold: {mtf_gold}\n"
            f"Blockworks: {blockworks_intel}\n"
            f"TwelveData: {twelvedata_quote}"
        )
        log_id = simpan_log("XAUUSD/BTC", "WAIT", 0, spread_aktif, macro_snap, "NO_API_KEY", laporan[:400])
        kirim_telegram(laporan, log_id=log_id)
        return laporan

    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = f"""
Kamu adalah Dewan Juri 4-Agen Kuantitatif Institusional. Analisa data riil berikut dalam Bahasa Indonesia dengan format SINGKAT, BERSIH, dan MUDAH DIBACA di Telegram.
Pemicu: {pemicu} | Sesi: {deteksi_sesi_trading()} | Spread XAUUSD: {spread_aktif} pips

ATURAN DATA & SOP WAJIB:
- Harga rujukan utama HANYA memakai LIVE_PRICES: {live_prices_ctx}.
- DILARANG mengarang angka atau level harga yang tidak ada di data.
- Jika XAUUSD_live atau BTCUSD_live "TIDAK_TERSEDIA", wajib skor < 75 dan bias "WAIT_NO_TRADE".
- Setiap field teks (macro_view, bull_case, bear_skeptic_veto, final_verdict) WAJIB ditulis singkat (maksimal 2 poin pendek dipisah newline "\\n", tiap poin maksimal 15 kata, JANGAN menumpuk banyak angka dalam satu kalimat panjang, dan JANGAN mengulang daftar error yang sama berkali-kali).

Data:
1. Makro & Risiko Ekor: {data_makro}
2. MTF & ADX Regime: Gold={mtf_gold} | BTC={mtf_btc}
- CoinMarketCap (Global & BTC): {cmc_data}
3. Institusi, Derivatif & Stablecoin: {data_institusi}
4. Blockworks & TwelveData: {blockworks_intel} | {twelvedata_quote}
5. Kalender & Risk Engine: {kalender_news} | {risk_status}

Kembalikan HANYA JSON valid:
{{
  "xau_score": integer (0-100),
  "xau_bias": "BULLISH" | "BEARISH" | "WAIT_NO_TRADE",
  "btc_score": integer (0-100),
  "btc_bias": "BULLISH" | "BEARISH" | "WAIT_NO_TRADE",
  "macro_view": "- poin 1 singkat\\n- poin 2 singkat",
  "bull_case": "- poin 1 singkat\\n- poin 2 singkat",
  "bear_skeptic_veto": "- poin 1 singkat\\n- poin 2 singkat",
  "final_verdict": "- keputusan & batas risiko singkat"
}}
"""

    SYSTEM_INSTRUCTION_QUANT = """Kamu adalah mitra analisis kuantitatif, makro, dan evaluasi setup trading yang kritis, objektif, dan tidak asal mengiyakan.
Setiap kali menjawab atau menganalisis:
1. Audit Sumber & Kesegaran Data: Pisahkan secara jelas mana angka/fakta yang benar-benar berasal dari data/API/MCP/konteks yang diberikan vs mana yang belum tersedia, bersifat historis/lag, atau sedang di luar sesi aktif. Jangan mengarang harga live, berita terbaru, atau angka yang tidak ada.
2. Uji Asumsi & Cari Blind Spot: Bongkar asumsi utama dari tesis/setup pengguna, lalu cari celah kegagalan terkuat (counter-thesis / skeptic view), jebakan likuiditas (sweep/false breakout), kondisi pasar sideways/choppy, atau benturan korelasi antar aset.
3. Skenario, Invalidasi & Risiko: Selalu sertakan titik invalidasi yang jelas sebelum target, perhitungkan biaya transaksi/spread/slippage, dan hormati batas risiko (risiko per setup, batas layer, kuota sesi, serta rem darurat saat rilis berita berdampak tinggi).
4. Berani Menahan Eksekusi: Jika data kurang lengkap, konfluensi lemah, atau profil risiko tidak seimbang, nyatakan sikap WAIT / NO TRADE secara tegas.
5. Format Jelas & Terukur: Jawab lugas, terstruktur, dan fokus pada pengujian logika serta probabilitas."""

    top_score = 0
    bias_summary = "WAIT"
    lolos_final = False
    hasil_teks = ""
    try:
        raw_json = ""
        anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
        if anthropic_key:
            try:
                import anthropic
                claude_client = anthropic.Anthropic(api_key=anthropic_key)
                claude_res = claude_client.messages.create(
                    model="claude-opus-4-6",
                    max_tokens=20000,
                    system=SYSTEM_INSTRUCTION_QUANT,
                    thinking={"type": "enabled", "budget_tokens": 16000},
                    messages=[{"role": "user", "content": prompt}],
                )
                for block in claude_res.content:
                    if getattr(block, "type", "") == "text":
                        raw_json = block.text.strip()
                        break
            except Exception as claude_err:
                print("Fallback ke Gemini 4 Hydrogen karena Claude error:", claude_err)

        if not raw_json:
            response = client.models.generate_content(
                model="models/gemini-4-hydrogen",
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION_QUANT,
                    response_mime_type="application/json",
                    thinking_config=types.ThinkingConfig(thinking_level="XHIGH"),
                ),
            )
            raw_json = response.text.strip()

        if "{" in raw_json and "}" in raw_json:
            raw_json = raw_json[raw_json.find("{") : raw_json.rfind("}") + 1]
        verdict = json.loads(raw_json)
        xau_score = int(verdict.get("xau_score", 0))
        btc_score = int(verdict.get("btc_score", 0))
        top_score = max(xau_score, btc_score)
        bias_summary = f"XAU:{verdict.get('xau_bias')}({xau_score}) | BTC:{verdict.get('btc_bias')}({btc_score})"
        lolos_final = risk_status.get("approved") and (top_score >= 75)

        hasil_teks = (
            f"QUANT AGENT v2.3 — SIDANG 4 AGEN ({pemicu})\n"
            f"Sesi: {deteksi_sesi_trading()} | Spread XAU: {spread_aktif} pips\n"
            f"• XAUUSD ({live_prices_ctx['XAUUSD_live']}): {verdict.get('xau_bias')} (Skor: {xau_score}/100) | Regime: {mtf_gold.get('market_regime')}\n"
            f"• BTCUSDT ({live_prices_ctx['BTCUSD_live']}): {verdict.get('btc_bias')} (Skor: {btc_score}/100) | Regime: {mtf_btc.get('market_regime')}\n\n"
            f"1. Macro Analyst:\n{verdict.get('macro_view')}\n\n"
            f"2. Bull Case:\n{verdict.get('bull_case')}\n\n"
            f"3. Bear/Skeptic Veto:\n{verdict.get('bear_skeptic_veto')}\n\n"
            f"4. Risk Judge:\n{verdict.get('final_verdict')}\n\n"
            f"Status Guardrail: {'LOLOS (>=75)' if lolos_final else 'DITAHAN / WAIT'}"
        )
        log_id = simpan_log(
            "XAUUSD/BTC",
            bias_summary,
            top_score,
            spread_aktif,
            macro_snap,
            "PENDING_REVIEW" if lolos_final else "AUTO_WAIT",
            hasil_teks[:400],
        )
        kirim_telegram(hasil_teks, log_id=log_id if lolos_final else None)
        return hasil_teks
    except Exception as e:
        groq_raw = panggil_groq_fallback(
            prompt,
            system_prompt=SYSTEM_INSTRUCTION_QUANT,
        )
        if groq_raw:
            try:
                bersih = groq_raw.strip()
                if "{" in bersih and "}" in bersih:
                    bersih = bersih[bersih.find("{") : bersih.rfind("}") + 1]
                verdict = json.loads(bersih)
                xau_score = int(verdict.get("xau_score", 0))
                btc_score = int(verdict.get("btc_score", 0))
                top_score = max(xau_score, btc_score)
                bias_summary = f"XAU:{verdict.get('xau_bias')}({xau_score}) | BTC:{verdict.get('btc_bias')}({btc_score})"
                lolos_final = risk_status.get("approved") and (top_score >= 75)
                hasil_teks = (
                    f"QUANT AGENT v2.3 — SIDANG 4 AGEN [GROQ FALLBACK] ({pemicu})\n"
                    f"Sesi: {deteksi_sesi_trading()} | Spread XAU: {spread_aktif} pips\n"
                    f"• XAUUSD ({live_prices_ctx['XAUUSD_live']}): {verdict.get('xau_bias')} (Skor: {xau_score}/100) | Regime: {mtf_gold.get('market_regime')}\n"
                    f"• BTCUSDT ({live_prices_ctx['BTCUSD_live']}): {verdict.get('btc_bias')} (Skor: {btc_score}/100) | Regime: {mtf_btc.get('market_regime')}\n\n"
                    f"1. Macro Analyst:\n{verdict.get('macro_view')}\n\n"
                    f"2. Bull Case:\n{verdict.get('bull_case')}\n\n"
                    f"3. Bear/Skeptic Veto:\n{verdict.get('bear_skeptic_veto')}\n\n"
                    f"4. Risk Judge:\n{verdict.get('final_verdict')}\n\n"
                    f"Status Guardrail: {'LOLOS (>=75)' if lolos_final else 'DITAHAN / WAIT'}"
                )
                log_id = simpan_log(
                    "XAUUSD/BTC",
                    bias_summary,
                    top_score,
                    spread_aktif,
                    macro_snap,
                    "PENDING_REVIEW" if lolos_final else "AUTO_WAIT",
                    hasil_teks[:400],
                )
                kirim_telegram(hasil_teks, log_id=log_id if lolos_final else None)
                return hasil_teks
            except Exception:
                top_score = 0
                bias_summary = "GROQ_TEXT"
                lolos_final = False
                hasil_teks = (
                    f"QUANT AGENT v2.3 — SIDANG 4 AGEN [GROQ FALLBACK] ({pemicu})\n"
                    f"Sesi: {deteksi_sesi_trading()} | Spread XAU: {spread_aktif} pips\n\n"
                    f"{groq_raw}"
                )
                log_id = simpan_log(
                    "XAUUSD/BTC",
                    bias_summary,
                    top_score,
                    spread_aktif,
                    macro_snap,
                    "AUTO_WAIT",
                    hasil_teks[:400],
                )
                kirim_telegram(hasil_teks, log_id=None)
                return hasil_teks

    log_id = simpan_log(
        "XAUUSD/BTC",
        bias_summary,
        top_score,
        spread_aktif,
        macro_snap,
        "PENDING_REVIEW" if lolos_final else "AUTO_WAIT",
        hasil_teks[:400],
    )
    kirim_telegram(hasil_teks, log_id=log_id if lolos_final else None)
    return hasil_teks


# ==============================================================================
# 8. BACKGROUND WORKER: SCHEDULER 24/7, EVALUASI MINGGUAN & LISTENER TELEGRAM
# ==============================================================================
def loop_scheduler_otomatis():
    time.sleep(AUTO_SCAN_INTERVAL_SEC)
    while True:
        try:
            jalankan_analisa_lengkap(pemicu="Auto-Scan Cloud 24/7")
        except Exception as e:
            print("Error scheduler:", e)
        time.sleep(AUTO_SCAN_INTERVAL_SEC)


def loop_listener_telegram():
    if not TELEGRAM_BOT_TOKEN:
        print("Listener berhenti: TELEGRAM_BOT_TOKEN kosong.", flush=True)
        return
    offset = 0
    base_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    print("Listener Telegram aktif polling...", flush=True)
    kirim_telegram("Bot Quant Agent aktif & terhubung!")
    while True:
        try:
            resp = requests.get(
                f"{base_url}/getUpdates",
                params={"offset": offset, "timeout": 30},
                timeout=35,
            ).json()
            if not resp.get("ok"):
                err_code = resp.get("error_code")
                retry_after = int(resp.get("parameters", {}).get("retry_after", 10)) + 1
                print(f"getUpdates gagal ({err_code}): {resp.get('description')} -> tidur {retry_after}s", flush=True)
                time.sleep(retry_after if err_code == 429 else 5)
                continue

            for item in resp.get("result", []):
                offset = item["update_id"] + 1

                if "callback_query" in item:
                    cb = item["callback_query"]
                    cb_id = cb["id"]
                    data_cb = cb.get("data", "")
                    from_id = str(cb.get("from", {}).get("id", ""))
                    print(f"Callback dari {from_id}: {data_cb}")
                    if from_id == str(TELEGRAM_CHAT_ID) and "_" in data_cb:
                        aksi, id_str = data_cb.split("_", 1)
                        log_id = int(id_str)
                        if aksi == "approve":
                            update_keputusan_user(log_id, "APPROVED_SIMULATION")
                            requests.post(
                                f"{base_url}/answerCallbackQuery",
                                json={"callback_query_id": cb_id, "text": "Disetujui & dicatat ke jurnal!"},
                                timeout=10,
                            )
                            kirim_telegram(
                                f"✅ Sinyal #{log_id} DISETUJUI untuk simulasi. Kuota sesi {deteksi_sesi_trading()} diperbarui."
                            )
                        elif aksi == "reject":
                            update_keputusan_user(log_id, "REJECTED_BY_USER")
                            requests.post(
                                f"{base_url}/answerCallbackQuery",
                                json={"callback_query_id": cb_id, "text": "Ditolak & dicatat."},
                                timeout=10,
                            )
                            kirim_telegram(f"❌ Sinyal #{log_id} DITOLAK. Disiplin terjaga.")
                    continue

                msg = item.get("message", {})
                text = msg.get("text", "").strip().lower()
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if text:
                    print(f"Pesan masuk dari chat_id={chat_id} (target={TELEGRAM_CHAT_ID}): {text}")

                if chat_id == str(TELEGRAM_CHAT_ID):
                    if text in ["/start", "/help"]:
                        kirim_telegram(
                            "Quant Agent v2.3 (Institutional Multi-Asset 24/7) Aktif.\n"
                            "Perintah tersedia:\n"
                            "/cek - Jalankan sidang 4 agen, Blockworks/TwelveData & MTF sekarang\n"
                            "/backtest - Uji statistik backtesting.py & 1.000x Monte Carlo\n"
                            "/jurnal - Lihat jurnal audit SQLite & kuota sesi\n"
                            "/evaluasi - Lihat laporan evaluasi akurasi mingguan"
                        )
                    elif text in ["/cek", "/analisa"]:
                        kirim_telegram("AgentLang lagi cek data & analisa market ter update, tunggu bentar...")

                        def _run_manual_analisa():
                            try:
                                jalankan_analisa_lengkap(pemicu="Perintah Manual /cek dari HP")
                            except Exception as exc:
                                kirim_telegram(f"Error saat menjalankan /cek: {exc}")

                        threading.Thread(target=_run_manual_analisa, daemon=True).start()
                    elif text == "/backtest":
                        kirim_telegram("Menjalankan Backtest Out-of-Sample & 1.000x Monte Carlo...")
                        bt_gold = jalankan_backtest_dan_montecarlo("GC=F", "2y")
                        bt_btc = jalankan_backtest_dan_montecarlo("BTC-USD", "2y")
                        kirim_telegram(f"{bt_gold}\n\n{bt_btc}")
                    elif text in ["/jurnal", "/evaluasi"]:
                        kirim_telegram(ambil_ringkasan_jurnal_dan_evaluasi_mingguan())
                    else:
                        pesan_asli = msg.get("text", "").strip()
                        if pesan_asli:
                            kirim_telegram("Sedang meracik jawaban...")
                            try:
                                snap_db = ambil_ringkasan_jurnal_dan_evaluasi_mingguan()
                                prompt_chat = (
                                    f"Kamu adalah AgentLang, partner diskusi quant, makro, dan eksekusi trading di Telegram. "
                                    f"Bicara seperti manusia yang lugas, tenang, dan analitis—hindari gaya bahasa robotik, klise, atau template kaku, "
                                    f"dan JANGAN pakai tanda bintang (* atau **) karena Telegram menampilkan teks polos. "
                                    f"Saat menjawab, utamakan critical thinking: uji asumsi user, soroti potensi jebakan atau blind spot (seperti bias konfirmasi, "
                                    f"likuiditas palsu, risiko makro/berita, atau rasio risiko yang kurang masuk akal), dan beri perbandingan skenario jika relevan. "
                                    f"Jika ada data yang tidak ada di konteks, katakan terus terang tanpa mengarang. "
                                    f"Jika ditanya nama atau menyapa, perkenalkan diri secara wajar sebagai AgentLang.\n"
                                    f"Konteks jurnal & kuota sesi terakhir:\n{snap_db}\n\n"
                                    f"Pesan user: {pesan_asli}"
                                )
                                client_chat = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
                                balasan = analisa_dengan_retry(
                                    client_chat,
                                    ["gemini-2.5-flash"],
                                    prompt_chat,
                                    config=None,
                                )
                                if not balasan or balasan.startswith("Maaf, semua percobaan model gagal"):
                                    groq_balasan = panggil_groq_fallback(
                                        prompt_chat,
                                        system_prompt=(
                                            "Kamu adalah AgentLang, partner diskusi quant, makro, dan eksekusi trading di Telegram. "
                                            "Jawab lugas, tenang, kritis, dan tanpa tanda bintang (* atau **)."
                                        ),
                                    )
                                    if groq_balasan:
                                        balasan = groq_balasan
                                kirim_telegram(balasan)
                            except Exception as e:
                                kirim_telegram(f"Gagal balas chat: {str(e)[:150]}")
        except Exception as e:
            print("Listener error:", e)
            time.sleep(5)


# ==============================================================================
# 9. ENDPOINT WEB (HEALTH CHECK 24/7 & WEBHOOK TRADINGVIEW)
# ==============================================================================
@app.route("/", methods=["GET"])
@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({
        "status": "alive",
        "version": "2.3",
        "session": deteksi_sesi_trading(),
        "utc": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/webhook", methods=["POST"])
def tradingview_webhook():
    payload = request.get_json(silent=True) or {"raw": request.data.decode("utf-8", errors="ignore")}
    threading.Thread(
        target=jalankan_analisa_lengkap,
        args=(f"TradingView Alert: {payload}",),
        daemon=True,
    ).start()
    return jsonify({"received": True})


if __name__ == "__main__":
    init_db()
    threading.Thread(target=loop_scheduler_otomatis, daemon=True).start()
    threading.Thread(target=loop_listener_telegram, daemon=True).start()
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
