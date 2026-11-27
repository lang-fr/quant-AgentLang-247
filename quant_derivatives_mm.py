import logging
from typing import Dict, Any, List, Tuple
import numpy as np
import pandas as pd
import requests
import yfinance as yf

logger = logging.getLogger("QuantDerivativesMM")


class QuantStatDerivativesMMEngine:
    """
    Modul Lanjutan (100% API Publik Gratis):
    1. Quant Statistik: Hurst Exponent, VWAP/Price Z-Score, Cointegration Spread (XAU/DXY & BTC/ETH), & Half-Kelly Sizing
    2. Spesifik Derivatif: Perpetual Funding Annualized, Spot-Futures Basis, OI vs Price Quadrant, & Deribit Options Max Pain / GEX Proxy
    3. Market Making: L2 Order Book Imbalance (OBI), Stoikov Micro-Price, Spread BPS, & Avellaneda-Stoikov Optimal Bid/Ask Quotes
    """

    def __init__(self, timeout: int = 12):
        self.timeout = timeout
        self.headers = {"User-Agent": "QuantAgentLang247/2.9"}

    # =========================================================================
    # 1. QUANT STATISTIK (HURST EXPONENT, Z-SCORE, PAIRS SPREAD, HALF-KELLY)
    # =========================================================================
    @staticmethod
    def compute_hurst_exponent(series: pd.Series, max_lag: int = 20) -> float:
        vals = series.dropna().astype(float).values
        if len(vals) < max_lag * 3:
            return 0.5
        lags = range(2, max_lag)
        tau = [np.std(np.subtract(vals[lag:], vals[:-lag])) for lag in lags]
        tau = [t if t > 1e-12 else 1e-12 for t in tau]
        poly = np.polyfit(np.log(list(lags)), np.log(tau), 1)
        return round(float(poly[0]), 3)

    def analyze_quant_statistics(self, symbol: str) -> Dict[str, Any]:
        is_xau = symbol.upper() == "XAUUSD"
        primary_tkr = "GC=F" if is_xau else "BTC-USD"
        pair_tkr = "DX-Y.NYB" if is_xau else "ETH-USD"

        try:
            raw = yf.download([primary_tkr, pair_tkr], period="6mo", interval="1d", progress=False, auto_adjust=True)
            close_df = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw
            close_df = close_df.ffill().dropna()

            s1 = close_df[primary_tkr].astype(float)
            s2 = close_df[pair_tkr].astype(float)

            hurst = self.compute_hurst_exponent(s1, max_lag=20)
            if hurst < 0.45:
                stat_regime = "MEAN_REVERTING_FADE_EXTREMES"
            elif hurst > 0.55:
                stat_regime = "PERSISTENT_TRENDING_MOMENTUM"
            else:
                stat_regime = "RANDOM_WALK_SELECTIVE_POI"

            # Z-Score 20-hari harga & spread pasangan (Cointegration / Stat-Arb proxy)
            ma20 = s1.rolling(20).mean().iloc[-1]
            std20 = max(float(s1.rolling(20).std().iloc[-1]), 1e-9)
            price_z = round(float((s1.iloc[-1] - ma20) / std20), 2)

            log_spread = np.log(s1) + np.log(s2) if is_xau else (np.log(s1) - np.log(s2))
            sp_mean = log_spread.rolling(30).mean().iloc[-1]
            sp_std = max(float(log_spread.rolling(30).std().iloc[-1]), 1e-9)
            pair_z = round(float((log_spread.iloc[-1] - sp_mean) / sp_std), 2)

            if pair_z >= 2.0:
                stat_arb_bias = "OVEREXTENDED_HIGH_MEAN_REVERT_SHORT"
            elif pair_z <= -2.0:
                stat_arb_bias = "OVEREXTENDED_LOW_MEAN_REVERT_LONG"
            else:
                stat_arb_bias = "EQUILIBRIUM_WITHIN_2SIGMA"

            return {
                "hurst_exponent": hurst,
                "stat_regime": stat_regime,
                "price_zscore_20d": price_z,
                "pair_reference": f"{primary_tkr}_vs_{pair_tkr}",
                "pair_spread_zscore_30d": pair_z,
                "stat_arb_signal": stat_arb_bias,
            }
        except Exception as e:
            return {"status": "ERROR", "detail": str(e)}

    @staticmethod
    def compute_half_kelly_risk_pct(win_rate: float, avg_win_r: float = 2.5, avg_loss_r: float = 1.0) -> float:
        """Menghitung ukuran risiko Half-Kelly (dibatasi aman 0.5% - 2.5% per trade)."""
        if win_rate <= 0.05 or win_rate >= 0.95 or avg_loss_r <= 0:
            return 1.0
        b = avg_win_r / avg_loss_r
        kelly = win_rate - ((1.0 - win_rate) / b)
        half_kelly_pct = (kelly * 0.5) * 100.0
        return round(float(np.clip(half_kelly_pct, 0.5, 2.5)), 2)

    # =========================================================================
    # 2. SPESIFIK DERIVATIF (FUNDING, BASIS, OI QUADRANT, DERIBIT MAX PAIN)
    # =========================================================================
    def analyze_derivatives_microstructure(self, symbol: str) -> Dict[str, Any]:
        inst_swap = "XAUT-USDT-SWAP" if symbol.upper() == "XAUUSD" else "BTC-USDT-SWAP"
        inst_spot = "XAUT-USDT" if symbol.upper() == "XAUUSD" else "BTC-USDT"

        try:
            # 1. Funding Rate (OKX Public)
            r_fund = requests.get(
                f"https://www.okx.com/api/v5/public/funding-rate?instId={inst_swap}",
                headers=self.headers,
                timeout=self.timeout,
            ).json()
            fund_data = (r_fund.get("data") or [{}])[0]
            funding_rate = float(fund_data.get("fundingRate", 0.0))
            funding_apr = round(funding_rate * 3 * 365 * 100.0, 2)

            # 2. Spot vs Futures Basis (Contango / Backwardation)
            r_spot = requests.get(
                f"https://www.okx.com/api/v5/market/ticker?instId={inst_spot}",
                headers=self.headers,
                timeout=self.timeout,
            ).json()
            r_perp = requests.get(
                f"https://www.okx.com/api/v5/market/ticker?instId={inst_swap}",
                headers=self.headers,
                timeout=self.timeout,
            ).json()
            spot_px = float(((r_spot.get("data") or [{}])[0]).get("last", 0.0))
            perp_px = float(((r_perp.get("data") or [{}])[0]).get("last", 0.0))
            basis_bps = round(((perp_px - spot_px) / max(spot_px, 1e-9)) * 10000.0, 2) if spot_px > 0 else 0.0

            # 3. Open Interest & Kuadran Harga vs OI (Candle 1H OKX)
            r_oi = requests.get(
                f"https://www.okx.com/api/v5/public/open-interest?instId={inst_swap}",
                headers=self.headers,
                timeout=self.timeout,
            ).json()
            oi_usd = float(((r_oi.get("data") or [{}])[0]).get("oiUsd", 0.0))

            r_kline = requests.get(
                f"https://www.okx.com/api/v5/market/candles?instId={inst_swap}&bar=1H&limit=3",
                headers=self.headers,
                timeout=self.timeout,
            ).json()
            candles = r_kline.get("data") or []
            px_delta_pct = 0.0
            if len(candles) >= 2:
                c_now = float(candles[0][4])
                c_prev = float(candles[1][4])
                px_delta_pct = round(((c_now - c_prev) / max(c_prev, 1e-9)) * 100.0, 3)

            if funding_rate > 0.0003 and basis_bps > 8.0:
                squeeze_risk = "HIGH_LONG_CROWDING_SQUEEZE_DOWN_RISK"
            elif funding_rate < -0.0002 and basis_bps < -5.0:
                squeeze_risk = "HIGH_SHORT_CROWDING_SQUEEZE_UP_RISK"
            else:
                squeeze_risk = "BALANCED_DERIVATIVES_LEVERAGE"

            result: Dict[str, Any] = {
                "instrument": inst_swap,
                "funding_rate_pct": round(funding_rate * 100.0, 4),
                "funding_apr_pct": funding_apr,
                "spot_perp_basis_bps": basis_bps,
                "open_interest_usd_m": round(oi_usd / 1e6, 2),
                "h1_price_delta_pct": px_delta_pct,
                "squeeze_regime": squeeze_risk,
            }

            # 4. Khusus BTC: Hitung Options Max Pain & Gamma Wall dari Deribit
            if symbol.upper() != "XAUUSD":
                result["deribit_options_levels"] = self._compute_deribit_max_pain_and_walls(spot_px)

            return result
        except Exception as e:
            return {"status": "ERROR", "detail": str(e)}

    def _compute_deribit_max_pain_and_walls(self, live_btc: float) -> Dict[str, Any]:
        try:
            r = requests.get(
                "https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency=BTC&kind=option",
                headers=self.headers,
                timeout=15,
            ).json()
            strikes_call: Dict[float, float] = {}
            strikes_put: Dict[float, float] = {}

            for item in r.get("result", []):
                name = str(item.get("instrument_name", ""))
                oi = float(item.get("open_interest", 0.0))
                parts = name.split("-")
                if len(parts) < 4 or oi <= 0:
                    continue
                try:
                    k = float(parts[2])
                except ValueError:
                    continue
                if parts[3] == "C":
                    strikes_call[k] = strikes_call.get(k, 0.0) + oi
                elif parts[3] == "P":
                    strikes_put[k] = strikes_put.get(k, 0.0) + oi

            all_strikes = sorted(set(strikes_call.keys()) | set(strikes_put.keys()))
            if not all_strikes:
                return {"status": "NO_OPTIONS_STRIKES"}

            # Filter strike dalam rentang +-25% dari harga spot agar fokus ke expiry terdekat
            ref = live_btc if live_btc > 0 else float(np.median(all_strikes))
            near_strikes = [k for k in all_strikes if 0.75 * ref <= k <= 1.25 * ref] or all_strikes

            min_pain = float("inf")
            max_pain_strike = ref
            for test_px in near_strikes:
                call_loss = sum(max(0.0, test_px - k) * oi for k, oi in strikes_call.items())
                put_loss = sum(max(0.0, k - test_px) * oi for k, oi in strikes_put.items())
                total_loss = call_loss + put_loss
                if total_loss < min_pain:
                    min_pain = total_loss
                    max_pain_strike = test_px

            call_wall = max(near_strikes, key=lambda k: strikes_call.get(k, 0.0))
            put_wall = max(near_strikes, key=lambda k: strikes_put.get(k, 0.0))

            return {
                "max_pain_magnet": max_pain_strike,
                "call_resistance_wall": call_wall,
                "put_support_wall": put_wall,
            }
        except Exception as e:
            return {"status": "ERROR", "detail": str(e)}

    # =========================================================================
    # 3. MARKET MAKING (L2 OBI, STOIKOV MICRO-PRICE, AVELLANEDA-STOIKOV QUOTES)
    # =========================================================================
    def analyze_market_making_orderbook(
        self, symbol: str, inventory_skew: float = 0.0, gamma: float = 0.15
    ) -> Dict[str, Any]:
        """
        Menghitung dinamika Market Making dari Order Book L2 (20 level):
        - Order Book Imbalance (OBI)
        - Stoikov Micro-Price (nilai wajar berdasarkan tekanan antrean bid/ask)
        - Avellaneda-Stoikov Reservation Price & Optimal Bid/Ask Quote
        """
        inst_id = "XAUT-USDT" if symbol.upper() == "XAUUSD" else "BTC-USDT"
        try:
            r = requests.get(
                f"https://www.okx.com/api/v5/market/books?instId={inst_id}&sz=20",
                headers=self.headers,
                timeout=self.timeout,
            ).json()
            book = (r.get("data") or [{}])[0]
            bids = book.get("bids") or []
            asks = book.get("asks") or []
            if not bids or not asks:
                return {"status": "EMPTY_ORDERBOOK"}

            best_bid = float(bids[0][0])
            best_bid_sz = float(bids[0][1])
            best_ask = float(asks[0][0])
            best_ask_sz = float(asks[0][1])

            mid_price = (best_bid + best_ask) / 2.0
            spread = max(best_ask - best_bid, 1e-6)
            spread_bps = round((spread / mid_price) * 10000.0, 2)

            # Stoikov Micro-Price (weighted by opposite top-of-book size)
            micro_price = (best_bid * best_ask_sz + best_ask * best_bid_sz) / max(best_bid_sz + best_ask_sz, 1e-9)

            # Order Book Imbalance (OBI) 20 level teratas
            total_bid_vol = sum(float(b[1]) for b in bids)
            total_ask_vol = sum(float(a[1]) for a in asks)
            obi = round((total_bid_vol - total_ask_vol) / max(total_bid_vol + total_ask_vol, 1e-9), 3)

            # Avellaneda-Stoikov Reservation Price & Optimal Half-Spread
            sigma_proxy = max(spread * 3.5, mid_price * 0.0012)
            reservation_px = micro_price - (inventory_skew * gamma * (sigma_proxy ** 2) / max(mid_price, 1.0))
            half_opt_spread = max(spread * 1.25, mid_price * 0.0008)

            mm_bid_quote = round(reservation_px - half_opt_spread, 2)
            mm_ask_quote = round(reservation_px + half_opt_spread, 2)

            if obi >= 0.30:
                mm_flow = "STRONG_BID_WALL_SUPPORT_ACCUMULATION"
            elif obi <= -0.30:
                mm_flow = "STRONG_ASK_WALL_RESISTANCE_DISTRIBUTION"
            else:
                mm_flow = "TWO_SIDED_LIQUIDITY_NEUTRAL"

            toxicity_alert = "TOXIC_FLOW_WIDEN_QUOTES" if (abs(obi) >= 0.55 or spread_bps > 8.0) else "HEALTHY_MM_BOOK"

            return {
                "instrument": inst_id,
                "mid_price": round(mid_price, 2),
                "stoikov_micro_price": round(micro_price, 2),
                "spread_bps": spread_bps,
                "orderbook_imbalance_obi": obi,
                "mm_flow_verdict": mm_flow,
                "toxicity_status": toxicity_alert,
                "avellaneda_optimal_bid_limit": mm_bid_quote,
                "avellaneda_optimal_ask_limit": mm_ask_quote,
            }
        except Exception as e:
            return {"status": "ERROR", "detail": str(e)}

    def run_all_three_pillars(self, symbol: str) -> Dict[str, Any]:
        return {
            "symbol": symbol,
            "quant_statistics": self.analyze_quant_statistics(symbol),
            "derivatives_microstructure": self.analyze_derivatives_microstructure(symbol),
            "market_making_orderbook": self.analyze_market_making_orderbook(symbol),
        }


if __name__ == "__main__":
    import json
    eng = QuantStatDerivativesMMEngine()
    print(json.dumps(eng.run_all_three_pillars("BTCUSDT"), indent=2))
