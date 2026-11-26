import os
import time
import numpy as np
import pandas as pd
import requests
import yfinance as yf

TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()


# ==============================================================================
# 1. DATA LOADER MULTI-SUMBER (OANDA/TV XAUUSD, HYPERLIQUID XYZ:GOLD, BINANCE BTC)
# ==============================================================================
def ambil_ohlcv_tv_atau_yf(ticker_yf: str, symbol_td: str, interval: str = "1h", period: str = "30d") -> pd.DataFrame:
    """Mengambil candle OHLCV dengan fallback TwelveData -> Yahoo Finance."""
    if TWELVEDATA_API_KEY:
        try:
            td_int = {"15m": "15min", "1h": "1h", "4h": "4h", "1d": "1day"}.get(interval, "1h")
            url = (
                f"https://api.twelvedata.com/time_series?symbol={symbol_td}&interval={td_int}"
                f"&outputsize=180&apikey={TWELVEDATA_API_KEY}"
            )
            r = requests.get(url, timeout=10).json()
            vals = r.get("values", [])
            if vals:
                df = pd.DataFrame(vals)
                for col in ["open", "high", "low", "close"]:
                    df[col] = df[col].astype(float)
                df["volume"] = df["volume"].astype(float) if "volume" in df.columns else 1000.0
                df = df.iloc[::-1].reset_index(drop=True)
                df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"}, inplace=True)
                return df
        except Exception:
            pass

    try:
        df = yf.download(ticker_yf, period=period, interval=interval, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna(subset=["Open", "High", "Low", "Close"]).reset_index(drop=True)
        if "Volume" not in df.columns:
            df["Volume"] = 1000.0
        return df
    except Exception:
        return pd.DataFrame()


def ambil_ohlcv_hyperliquid_xyz_gold(interval: str = "1h", limit: int = 180) -> pd.DataFrame:
    """Menarik candle live xyz:GOLD langsung dari Hyperliquid Info API."""
    try:
        ms_per_bar = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}.get(interval, 3_600_000)
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - (limit * ms_per_bar)
        payload = {
            "type": "candleSnapshot",
            "req": {"coin": "xyz:GOLD", "interval": interval, "startTime": start_ms, "endTime": end_ms},
        }
        resp = requests.post("https://api.hyperliquid.xyz/info", json=payload, timeout=10).json()
        if isinstance(resp, list) and len(resp) > 10:
            rows = []
            for c in resp:
                rows.append({
                    "Open": float(c["o"]),
                    "High": float(c["h"]),
                    "Low": float(c["l"]),
                    "Close": float(c["c"]),
                    "Volume": float(c.get("v", 100.0)),
                })
            return pd.DataFrame(rows)
    except Exception:
        pass
    return pd.DataFrame()


def ambil_ohlcv_binance_btc(interval: str = "1h", limit: int = 180) -> pd.DataFrame:
    """Menarik candle live BTCUSDT dari Binance Futures Public API."""
    try:
        url = f"https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval={interval}&limit={limit}"
        resp = requests.get(url, timeout=10).json()
        if isinstance(resp, list) and len(resp) > 10:
            rows = []
            for k in resp:
                rows.append({
                    "Open": float(k[1]),
                    "High": float(k[2]),
                    "Low": float(k[3]),
                    "Close": float(k[4]),
                    "Volume": float(k[5]),
                })
            return pd.DataFrame(rows)
    except Exception:
        pass
    return pd.DataFrame()


# ==============================================================================
# 2. KALKULASI MATEMATIS: OHLC PIVOT/NH/NL, FVG, OB, OTE, SWEEP & MSS
# ==============================================================================
def hitung_atr(df: pd.DataFrame, period: int = 14) -> float:
    if len(df) < period + 2:
        return float((df["High"] - df["Low"]).mean()) if len(df) > 0 else 1.0
    high_low = df["High"] - df["Low"]
    high_close = (df["High"] - df["Close"].shift()).abs()
    low_close = (df["Low"] - df["Close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])


def hitung_strategi_ohlc_harian(df_d1: pd.DataFrame) -> dict:
    """Menghitung rumus kuantitatif OHLC (Pivot, Konstanta X, New High, New Low, dan Bias Harian)."""
    if len(df_d1) < 2:
        return {"error": "Data D1 tidak cukup"}

    prev = df_d1.iloc[-2]
    curr = df_d1.iloc[-1]

    p_open, p_high, p_low, p_close = float(prev["Open"]), float(prev["High"]), float(prev["Low"]), float(prev["Close"])
    c_open, c_close = float(curr["Open"]), float(curr["Close"])

    pivot = ((2.0 * p_close) + p_high + p_low) / 4.0
    candle_kemarin = "BULL" if p_close > p_open else "BEAR"

    if candle_kemarin == "BULL":
        x_val = ((2.0 * p_high) + p_low + p_close) / 2.0
    else:
        x_val = ((2.0 * p_low) + p_high + p_close) / 2.0

    nh_resisten = x_val - p_low
    nl_support = x_val - p_high
    bias_ohlc = "BUY" if c_open > pivot else "SELL"

    return {
        "prev_candle": candle_kemarin,
        "prev_high": round(p_high, 2),
        "prev_low": round(p_low, 2),
        "pivot": round(pivot, 2),
        "NH_resisten": round(nh_resisten, 2),
        "NL_support": round(nl_support, 2),
        "today_open": round(c_open, 2),
        "last_price": round(c_close, 2),
        "bias_ohlc": bias_ohlc,
    }


def deteksi_fvg(df: pd.DataFrame) -> dict:
    """Mendeteksi unmitigated Bullish & Bearish Fair Value Gap pada 15 candle terakhir."""
    bull_fvg, bear_fvg = None, None
    if len(df) < 15:
        return {"bullish_fvg": None, "bearish_fvg": None}

    last_close = float(df["Close"].iloc[-1])
    for i in range(len(df) - 12, len(df) - 2):
        c1_high, c1_low = float(df["High"].iloc[i]), float(df["Low"].iloc[i])
        c3_high, c3_low = float(df["High"].iloc[i + 2]), float(df["Low"].iloc[i + 2])

        if c3_low > c1_high and last_close >= c1_high:
            bull_fvg = [round(c1_high, 2), round(c3_low, 2)]
        if c3_high < c1_low and last_close <= c1_low:
            bear_fvg = [round(c3_high, 2), round(c1_low, 2)]

    return {"bullish_fvg": bull_fvg, "bearish_fvg": bear_fvg}


def deteksi_order_block(df: pd.DataFrame) -> dict:
    """Mendeteksi Order Block terakhir sebelum pergerakan impulsif bervolume di atas rata-rata."""
    bull_ob, bear_ob = None, None
    if len(df) < 25:
        return {"bullish_ob": None, "bearish_ob": None}

    vol_ma = df["Volume"].rolling(20).mean()
    for i in range(len(df) - 18, len(df) - 2):
        c0 = df.iloc[i]
        c1 = df.iloc[i + 1]
        v_avg = float(vol_ma.iloc[i + 1]) if not np.isnan(vol_ma.iloc[i + 1]) else 0.0

        # Bullish OB: candle merah terakhir sebelum candle hijau impulsif
        if float(c0["Close"]) < float(c0["Open"]) and float(c1["Close"]) > float(c0["High"]):
            if v_avg == 0 or float(c1["Volume"]) >= v_avg:
                bull_ob = [round(float(c0["Low"]), 2), round(float(c0["High"]), 2)]

        # Bearish OB: candle hijau terakhir sebelum candle merah impulsif
        if float(c0["Close"]) > float(c0["Open"]) and float(c1["Close"]) < float(c0["Low"]):
            if v_avg == 0 or float(c1["Volume"]) >= v_avg:
                bear_ob = [round(float(c0["Low"]), 2), round(float(c0["High"]), 2)]

    return {"bullish_ob": bull_ob, "bearish_ob": bear_ob}


def deteksi_liquidity_sweep_dan_mss(df_h1: pd.DataFrame, prev_day_high: float, prev_day_low: float) -> dict:
    """
    Mendeteksi Liquidity Sweep (penembusan palsu PDH/PDL < 0.5% ATR lalu close balik)
    serta Market Structure Shift (MSS / CHoCH) pada H1.
    """
    hasil = {"sweep_signal": "NONE", "mss_signal": "NONE", "htf_trend": "RANGING"}
    if len(df_h1) < 30:
        return hasil

    atr = hitung_atr(df_h1, 14)
    toleransi_sweep = max(atr * 0.6, float(df_h1["Close"].iloc[-1]) * 0.005)
    recent = df_h1.iloc[-5:]

    # 1. Cek Liquidity Sweep pada Previous Day High / Low
    for idx in range(len(recent) - 1):
        bar = recent.iloc[idx]
        next_bar = recent.iloc[idx + 1]
        # Bearish Sweep: sumbu menembus PDH tipis lalu ditutup kembali di bawah PDH
        if float(bar["High"]) > prev_day_high and (float(bar["High"]) - prev_day_high) <= toleransi_sweep:
            if float(bar["Close"]) < prev_day_high or float(next_bar["Close"]) < prev_day_high:
                hasil["sweep_signal"] = "BEARISH_LIQUIDITY_SWEEP"
        # Bullish Sweep: sumbu menembus PDL tipis lalu ditutup kembali di atas PDL
        if float(bar["Low"]) < prev_day_low and (prev_day_low - float(bar["Low"])) <= toleransi_sweep:
            if float(bar["Close"]) > prev_day_low or float(next_bar["Close"]) > prev_day_low:
                hasil["sweep_signal"] = "BULLISH_LIQUIDITY_SWEEP"

    # 2. Cek Market Structure Shift (MSS) & Struktur Tren H1/H4
    swing_high_prev = float(df_h1["High"].iloc[-20:-4].max())
    swing_low_prev = float(df_h1["Low"].iloc[-20:-4].min())
    last_close = float(df_h1["Close"].iloc[-1])
    ema_50 = float(df_h1["Close"].ewm(span=50).mean().iloc[-1])

    if last_close > swing_high_prev:
        hasil["mss_signal"] = "BULLISH_MSS"
    elif last_close < swing_low_prev:
        hasil["mss_signal"] = "BEARISH_MSS"

    if last_close > ema_50 and last_close > (swing_high_prev + swing_low_prev) / 2.0:
        hasil["htf_trend"] = "BULLISH"
    elif last_close < ema_50 and last_close < (swing_high_prev + swing_low_prev) / 2.0:
        hasil["htf_trend"] = "BEARISH"

    return hasil


def hitung_fibonacci_ote(df_h1: pd.DataFrame) -> dict:
    """Menghitung zona Fibonacci Optimal Trade Entry (0.618 - 0.786) dari swing 48 jam terakhir."""
    if len(df_h1) < 20:
        return {"price_position": "UNKNOWN"}

    window = df_h1.iloc[-48:]
    sh = float(window["High"].max())
    sl = float(window["Low"].min())
    last_c = float(df_h1["Close"].iloc[-1])
    rng = max(sh - sl, 1e-6)

    buy_ote_top = round(sh - (0.618 * rng), 2)
    buy_ote_bot = round(sh - (0.786 * rng), 2)
    sell_ote_bot = round(sl + (0.618 * rng), 2)
    sell_ote_top = round(sl + (0.786 * rng), 2)
    eq = (sh + sl) / 2.0

    if buy_ote_bot <= last_c <= buy_ote_top:
        pos = "IN_BUY_OTE_DISCOUNT"
    elif sell_ote_bot <= last_c <= sell_ote_top:
        pos = "IN_SELL_OTE_PREMIUM"
    elif last_c < eq:
        pos = "DISCOUNT_ZONE"
    else:
        pos = "PREMIUM_ZONE"

    return {
        "swing_high": round(sh, 2),
        "swing_low": round(sl, 2),
        "buy_ote_zone": [buy_ote_bot, buy_ote_top],
        "sell_ote_zone": [sell_ote_bot, sell_ote_top],
        "price_position": pos,
    }


def analisa_smc_matematis(aset: str) -> dict:
    """Orkestrasi analisa teknikal multi-timeframe untuk XAUUSD, XYZ:GOLD, dan BTCUSDT."""
    sym = aset.upper()
    if sym == "XAUUSD":
        df_d1 = ambil_ohlcv_tv_atau_yf("GC=F", "XAU/USD", interval="1d", period="60d")
        df_h1 = ambil_ohlcv_tv_atau_yf("GC=F", "XAU/USD", interval="1h", period="30d")
        df_m15 = ambil_ohlcv_tv_atau_yf("GC=F", "XAU/USD", interval="15m", period="7d")
        sumber = "OANDA_TV_XAUUSD"
    elif sym == "XYZ:GOLD":
        df_d1 = ambil_ohlcv_hyperliquid_xyz_gold("1d", 60)
        df_h1 = ambil_ohlcv_hyperliquid_xyz_gold("1h", 180)
        df_m15 = ambil_ohlcv_hyperliquid_xyz_gold("15m", 180)
        if df_h1.empty:
            df_d1 = ambil_ohlcv_tv_atau_yf("GC=F", "XAU/USD", interval="1d", period="60d")
            df_h1 = ambil_ohlcv_tv_atau_yf("GC=F", "XAU/USD", interval="1h", period="30d")
            df_m15 = ambil_ohlcv_tv_atau_yf("GC=F", "XAU/USD", interval="15m", period="7d")
        sumber = "HYPERLIQUID_XYZ_GOLD"
    else:
        df_d1 = ambil_ohlcv_binance_btc("1d", 60)
        df_h1 = ambil_ohlcv_binance_btc("1h", 180)
        df_m15 = ambil_ohlcv_binance_btc("15m", 180)
        sumber = "BINANCE_BTCUSDT"

    if df_d1.empty or df_h1.empty:
        return {"error": f"Data OHLCV kosong untuk {aset}"}

    ohlc = hitung_strategi_ohlc_harian(df_d1)
    if "error" in ohlc:
        return ohlc

    sweep_mss = deteksi_liquidity_sweep_dan_mss(df_h1, ohlc["prev_high"], ohlc["prev_low"])

    return {
        "asset": sym,
        "data_source": sumber,
        "daily_ohlc": ohlc,
        "fvg_m15": deteksi_fvg(df_m15 if not df_m15.empty else df_h1),
        "fvg_h1": deteksi_fvg(df_h1),
        "order_block_h1": deteksi_order_block(df_h1),
        "sweep_and_mss": sweep_mss,
        "fibonacci_ote_h1": hitung_fibonacci_ote(df_h1),
    }


def cek_anti_korelasi_ganda(threshold: float = 0.80) -> dict:
    """Menghitung korelasi 30 hari antara Emas dan Bitcoin untuk mencegah eksposur risiko ganda."""
    try:
        df_g = yf.download("GC=F", period="45d", interval="1d", progress=False)
        df_b = yf.download("BTC-USD", period="45d", interval="1d", progress=False)
        if isinstance(df_g.columns, pd.MultiIndex):
            df_g.columns = df_g.columns.get_level_values(0)
        if isinstance(df_b.columns, pd.MultiIndex):
            df_b.columns = df_b.columns.get_level_values(0)

        gabung = pd.DataFrame({
            "gold": df_g["Close"].pct_change(),
            "btc": df_b["Close"].pct_change(),
        }).dropna().tail(30)

        if len(gabung) >= 15:
            corr = float(gabung["gold"].corr(gabung["btc"]))
            return {
                "corr_30d": round(corr, 3),
                "double_exposure_blocked": abs(corr) >= threshold,
            }
    except Exception:
        pass
    return {"corr_30d": 0.0, "double_exposure_blocked": False}
