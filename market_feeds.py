import os
import requests
import yfinance as yf
from datetime import datetime, timezone


class InstitutionalMarketFeeds:
    def collect_all_8_pillars(self) -> dict:
        xau_meta = self.get_live_price_with_meta("XAUUSD") if hasattr(self, "get_live_price_with_meta") else {
            "price": self.get_live_price("XAUUSD"),
            "source": "Primary Feed",
            "realtime": True,
        }
        btc_meta = self.get_live_price_with_meta("BTCUSDT") if hasattr(self, "get_live_price_with_meta") else {
            "price": self.get_live_price("BTCUSDT"),
            "source": "Primary Feed",
            "realtime": True,
        }
        extended = (
            self.get_extended_institutional_pillars(btc_meta.get("price", 0.0))
            if hasattr(self, "get_extended_institutional_pillars")
            else {}
        )
        return {
            "xau_price": xau_meta.get("price", 0.0),
            "xau_source": xau_meta.get("source", "OFFLINE"),
            "btc_price": btc_meta.get("price", 0.0),
            "btc_source": btc_meta.get("source", "OFFLINE"),
            "extended": extended,
            "macro": {
                "net_liquidity_b": 0.0,
                "dfii10": 0.0,
                "us02y": 0.0,
                "us10y": 0.0,
                "fedfunds": 0.0,
                "breakeven_10y": extended.get("breakeven_10y", 0.0),
            },
            "matrix": {
                "DXY": {"price": 0.0},
                "USOIL": {"price": 0.0},
                "NDX": {"price": 0.0},
                "SPX": {"price": 0.0},
                "VIX": {"price": 0.0},
            },
            "crypto_dom": {
                "TOTAL": 0.0,
                "TOTAL2": 0.0,
                "TOTAL3": 0.0,
                "BTC_D": 0.0,
                "USDT_D": 0.0,
            },
            "deriv_onchain": {
                "stablecoin_7d_flow_b": 0.0,
                "funding_rate": 0.0,
                "ls_ratio": 1.0,
                "fng_value": 0,
            },
        }
    def __init__(self):
        self.fred_key = os.getenv("FRED_API_KEY", "").strip()
        self.twelvedata_key = os.getenv("TWELVEDATA_API_KEY", "").strip()
        self.price_sources = {}

    def get_live_price_with_meta(self, symbol: str) -> dict:
        sym = symbol.upper()
        if "XAU" in sym:
            # 1. Swissquote Live Forex BBO Spot XAU/USD (Real-time tanpa delay)
            try:
                r = requests.get(
                    "https://forex-data-feed.swissquote.com/public-quotes/bboquote/instrument/XAU/USD",
                    timeout=6,
                )
                if r.status_code == 200:
                    data = r.json()
                    profiles = data[0]["spreadProfilePrices"]
                    bid = float(profiles[0]["bid"])
                    ask = float(profiles[0]["ask"])
                    mid = round((bid + ask) / 2.0, 2)
                    if mid > 1000:
                        self.price_sources[sym] = "Swissquote Spot Live"
                        return {"price": mid, "source": "Swissquote Spot Live", "realtime": True}
            except Exception:
                pass

            # 2. TwelveData Live Quote (jika API key tersedia)
            if self.twelvedata_key:
                try:
                    r = requests.get(
                        f"https://api.twelvedata.com/price?symbol=XAU/USD&apikey={self.twelvedata_key}",
                        timeout=6,
                    )
                    val = float(r.json().get("price", 0))
                    if val > 1000:
                        self.price_sources[sym] = "TwelveData Spot Live"
                        return {"price": round(val, 2), "source": "TwelveData Spot Live", "realtime": True}
                except Exception:
                    pass

            # 3. Binance PAXGUSDT Real-Time Gold Spot Proxy (24/7 tick)
            try:
                r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=PAXGUSDT", timeout=5)
                val = float(r.json().get("price", 0))
                if val > 1000:
                    self.price_sources[sym] = "Binance PAXG Spot Live"
                    return {"price": round(val, 2), "source": "Binance PAXG Spot Live", "realtime": True}
            except Exception:
                pass

            # 4. Yahoo Finance Fast Info (bukan angka statis)
            for ticker in ["XAUUSD=X", "GC=F"]:
                try:
                    t = yf.Ticker(ticker)
                    val = float(t.fast_info.get("lastPrice", 0) or 0)
                    if val > 1000:
                        self.price_sources[sym] = f"Yahoo {ticker}"
                        return {"price": round(val, 2), "source": f"Yahoo {ticker}", "realtime": False}
                except Exception:
                    continue

            return {"price": 0.0, "source": "OFFLINE", "realtime": False}

        # BTCUSDT / Kripto Real-Time Waterfall
        for url, src in [
            ("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", "Binance Spot Live"),
            ("https://fapi.binance.com/fapi/v1/ticker/price?symbol=BTCUSDT", "Binance Futures Live"),
            ("https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT", "OKX Spot Live"),
        ]:
            try:
                r = requests.get(url, timeout=5)
                if r.status_code == 200:
                    j = r.json()
                    val = float(j["data"][0]["last"]) if "data" in j else float(j["price"])
                    if val > 1000:
                        self.price_sources[sym] = src
                        return {"price": round(val, 2), "source": src, "realtime": True}
            except Exception:
                continue

        return {"price": 0.0, "source": "OFFLINE", "realtime": False}

    def get_live_price(self, symbol: str) -> float:
        return self.get_live_price_with_meta(symbol)["price"]

    def get_extended_institutional_pillars(self, btc_binance_price: float) -> dict:
        pillars = {
            "xagusd": 0.0,
            "coinbase_premium_pct": 0.0,
            "taker_buy_sell_ratio": 1.0,
            "open_interest_btc": 0.0,
            "breakeven_10y": 0.0,
            "cot_bias": "NEUTRAL",
        }
        # 1. Silver Live (XAGUSD) untuk SMT Divergence vs XAUUSD
        try:
            r = requests.get(
                "https://forex-data-feed.swissquote.com/public-quotes/bboquote/instrument/XAG/USD",
                timeout=5,
            )
            if r.status_code == 200:
                p = r.json()[0]["spreadProfilePrices"][0]
                pillars["xagusd"] = round((float(p["bid"]) + float(p["ask"])) / 2.0, 3)
        except Exception:
            pass

        # 2. Coinbase Premium Index (Institusi Spot AS vs Binance)
        try:
            r = requests.get("https://api.exchange.coinbase.com/products/BTC-USD/ticker", timeout=5)
            if r.status_code == 200 and btc_binance_price > 0:
                cb_price = float(r.json()["price"])
                pillars["coinbase_premium_pct"] = round(((cb_price - btc_binance_price) / btc_binance_price) * 100.0, 3)
        except Exception:
            pass

        # 3. Binance Futures Open Interest & Taker Buy/Sell Ratio
        try:
            oi_r = requests.get("https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT", timeout=5)
            if oi_r.status_code == 200:
                pillars["open_interest_btc"] = round(float(oi_r.json()["openInterest"]), 2)
            tk_r = requests.get(
                "https://fapi.binance.com/futures/data/takerlongshortRatio?symbol=BTCUSDT&period=15m&limit=1",
                timeout=5,
            )
            if tk_r.status_code == 200 and tk_r.json():
                pillars["taker_buy_sell_ratio"] = round(float(tk_r.json()[0]["buySellRatio"]), 3)
        except Exception:
            pass

        # 4. FRED 10Y Breakeven Inflation (T10YIE)
        if self.fred_key:
            try:
                f_r = requests.get(
                    f"https://api.stlouisfed.org/fred/series/observations?series_id=T10YIE&api_key={self.fred_key}&file_type=json&sort_order=desc&limit=1",
                    timeout=5,
                )
                obs = f_r.json().get("observations", [])
                if obs and obs[0]["value"] != ".":
                    pillars["breakeven_10y"] = float(obs[0]["value"])
            except Exception:
                pass

        return pillars
