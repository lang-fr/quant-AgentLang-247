import numpy as np
import pandas as pd
import yfinance as yf


def _bersihkan_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df.dropna(subset=["Open", "High", "Low", "Close"]).copy()


def hitung_daily_ohlc_levels(df_d1: pd.DataFrame) -> dict:
    """Menghitung Pivot, New High (NH), New Low (NL), dan bias harian mekanis."""
    df = _bersihkan_df(df_d1)
    if len(df) < 2:
        return {"error": "Data D1 tidak cukup"}

    kemarin = df.iloc[-2]
    hari_ini = df.iloc[-1]

    o_y, h_y, l_y, c_y = (
        float(kemarin["Open"]),
        float(kemarin["High"]),
        float(kemarin["Low"]),
        float(kemarin["Close"]),
    )
    o_t = float(hari_ini["Open"])

    pivot = (h_y + l_y + c_y) / 3.0
    bias_harian = "BUY" if o_t > pivot else "SELL"

    # Rumus konstanta X berdasarkan sifat candle kemarin (BEAR vs BULL)
    if o_y > c_y:
        x = ((2.0 * l_y) + h_y + c_y) / 2.0
    else:
        x = ((2.0 * h_y) + l_y + c_y) / 2.0

    nh = x - l_y  # Zona Resisten / Target Sell
    nl = x - h_y  # Zona Support / Target Buy

    return {
        "bias_ohlc": bias_harian,
        "today_open": round(o_t, 2),
        "pivot": round(pivot, 2),
        "NH_resisten": round(nh, 2),
        "NL_support": round(nl, 2),
    }


def deteksi_unmitigated_fvg(df: pd.DataFrame, tf_label: str = "M15", lookback: int = 60) -> dict:
    """Mendeteksi celah Fair Value Gap (3-candle imbalance) yang belum tertutup (unmitigated)."""
    data = _bersihkan_df(df).tail(lookback)
    if len(data) < 5:
        return {"bullish_fvg": None, "bearish_fvg": None}

    highs = data["High"].values
    lows = data["Low"].values

    bull_fvgs = []
    bear_fvgs = []

    for i in range(len(data) - 3):
        # Bullish FVG: Low candle ke-3 > High candle ke-1
        if lows[i + 2] > highs[i]:
            bawah = float(highs[i])
            atas = float(lows[i + 2])
            # Cek apakah candle setelahnya sudah menutup seluruh celah (mitigated)
            low_setelahnya = float(np.min(lows[i + 3 :])) if (i + 3) < len(data) else atas
            if low_setelahnya > bawah:
                bull_fvgs.append({
                    "tf": tf_label,
                    "top": round(atas, 2),
                    "bottom": round(bawah, 2),
                    "mid_ce": round((atas + bawah) / 2.0, 2),
                })

        # Bearish FVG: High candle ke-3 < Low candle ke-1
        if highs[i + 2] < lows[i]:
            atas = float(lows[i])
            bawah = float(highs[i + 2])
            high_setelahnya = float(np.max(highs[i + 3 :])) if (i + 3) < len(data) else bawah
            if high_setelahnya < atas:
                bear_fvgs.append({
                    "tf": tf_label,
                    "top": round(atas, 2),
                    "bottom": round(bawah, 2),
                    "mid_ce": round((atas + bawah) / 2.0, 2),
                })

    return {
        "bullish_fvg": bull_fvgs[-1] if bull_fvgs else None,
        "bearish_fvg": bear_fvgs[-1] if bear_fvgs else None,
    }


def deteksi_order_block(df: pd.DataFrame, tf_label: str = "H1", lookback: int = 60) -> dict:
    """Mendeteksi Unmitigated Order Block sebelum pergerakan impulsif (displacement)."""
    data = _bersihkan_df(df).tail(lookback)
    if len(data) < 25:
        return {"bullish_ob": None, "bearish_ob": None}

    opens = data["Open"].values
    highs = data["High"].values
    lows = data["Low"].values
    closes = data["Close"].values
    bodies = np.abs(closes - opens)
    avg_body = float(np.mean(bodies)) if np.mean(bodies) > 0 else 1.0

    bull_obs = []
    bear_obs = []

    for i in range(1, len(data) - 2):
        # Bullish OB: candle bearish diikuti candle bullish impulsif (> 1.6x rata-rata body)
        if closes[i] < opens[i] and closes[i + 1] > opens[i + 1] and bodies[i + 1] > (1.6 * avg_body):
            ob_high = float(highs[i])
            ob_low = float(lows[i])
            min_setelahnya = float(np.min(lows[i + 2 :]))
            if min_setelahnya > ob_low:
                bull_obs.append({
                    "tf": tf_label,
                    "high": round(ob_high, 2),
                    "low": round(ob_low, 2),
                })

        # Bearish OB: candle bullish diikuti candle bearish impulsif (> 1.6x rata-rata body)
        if closes[i] > opens[i] and closes[i + 1] < opens[i + 1] and bodies[i + 1] > (1.6 * avg_body):
            ob_high = float(highs[i])
            ob_low = float(lows[i])
            max_setelahnya = float(np.max(highs[i + 2 :]))
            if max_setelahnya < ob_high:
                bear_obs.append({
                    "tf": tf_label,
                    "high": round(ob_high, 2),
                    "low": round(ob_low, 2),
                })

    return {
        "bullish_ob": bull_obs[-1] if bull_obs else None,
        "bearish_ob": bear_obs[-1] if bear_obs else None,
    }


def hitung_fibonacci_ote(df_h1: pd.DataFrame, lookback: int = 48) -> dict:
    """Menghitung rentang Dealing Range H1, Equilibrium (0.5), dan zona OTE (0.618 - 0.786)."""
    data = _bersihkan_df(df_h1).tail(lookback)
    if len(data) < 10:
        return {"error": "Data H1 tidak cukup untuk OTE"}

    swing_high = float(data["High"].max())
    swing_low = float(data["Low"].min())
    last_close = float(data["Close"].iloc[-1])
    rentang = swing_high - swing_low
    if rentang <= 0:
        return {"error": "Rentang harga nol"}

    eq_50 = swing_low + (0.50 * rentang)
    # Zona diskon untuk skenario BUY (retracement dari High ke Low)
    buy_ote_618 = swing_high - (0.618 * rentang)
    buy_ote_705 = swing_high - (0.705 * rentang)
    buy_ote_786 = swing_high - (0.786 * rentang)

    # Zona premium untuk skenario SELL (retracement dari Low ke High)
    sell_ote_618 = swing_low + (0.618 * rentang)
    sell_ote_705 = swing_low + (0.705 * rentang)
    sell_ote_786 = swing_low + (0.786 * rentang)

    if last_close <= buy_ote_618 and last_close >= buy_ote_786:
        posisi = "IN_BUY_OTE_DISCOUNT"
    elif last_close >= sell_ote_618 and last_close <= sell_ote_786:
        posisi = "IN_SELL_OTE_PREMIUM"
    elif last_close < eq_50:
        posisi = "DISCOUNT_ZONE"
    else:
        posisi = "PREMIUM_ZONE"

    return {
        "swing_high": round(swing_high, 2),
        "swing_low": round(swing_low, 2),
        "equilibrium_50": round(eq_50, 2),
        "buy_ote_zone": [round(buy_ote_786, 2), round(buy_ote_618, 2)],
        "buy_sweet_spot_705": round(buy_ote_705, 2),
        "sell_ote_zone": [round(sell_ote_618, 2), round(sell_ote_786, 2)],
        "sell_sweet_spot_705": round(sell_ote_705, 2),
        "price_position": posisi,
    }


def cek_anti_korelasi_ganda(threshold: float = 0.80) -> dict:
    """Memeriksa korelasi 30 hari XAUUSD vs BTCUSDT untuk mencegah eksposur ganda."""
    try:
        g = _bersihkan_df(yf.download("GC=F", period="60d", interval="1d", progress=False))["Close"]
        b = _bersihkan_df(yf.download("BTC-USD", period="60d", interval="1d", progress=False))["Close"]
        df_join = pd.DataFrame({"XAU": g, "BTC": b}).dropna().tail(30)
        if len(df_join) < 15:
            return {"corr_30d": 0.0, "double_exposure_blocked": False}

        ret = np.log(df_join / df_join.shift(1)).dropna()
        corr_val = round(float(ret["XAU"].corr(ret["BTC"])), 3)
        return {
            "corr_30d": corr_val,
            "double_exposure_blocked": abs(corr_val) >= threshold,
            "rule": "Pilih 1 aset skor tertinggi saja jika |corr| >= 0.80" if abs(corr_val) >= threshold else "Aman eksekusi independen",
        }
    except Exception as e:
        return {"corr_30d": 0.0, "double_exposure_blocked": False, "error": str(e)}


def analisa_smc_matematis(ticker_yf: str, aset_label: str) -> dict:
    """Paket lengkap koordinat SMC/ICT + OHLC + OTE untuk satu instrumen."""
    try:
        df_m15 = _bersihkan_df(yf.download(ticker_yf, period="5d", interval="15m", progress=False))
        df_h1 = _bersihkan_df(yf.download(ticker_yf, period="30d", interval="1h", progress=False))
        df_d1 = _bersihkan_df(yf.download(ticker_yf, period="10d", interval="1d", progress=False))

        return {
            "asset": aset_label,
            "daily_ohlc": hitung_daily_ohlc_levels(df_d1),
            "fvg_m15": deteksi_unmitigated_fvg(df_m15, "M15", lookback=60),
            "fvg_h1": deteksi_unmitigated_fvg(df_h1, "H1", lookback=48),
            "order_block_h1": deteksi_order_block(df_h1, "H1", lookback=48),
            "fibonacci_ote_h1": hitung_fibonacci_ote(df_h1, lookback=48),
        }
    except Exception as e:
        return {"asset": aset_label, "error": str(e)}
