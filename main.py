import os
import json
import time
import threading
import sqlite3
from concurrent.futures import ThreadPoolExecutor
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
AUTO_SCAN_INTERVAL_SEC = int(os.getenv("AUTO_SCAN_INTERVAL_SEC", "14400"))

app = Flask(__name__)
DB_PATH = "quant_cloud_audit.db"
_COT_CACHE = {"tanggal": "", "nilai": None}
_LAST_MARKET_CONTEXT = {"updated_utc": "", "summary": "Belum ada scan pasar di sesi ini."}
_MODEL_CACHE = {"checked": False, "models": []}


# ==============================================================================
# 2. MODUL TRADING SKILLS (QUANT, BANK DESK, HEDGE FUND & MARKET MAKER)
# ==============================================================================
TRADING_SKILLS = {
    "MARKET_MAKER_ORDERFLOW": """
[SKILL: MARKET MAKER LIQUIDITY SWEEP & ORDERFLOW ABSORPTION]
- Bank besar & Market Maker menyapu likuiditas lawan arah sebelum menggerakkan harga: cari sweep di atas PDH/PWH/Asia High untuk distribusi short, atau di bawah PDL/PWL/Asia Low untuk akumulasi long.
- Validasi breakout/sweep dengan Cumulative Volume Delta (CVD) H1/M15 & Orderbook Imbalance:
  * Harga naik menembus high tetapi CVD melemah/negatif = Bearish Absorption (bull trap).
  * Harga turun menembus low tetapi CVD menguat/positif = Bullish Absorption (akumulasi institusi).
- Peta nilai wajar VWAP: hindari FOMO buy di Extreme Premium (> +1.5 SD) atau FOMO sell di Extreme Discount (< -1.5 SD).
""",
    "SESSION_ICT_LIQUIDITY": """
[SKILL: SESSION & ICT LIQUIDITY MODEL (ASIA / LONDON / NEW YORK)]
- ASIA (00:00-07:00 UTC): Petakan Asia High & Asia Low sebagai kolam likuiditas awal. Hindari mengejar breakout tipis di tengah sesi Asia tanpa katalis makro Asia.
- LONDON (07:00-13:00 UTC): Fokus pada Judas Swing / liquidity sweep terhadap Asia High atau Asia Low, diikuti displacement M5-M15 yang searah bias H1-H4.
- NEW_YORK (13:00-21:00 UTC): Evaluasi kelanjutan tren London atau NY Reversal setelah rilis data makro AS (13:30/14:00 UTC).
""",
    "INTRADAY_EXECUTION": """
[SKILL: INTRADAY & SCALP PRECISION (M1-H4)]
- Kompas arah: H4 & H1; struktur setup: M15; pemicu entry presisi: M5 & M1.
- Cek spread aktual, jarak ke VWAP, dan berita USD Tier-1 (±45 menit). Tetapkan SL di balik struktur swing/sweep dengan minimal R:R 1:2.
""",
    "SWING_HEDGE_FUND": """
[SKILL: HEDGE FUND SWING TRADING (H4-W1, BEBERAPA HARI - MINGGU)]
- Fokus pada struktur W1 & D1 serta area akumulasi/distribusi H4 (abaikan noise M1/M5).
- Gabungkan volatilitas GARCH, positioning CFTC COT (hedge fund net), serta untuk kripto wajib audit Funding Rate, Open Interest, dan Deribit DVOL / Put-Call OI Ratio.
""",
    "MACRO_POSITION_HOLD": """
[SKILL: INSTITUTIONAL MACRO & LONG-TERM HOLD (BULANAN - TAHUNAN)]
- Fokus pada MN1 (Bulanan), W1 (Mingguan), dan D1 (Harian). Fluktuasi M1-H1 tidak membatalkan tesis jangka panjang.
- Pilar fundamental wajib: arah US 10Y Real Yield (DFII10), tren Fed Net Liquidity (WALCL - TGA - RRP), siklus Fed Funds, indeks DXY, serta aliran Stablecoin 7 hari & kapitalisasi pasar global.
- Gunakan akumulasi bertahap (DCA/scaling pada zona diskon W1/MN1) dengan kontrol risiko ekor CVaR 95%.
""",
}


def pilih_skills_konteks(mode_fokus: str = "ALL", sesi_target: str = "") -> str:
    mode = (mode_fokus or "ALL").upper()
    sesi = (sesi_target or deteksi_sesi_trading()).upper()
    terpilih = [TRADING_SKILLS["MARKET_MAKER_ORDERFLOW"]]

    if mode in ("ALL", "SESSION", "INTRADAY") or sesi in ("ASIA", "LONDON", "NEW_YORK"):
        terpilih.append(TRADING_SKILLS["SESSION_ICT_LIQUIDITY"])
    if mode in ("ALL", "INTRADAY"):
        terpilih.append(TRADING_SKILLS["INTRADAY_EXECUTION"])
    if mode in ("ALL", "SWING"):
        terpilih.append(TRADING_SKILLS["SWING_HEDGE_FUND"])
    if mode in ("ALL", "HOLD", "POSITION"):
        terpilih.append(TRADING_SKILLS["MACRO_POSITION_HOLD"])

    return "\n".join(terpilih).strip()


# ==============================================================================
# 3. LAPISAN 2: DATABASE SQLITE, PELACAK SESI & EVALUASI AKURASI MINGGUAN
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
# 4. MODUL KALENDER TIER-1, MTF (M1-MN1), ORDERFLOW CVD & MARKET MAKER SWEEP
# ==============================================================================
def cek_kalender_berita_tier1_otomatis() -> dict:
    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=8,
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


def _arah_ema(seri_close: pd.Series, fast: int = 20, slow: int = 50) -> str:
    bersih = seri_close.dropna()
    if len(bersih) < slow:
        return "NETRAL"
    ema_fast = bersih.ewm(span=fast, adjust=False).mean().iloc[-1]
    ema_slow = bersih.ewm(span=slow, adjust=False).mean().iloc[-1]
    return "BULLISH" if ema_fast > ema_slow else "BEARISH"


def _hitung_rsi(seri_close: pd.Series, period: int = 14) -> float:
    bersih = seri_close.dropna()
    if len(bersih) <= period:
        return 50.0
    delta = bersih.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    val = rsi.iloc[-1]
    return round(float(val), 1) if pd.notna(val) else 50.0


def _hitung_rentang_sesi_hari_ini(df_h1: pd.DataFrame) -> dict:
    try:
        if df_h1.empty:
            return {}
        idx_utc = df_h1.index.tz_convert("UTC") if df_h1.index.tz is not None else df_h1.index.tz_localize("UTC")
        df_utc = df_h1.copy()
        df_utc.index = idx_utc
        hari_ini = df_utc.index[-1].date()
        hari_df = df_utc[df_utc.index.date == hari_ini]
        if hari_df.empty:
            return {}

        sesi_ranges = {}
        for nama, (jam_mulai, jam_selesai) in [
            ("ASIA", (0, 7)),
            ("LONDON", (7, 13)),
            ("NEW_YORK", (13, 21)),
        ]:
            sub = hari_df[(hari_df.index.hour >= jam_mulai) & (hari_df.index.hour < jam_selesai)]
            if not sub.empty:
                sesi_ranges[nama] = {
                    "high": round(float(sub["High"].max()), 2),
                    "low": round(float(sub["Low"].min()), 2),
                }
        return sesi_ranges
    except Exception:
        return {}


def analisa_orderflow_dan_likuiditas(
    df_m15: pd.DataFrame,
    df_h1: pd.DataFrame,
    df_d1: pd.DataFrame,
    session_ranges: dict,
) -> dict:
    try:
        if df_h1.empty or len(df_h1) < 30 or df_d1.empty or len(df_d1) < 10:
            return {"status": "data_tidak_cukup"}

        def _hitung_cvd_dan_divergensi(df_in: pd.DataFrame, lookback: int = 24) -> dict:
            sub = df_in.tail(max(lookback + 5, 30)).copy()
            rng = (sub["High"] - sub["Low"]).replace(0, np.nan)
            clv = (((sub["Close"] - sub["Low"]) - (sub["High"] - sub["Close"])) / rng).fillna(0.0)
            vol = sub["Volume"].fillna(0.0).replace(0.0, 1.0) if "Volume" in sub.columns else pd.Series(1.0, index=sub.index)
            bar_delta = clv * vol
            cvd = bar_delta.cumsum()

            recent = sub.tail(lookback)
            cvd_recent = cvd.tail(lookback)
            half = lookback // 2

            p_first_high = float(recent["High"].iloc[:half].max())
            p_second_high = float(recent["High"].iloc[half:].max())
            c_first_high = float(cvd_recent.iloc[:half].max())
            c_second_high = float(cvd_recent.iloc[half:].max())

            p_first_low = float(recent["Low"].iloc[:half].min())
            p_second_low = float(recent["Low"].iloc[half:].min())
            c_first_low = float(cvd_recent.iloc[:half].min())
            c_second_low = float(cvd_recent.iloc[half:].min())

            net_delta = float(bar_delta.tail(lookback).sum())
            total_vol = float(vol.tail(lookback).sum())
            delta_ratio_pct = round((net_delta / total_vol) * 100, 1) if total_vol > 0 else 0.0

            divergensi = "ALIGNED_FLOW"
            if p_second_high > p_first_high and c_second_high < c_first_high:
                divergensi = "BEARISH_ABSORPTION"
            elif p_second_low < p_first_low and c_second_low > c_first_low:
                divergensi = "BULLISH_ABSORPTION"

            return {
                "net_delta_pct": delta_ratio_pct,
                "flow_bias": "BUY_PRESSURE" if delta_ratio_pct > 5 else ("SELL_PRESSURE" if delta_ratio_pct < -5 else "NEUTRAL"),
                "absorption": divergensi,
            }

        of_h1 = _hitung_cvd_dan_divergensi(df_h1, 24)
        of_m15 = _hitung_cvd_dan_divergensi(df_m15, 32) if not df_m15.empty and len(df_m15) >= 32 else {}

        vwap_df = df_h1.tail(24).copy()
        tp = (vwap_df["High"] + vwap_df["Low"] + vwap_df["Close"]) / 3.0
        vol_v = vwap_df["Volume"].fillna(0.0).replace(0.0, 1.0) if "Volume" in vwap_df.columns else pd.Series(1.0, index=vwap_df.index)
        vwap_val = float((tp * vol_v).sum() / vol_v.sum())
        vwap_std = float(tp.std()) if pd.notna(tp.std()) and tp.std() > 0 else 1.0
        last_close = float(df_h1["Close"].iloc[-1])
        vwap_z = round((last_close - vwap_val) / vwap_std, 2)

        if vwap_z > 1.5:
            zona_nilai = "EXTREME_PREMIUM"
        elif vwap_z > 0.3:
            zona_nilai = "PREMIUM"
        elif vwap_z < -1.5:
            zona_nilai = "EXTREME_DISCOUNT"
        elif vwap_z < -0.3:
            zona_nilai = "DISCOUNT"
        else:
            zona_nilai = "EQUILIBRIUM"

        pdh = round(float(df_d1["High"].iloc[-2]), 2)
        pdl = round(float(df_d1["Low"].iloc[-2]), 2)
        pwh = round(float(df_d1["High"].iloc[-7:-1].max()), 2)
        pwl = round(float(df_d1["Low"].iloc[-7:-1].min()), 2)

        recent_h1 = df_h1.tail(6)
        recent_high = float(recent_h1["High"].max())
        recent_low = float(recent_h1["Low"].min())

        sweeps_terdeteksi = []
        if recent_high > pdh and last_close < pdh:
            sweeps_terdeteksi.append(f"SWEEP_PDH_BEARISH({pdh})")
        if recent_low < pdl and last_close > pdl:
            sweeps_terdeteksi.append(f"SWEEP_PDL_BULLISH({pdl})")
        if recent_high > pwh and last_close < pwh:
            sweeps_terdeteksi.append(f"SWEEP_PWH_BEARISH({pwh})")
        if recent_low < pwl and last_close > pwl:
            sweeps_terdeteksi.append(f"SWEEP_PWL_BULLISH({pwl})")

        asia_rng = session_ranges.get("ASIA", {})
        if asia_rng:
            a_high = float(asia_rng.get("high", 0))
            a_low = float(asia_rng.get("low", 0))
            if a_high > 0 and recent_high > a_high and last_close < a_high:
                sweeps_terdeteksi.append(f"SWEEP_ASIA_HIGH_BEARISH({a_high})")
            if a_low > 0 and recent_low < a_low and last_close > a_low:
                sweeps_terdeteksi.append(f"SWEEP_ASIA_LOW_BULLISH({a_low})")

        return {
            "H1_of": of_h1,
            "M15_of": of_m15,
            "VWAP_24h": round(vwap_val, 2),
            "VWAP_z": vwap_z,
            "zone": zona_nilai,
            "levels": {"PDH": pdh, "PDL": pdl, "PWH": pwh, "PWL": pwl},
            "sweeps": sweeps_terdeteksi if sweeps_terdeteksi else ["NONE"],
        }
    except Exception as e:
        return {"error": str(e)}


def cek_konfluensi_mtf_dan_regime(ticker: str = "GC=F") -> dict:
    try:
        df_m1 = yf.download(ticker, period="2d", interval="1m", progress=False)
        df_m5 = yf.download(ticker, period="5d", interval="5m", progress=False)
        df_m15 = yf.download(ticker, period="5d", interval="15m", progress=False)
        df_h1 = yf.download(ticker, period="30d", interval="1h", progress=False)
        df_d1 = yf.download(ticker, period="10y", interval="1d", progress=False)
        if df_h1.empty or df_d1.empty:
            return {"error": f"Data MTF {ticker} tidak tersedia."}

        for df_item in (df_m1, df_m5, df_m15, df_h1, df_d1):
            if not df_item.empty and isinstance(df_item.columns, pd.MultiIndex):
                df_item.columns = df_item.columns.get_level_values(0)

        m1_trend = _arah_ema(df_m1["Close"], 20, 50) if not df_m1.empty else "NETRAL"
        m5_trend = _arah_ema(df_m5["Close"], 20, 50) if not df_m5.empty else "NETRAL"
        m15_trend = _arah_ema(df_m15["Close"], 20, 50) if not df_m15.empty else "NETRAL"

        h1_close = df_h1["Close"]
        last_close_val = round(float(h1_close.dropna().iloc[-1]), 2)
        h1_trend = _arah_ema(h1_close, 20, 50)

        df_h4 = df_h1.resample("4h").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
        h4_trend = _arah_ema(df_h4["Close"], 20, 50)

        d1_trend = _arah_ema(df_d1["Close"], 20, 50)

        df_w1 = df_d1.resample("1W-MON").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
        w1_trend = _arah_ema(df_w1["Close"], 10, 20) if len(df_w1) >= 20 else d1_trend

        try:
            df_mn1 = df_d1.resample("ME").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
        except ValueError:
            df_mn1 = df_d1.resample("M").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
        mn1_trend = _arah_ema(df_mn1["Close"], 6, 12) if len(df_mn1) >= 12 else w1_trend

        high, low, close = df_d1["High"], df_d1["Low"], df_d1["Close"]
        plus_dm = high.diff().clip(lower=0)
        minus_dm = (-low.diff()).clip(lower=0)
        tr_d1 = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
        atr_d1 = tr_d1.rolling(14).mean()
        plus_di = 100 * (plus_dm.rolling(14).mean() / atr_d1)
        minus_di = 100 * (minus_dm.rolling(14).mean() / atr_d1)
        dx = (abs(plus_di - minus_di) / (plus_di + minus_di)) * 100
        adx_val = round(float(dx.rolling(14).mean().iloc[-1]), 1)

        tr_h1 = pd.concat(
            [df_h1["High"] - df_h1["Low"], (df_h1["High"] - h1_close.shift()).abs(), (df_h1["Low"] - h1_close.shift()).abs()],
            axis=1,
        ).max(axis=1)
        atr_h1_pct = round(float((tr_h1.rolling(14).mean().iloc[-1] / h1_close.iloc[-1]) * 100), 3)
        rsi_h1 = _hitung_rsi(h1_close, 14)
        rsi_d1 = _hitung_rsi(df_d1["Close"], 14)

        h1_ret = np.log(h1_close / h1_close.shift(1)).dropna()
        z_score_h1 = round(float((h1_ret.iloc[-1] - h1_ret.mean()) / h1_ret.std()), 2)

        regime = "TRENDING" if adx_val >= 22 else ("TRANSITION" if adx_val >= 18 else "CHOPPY_SIDEWAYS")

        session_ranges = _hitung_rentang_sesi_hari_ini(df_h1)
        orderflow_mm = analisa_orderflow_dan_likuiditas(df_m15, df_h1, df_d1, session_ranges)

        def _skor_grup(tfs: list, arah_ref: str) -> int:
            if arah_ref not in ("BULLISH", "BEARISH"):
                return 35
            cocok = sum(1 for x in tfs if x == arah_ref)
            return round((cocok / len(tfs)) * 100)

        arah_intraday = h1_trend if h1_trend in ("BULLISH", "BEARISH") else m15_trend
        skor_intraday = _skor_grup([m1_trend, m5_trend, m15_trend, h1_trend, h4_trend], arah_intraday)

        arah_swing = h4_trend if h4_trend in ("BULLISH", "BEARISH") else d1_trend
        skor_swing = _skor_grup([h1_trend, h4_trend, d1_trend, w1_trend], arah_swing)

        arah_hold = d1_trend if d1_trend in ("BULLISH", "BEARISH") else w1_trend
        skor_hold = _skor_grup([d1_trend, w1_trend, mn1_trend], arah_hold)

        confluence_score = round((skor_intraday * 0.35) + (skor_swing * 0.35) + (skor_hold * 0.30))

        return {
            "ticker": ticker,
            "last_close": last_close_val,
            "M1": m1_trend,
            "M5": m5_trend,
            "M15": m15_trend,
            "H1": h1_trend,
            "H4": h4_trend,
            "D1": d1_trend,
            "W1": w1_trend,
            "MN1": mn1_trend,
            "session_ranges_utc": session_ranges,
            "orderflow_liq": orderflow_mm,
            "intraday": {"bias": arah_intraday, "score": skor_intraday},
            "swing": {"bias": arah_swing, "score": skor_swing},
            "hold": {"bias": arah_hold, "score": skor_hold},
            "ADX_14": adx_val,
            "RSI_H1": rsi_h1,
            "RSI_D1": rsi_d1,
            "ATR_H1_pct": atr_h1_pct,
            "regime": regime,
            "Z_score_H1": z_score_h1,
            "confluence_score": confluence_score,
        }
    except Exception as e:
        return {"error": str(e)}


def audit_high_probability_setup(
    aset_label: str,
    mtf: dict,
    data_makro: dict,
    data_institusi: dict,
    spread_pips: float,
    is_news_blocked: bool,
) -> dict:
    red_flags = []
    edge_confirmations = []

    if not isinstance(mtf, dict) or "error" in mtf:
        return {
            "asset": aset_label,
            "max_allowed_score": 40,
            "red_flags": ["Data struktur & orderflow tidak lengkap"],
            "edge_confirmations": [],
        }

    regime = mtf.get("regime", "CHOPPY_SIDEWAYS")
    adx = mtf.get("ADX_14", 0)
    if regime == "CHOPPY_SIDEWAYS":
        red_flags.append(f"Regime CHOPPY_SIDEWAYS (ADX {adx} < 18)")
    else:
        edge_confirmations.append(f"Regime {regime} (ADX {adx})")

    of_liq = mtf.get("orderflow_liq", {})
    h1_of = of_liq.get("H1_of", {})
    flow_bias = h1_of.get("flow_bias", "NEUTRAL")
    abs_sig = h1_of.get("absorption", "ALIGNED_FLOW")
    val_zone = of_liq.get("zone", "EQUILIBRIUM")
    sweeps = of_liq.get("sweeps", [])

    intraday_bias = mtf.get("intraday", {}).get("bias", "NETRAL")
    if intraday_bias == "BULLISH" and flow_bias == "SELL_PRESSURE":
        red_flags.append("Divergensi CVD: H1 bullish tapi delta H1 negatif (distribusi)")
    elif intraday_bias == "BEARISH" and flow_bias == "BUY_PRESSURE":
        red_flags.append("Divergensi CVD: H1 bearish tapi delta H1 positif (akumulasi)")
    elif flow_bias != "NEUTRAL":
        edge_confirmations.append(f"CVD H1 selaras ({flow_bias}, {h1_of.get('net_delta_pct')}%)")

    if abs_sig != "ALIGNED_FLOW":
        edge_confirmations.append(f"Absorpsi: {abs_sig}")

    if intraday_bias == "BULLISH" and val_zone == "EXTREME_PREMIUM":
        red_flags.append(f"Overextended di {val_zone} (VWAP Z={of_liq.get('VWAP_z')})")
    elif intraday_bias == "BEARISH" and val_zone == "EXTREME_DISCOUNT":
        red_flags.append(f"Overextended di {val_zone} (VWAP Z={of_liq.get('VWAP_z')})")
    else:
        edge_confirmations.append(f"Zona VWAP: {val_zone} (Z={of_liq.get('VWAP_z')})")

    if sweeps and sweeps != ["NONE"]:
        edge_confirmations.append(f"Liquidity Sweep: {', '.join(sweeps)}")

    if aset_label == "XAUUSD":
        if spread_pips > MAX_SPREAD_PIPS:
            red_flags.append(f"Spread lebar ({spread_pips} pips)")
        cot_net = data_institusi.get("cftc_cot", {}).get("GOLD_Net_HedgeFund_Contracts")
        if isinstance(cot_net, int):
            edge_confirmations.append(f"CFTC COT Net Gold: {cot_net:,}")

    if aset_label == "BTCUSDT":
        deriv = data_institusi.get("kripto_derivatif", {})
        fr = deriv.get("funding_rate_pct")
        if isinstance(fr, (int, float)):
            if fr > 0.03 and intraday_bias == "BULLISH":
                red_flags.append(f"Crowded Long (Funding {fr}%)")
            elif fr < -0.02 and intraday_bias == "BEARISH":
                red_flags.append(f"Crowded Short (Funding {fr}%)")

    if is_news_blocked:
        red_flags.append("Circuit Breaker Berita USD Tier-1 aktif (±45m)")

    max_cap = 100
    if len(red_flags) >= 2 or is_news_blocked:
        max_cap = 70
    elif len(red_flags) == 1:
        max_cap = 82

    return {
        "asset": aset_label,
        "max_allowed_score": max_cap,
        "red_flags": red_flags if red_flags else ["Clean"],
        "edge_confirmations": edge_confirmations,
    }


# ==============================================================================
# 5. LAPISAN 3: HARDCODED RISK ENGINE & CIRCUIT BREAKER
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
    if abs(z_score_h1) >= 2.2:
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
# 6. LAPISAN 1: MESIN BACKTESTING (backtesting.py) & SIMULASI MONTE CARLO
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
# 7. MODUL PENARIK DATA KUANTITATIF, MAKRO, INSTITUSI & INTELIJEN MULTI-ASET
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
            df = yf.download(kode, period="1y", interval="1d", progress=False)
            if not df.empty and "Close" in df:
                s = df["Close"].squeeze().dropna()
                if not s.empty:
                    prices[nama] = s
        except Exception:
            pass

    hasil = {"harga_terakhir": {}, "korelasi_gold_btc": {}, "garch_risk": {}, "fred_macro": {}}

    if not prices.empty:
        for col in prices.columns:
            valid_vals = prices[col].dropna()
            if not valid_vals.empty:
                hasil["harga_terakhir"][col] = round(float(valid_vals.iloc[-1]), 2)
        log_ret = np.log(prices / prices.shift(1)).dropna(how="all")
        corr_mat = log_ret.corr().round(3)
        for aset in ["GOLD", "BTC"]:
            if aset in corr_mat.columns:
                hasil["korelasi_gold_btc"][aset] = corr_mat[aset].to_dict()

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
                    "vol_besok_pct": round(pred_vol, 3),
                    "CVaR_95_pct": round(cvar_95, 3),
                }

    if FRED_API_KEY:
        try:
            fred = Fred(api_key=FRED_API_KEY)
            hasil["fred_macro"]["FEDFUNDS"] = round(float(fred.get_series("FEDFUNDS").iloc[-1]), 2)
            hasil["fred_macro"]["REAL_YIELD_DFII10"] = round(float(fred.get_series("DFII10").iloc[-1]), 2)
            walcl = float(fred.get_series("WALCL").iloc[-1]) / 1000.0
            tga = float(fred.get_series("WTREGEN").iloc[-1]) / 1000.0
            rrp = float(fred.get_series("RRPONTSYD").iloc[-1])
            hasil["fred_macro"]["FED_NET_LIQ_B"] = round(walcl - tga - rrp, 2)
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
            timeout=8,
        ).json()
        dvol_data = dvol_resp.get("result", {}).get("data", [])
        dvol_terakhir = round(float(dvol_data[-1][4]), 2) if dvol_data else None

        books_resp = requests.get(
            "https://www.deribit.com/api/v2/public/get_book_summary_by_currency",
            params={"currency": "BTC", "kind": "option"},
            timeout=8,
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
            "Put_Call_OI": put_call_ratio,
        }
    except Exception as e:
        return {"error": str(e)}


def ambil_berita_financial_juice(limit: int = 5) -> list:
    urls_fj = [
        "https://www.financialjuice.com/feed.ashx?xy=rss",
        "https://feeds.feedburner.com/financialjuice",
    ]
    headlines = []
    headers = {"User-Agent": "Mozilla/5.0"}
    for url in urls_fj:
        try:
            resp = requests.get(url, headers=headers, timeout=6)
            if resp.status_code != 200:
                continue
            feed = feedparser.parse(resp.content)
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
        ex = ccxt.okx({"enableRateLimit": True, "timeout": 8000, "options": {"defaultType": "swap"}})
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
        sc_data = requests.get("https://stablecoins.llama.fi/stablecoincharts/all", timeout=8).json()
        if len(sc_data) >= 8:
            mcap_now = float(sc_data[-1]["totalCirculatingUSD"]["peggedUSD"])
            mcap_7d = float(sc_data[-8]["totalCirculatingUSD"]["peggedUSD"])
            diff_b = (mcap_now - mcap_7d) / 1e9
            info["stablecoin_flow"] = {
                "total_B_USD": round(mcap_now / 1e9, 2),
                "change_7d_B": round(diff_b, 2),
            }
    except Exception as e:
        info["stablecoin_flow"]["error"] = str(e)

    hari_ini = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _COT_CACHE["tanggal"] == hari_ini and _COT_CACHE["nilai"] is not None:
        info["cftc_cot"]["GOLD_Net_HedgeFund_Contracts"] = _COT_CACHE["nilai"]
    else:
        try:
            df_cot = cot.cot_year(year=datetime.now().year, cot_report_type="legacy_fut")
            gold_row = df_cot[df_cot["Market and Exchange Names"].str.contains("GOLD - COMMODITY EXCHANGE", na=False)].iloc[0]
            net_pos = int(gold_row["Noncommercial Positions-Long (All)"]) - int(
                gold_row["Noncommercial Positions-Short (All)"]
            )
            _COT_CACHE["tanggal"] = hari_ini
            _COT_CACHE["nilai"] = net_pos
            info["cftc_cot"]["GOLD_Net_HedgeFund_Contracts"] = net_pos
        except Exception as e:
            info["cftc_cot"]["error"] = str(e)

    for url in ["https://www.investing.com/rss/news_25.rss", "https://www.investing.com/rss/news_301.rss"]:
        try:
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=6)
            if resp.status_code == 200:
                feed = feedparser.parse(resp.content)
                for entry in feed.entries[:4]:
                    if entry.title not in info["berita"]:
                        info["berita"].append(entry.title)
        except Exception:
            pass

    return info


def ambil_intelijen_blockworks() -> dict:
    api_key = os.getenv("BLOCKWORKS_API_KEY", "").strip()
    if not api_key:
        return {"status": "nonaktif_tanpa_key"}

    headers = {"x-blockworks-api-key": api_key, "Accept": "application/json"}
    kandidat_url = [
        ("https://api.blockworks.com/query/tabular/assets", {"pageSize": 5}),
        ("https://api.blockworks.com/v1/assets", None),
    ]

    for url, params in kandidat_url:
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                return {"status": "aktif", "ringkasan": str(data)[:350]}
            if resp.status_code != 401:
                return {"status": f"http_{resp.status_code}"}
        except Exception as e:
            return {"status": "error", "detail": str(e)}

    return {"status": "http_401"}


def ambil_data_coinmarketcap() -> dict:
    if not CMC_API_KEY:
        return {"status": "opsional_belum_diisi"}
    headers = {"X-CMC_PRO_API_KEY": CMC_API_KEY, "Accept": "application/json"}
    hasil = {"status": "aktif"}
    try:
        g_resp = requests.get(
            "https://pro-api.coinmarketcap.com/v1/global-metrics/quotes/latest",
            headers=headers,
            timeout=8,
        ).json()
        g_data = g_resp.get("data", {})
        usd_quote = g_data.get("quote", {}).get("USD", {})
        if usd_quote:
            hasil["global"] = {
                "mcap_B": round(float(usd_quote.get("total_market_cap", 0)) / 1e9, 2),
                "btc_dom_pct": round(float(g_data.get("btc_dominance", 0)), 2),
                "chg_24h_pct": round(float(usd_quote.get("total_market_cap_yesterday_percentage_change", 0)), 2),
            }

        q_resp = requests.get(
            "https://pro-api.coinmarketcap.com/v2/cryptocurrency/quotes/latest",
            headers=headers,
            params={"symbol": "BTC", "convert": "USD"},
            timeout=8,
        ).json()
        btc_list = q_resp.get("data", {}).get("BTC", [])
        if isinstance(btc_list, list) and btc_list:
            btc_usd = btc_list[0].get("quote", {}).get("USD", {})
            hasil["BTC_USD"] = {
                "price": round(float(btc_usd.get("price", 0)), 2),
                "chg_24h": round(float(btc_usd.get("percent_change_24h", 0)), 2),
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
                timeout=8,
            ).json()
            if "close" in resp:
                hasil[label] = {
                    "close": resp.get("close"),
                    "chg_pct": resp.get("percent_change"),
                    "high": resp.get("high"),
                    "low": resp.get("low"),
                }
        except Exception as e:
            hasil[label] = {"error": str(e)}
    return hasil


def cek_spread_mt5_cloud(symbol: str = "XAUUSD") -> float:
    if not METAAPI_TOKEN or not METAAPI_ACCOUNT_ID:
        return 1.5
    try:
        headers = {"auth-token": METAAPI_TOKEN}
        url = f"https://mt-client-api-v1.agiliumtrade.agiliumtrade.ai/users/current/accounts/{METAAPI_ACCOUNT_ID}/symbols/{symbol}/current-price"
        resp = requests.get(url, headers=headers, timeout=8).json()
        bid = float(resp.get("bid", 0))
        ask = float(resp.get("ask", 0))
        if bid > 0 and ask > 0:
            return round((ask - bid) * 10, 2)
    except Exception:
        pass
    return 1.5


# ==============================================================================
# 8. LAPISAN 4: AUTO-DISCOVERY MODEL AI & DEWAN JURI 4-AGEN (SKOR >= 75)
# ==============================================================================
def bersihkan_teks_telegram(teks: str) -> str:
    if not teks:
        return ""
    teks_bersih = teks.strip()
    if teks_bersih.startswith("```"):
        lines = teks_bersih.splitlines()
        if len(lines) >= 2 and lines[-1].strip().startswith("```"):
            teks_bersih = "\n".join(lines[1:-1]).strip()
    return teks_bersih.replace("**", "")


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


def deteksi_model_gemini_aktif(client) -> list:
    if _MODEL_CACHE["checked"] and _MODEL_CACHE["models"]:
        return _MODEL_CACHE["models"]

    prioritas_prefix = [
        "gemini-4-hydrogen",
        "gemini-3.1-pro",
        "gemini-3-pro",
        "gemini-3-flash",
        "gemini-3.1-flash",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.0-flash",
    ]
    ditemukan = []
    try:
        for m in client.models.list():
            nama = getattr(m, "name", "") or ""
            nama_bersih = nama.replace("models/", "")
            methods = getattr(m, "supported_generation_methods", None) or []
            if methods and "generateContent" not in methods:
                continue
            if "gemini" in nama_bersih and not any(
                x in nama_bersih for x in ["embedding", "imagen", "veo", "aqa", "tts", "audio", "robotics"]
            ):
                ditemukan.append(nama_bersih)
    except Exception as e:
        print(f"Gagal list model Gemini: {str(e)[:120]}", flush=True)

    terurut = []
    for pref in prioritas_prefix:
        for m in ditemukan:
            if m.startswith(pref) and m not in terurut:
                terurut.append(m)
    for m in ditemukan:
        if m not in terurut:
            terurut.append(m)

    if not terurut:
        terurut = ["gemini-3-flash-preview", "gemini-3.1-pro-preview", "gemini-2.5-pro", "gemini-2.5-flash"]

    _MODEL_CACHE["checked"] = True
    _MODEL_CACHE["models"] = terurut[:6]
    print(f"Model Gemini aktif terdeteksi: {_MODEL_CACHE['models']}", flush=True)
    return _MODEL_CACHE["models"]


def panggil_groq_api(prompt: str, system_prompt: str = "", json_mode: bool = False) -> tuple[str, str]:
    if not GROQ_API_KEY:
        return "", ""
    kandidat_groq = ["llama-3.3-70b-versatile", "openai/gpt-oss-120b", "qwen/qwen3-32b"]
    for model_name in kandidat_groq:
        try:
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})

            body = {
                "model": model_name,
                "messages": messages,
                "temperature": 0.2,
                "max_tokens": 2400,
            }
            if json_mode:
                body["response_format"] = {"type": "json_object"}

            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=30,
            )
            if resp.status_code != 200:
                print(f"Groq {model_name} HTTP {resp.status_code}: {resp.text[:160]}", flush=True)
                continue
            data = resp.json()
            konten = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            if konten and konten.strip():
                return konten.strip(), f"GROQ ({model_name})"
        except Exception as e:
            print(f"Groq {model_name} exception: {str(e)[:120]}", flush=True)
    return "", ""


def panggil_ai_reasoning_terbaik(
    prompt: str,
    system_prompt: str,
    json_mode: bool = False,
) -> tuple[str, str]:
    if GEMINI_API_KEY:
        try:
            client = genai.Client(api_key=GEMINI_API_KEY)
            daftar_model = deteksi_model_gemini_aktif(client)
            for nama_model in daftar_model:
                if "hydrogen" in nama_model:
                    try:
                        inter = client.interactions.create(
                            model=f"models/{nama_model}",
                            input=f"{system_prompt}\n\n{prompt}",
                            generation_config={"max_output_tokens": 32768, "thinking_level": "high"},
                        )
                        txt = getattr(inter, "text", None)
                        if txt and str(txt).strip():
                            return str(txt).strip(), f"{nama_model} (HIGH)"
                    except Exception as e_int:
                        print(f"Interactions {nama_model} skip: {str(e_int)[:110]}", flush=True)

                cfg_list = []
                cfg_kwargs = {"system_instruction": system_prompt}
                if json_mode:
                    cfg_kwargs["response_mime_type"] = "application/json"

                if any(k in nama_model for k in ["2.5", "3", "4", "thinking", "pro"]):
                    cfg_think = dict(cfg_kwargs)
                    cfg_think["thinking_config"] = types.ThinkingConfig(thinking_budget=8192)
                    cfg_list.append((types.GenerateContentConfig(**cfg_think), "THINKING"))

                cfg_list.append((types.GenerateContentConfig(**cfg_kwargs), "STANDARD"))

                for cfg_obj, label_mode in cfg_list:
                    try:
                        res = client.models.generate_content(
                            model=nama_model,
                            contents=prompt,
                            config=cfg_obj,
                        )
                        teks = getattr(res, "text", None)
                        if teks and teks.strip():
                            return teks.strip(), f"{nama_model} ({label_mode})"
                    except Exception as e_gen:
                        print(f"Gemini {nama_model} [{label_mode}] gagal: {str(e_gen)[:130]}", flush=True)
        except Exception as e_cli:
            print(f"Gemini client error: {str(e_cli)[:130]}", flush=True)

    return panggil_groq_api(prompt, system_prompt=system_prompt, json_mode=json_mode)


def _ringkas_mtf(mtf: dict) -> str:
    if not isinstance(mtf, dict) or "error" in mtf:
        return "MTF: tidak tersedia"
    of_liq = mtf.get("orderflow_liq", {})
    h1_of = of_liq.get("H1_of", {})
    sweeps = ", ".join(of_liq.get("sweeps", ["NONE"]))
    intra = mtf.get("intraday", {})
    swg = mtf.get("swing", {})
    hld = mtf.get("hold", {})
    return (
        f"M1:{mtf.get('M1')} | M5:{mtf.get('M5')} | M15:{mtf.get('M15')} | "
        f"H1:{mtf.get('H1')} | H4:{mtf.get('H4')} | D1:{mtf.get('D1')} | "
        f"W1:{mtf.get('W1')} | MN1:{mtf.get('MN1')}\n"
        f"  Orderflow H1: {h1_of.get('flow_bias')} ({h1_of.get('net_delta_pct')}%) | "
        f"{h1_of.get('absorption')} | Zona: {of_liq.get('zone')} (VWAP Z={of_liq.get('VWAP_z')})\n"
        f"  Liquidity Sweep: {sweeps}\n"
        f"  Skor Horizon -> Intraday:{intra.get('score')} ({intra.get('bias')}) | "
        f"Swing:{swg.get('score')} ({swg.get('bias')}) | "
        f"Hold:{hld.get('score')} ({hld.get('bias')})"
    )


def jalankan_analisa_lengkap(
    pemicu: str = "Terjadwal 24/7",
    mode_fokus: str = "ALL",
    sesi_target: str = "",
) -> str:
    with ThreadPoolExecutor(max_workers=9) as pool:
        f_makro = pool.submit(ambil_statistik_dan_makro)
        f_inst = pool.submit(ambil_data_institusi_dan_berita)
        f_bw = pool.submit(ambil_intelijen_blockworks)
        f_cmc = pool.submit(ambil_data_coinmarketcap)
        f_td = pool.submit(ambil_data_twelvedata_intraday)
        f_mtf_g = pool.submit(cek_konfluensi_mtf_dan_regime, "GC=F")
        f_mtf_b = pool.submit(cek_konfluensi_mtf_dan_regime, "BTC-USD")
        f_kal = pool.submit(cek_kalender_berita_tier1_otomatis)
        f_spr = pool.submit(cek_spread_mt5_cloud, "XAUUSD")

        data_makro = f_makro.result()
        data_institusi = f_inst.result()
        blockworks_intel = f_bw.result()
        cmc_data = f_cmc.result()
        twelvedata_quote = f_td.result()
        mtf_gold = f_mtf_g.result()
        mtf_btc = f_mtf_b.result()
        kalender_news = f_kal.result()
        spread_aktif = f_spr.result()

    xau_live_td = (
        twelvedata_quote.get("XAUUSD", {}).get("close")
        if isinstance(twelvedata_quote, dict) and isinstance(twelvedata_quote.get("XAUUSD"), dict)
        else None
    )
    xau_live_main = xau_live_td or (mtf_gold.get("last_close") if isinstance(mtf_gold, dict) else None)
    if not xau_live_main:
        try:
            fast_px = yf.Ticker("GC=F").fast_info.get("lastPrice")
            if fast_px is not None:
                xau_live_main = round(float(fast_px), 5)
        except Exception:
            xau_live_main = None

    btc_live = mtf_btc.get("last_close") if isinstance(mtf_btc, dict) else None
    if not btc_live:
        btc_live = data_institusi.get("kripto_derivatif", {}).get("BTC_last")
    if not btc_live:
        cmc_btc_px = cmc_data.get("BTC_USD", {}).get("price") if isinstance(cmc_data, dict) else None
        btc_live = round(float(cmc_btc_px), 2) if cmc_btc_px else None

    live_prices_ctx = {
        "XAUUSD": xau_live_main or "TIDAK_TERSEDIA",
        "sumber_XAU": "TwelveData" if xau_live_td else ("yfinance_GC=F" if xau_live_main else "TIDAK_TERSEDIA"),
        "BTCUSDT": btc_live or "TIDAK_TERSEDIA",
    }

    is_news_blocked = data_institusi.get("kalender_ff", {}).get(
        "blokir_entry_news", kalender_news.get("is_high_impact_active", False)
    )

    risk_status = validasi_hardcoded_risk(
        balance=1000.0,
        sl_pips=30.0,
        pip_value=1.0,
        layers=1,
        spread_pips=spread_aktif,
        is_high_impact_news=is_news_blocked,
        z_score_h1=mtf_gold.get("Z_score_H1", 0.0),
    )

    audit_gold = audit_high_probability_setup("XAUUSD", mtf_gold, data_makro, data_institusi, spread_aktif, is_news_blocked)
    audit_btc = audit_high_probability_setup("BTCUSDT", mtf_btc, data_makro, data_institusi, spread_aktif, is_news_blocked)

    sesi_aktif = sesi_target.upper() if sesi_target else deteksi_sesi_trading()
    skills_aktif = pilih_skills_konteks(mode_fokus=mode_fokus, sesi_target=sesi_aktif)
    macro_snap = json.dumps(data_makro.get("harga_terakhir", {}))

    ringkas_data_untuk_ai = {
        "harga_live": live_prices_ctx,
        "sesi_dan_spread": {"sesi": sesi_aktif, "mode_fokus": mode_fokus, "spread_xau_pips": spread_aktif},
        "makro_dan_risiko": data_makro,
        "makro_fred_detail": data_institusi.get("makro_fred_detail", {}),
        "gold_mtf_orderflow": mtf_gold,
        "btc_mtf_orderflow": mtf_btc,
        "derivatif_dan_institusi": {
            "cftc_cot": data_institusi.get("cftc_cot", {}),
            "okx_derivatif": data_institusi.get("kripto_derivatif", {}),
            "deribit_options": data_institusi.get("deribit_options", {}),
            "orderflow_ccxt": data_institusi.get("orderflow_ccxt", {}),
            "stablecoin": data_institusi.get("stablecoin_flow", {}),
            "cmc": cmc_data,
            "blockworks": blockworks_intel,
            "twelvedata": twelvedata_quote,
        },
        "narasi_dan_news": {
            "kalender_ff": data_institusi.get("kalender_ff", {}),
            "kalender_tier1": kalender_news,
            "headlines": data_institusi.get("berita", [])[:8],
        },
        "audit_critical_thinking": {"XAUUSD": audit_gold, "BTCUSDT": audit_btc},
        "hardcoded_risk": risk_status,
    }

    _LAST_MARKET_CONTEXT["updated_utc"] = datetime.now(timezone.utc).isoformat()
    _LAST_MARKET_CONTEXT["summary"] = json.dumps(ringkas_data_untuk_ai, default=str)[:4500]

    SYSTEM_INSTRUCTION_4_AGENTS = f"""Kamu adalah Dewan Juri 4-Agen Kuantitatif Institusional (berpikir layaknya Quant Trader, Desk Bank Besar, Hedge Fund PM, dan Market Maker).
Gunakan playbook skill berikut dalam penalaranmu:
{skills_aktif}

Aturan Wajib:
1. Lakukan penalaran kritis (Critical Thinking) yang mendalam di seluruh 4 agen:
   - Macro Analyst: bedah arah makro (FRED Real Yield DFII10, Fed Net Liquidity, DXY, korelasi, GARCH/CVaR, CFTC COT, Stablecoin) dan konteks siklus besar (D1-MN1 untuk Hold Bulanan-Tahunan).
   - Bull Researcher: susun tesis bullish terbaik berbasis orderflow CVD, absorpsi pembeli, liquidity sweep bawah (PDL/PWL/Asia Low), katalis berita/narasi, serta peluang per sesi/horizon.
   - Bear/Skeptic Researcher: bongkar kelemahan tesis, cari distribusi CVD, sweep atas (PDH/PWH/Asia High), crowded funding/options, benturan timeframe, dan jebakan likuiditas.
   - Risk Judge: timbang bukti ketiga agen, hormati batas skor maksimum audit (XAU max={audit_gold['max_allowed_score']}, BTC max={audit_btc['max_allowed_score']}). Setup HANYA lolos jika skor >= 75 dan konfluensi kuat; jika masih ada red flag atau bentrok, wajib beri skor < 75 dan bias WAIT_NO_TRADE.
2. Jelaskan secara spesifik implikasi untuk Sesi ({sesi_aktif}: Asia/London/NY), Intraday (M1-H4), Swing (H4-W1), dan Hold Position (D1-MN1).
3. DILARANG mengarang harga atau berita di luar data. JANGAN pakai tanda bintang (* atau **)."""

    prompt = f"""
Lakukan Sidang Dewan Juri 4-Agen dengan reasoning yang tajam, terstruktur, dan mendalam dalam Bahasa Indonesia untuk Telegram.
Pemicu: {pemicu} | Mode Fokus: {mode_fokus} | Sesi: {sesi_aktif}

Data Pasar Lengkap (Makro, Mikro M1-MN1, Orderflow CVD, Liquidity Sweep, Fundamental, Derivatif & News):
{json.dumps(ringkas_data_untuk_ai, default=str)}

Kembalikan HANYA JSON valid dengan struktur berikut:
{{
  "xau_score": integer (0-{audit_gold['max_allowed_score']}),
  "xau_bias": "BULLISH" | "BEARISH" | "WAIT_NO_TRADE",
  "btc_score": integer (0-{audit_btc['max_allowed_score']}),
  "btc_bias": "BULLISH" | "BEARISH" | "WAIT_NO_TRADE",
  "macro_analyst": "Penalaran mendalam makro, fundamental, likuiditas global & bias Hold D1-MN1 (3-4 kalimat padat)",
  "bull_researcher": "Penalaran tesis bullish, orderflow CVD, sweep likuiditas, narasi/news & setup terbaik (3-4 kalimat padat)",
  "bear_skeptic": "Audit kritis, red flag, jebakan likuiditas, benturan timeframe & skenario gagal (3-4 kalimat padat)",
  "risk_judge": "Keputusan final per horizon (Sesi {sesi_aktif}, Intraday, Swing, Hold), titik invalidasi, risiko & alasan lolos/ditahan < 75 (3-4 kalimat padat)"
}}
"""

    raw_json, engine_dipakai = panggil_ai_reasoning_terbaik(
        prompt=prompt,
        system_prompt=SYSTEM_INSTRUCTION_4_AGENTS,
        json_mode=True,
    )

    if raw_json:
        try:
            bersih = raw_json.strip()
            if "{" in bersih and "}" in bersih:
                bersih = bersih[bersih.find("{") : bersih.rfind("}") + 1]
            verdict = json.loads(bersih)

            x_sc = min(int(verdict.get("xau_score", 0)), audit_gold["max_allowed_score"])
            b_sc = min(int(verdict.get("btc_score", 0)), audit_btc["max_allowed_score"])
            top_score = max(x_sc, b_sc)
            bias_summary = f"[{mode_fokus}] XAU:{verdict.get('xau_bias')}({x_sc}) | BTC:{verdict.get('btc_bias')}({b_sc})"
            lolos_final = bool(risk_status.get("approved")) and (top_score >= 75)

            hasil_teks = (
                f"QUANT AGENT v2.8 — SIDANG 4 AGEN [{engine_dipakai}]\n"
                f"Pemicu: {pemicu} | Fokus: {mode_fokus} | Sesi: {sesi_aktif} | Spread XAU: {spread_aktif} pips\n\n"
                f"• XAUUSD ({live_prices_ctx['XAUUSD']}): {verdict.get('xau_bias')} (Skor: {x_sc}/100) | {mtf_gold.get('regime')}\n"
                f"  {_ringkas_mtf(mtf_gold)}\n"
                f"• BTCUSDT ({live_prices_ctx['BTCUSDT']}): {verdict.get('btc_bias')} (Skor: {b_sc}/100) | {mtf_btc.get('regime')}\n"
                f"  {_ringkas_mtf(mtf_btc)}\n\n"
                f"1. Macro Analyst (Makro & Fundamental):\n{verdict.get('macro_analyst')}\n\n"
                f"2. Bull Researcher (Orderflow, Narasi & Peluang):\n{verdict.get('bull_researcher')}\n\n"
                f"3. Bear / Skeptic Researcher (Blind Spot & Jebakan):\n{verdict.get('bear_skeptic')}\n\n"
                f"4. Risk Judge (Keputusan Final & Horizon):\n{verdict.get('risk_judge')}\n\n"
                f"Status Guardrail: {'LOLOS HIGH-PROB (>=75) — SIAP REVIEW' if lolos_final else 'DITAHAN / WAIT (Skor < 75 atau Guardrail Aktif)'}"
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
            hasil_teks = (
                f"QUANT AGENT v2.8 — SIDANG 4 AGEN [{engine_dipakai}]\n"
                f"• XAUUSD: {_ringkas_mtf(mtf_gold)}\n"
                f"• BTCUSDT: {_ringkas_mtf(mtf_btc)}\n\n"
                f"{raw_json}"
            )
            simpan_log("XAUUSD/BTC", f"[{mode_fokus}] TEXT_OUT", 0, spread_aktif, macro_snap, "AUTO_WAIT", hasil_teks[:400])
            kirim_telegram(hasil_teks, log_id=None)
            return hasil_teks

    hasil_teks = (
        f"QUANT AGENT v2.8 [MODE DATA KUANTITATIF] — {pemicu}\n"
        f"Sesi: {sesi_aktif} | Spread XAU: {spread_aktif} pips\n"
        f"• XAUUSD ({live_prices_ctx['XAUUSD']}): {_ringkas_mtf(mtf_gold)}\n"
        f"• BTCUSDT ({live_prices_ctx['BTCUSDT']}): {_ringkas_mtf(mtf_btc)}\n\n"
        f"Catatan: Semua provider AI (Gemini & Groq) belum mengembalikan respons. Cek GEMINI_API_KEY / GROQ_API_KEY di GitHub Secrets."
    )
    simpan_log("XAUUSD/BTC", "WAIT_NO_MODEL", 0, spread_aktif, macro_snap, "AUTO_WAIT", hasil_teks[:400])
    kirim_telegram(hasil_teks, log_id=None)
    return hasil_teks


# ==============================================================================
# 9. BACKGROUND WORKER: SCHEDULER 24/7 & LISTENER TELEGRAM INTERAKTIF
# ==============================================================================
def loop_scheduler_otomatis():
    time.sleep(AUTO_SCAN_INTERVAL_SEC)
    while True:
        try:
            jalankan_analisa_lengkap(pemicu="Auto-Scan Cloud 24/7", mode_fokus="ALL")
        except Exception as e:
            print("Error scheduler:", e)
        time.sleep(AUTO_SCAN_INTERVAL_SEC)


def _deteksi_fokus_dari_teks(teks_lower: str) -> tuple[str, str]:
    sesi = ""
    if "asia" in teks_lower:
        sesi = "ASIA"
    elif "london" in teks_lower:
        sesi = "LONDON"
    elif "new york" in teks_lower or "newyork" in teks_lower or " ny" in teks_lower or teks_lower.endswith("ny"):
        sesi = "NEW_YORK"

    if any(k in teks_lower for k in ["hold", "bulan", "tahun", "invest", "jangka panjang", "position"]):
        return "HOLD", sesi
    if "swing" in teks_lower:
        return "SWING", sesi
    if any(k in teks_lower for k in ["intraday", "scalp", "hari ini"]):
        return "INTRADAY", sesi
    if sesi:
        return "SESSION", sesi
    return "ALL", ""


def loop_listener_telegram():
    if not TELEGRAM_BOT_TOKEN:
        print("Listener berhenti: TELEGRAM_BOT_TOKEN kosong.", flush=True)
        return
    offset = 0
    base_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    print("Listener Telegram v2.8 aktif polling...", flush=True)
    kirim_telegram("Bot Quant Agent v2.8 (Sidang 4 Agen Skor >= 75 + M1-MN1 + Orderflow + Chat Aktif) terhubung!")
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
                time.sleep(retry_after if err_code == 429 else 5)
                continue

            for item in resp.get("result", []):
                offset = item["update_id"] + 1

                if "callback_query" in item:
                    cb = item["callback_query"]
                    cb_id = cb["id"]
                    data_cb = cb.get("data", "")
                    from_id = str(cb.get("from", {}).get("id", ""))
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

                if chat_id == str(TELEGRAM_CHAT_ID):
                    if text in ["/start", "/help"]:
                        kirim_telegram(
                            "Quant Agent v2.8 Aktif (Sidang 4 Agen Skor >= 75 + M1-MN1 + Orderflow).\n"
                            "Perintah tersedia:\n"
                            "/cek - Sidang 4 Agen lengkap (Makro, Mikro, Narasi, Fundamental, News)\n"
                            "/cek asia | /cek london | /cek ny - Fokus likuiditas per sesi\n"
                            "/cek intraday - Fokus eksekusi M1-H4, CVD & VWAP\n"
                            "/cek swing - Fokus ayunan H4-W1, COT & derivatif\n"
                            "/cek hold - Fokus posisi bulanan-tahunan D1-MN1 & makro\n"
                            "/backtest - Uji statistik backtesting.py & 1.000x Monte Carlo\n"
                            "/jurnal - Lihat jurnal audit SQLite & kuota sesi\n"
                            "Atau ketik pesan biasa untuk ngobrol & diskusi analisa langsung."
                        )
                    elif text.startswith("/cek") or text.startswith("/analisa"):
                        mode_fokus, sesi_target = _deteksi_fokus_dari_teks(text)
                        label_cmd = f"{mode_fokus}" + (f" ({sesi_target})" if sesi_target else "")
                        kirim_telegram(f"AgentLang menjalankan Sidang 4 Agen [{label_cmd}] (Makro, Mikro M1-MN1, Orderflow, News), tunggu sebentar...")

                        def _run_manual_analisa(m_fokus=mode_fokus, s_target=sesi_target, lbl=label_cmd):
                            try:
                                jalankan_analisa_lengkap(
                                    pemicu=f"Perintah Manual /cek [{lbl}]",
                                    mode_fokus=m_fokus,
                                    sesi_target=s_target,
                                )
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
                            def _balas_chat_async(teks_user=pesan_asli, teks_low=text):
                                try:
                                    snap_db = ambil_ringkasan_jurnal_dan_evaluasi_mingguan()
                                    mode_chat, sesi_chat = _deteksi_fokus_dari_teks(teks_low)
                                    skill_chat = pilih_skills_konteks(mode_fokus=mode_chat, sesi_target=sesi_chat)
                                    sys_chat = (
                                        "Kamu adalah AgentLang (Quant Agent v2.8), partner diskusi quant trader, desk bank besar, "
                                        "hedge fund, dan market maker di Telegram. Jawab lugas, tenang, kritis, dan mengalir seperti "
                                        "rekan diskusi profesional. JANGAN pakai tanda bintang (* atau **)."
                                    )
                                    prompt_chat = (
                                        f"Status sistem saat ini: Sesi={deteksi_sesi_trading()}, Ambang lolos sinyal=Skor >= 75, "
                                        f"Data terakhir={_LAST_MARKET_CONTEXT['updated_utc'] or 'belum di-scan'}.\n"
                                        f"Skill relevan:\n{skill_chat}\n\n"
                                        f"Snapshot data pasar terakhir:\n{_LAST_MARKET_CONTEXT['summary']}\n\n"
                                        f"Jurnal & kuota sesi:\n{snap_db}\n\n"
                                        f"Pesan user: {teks_user}"
                                    )
                                    balasan, engine_used = panggil_ai_reasoning_terbaik(
                                        prompt=prompt_chat,
                                        system_prompt=sys_chat,
                                        json_mode=False,
                                    )
                                    if balasan:
                                        kirim_telegram(balasan)
                                    else:
                                        kirim_telegram(
                                            "Semua koneksi model AI (Gemini & Groq) sedang tidak merespons. "
                                            "Pastikan GEMINI_API_KEY dan GROQ_API_KEY di GitHub Secrets masih aktif."
                                        )
                                except Exception as e:
                                    kirim_telegram(f"Gagal balas chat: {str(e)[:150]}")

                            threading.Thread(target=_balas_chat_async, daemon=True).start()
        except Exception as e:
            print("Listener error:", e)
            time.sleep(5)


# ==============================================================================
# 10. ENDPOINT WEB (HEALTH CHECK 24/7 & WEBHOOK TRADINGVIEW)
# ==============================================================================
@app.route("/", methods=["GET"])
@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({
        "status": "alive",
        "version": "2.8",
        "session": deteksi_sesi_trading(),
        "utc": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/webhook", methods=["POST"])
def tradingview_webhook():
    payload = request.get_json(silent=True) or {"raw": request.data.decode("utf-8", errors="ignore")}
    threading.Thread(
        target=jalankan_analisa_lengkap,
        args=(f"TradingView Alert: {payload}", "INTRADAY", deteksi_sesi_trading()),
        daemon=True,
    ).start()
    return jsonify({"received": True})


if __name__ == "__main__":
    init_db()
    threading.Thread(target=loop_scheduler_otomatis, daemon=True).start()
    threading.Thread(target=loop_listener_telegram, daemon=True).start()
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
