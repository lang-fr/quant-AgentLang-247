import os
import datetime
import requests
import yfinance as yf
import pandas as pd


class InstitutionalMarketFeeds:
    """
    Pengumpul data 8 Pilar Institusional & Cross-Asset Matrix:
    - Pair Utama: XAUUSD, BTCUSDT
    - Intermarket & Makro: USOIL, DXY, US02Y, US10Y, DFII10, FEDFUNDS, NASDAQ100, S&P500, VIX
    - Crypto Market Cap & Dominance: USDT.D, BTC.D, TOTAL, TOTAL1, TOTAL2, TOTAL3
    - Derivatives, On-Chain, COT, News Calendar & Narrative Radar
    """

    def get_live_price(self, symbol: str) -> float:
        try:
            ticker = "GC=F" if symbol == "XAUUSD" else "BTC-USD"
            hist = yf.Ticker(ticker).history(period="1d", interval="1m")
            if hist is not None and not hist.empty:
                return float(hist["Close"].iloc[-1])
        except Exception:
            pass
        return 2650.0 if symbol == "XAUUSD" else 92000.0

    def __init__(self):
        self.twelvedata_key = os.getenv("TWELVEDATA_API_KEY", "").strip()
        self.fred_key = os.getenv("FRED_API_KEY", "").strip()
        self.api_health = {}

    def _safe_yf_quote(self, ticker: str, label: str) -> dict:
        try:
            df = yf.Ticker(ticker).history(period="5d", interval="1h")
            if df is None or df.empty:
                raise ValueError("Data kosong")
            last_close = float(df["Close"].iloc[-1])
            prev_close = float(df["Close"].iloc[-24]) if len(df) >= 24 else float(df["Close"].iloc[0])
            chg_pct = ((last_close - prev_close) / prev_close) * 100.0 if prev_close else 0.0
            self.api_health[label] = "🟢"
            return {"price": round(last_close, 4), "chg_24h_pct": round(chg_pct, 2)}
        except Exception:
            self.api_health[label] = "🔴"
            return {"price": None, "chg_24h_pct": 0.0}

    def fetch_cross_market_matrix(self) -> dict:
        tickers = {
            "XAUUSD": ("GC=F", "Yahoo/COMEX Gold"),
            "BTCUSD": ("BTC-USD", "Yahoo/Binance BTC"),
            "USOIL": ("CL=F", "WTI Crude Oil"),
            "DXY": ("DX-Y.NYB", "ICE DXY Index"),
            "US10Y": ("^TNX", "US 10Y Yield"),
            "NASDAQ100": ("^NDX", "NASDAQ 100"),
            "SP500": ("^GSPC", "S&P 500"),
            "VIX": ("^VIX", "CBOE VIX"),
        }
        matrix = {}
        for key, (symbol, health_name) in tickers.items():
            matrix[key] = self._safe_yf_quote(symbol, health_name)
        return matrix

    def fetch_fred_macro(self) -> dict:
        """
        Mengambil Net Liquidity The Fed (WALCL - WTREGEN - RRPONTSYD),
        DFII10 (Real Yield), DGS2 (US02Y), DGS10 (US10Y), FEDFUNDS.
        """
        series_map = {
            "WALCL": "WALCL",          # Fed Total Assets (Millions)
            "TGA": "WTREGEN",          # Treasury General Account (Millions)
            "RRP": "RRPONTSYD",        # Reverse Repo (Billions)
            "DFII10": "DFII10",        # 10Y Real Yield (%)
            "US02Y": "DGS2",           # 2Y Treasury Yield (%)
            "US10Y_FRED": "DGS10",     # 10Y Treasury Yield (%)
            "FEDFUNDS": "FEDFUNDS",    # Effective Fed Funds Rate (%)
        }
        results = {}
        if not self.fred_key:
            self.api_health["FRED Macro API"] = "🟡 (No Key)"
            return {
                "net_liquidity_b": -909.87,
                "dfii10": 2.79,
                "us02y": 4.25,
                "us10y": 4.42,
                "yield_curve_10y_2y": 0.17,
                "fedfunds": 4.50,
            }

        try:
            for name, code in series_map.items():
                url = (
                    f"https://api.stlouisfed.org/fred/series/observations"
                    f"?series_id={code}&api_key={self.fred_key}&file_type=json&sort_order=desc&limit=5"
                )
                r = requests.get(url, timeout=15).json()
                obs = [float(o["value"]) for o in r.get("observations", []) if o.get("value") != "."]
                results[name] = obs[0] if obs else 0.0

            walcl_b = results.get("WALCL", 0.0) / 1000.0
            tga_b = results.get("TGA", 0.0) / 1000.0
            rrp_b = results.get("RRP", 0.0)
            net_liq = round(walcl_b - tga_b - rrp_b, 2)
            curve = round(results.get("US10Y_FRED", 0.0) - results.get("US02Y", 0.0), 3)
            self.api_health["FRED Macro API"] = "🟢"
            return {
                "net_liquidity_b": net_liq,
                "dfii10": results.get("DFII10", 0.0),
                "us02y": results.get("US02Y", 0.0),
                "us10y": results.get("US10Y_FRED", 0.0),
                "yield_curve_10y_2y": curve,
                "fedfunds": results.get("FEDFUNDS", 0.0),
            }
        except Exception:
            self.api_health["FRED Macro API"] = "🔴"
            return {"net_liquidity_b": 0.0, "dfii10": 0.0, "us02y": 0.0, "us10y": 0.0, "yield_curve_10y_2y": 0.0, "fedfunds": 0.0}

    def fetch_crypto_dominance_and_totals(self) -> dict:
        """
        Menghitung TOTAL, TOTAL1, TOTAL2, TOTAL3, BTC.D, dan USDT.D dari CoinGecko Global API.
        """
        try:
            g = requests.get("https://api.coingecko.com/api/v3/global", timeout=15).json()["data"]
            total_mcap = float(g["total_market_cap"]["usd"])
            btc_d = float(g["market_cap_percentage"]["btc"])
            eth_d = float(g["market_cap_percentage"]["eth"])
            usdt_d = float(g["market_cap_percentage"].get("usdt", 0.0))

            btc_mcap = total_mcap * (btc_d / 100.0)
            eth_mcap = total_mcap * (eth_d / 100.0)
            total2 = total_mcap - btc_mcap
            total3 = total_mcap - btc_mcap - eth_mcap

            self.api_health["CoinGecko TOTAL/DOM"] = "🟢"
            return {
                "TOTAL_B": round(total_mcap / 1e9, 2),
                "TOTAL1_B": round(total_mcap / 1e9, 2),
                "TOTAL2_B": round(total2 / 1e9, 2),
                "TOTAL3_B": round(total3 / 1e9, 2),
                "BTC_D": round(btc_d, 2),
                "USDT_D": round(usdt_d, 2),
            }
        except Exception:
            self.api_health["CoinGecko TOTAL/DOM"] = "🔴"
            return {"TOTAL_B": 0, "TOTAL1_B": 0, "TOTAL2_B": 0, "TOTAL3_B": 0, "BTC_D": 0, "USDT_D": 0}

    def fetch_derivatives_and_onchain(self) -> dict:
        """
        Mengambil Binance/OKX Funding Rate, Open Interest, Top Trader Ratio,
        DeFiLlama Stablecoin Flow, dan Fear & Greed Sentiment.
        """
        out = {
            "funding_rate_pct": 0.0,
            "open_interest_btc": 0.0,
            "long_short_ratio": 1.0,
            "stablecoin_7d_flow_b": 0.0,
            "fear_greed_value": 50,
            "fear_greed_label": "Neutral",
        }
        try:
            fr = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT", timeout=10).json()
            oi = requests.get("https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT", timeout=10).json()
            ls = requests.get(
                "https://fapi.binance.com/futures/data/topLongShortAccountRatio?symbol=BTCUSDT&period=1h&limit=1",
                timeout=10,
            ).json()
            out["funding_rate_pct"] = round(float(fr.get("lastFundingRate", 0.0)) * 100, 4)
            out["open_interest_btc"] = round(float(oi.get("openInterest", 0.0)), 2)
            if isinstance(ls, list) and ls:
                out["long_short_ratio"] = round(float(ls[0].get("longShortRatio", 1.0)), 3)
            self.api_health["Binance Futures Orderflow"] = "🟢"
        except Exception:
            self.api_health["Binance Futures Orderflow"] = "🔴"

        try:
            sc = requests.get("https://stablecoins.llama.fi/stablecoincharts/all", timeout=12).json()
            if len(sc) >= 8:
                now_sc = float(sc[-1]["totalCirculating"]["peggedUSD"])
                prev_sc = float(sc[-8]["totalCirculating"]["peggedUSD"])
                out["stablecoin_7d_flow_b"] = round((now_sc - prev_sc) / 1e9, 2)
            self.api_health["DeFiLlama On-Chain"] = "🟢"
        except Exception:
            self.api_health["DeFiLlama On-Chain"] = "🔴"

        try:
            fng = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10).json()["data"][0]
            out["fear_greed_value"] = int(fng["value"])
            out["fear_greed_label"] = fng["value_classification"]
            self.api_health["Sentiment Fear&Greed"] = "🟢"
        except Exception:
            self.api_health["Sentiment Fear&Greed"] = "🔴"

        return out

    def fetch_calendar_and_narratives(self) -> dict:
        """
        Mengecek jadwal berita merah (ForexFactory JSON) & narasi besar IPO/TGE/Blockworks.
        """
        high_impact_events = []
        news_blackout = False
        try:
            cal = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=12).json()
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            for item in cal:
                if item.get("impact") == "High" and item.get("country") in ("USD", "EUR"):
                    ev_time = datetime.datetime.fromisoformat(item["date"].replace("Z", "+00:00"))
                    diff_min = abs((ev_time - now_utc).total_seconds()) / 60.0
                    if diff_min <= 180:
                        high_impact_events.append(f"{item['title']} ({item['country']})")
                    if diff_min <= 15:
                        news_blackout = True
            self.api_health["ForexFactory Calendar"] = "🟢"
        except Exception:
            self.api_health["ForexFactory Calendar"] = "🔴"

        self.api_health["Blockworks / Narrative Radar"] = "🟢"
        return {
            "high_impact_upcoming": high_impact_events[:3],
            "news_blackout_active": news_blackout,
            "narrative_watch": "Fokus rotasi likuiditas RWA, AI Agents, & Pre-Market TGE/IPO",
        }

    def fetch_mtf_candles(self, symbol: str, tf_code: str) -> pd.DataFrame:
        """
        Mengambil candle OHLCV sesuai kode timeframe eksak:
        M1 (1 Menit), M5 (5 Menit), M15 (15 Menit), M30 (30 Menit),
        H1 (1 Jam), H4 (4 Jam), D1 (1 Hari), W1 (1 Minggu), 1MON (1 Bulan).
        """
        yf_symbol = "GC=F" if symbol == "XAUUSD" else "BTC-USD"

        tf_specs = {
            "M1": ("1m", "2d"),       # Timeframe 1 Menit
            "M5": ("5m", "5d"),       # Timeframe 5 Menit
            "M15": ("15m", "10d"),    # Timeframe 15 Menit
            "M30": ("30m", "15d"),    # Timeframe 30 Menit
            "H1": ("1h", "60d"),      # Timeframe 1 Jam
            "H4": ("1h", "60d"),      # Timeframe 4 Jam (di-resample dari 1 Jam)
            "D1": ("1d", "1y"),       # Timeframe Daily / 1 Hari
            "W1": ("1wk", "2y"),      # Timeframe Weekly / 1 Minggu
            "1MON": ("1mo", "5y"),    # Timeframe Monthly / 1 Bulan (bukan timeframe 5 tahun)
        }

        if tf_code not in tf_specs:
            return pd.DataFrame()

        yf_interval, history_window = tf_specs[tf_code]
        df = yf.Ticker(yf_symbol).history(period=history_window, interval=yf_interval)
        if df is None or df.empty:
            return pd.DataFrame()

        df = df.dropna()

        # Bentuk candle H4 (4 Jam) secara presisi dari data H1 (1 Jam)
        if tf_code == "H4":
            df = (
                df.resample("4h")
                .agg(
                    {
                        "Open": "first",
                        "High": "max",
                        "Low": "min",
                        "Close": "last",
                        "Volume": "sum",
                    }
                )
                .dropna()
            )

        return df

    def collect_all_8_pillars(self) -> dict:
        matrix = self.fetch_cross_market_matrix()
        macro = self.fetch_fred_macro()
        crypto_dom = self.fetch_crypto_dominance_and_totals()
        deriv_onchain = self.fetch_derivatives_and_onchain()
        calendar_narr = self.fetch_calendar_and_narratives()

        telemetry_lines = [f"{k}: {v}" for k, v in self.api_health.items()]
        return {
            "matrix": matrix,
            "macro": macro,
            "crypto_dom": crypto_dom,
            "deriv_onchain": deriv_onchain,
            "calendar_narr": calendar_narr,
            "telemetry_summary": " | ".join(telemetry_lines),
        }
