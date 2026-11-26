import os
import requests
import numpy as np
import pandas as pd
import yfinance as yf

TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()


# ==============================================================================
# 1. DATA INGESTION MULTI-TIMEFRAME (BEBAS GEOBLOCK IP US GITHUB ACTIONS)
# ==============================================================================
def ambil_ohlcv_tv_atau_yf(ticker_yf: str, symbol_td: str, interval: str = "1h", period: str = "30d") -> tuple[pd.DataFrame, str]:
    """Mengambil candle OHLCV dengan Binance Vision (BTC), TwelveData, dan Yahoo Finance."""
    is_btc = "BTC" in ticker_yf.upper() or "BTC" in symbol_td.upper()

    # 1. Jalur khusus BTC bebas geoblock IP US (Binance Vision API -> Yahoo BTC-USD)
    if is_btc:
        try:
            bn_int = {"15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}.get(interval, "1h")
            url = f"https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval={bn_int}&limit=200"
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                raw = r.json()
                df = pd.DataFrame(raw, columns=[
                    "Open_time", "Open", "High", "Low", "Close", "Volume",
                    "Close_time", "QAV", "Trades", "TBBAV", "TBQAV", "Ignore"
                ])
                for col in ["Open", "High", "Low", "Close", "Volume"]:
                    df[col] = df[col].astype(float)
                df.index = pd.to_datetime(df["Open_time"], unit="ms", utc=True)
                return df[["Open", "High", "Low", "Close", "Volume"]].dropna(), "BINANCE_VISION_API"
        except Exception:
            pass

        try:
            df = yf.download("BTC-USD", period=period, interval=interval, progress=False)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            if not df.empty:
                if df.index.tz is None:
                    df.index = df.index.tz_localize("UTC")
                return df[["Open", "High", "Low", "Close", "Volume"]].dropna(), "YAHOO_FINANCE_BTC"
        except Exception:
            return pd.DataFrame(), "ERROR"

    # 2. Jalur Emas (XAUUSD / XYZ:GOLD) via TwelveData -> Yahoo Finance GC=F
    if TWELVEDATA_API_KEY:
        try:
            td_int = {"15m": "15min", "1h": "1h", "4h": "4h", "1d": "1day"}.get(interval, "1h")
            url = (
                f"https://api.twelvedata.com/time_series?symbol={symbol_td}&interval={td_int}"
                f"&outputsize=200&apikey={TWELVEDATA_API_KEY}"
            )
            r = requests.get(url, timeout=10).json()
            vals = r.get("values", [])
            if vals:
                df = pd.DataFrame(vals)
                df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
                df = df.sort_values("datetime").set_index("datetime")
                for col in ["open", "high", "low", "close"]:
                    df[col] = df[col].astype(float)
                df["Volume"] = 1000.0
                df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"})
                return df[["Open", "High", "Low", "Close", "Volume"]].dropna(), "TWELVEDATA_API"
        except Exception:
            pass

    try:
        df = yf.download(ticker_yf, period=period, interval=interval, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if not df.empty:
            if "Volume" not in df.columns:
                df["Volume"] = 1000.0
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
            return df[["Open", "High", "Low", "Close", "Volume"]].dropna(), "YAHOO_FINANCE_GCF"
    except Exception:
        pass

    return pd.DataFrame(), "ERROR"


# ==============================================================================
# 2. MARKET REGIME ENGINE (ADX 14 & ATR 14)
# ==============================================================================
def hitung_market_regime(df_h1: pd.DataFrame, period: int = 14) -> dict:
    """Mendeteksi rezim pasar: TRENDING (ADX >= 20) vs RANGING/CHOPPY (ADX < 20)."""
    if len(df_h1) < period + 5:
        return {"adx_14": 22.0, "atr_14": 0.0, "regime": "UNKNOWN"}

    high = df_h1["High"]
    low = df_h1["Low"]
    close = df_h1["Close"]

    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)

    up_move = high - high.shift(1)
    down_move = low.shift(1) - low

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * (pd.Series(plus_dm, index=df_h1.index).ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.nan))
    minus_di = 100 * (pd.Series(minus_dm, index=df_h1.index).ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.nan))

    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    adx = dx.ewm(alpha=1 / period, adjust=False).mean()

    adx_val = round(float(adx.iloc[-1]), 2) if not np.isnan(adx.iloc[-1]) else 22.0
    atr_val = round(float(atr.iloc[-1]), 2) if not np.isnan(atr.iloc[-1]) else 0.0
    regime = "TRENDING" if adx_val >= 20.0 else "RANGING"

    return {
        "adx_14": adx_val,
        "atr_14": atr_val,
        "regime": regime,
    }


# ==============================================================================
# 3. KALKULASI OHLC HARIAN, FIBONACCI OTE, FVG, ORDER BLOCK & LIQUIDITY SWEEP
# ==============================================================================
def hitung_daily_ohlc_levels(df_d1: pd.DataFrame) -> dict:
    if len(df_d1) < 2:
        return {"error": "Data D1 tidak cukup"}

    kemarin = df_d1.iloc[-2]
    hari_ini = df_d1.iloc[-1]

    o_y = float(kemarin["Open"])
    h_y = float(kemarin["High"])
    l_y = float(kemarin["Low"])
    c_y = float(kemarin["Close"])
    last_px = float(hari_ini["Close"])

    if c_y < o_y:
        x_val = ((2.0 * l_y) + h_y + c_y) / 2.0
        bias_ohlc = "SELL"
    else:
        x_val = ((2.0 * h_y) + l_y + c_y) / 2.0
        bias_ohlc = "BUY"

    nh_resisten = round(x_val - l_y, 2)
    nl_support = round(x_val - h_y, 2)
    pivot = round(x_val / 2.0, 2)

    return {
        "bias_ohlc": bias_ohlc,
        "pivot": pivot,
        "NH_resisten": nh_resisten,
        "NL_support": nl_support,
        "PDH": round(h_y, 2),
        "PDL": round(l_y, 2),
        "last_price": round(last_px, 2),
    }


def hitung_fibonacci_ote(df_h1: pd.DataFrame) -> dict:
    if len(df_h1) < 30:
        return {"price_position": "UNKNOWN"}

    window = df_h1.tail(48)
    sh = float(window["High"].max())
    sl = float(window["Low"].min())
    last_px = float(df_h1["Close"].iloc[-1])
    rng = max(sh - sl, 1e-6)

    # Tarikan OTE Buy (retracement dari Swing High ke Swing Low: 0.618 - 0.786)
    buy_618 = round(sh - (0.618 * rng), 2)
    buy_786 = round(sh - (0.786 * rng), 2)

    # Tarikan OTE Sell (retracement dari Swing Low ke Swing High: 0.618 - 0.786)
    sell_618 = round(sl + (0.618 * rng), 2)
    sell_786 = round(sl + (0.786 * rng), 2)

    mid_px = (sh + sl) / 2.0
    if buy_786 <= last_px <= buy_618:
        pos = "IN_BUY_OTE_DISCOUNT"
    elif sell_618 <= last_px <= sell_786:
        pos = "IN_SELL_OTE_PREMIUM"
    elif last_px < mid_px:
        pos = "DISCOUNT_ZONE"
    else:
        pos = "PREMIUM_ZONE"

    return {
        "swing_high": round(sh, 2),
        "swing_low": round(sl, 2),
        "buy_ote_zone": [buy_786, buy_618],
        "sell_ote_zone": [sell_618, sell_786],
        "price_position": pos,
    }


def deteksi_fvg(df: pd.DataFrame) -> dict:
    bull_fvg = None
    bear_fvg = None
    if len(df) < 10:
        return {"bullish_fvg": None, "bearish_fvg": None}

    sub = df.tail(25)
    for i in range(len(sub) - 3, 1, -1):
        c1_h = float(sub["High"].iloc[i - 2])
        c1_l = float(sub["Low"].iloc[i - 2])
        c3_h = float(sub["High"].iloc[i])
        c3_l = float(sub["Low"].iloc[i])

        if bull_fvg is None and c3_l > c1_h:
            bull_fvg = [round(c1_h, 2), round(c3_l, 2)]
        if bear_fvg is None and c3_h < c1_l:
            bear_fvg = [round(c3_h, 2), round(c1_l, 2)]
        if bull_fvg and bear_fvg:
            break

    return {"bullish_fvg": bull_fvg, "bearish_fvg": bear_fvg}


def deteksi_order_block(df_h1: pd.DataFrame) -> dict:
    bull_ob = None
    bear_ob = None
    if len(df_h1) < 15:
        return {"bullish_ob": None, "bearish_ob": None}

    sub = df_h1.tail(30)
    for i in range(len(sub) - 2, 2, -1):
        o = float(sub["Open"].iloc[i])
        c = float(sub["Close"].iloc[i])
        h = float(sub["High"].iloc[i])
        l = float(sub["Low"].iloc[i])

        next_c = float(sub["Close"].iloc[i + 1])
        next_o = float(sub["Open"].iloc[i + 1])
        body_next = abs(next_c - next_o)
        body_curr = max(abs(c - o), 1e-6)

        # Bullish OB: candle turun terakhir sebelum ekspansi naik kuat
        if bull_ob is None and c < o and next_c > next_o and body_next > (1.5 * body_curr):
            bull_ob = [round(l, 2), round(h, 2)]

        # Bearish OB: candle naik terakhir sebelum ekspansi turun kuat
        if bear_ob is None and c > o and next_c < next_o and body_next > (1.5 * body_curr):
            bear_ob = [round(l, 2), round(h, 2)]

        if bull_ob and bear_ob:
            break

    return {"bullish_ob": bull_ob, "bearish_ob": bear_ob}


def deteksi_sweep_mss_dan_asian_range(df_m15: pd.DataFrame, pdh: float, pdl: float) -> dict:
    """Mendeteksi Liquidity Sweep (PDH/PDL + Asian Range Judas Swing) dan MSS."""
    if len(df_m15) < 20:
        return {"sweep_signal": "NONE", "mss_signal": "NONE", "asian_sweep": "NONE"}

    recent = df_m15.tail(8)
    last_close = float(recent["Close"].iloc[-1])
    recent_high = float(recent["High"].max())
    recent_low = float(recent["Low"].min())

    sweep_signal = "NONE"
    if recent_high > pdh and last_close < pdh:
        sweep_signal = "BEARISH_LIQUIDITY_SWEEP"
    elif recent_low < pdl and last_close > pdl:
        sweep_signal = "BULLISH_LIQUIDITY_SWEEP"

    # Asian Range Sweep (00:00 - 06:00 UTC = 07:00 - 13:00 WIB)
    asian_sweep = "NONE"
    asian_high = None
    asian_low = None
    try:
        last_date = df_m15.index[-1].date()
        today_candles = df_m15[df_m15.index.date == last_date]
        asian_candles = today_candles[(today_candles.index.hour >= 0) & (today_candles.index.hour < 6)]
        if len(asian_candles) >= 4:
            asian_high = round(float(asian_candles["High"].max()), 2)
            asian_low = round(float(asian_candles["Low"].min()), 2)
            if recent_high > asian_high and last_close < asian_high:
                asian_sweep = "BEARISH_ASIAN_SWEEP"
                if sweep_signal == "NONE":
                    sweep_signal = "BEARISH_LIQUIDITY_SWEEP"
            elif recent_low < asian_low and last_close > asian_low:
                asian_sweep = "BULLISH_ASIAN_SWEEP"
                if sweep_signal == "NONE":
                    sweep_signal = "BULLISH_LIQUIDITY_SWEEP"
    except Exception:
        pass

    # Market Structure Shift (MSS) lokal M15
    prev_swing_h = float(df_m15["High"].iloc[-20:-5].max())
    prev_swing_l = float(df_m15["Low"].iloc[-20:-5].min())
    mss_signal = "NONE"
    if last_close > prev_swing_h:
        mss_signal = "BULLISH_MSS"
    elif last_close < prev_swing_l:
        mss_signal = "BEARISH_MSS"

    return {
        "sweep_signal": sweep_signal,
        "mss_signal": mss_signal,
        "asian_sweep": asian_sweep,
        "asian_high": asian_high,
        "asian_low": asian_low,
    }


def analisa_smc_matematis(aset: str) -> dict:
    is_btc = "BTC" in aset.upper()
    ticker_yf = "BTC-USD" if is_btc else "GC=F"
    symbol_td = "BTC/USD" if is_btc else "XAU/USD"

    df_d1, src_d1 = ambil_ohlcv_tv_atau_yf(ticker_yf, symbol_td, interval="1d", period="60d")
    df_h1, src_h1 = ambil_ohlcv_tv_atau_yf(ticker_yf, symbol_td, interval="1h", period="14d")
    df_m15, src_m15 = ambil_ohlcv_tv_atau_yf(ticker_yf, symbol_td, interval="15m", period="5d")

    if df_d1.empty or df_h1.empty or df_m15.empty:
        return {"asset": aset, "data_source": src_h1, "error": f"Data OHLCV {aset} kosong/gagal ditarik"}

    ohlc = hitung_daily_ohlc_levels(df_d1)
    if "error" in ohlc:
        return {"asset": aset, "data_source": src_h1, "error": ohlc["error"]}

    regime = hitung_market_regime(df_h1)
    ote_h1 = hitung_fibonacci_ote(df_h1)
    fvg_h1 = deteksi_fvg(df_h1)
    fvg_m15 = deteksi_fvg(df_m15)
    ob_h1 = deteksi_order_block(df_h1)
    sweep_mss = deteksi_sweep_mss_dan_asian_range(df_m15, ohlc["PDH"], ohlc["PDL"])

    return {
        "asset": aset,
        "data_source": src_h1,
        "market_regime": regime,
        "daily_ohlc": ohlc,
        "fibonacci_ote_h1": ote_h1,
        "fvg_h1": fvg_h1,
        "fvg_m15": fvg_m15,
        "order_block_h1": ob_h1,
        "sweep_and_mss": sweep_mss,
    }


def cek_anti_korelasi_ganda(threshold: float = 0.80) -> dict:
    try:
        df_gold, _ = ambil_ohlcv_tv_atau_yf("GC=F", "XAU/USD", interval="1d", period="45d")
        df_btc, _ = ambil_ohlcv_tv_atau_yf("BTC-USD", "BTC/USD", interval="1d", period="45d")
        if len(df_gold) >= 20 and len(df_btc) >= 20:
            s1 = df_gold["Close"].pct_change().dropna().tail(30).reset_index(drop=True)
            s2 = df_btc["Close"].pct_change().dropna().tail(30).reset_index(drop=True)
            min_len = min(len(s1), len(s2))
            corr = float(s1.tail(min_len).corr(s2.tail(min_len)))
            if np.isnan(corr):
                corr = 0.0
            return {
                "corr_30d": round(corr, 3),
                "double_exposure_blocked": abs(corr) >= threshold,
            }
    except Exception:
        pass
    return {"corr_30d": 0.0, "double_exposure_blocked": False}
