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
            if not df.empty:
                prices[nama] = df["Close"].squeeze()
        except Exception:
            pass

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
            hasil["fred_macro"]["REAL_YIELD_DFII10"] = round(float(fred.get_series("DFII10").iloc[-1]), 2)
            walcl = float(fred.get_series("WALCL").iloc[-1]) / 1000.0
            tga = float(fred.get_series("WTREGEN").iloc[-1])
            rrp = float(fred.get_series("RRPONTSYD").iloc[-1])
            hasil["fred_macro"]["FED_NET_LIQUIDITY_B_USD"] = round(walcl - tga - rrp, 2)
        except Exception as e:
            hasil["fred_macro"]["error"] = str(e)

    return hasil


def ambil_data_institusi_dan_berita() -> dict:
    info = {"kripto_derivatif": {}, "stablecoin_flow": {}, "cftc_cot": {}, "berita": []}

    # 1. Derivatif Kripto (Funding Rate + Open Interest)
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

    # 2. DeFiLlama Stablecoin Supply 7-Day Flow
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

    # 3. CFTC COT Report
    try:
        df_cot = cot.cot_year(year=datetime.now().year, cot_report_type="legacy_fut")
        gold_row = df_cot[df_cot["Market and Exchange Names"].str.contains("GOLD - COMMODITY EXCHANGE", na=False)].iloc[0]
        net_pos = int(gold_row["Noncommercial Positions-Long (All)"]) - int(
            gold_row["Noncommercial Positions-Short (All)"]
        )
        info["cftc_cot"]["GOLD_Net_HedgeFund_Contracts"] = net_pos
    except Exception as e:
        info["cftc_cot"]["error"] = str(e)

    # 4. Headline Berita Makro & Kripto
    for url in ["https://www.investing.com/rss/news_25.rss", "https://www.investing.com/rss/news_301.rss"]:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:4]:
                info["berita"].append(entry.title)
        except Exception:
            pass

    return info


def ambil_intelijen_blockworks() -> dict:
    """
    Menarik data intelijen pasar & fundamental kripto dari Blockworks Intel API
    (menggunakan kredit API gratis $100 dari app.blockworks.com/claim).
    """
    if not BLOCKWORKS_API_KEY:
        return {"status": "opsional_belum_diisi"}
    try:
        headers = {
            "Authorization": f"Bearer {BLOCKWORKS_API_KEY}",
            "x-api-key": BLOCKWORKS_API_KEY,
            "Accept": "application/json",
        }
        resp = requests.get(
            "https://api.blockworks.com/v1/assets",
            headers=headers,
            params={"limit": 5},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            items = data.get("data", data) if isinstance(data, dict) else data
            ringkasan = []
            if isinstance(items, list):
                for item in items[:5]:
                    if isinstance(item, dict):
                        ringkasan.append({
                            "symbol": item.get("symbol") or item.get("ticker"),
                            "name": item.get("name"),
                            "price": item.get("price"),
                            "mcap": item.get("marketCap") or item.get("market_cap"),
                            "vol_24h": item.get("volume24h") or item.get("volume_24h"),
                        })
            return {"status": "aktif", "top_assets": ringkasan or str(data)[:300]}
        return {"status": f"http_{resp.status_code}", "detail": resp.text[:150]}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def ambil_data_twelvedata_intraday() -> dict:
    """
    Menarik kuotasi intraday real-time XAU/USD dan indeks AS (QQQ) dari Twelve Data API
    untuk memperkuat konfirmasi multi-aset ala meja institusional.
    """
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
def kirim_telegram(pesan: str, log_id: int = None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram belum dikonfigurasi:\n", pesan)
        return
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

    chunks = [pesan[i : i + 3900] for i in range(0, len(pesan), 3900)]
    for idx, chunk in enumerate(chunks):
        payload = {"chat_id": str(TELEGRAM_CHAT_ID).strip(), "text": chunk}
        if reply_markup and idx == len(chunks) - 1:
            payload["reply_markup"] = json.dumps(reply_markup)
        for percobaan in range(3):
            try:
                r = requests.post(url, json=payload, timeout=30).json()
                if r.get("ok"):
                    print("sendMessage terkirim ke Telegram!", flush=True)
                    break
                err_code = r.get("error_code")
                if err_code == 429:
                    jeda = int(r.get("parameters", {}).get("retry_after", 10)) + 1
                    print(f"sendMessage kena 429 -> tunggu {jeda}s lalu kirim ulang...", flush=True)
                    time.sleep(jeda)
                    continue
                print(f"sendMessage gagal: {r}", flush=True)
                break
            except Exception as e:
                print(f"Error kirim_telegram (percobaan {percobaan + 1}): {e}", flush=True)
                time.sleep(3)


def jalankan_analisa_lengkap(pemicu: str = "Terjadwal 24/7") -> str:
    data_makro = ambil_statistik_dan_makro()
    data_institusi = ambil_data_institusi_dan_berita()
    blockworks_intel = ambil_intelijen_blockworks()
    twelvedata_quote = ambil_data_twelvedata_intraday()
    mtf_gold = cek_konfluensi_mtf_dan_regime("GC=F")
    mtf_btc = cek_konfluensi_mtf_dan_regime("BTC-USD")
    kalender_news = cek_kalender_berita_tier1_otomatis()
    spread_aktif = cek_spread_mt5_cloud("XAUUSD")

    risk_status = validasi_hardcoded_risk(
        balance=1000.0,
        sl_pips=30.0,
        pip_value=1.0,
        layers=1,
        spread_pips=spread_aktif,
        is_high_impact_news=kalender_news.get("is_high_impact_active", False),
        z_score_h1=mtf_gold.get("Z_score_H1_latest", 0.0),
    )

    macro_snap = json.dumps(data_makro.get("harga_terakhir", {}))

    if not GEMINI_API_KEY:
        laporan = (
            f"[MODE DATA MENTAH - {pemicu}]\n"
            f"Risk: {risk_status}\n"
            f"MTF Gold: {mtf_gold}\n"
            f"Blockworks: {blockworks_intel}\n"
            f"TwelveData: {twelvedata_quote}"
        )
        log_id = simpan_log("XAUUSD/BTC", "WAIT", 0, spread_aktif, macro_snap, "NO_API_KEY", laporan[:400])
        kirim_telegram(laporan, log_id=log_id)
        return laporan

    def analisa_dengan_retry(client, model_name: str, prompt: str, config=None, max_retries: int = 3):
        for percobaan in range(max_retries):
            try:
                return client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=config,
                )
            except Exception as e:
                pesan_error = str(e)
                if any(kode in pesan_error for kode in ["429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE"]):
                    waktu_tunggu = 15 * (percobaan + 1)
                    print(f"Server/kuota API sibuk, nunggu {waktu_tunggu} detik...")
                    time.sleep(waktu_tunggu)
                else:
                    raise e
        raise RuntimeError("Gagal analisa setelah 3x percobaan karena API sibuk/limit.")

    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = f"""
Kamu adalah Dewan Juri 4-Agen Kuantitatif Institusional. Analisa data riil berikut dalam Bahasa Indonesia:

Pemicu: {pemicu} | Sesi: {deteksi_sesi_trading()} | Spread XAUUSD: {spread_aktif} pips
1. Makro, Korelasi Log Return, GARCH & CVaR: {data_makro}
2. Multi-Timeframe & ADX Regime (Gold & BTC): {mtf_gold} | {mtf_btc}
3. Posisi Institusi (COT), Derivatif OI, & Stablecoin Flow: {data_institusi}
4. Intelijen Multi-Aset (Blockworks Intel & Twelve Data): {blockworks_intel} | {twelvedata_quote}
5. Kalender Berita Tier-1 & Hardcoded Risk Engine: {kalender_news} | {risk_status}

Jalankan sidang singkat 4 peran:
- Macro Analyst: Evaluasi Likuiditas Fed, DFII10, DXY, & COT.
- Bull Researcher: Argumen terkuat untuk BUY berdasarkan data di atas.
- Bear/Skeptic Researcher: Argumen terkuat untuk SELL atau VETO (celah kegagalan, choppy ADX < 20, atau korelasi bertabrakan).
- Risk Judge: Tentukan skor objektif 0-100 untuk XAUUSD dan BTCUSDT. Jika skor < 75 atau risk_status approved=False, wajib rekomendasikan WAIT_NO_TRADE.

Kembalikan HANYA format JSON valid dengan kunci:
{{
  "xau_score": integer (0-100),
  "xau_bias": "BULLISH" | "BEARISH" | "WAIT_NO_TRADE",
  "btc_score": integer (0-100),
  "btc_bias": "BULLISH" | "BEARISH" | "WAIT_NO_TRADE",
  "macro_view": "ringkasan 1 kalimat",
  "bull_case": "ringkasan 1 kalimat",
  "bear_skeptic_veto": "ringkasan 1 kalimat",
  "final_verdict": "kesimpulan singkat berbasis angka"
}}
"""
    try:
        response = analisa_dengan_retry(
            client,
            "gemini-3.8-flash",
            prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        verdict = json.loads(response.text)
        xau_score = int(verdict.get("xau_score", 0))
        btc_score = int(verdict.get("btc_score", 0))
        top_score = max(xau_score, btc_score)
        bias_summary = f"XAU:{verdict.get('xau_bias')}({xau_score}) | BTC:{verdict.get('btc_bias')}({btc_score})"

        lolos_final = risk_status.get("approved") and (top_score >= 75)
        hasil_teks = (
            f"QUANT AGENT v2.3 — SIDANG 4 AGEN ({pemicu})\n"
            f"Sesi: {deteksi_sesi_trading()} | Spread XAU: {spread_aktif} pips\n"
            f"• XAUUSD: {verdict.get('xau_bias')} (Skor: {xau_score}/100) | Regime: {mtf_gold.get('market_regime')}\n"
            f"• BTCUSDT: {verdict.get('btc_bias')} (Skor: {btc_score}/100) | Regime: {mtf_btc.get('market_regime')}\n\n"
            f"1. Macro Analyst: {verdict.get('macro_view')}\n"
            f"2. Bull Case: {verdict.get('bull_case')}\n"
            f"3. Bear/Skeptic Veto: {verdict.get('bear_skeptic_veto')}\n"
            f"4. Risk Judge: {verdict.get('final_verdict')}\n"
            f"Status Guardrail: {'LOLOS (>=75)' if lolos_final else 'DITAHAN / WAIT'}"
        )
    except Exception as e:
        top_score = 0
        bias_summary = "ERROR"
        lolos_final = False
        hasil_teks = f"Gagal memproses sidang AI: {str(e)}"

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
    time.sleep(15)
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
                        kirim_telegram("Menjalankan sidang 4 agen, cek MTF H1/H4/D1, Blockworks & Twelve Data...")
                        jalankan_analisa_lengkap(pemicu="Perintah Manual /cek dari HP")
                    elif text == "/backtest":
                        kirim_telegram("Menjalankan Backtest Out-of-Sample & 1.000x Monte Carlo...")
                        bt_gold = jalankan_backtest_dan_montecarlo("GC=F", "2y")
                        bt_btc = jalankan_backtest_dan_montecarlo("BTC-USD", "2y")
                        kirim_telegram(f"{bt_gold}\n\n{bt_btc}")
                    elif text in ["/jurnal", "/evaluasi"]:
                        kirim_telegram(ambil_ringkasan_jurnal_dan_evaluasi_mingguan())
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
