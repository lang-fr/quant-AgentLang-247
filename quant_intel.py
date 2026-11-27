import os
import json
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from scipy.optimize import minimize
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("QuantIntelEngine")


class QuantIntelEngine:
    """
    Tahap 2: Mesin Intelijen Kuantitatif & Intermarket Institusional (100% Gratis):
    1. Kalkulasi Matematis OHLC Daily Pivot, New High (NH), & New Low (NL)
    2. Estimasi Volatilitas GARCH(1,1) & Tail Risk CVaR (95% & 99%)
    3. Filter Korelasi Intermarket (DXY, US02Y, US10Y, S&P500, NASDAQ100)
    4. Posisi Institusional CFTC COT (Commitments of Traders - Socrata Public API)
    5. Arus Likuiditas On-Chain Stablecoin 7 Hari (DeFiLlama Public API)
    """

    def __init__(self, timeout: int = 20):
        self.timeout = timeout
        self.fred_api_key = os.getenv("FRED_API_KEY", "").strip()
        self.headers = {
            "User-Agent": "QuantAgentLang247/2.0 (Institutional-Research; contact@local.dev)"
        }

    # =========================================================================
    # 1. KALKULASI MATEMATIS OHLC (DAILY PIVOT, NEW HIGH / NH, NEW LOW / NL)
    # =========================================================================
    def calculate_ohlc_levels(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Menghitung konstanta X, New High (NH / Resisten), dan New Low (NL / Support)
        berdasarkan candle harian terakhir yang sudah tutup (completed daily bar).
        """
        if df is None or len(df) < 2:
            return {"status": "INSUFFICIENT_DATA"}

        # Gunakan candle kemarin (-2) jika candle hari ini (-1) masih berjalan
        prev = df.iloc[-2]
        o = float(prev["Open"])
        h = float(prev["High"])
        l = float(prev["Low"])
        c = float(prev["Close"])
        live_price = float(df.iloc[-1]["Close"])

        if c < o:
            candle_regime = "BEAR"
            x_val = ((2.0 * l) + h + c) / 2.0
        elif c > o:
            candle_regime = "BULL"
            x_val = ((2.0 * h) + l + c) / 2.0
        else:
            candle_regime = "DOJI"
            x_val = (h + l + (2.0 * c)) / 2.0

        new_high = x_val - l  # Zona optimal Sell Limit (Premium)
        new_low = x_val - h   # Zona optimal Buy Limit (Discount)
        pivot_mid = (new_high + new_low) / 2.0

        if live_price > new_high:
            regime_shift = "BREAKOUT_BULLISH_ABOVE_NH"
        elif live_price < new_low:
            regime_shift = "BREAKDOWN_BEARISH_BELOW_NL"
        elif live_price >= pivot_mid:
            regime_shift = "PREMIUM_ZONE_NEAR_NH"
        else:
            regime_shift = "DISCOUNT_ZONE_NEAR_NL"

        return {
            "prev_candle_regime": candle_regime,
            "prev_ohlc": {
                "open": round(o, 4),
                "high": round(h, 4),
                "low": round(l, 4),
                "close": round(c, 4),
            },
            "constant_X": round(x_val, 4),
            "new_high_NH": round(new_high, 4),
            "new_low_NL": round(new_low, 4),
            "equilibrium_mid": round(pivot_mid, 4),
            "live_price": round(live_price, 4),
            "spatial_status": regime_shift,
        }

    # =========================================================================
    # 2. GARCH(1,1) CONDITIONAL VOLATILITY & CVaR (95% / 99%)
    # =========================================================================
    def calculate_garch_and_cvar(
        self, close_series: pd.Series, annual_factor: int = 252
    ) -> Dict[str, Any]:
        """
        Menghitung volatilitas bersyarat GARCH(1,1) dan Conditional Value at Risk (CVaR)
        untuk mengukur risiko ekor (tail risk) sebelum menentukan ukuran lot.
        """
        prices = close_series.dropna().astype(float)
        if len(prices) < 35:
            return {"status": "INSUFFICIENT_DATA"}

        returns = np.log(prices / prices.shift(1)).dropna().values
        sample_var = float(np.var(returns, ddof=1))
        if sample_var <= 1e-12:
            return {"status": "ZERO_VARIANCE"}

        def garch_neg_loglik(params: np.ndarray) -> float:
            omega, alpha, beta = params
            if omega <= 0 or alpha < 0 or beta < 0 or (alpha + beta) >= 0.995:
                return 1e10
            n = len(returns)
            sigma2 = np.empty(n)
            sigma2[0] = sample_var
            for t in range(1, n):
                sigma2[t] = omega + alpha * (returns[t - 1] ** 2) + beta * sigma2[t - 1]
                if sigma2[t] <= 1e-12:
                    sigma2[t] = 1e-12
            ll = -0.5 * np.sum(np.log(2.0 * np.pi) + np.log(sigma2) + (returns ** 2) / sigma2)
            return -float(ll)

        init_params = np.array([sample_var * 0.05, 0.08, 0.87])
        bounds = [(1e-10, sample_var * 5.0), (0.001, 0.45), (0.40, 0.985)]

        try:
            res = minimize(garch_neg_loglik, init_params, method="L-BFGS-B", bounds=bounds)
            if res.success and (res.x[1] + res.x[2]) < 0.995:
                omega, alpha, beta = res.x
            else:
                omega, alpha, beta = init_params
        except Exception:
            omega, alpha, beta = init_params

        sigma2 = np.empty(len(returns))
        sigma2[0] = sample_var
        for t in range(1, len(returns)):
            sigma2[t] = omega + alpha * (returns[t - 1] ** 2) + beta * sigma2[t - 1]

        next_var = omega + alpha * (returns[-1] ** 2) + beta * sigma2[-1]
        daily_vol = float(np.sqrt(max(next_var, 1e-12)))
        annual_vol = daily_vol * np.sqrt(annual_factor)

        # Historical & GARCH-scaled CVaR (Expected Shortfall)
        var_95 = float(np.percentile(returns, 5))
        var_99 = float(np.percentile(returns, 1))
        tail_95 = returns[returns <= var_95]
        tail_99 = returns[returns <= var_99]

        cvar_95 = float(tail_95.mean()) if len(tail_95) > 0 else var_95
        cvar_99 = float(tail_99.mean()) if len(tail_99) > 0 else var_99

        # Klasifikasi rezim volatilitas
        hist_daily_vol = float(np.std(returns, ddof=1))
        vol_ratio = daily_vol / max(hist_daily_vol, 1e-9)
        if vol_ratio >= 1.35 or abs(cvar_99) >= 0.045:
            vol_regime = "EXTREME_VOLATILITY_REDUCE_SIZE"
        elif vol_ratio >= 1.10:
            vol_regime = "ELEVATED_VOLATILITY"
        else:
            vol_regime = "NORMAL_VOLATILITY"

        return {
            "garch_params": {
                "omega": round(float(omega), 8),
                "alpha": round(float(alpha), 4),
                "beta": round(float(beta), 4),
                "persistence": round(float(alpha + beta), 4),
            },
            "next_day_volatility_pct": round(daily_vol * 100.0, 3),
            "annualized_volatility_pct": round(annual_vol * 100.0, 2),
            "cvar_95_daily_pct": round(abs(cvar_95) * 100.0, 3),
            "cvar_99_daily_pct": round(abs(cvar_99) * 100.0, 3),
            "volatility_ratio_vs_hist": round(vol_ratio, 3),
            "volatility_regime": vol_regime,
        }

    # =========================================================================
    # 3. FILTER KORELASI INTERMARKET (DXY, US02Y, US10Y, S&P500, NASDAQ100)
    # =========================================================================
    def _fetch_fred_latest(self, series_id: str) -> Optional[float]:
        if not self.fred_api_key:
            return None
        url = (
            f"https://api.stlouisfed.org/fred/series/observations"
            f"?series_id={series_id}&api_key={self.fred_api_key}&file_type=json&sort_order=desc&limit=5"
        )
        try:
            r = requests.get(url, timeout=self.timeout)
            if r.status_code == 200:
                for obs in r.json().get("observations", []):
                    val = obs.get("value", ".")
                    if val != ".":
                        return float(val)
        except Exception:
            pass
        return None

    def get_intermarket_correlation(
        self, period: str = "6mo", window: int = 20
    ) -> Dict[str, Any]:
        """
        Menghitung korelasi rolling 20-hari antara XAUUSD & BTCUSD terhadap
        DXY, US02Y, US10Y, S&P500, dan NASDAQ100 via Yahoo Finance (+ FRED fallback).
        """
        tickers = {
            "XAUUSD": "GC=F",
            "BTCUSD": "BTC-USD",
            "DXY": "DX-Y.NYB",
            "US02Y": "^IRX",   # Proxy yield jangka pendek AS (13-wk T-Bill) jika DGS2 tidak tersedia
            "US10Y": "^TNX",   # Yield Obligasi AS 10 Tahun
            "SP500": "^GSPC",
            "NASDAQ100": "^IXIC",
        }

        try:
            raw = yf.download(
                list(tickers.values()),
                period=period,
                interval="1d",
                progress=False,
                auto_adjust=True,
            )
            if raw is None or raw.empty:
                return {"status": "YF_DOWNLOAD_EMPTY"}

            close_df = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw
            inv_map = {v: k for k, v in tickers.items()}
            close_df = close_df.rename(columns=inv_map).ffill().dropna(how="all")

            # Override yield resmi FRED jika tersedia
            fred_2y = self._fetch_fred_latest("DGS2")
            fred_10y = self._fetch_fred_latest("DGS10")

            ret_df = close_df.pct_change().dropna()
            recent_ret = ret_df.tail(window)

            latest_levels: Dict[str, float] = {}
            daily_changes_pct: Dict[str, float] = {}
            for col in close_df.columns:
                s = close_df[col].dropna()
                if len(s) >= 2:
                    latest_levels[col] = round(float(s.iloc[-1]), 3)
                    chg = ((float(s.iloc[-1]) - float(s.iloc[-2])) / max(abs(float(s.iloc[-2])), 1e-9)) * 100.0
                    daily_changes_pct[col] = round(chg, 3)

            if fred_2y is not None:
                latest_levels["US02Y"] = round(fred_2y, 3)
            if fred_10y is not None:
                latest_levels["US10Y"] = round(fred_10y, 3)

            drivers = ["DXY", "US02Y", "US10Y", "SP500", "NASDAQ100"]
            xau_corr: Dict[str, float] = {}
            btc_corr: Dict[str, float] = {}

            for drv in drivers:
                if drv in recent_ret.columns and "XAUUSD" in recent_ret.columns:
                    c_val = recent_ret["XAUUSD"].corr(recent_ret[drv])
                    xau_corr[drv] = round(float(c_val), 3) if pd.notna(c_val) else 0.0
                if drv in recent_ret.columns and "BTCUSD" in recent_ret.columns:
                    c_val = recent_ret["BTCUSD"].corr(recent_ret[drv])
                    btc_corr[drv] = round(float(c_val), 3) if pd.notna(c_val) else 0.0

            # Penilaian tekanan makro terhadap XAUUSD & BTCUSD
            dxy_chg = daily_changes_pct.get("DXY", 0.0)
            us10y_chg = daily_changes_pct.get("US10Y", 0.0)
            ndx_chg = daily_changes_pct.get("NASDAQ100", 0.0)

            if dxy_chg < -0.15 and us10y_chg < 0:
                gold_macro_filter = "MENDUKUNG_BULLISH_XAU"
            elif dxy_chg > 0.20 and us10y_chg > 0:
                gold_macro_filter = "MENEKAN_BEARISH_XAU"
            else:
                gold_macro_filter = "NETRAL_MIXED_XAU"

            if ndx_chg > 0.30 and dxy_chg <= 0.10:
                btc_macro_filter = "RISK_ON_MENDUKUNG_BTC"
            elif ndx_chg < -0.50 or dxy_chg > 0.25:
                btc_macro_filter = "RISK_OFF_MENEKAN_BTC"
            else:
                btc_macro_filter = "NETRAL_MIXED_BTC"

            return {
                "latest_levels": latest_levels,
                "daily_changes_pct": daily_changes_pct,
                "rolling_20d_corr_xauusd": xau_corr,
                "rolling_20d_corr_btcusd": btc_corr,
                "xauusd_macro_verdict": gold_macro_filter,
                "btcusd_macro_verdict": btc_macro_filter,
            }
        except Exception as e:
            logger.warning(f"Gagal menghitung korelasi intermarket: {e}")
            return {"status": "ERROR", "detail": str(e)}

    # =========================================================================
    # 4. CFTC COT (COMMITMENTS OF TRADERS) - PUBLIC SOCRATA API (GRATIS)
    # =========================================================================
    def fetch_cftc_cot(self, commodity_keyword: str = "GOLD") -> Dict[str, Any]:
        """
        Mengambil laporan mingguan resmi CFTC Legacy Futures Only (dataset 6dca-aqww)
        secara gratis tanpa API key untuk membaca posisi Non-Commercial (Big Speculators)
        vs Commercial (Hedgers).
        """
        url = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
        params = {
            "$where": f"upper(market_and_exchange_names) like '%{commodity_keyword.upper()}%'",
            "$order": "report_date_as_yyyy_mm_dd DESC",
            "$limit": 4,
        }
        try:
            resp = requests.get(url, params=params, headers=self.headers, timeout=self.timeout)
            resp.raise_for_status()
            rows = resp.json()
            if not rows:
                return {"status": "NO_COT_ROWS", "keyword": commodity_keyword}

            latest = rows[0]
            prev = rows[1] if len(rows) > 1 else latest

            noncomm_long = float(latest.get("noncomm_positions_long_all", 0))
            noncomm_short = float(latest.get("noncomm_positions_short_all", 0))
            comm_long = float(latest.get("comm_positions_long_all", 0))
            comm_short = float(latest.get("comm_positions_short_all", 0))
            oi = float(latest.get("open_interest_all", 1))

            net_noncomm = noncomm_long - noncomm_short
            prev_net_noncomm = float(prev.get("noncomm_positions_long_all", 0)) - float(
                prev.get("noncomm_positions_short_all", 0)
            )
            weekly_net_change = net_noncomm - prev_net_noncomm
            net_oi_pct = (net_noncomm / max(oi, 1.0)) * 100.0

            if net_noncomm > 0 and weekly_net_change > 0:
                cot_bias = "INSTITUTIONAL_BULLISH_ACCUMULATION"
            elif net_noncomm > 0 and weekly_net_change < 0:
                cot_bias = "BULLISH_LONG_PROFIT_TAKING"
            elif net_noncomm < 0 and weekly_net_change < 0:
                cot_bias = "INSTITUTIONAL_BEARISH_DISTRIBUTION"
            else:
                cot_bias = "NEUTRAL_TRANSITION"

            return {
                "market": latest.get("market_and_exchange_names", commodity_keyword),
                "report_date": str(latest.get("report_date_as_yyyy_mm_dd", ""))[:10],
                "open_interest": int(oi),
                "noncomm_long": int(noncomm_long),
                "noncomm_short": int(noncomm_short),
                "noncomm_net": int(net_noncomm),
                "noncomm_weekly_change": int(weekly_net_change),
                "noncomm_net_pct_oi": round(net_oi_pct, 2),
                "comm_net": int(comm_long - comm_short),
                "cot_institutional_bias": cot_bias,
            }
        except Exception as e:
            logger.warning(f"Gagal mengambil data CFTC COT ({commodity_keyword}): {e}")
            return {"status": "ERROR", "detail": str(e)}

    # =========================================================================
    # 5. DEFILLAMA STABLECOIN LIQUIDITY FLOW (PUBLIC API GRATIS)
    # =========================================================================
    def fetch_defillama_stablecoin_flow(self) -> Dict[str, Any]:
        """
        Mengambil data total kapitalisasi stablecoin global & perubahan arus dana 7 hari
        dari DeFiLlama Public API (tanpa API key).
        """
        url = "https://stablecoins.llama.fi/stablecoincharts/all"
        try:
            resp = requests.get(url, headers=self.headers, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list) or len(data) < 8:
                return {"status": "INSUFFICIENT_DEFILLAMA_DATA"}

            latest_cap = float(data[-1].get("totalCirculatingUSD", {}).get("peggedUSD", 0))
            prev_1d_cap = float(data[-2].get("totalCirculatingUSD", {}).get("peggedUSD", latest_cap))
            prev_7d_cap = float(data[-8].get("totalCirculatingUSD", {}).get("peggedUSD", latest_cap))

            flow_1d_b = (latest_cap - prev_1d_cap) / 1e9
            flow_7d_b = (latest_cap - prev_7d_cap) / 1e9
            total_b = latest_cap / 1e9

            if flow_7d_b > 0.50:
                liquidity_regime = "STRONG_STABLECOIN_INFLOW_RISK_ON"
            elif flow_7d_b > 0:
                liquidity_regime = "MILD_STABLECOIN_INFLOW"
            elif flow_7d_b < -0.50:
                liquidity_regime = "STABLECOIN_OUTFLOW_RISK_OFF"
            else:
                liquidity_regime = "FLAT_LIQUIDITY"

            return {
                "total_stablecoin_mcap_usd_b": round(total_b, 2),
                "net_flow_24h_usd_b": round(flow_1d_b, 3),
                "net_flow_7d_usd_b": round(flow_7d_b, 3),
                "liquidity_regime": liquidity_regime,
            }
        except Exception as e:
            logger.warning(f"Gagal mengambil data DeFiLlama Stablecoins: {e}")
            return {"status": "ERROR", "detail": str(e)}

    # =========================================================================
    # AGREGATOR UTAMA UNTUK XAUUSD & BTCUSDT
    # =========================================================================
    def run_full_quant_scan(self) -> Dict[str, Any]:
        """Menjalankan seluruh pemindaian kuantitatif Tahap 2 secara terpadu."""
        xau_hist = yf.download("GC=F", period="6mo", interval="1d", progress=False, auto_adjust=True)
        btc_hist = yf.download("BTC-USD", period="6mo", interval="1d", progress=False, auto_adjust=True)

        if isinstance(xau_hist.columns, pd.MultiIndex):
            xau_hist.columns = xau_hist.columns.get_level_values(0)
        if isinstance(btc_hist.columns, pd.MultiIndex):
            btc_hist.columns = btc_hist.columns.get_level_values(0)

        return {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "xauusd": {
                "ohlc_strategy": self.calculate_ohlc_levels(xau_hist),
                "garch_cvar_risk": self.calculate_garch_and_cvar(xau_hist["Close"], annual_factor=252),
                "cftc_cot": self.fetch_cftc_cot("GOLD"),
            },
            "btcusdt": {
                "ohlc_strategy": self.calculate_ohlc_levels(btc_hist),
                "garch_cvar_risk": self.calculate_garch_and_cvar(btc_hist["Close"], annual_factor=365),
                "cftc_cot": self.fetch_cftc_cot("BITCOIN"),
                "defillama_liquidity": self.fetch_defillama_stablecoin_flow(),
            },
            "intermarket_correlation": self.get_intermarket_correlation(period="6mo", window=20),
        }


if __name__ == "__main__":
    engine = QuantIntelEngine()
    report = engine.run_full_quant_scan()
    print(json.dumps(report, indent=2))
