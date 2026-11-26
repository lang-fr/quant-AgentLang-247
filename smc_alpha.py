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
    hari_ini = df_d1.
