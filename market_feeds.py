import os
from datetime import datetime, timezone
import feedparser
import requests
import yfinance as yf


class InstitutionalMarketFeeds:
    def __init__(self):
        self.fred_key = os.getenv("FRED_API_KEY", "").strip()
        self.twelvedata_key = os.getenv("TWELVEDATA_API_KEY", "").strip()
        self.price_sources = {}

    def get_live_price_with_meta(self, symbol: str) -> dict:
        sym = symbol.upper()
        if "XAU" in sym:
            try:
                r = requests.get(
                    "https://forex-data-feed.swissquote.com/public-quotes/bboquote/instrument/XAU/USD",
                    timeout=6,
                )
                if r.status_code == 200:
                    profiles = r.json()[0]["spreadProfilePrices"]
                    mid = round((float(profiles[0]["bid"]) + float(profiles[0]["ask"])) / 2.0, 2)
                    if mid > 1000:
                        self.price_sources[sym] = "Swissquote Spot Live"
                        return {"price": mid, "source": "Swissquote Spot Live", "realtime": True}
            except Exception:
                pass

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

            try:
                r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=PAXGUSDT", timeout=5)
                val = float(r.json().get("price", 0))
                if val > 1000:
                    self.price_sources[sym] = "Binance PAXG Spot Live"
                    return {"price": round(val, 2), "source": "Binance PAXG Spot Live", "realtime": True}
            except Exception:
                pass

            for ticker in ["XAUUSD=X", "GC=F"]:
                try:
                    val = float(yf.Ticker(ticker).fast_info.get("lastPrice", 0) or 0)
                    if val > 1000:
                        self.price_sources[sym] = f"Yahoo {ticker}"
                        return {"price": round(val, 2), "source": f"Yahoo {ticker}", "realtime": False}
                except Exception:
                    continue

            return {"price": 0.0, "source": "OFFLINE", "realtime": False}

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

    def _fetch_fred_latest(self, series_id: str) -> float:
        if not self.fred_key:
            return 0.0
        try:
            url = (
                f"https://api.stlouisfed.org/fred/series/observations"
                f"?series_id={series_id}&api_key={self.fred_key}&file_type=json&sort_order=desc&limit=5"
            )
            r = requests.get(url, timeout=6)
            if r.status_code == 200:
                for obs in r.json().get("observations", []):
                    if obs.get("value") not in (None, "."):
                        return float(obs["value"])
        except Exception:
            pass
        return 0.0

    def _fetch_yf_last(self, ticker: str) -> float:
        try:
            val = float(yf.Ticker(ticker).fast_info.get("lastPrice", 0) or 0)
            if val > 0:
                return round(val, 2)
        except Exception:
            pass
        return 0.0

    def _get_macro_pillar(self) -> tuple[dict, bool]:
        walcl_m = self._fetch_fred_latest("WALCL")
        tga_b = self._fetch_fred_latest("WTREGEN")
        rrp_b = self._fetch_fred_latest("RRPONTSYD")
        dfii10 = self._fetch_fred_latest("DFII10")
        us02y = self._fetch_fred_latest("DGS2")
        us10y = self._fetch_fred_latest("DGS10")
        fedfunds = self._fetch_fred_latest("FEDFUNDS")
        breakeven_10y = self._fetch_fred_latest("T10YIE")

        if us10y == 0.0:
            tnx = self._fetch_yf_last("^TNX")
            if tnx > 0:
                us10y = round(tnx / 10.0, 2) if tnx > 15 else round(tnx, 2)

        net_liq_b = round((walcl_m / 1000.0) - tga_b - rrp_b, 2) if walcl_m > 0 else 0.0
        ok = (us10y > 0) or (net_liq_b != 0.0)
        return (
            {
                "net_liquidity_b": net_liq_b,
                "dfii10": round(dfii10, 2),
                "us02y": round(us02y, 2),
                "us10y": round(us10y, 2),
                "fedfunds": round(fedfunds, 2),
                "breakeven_10y": round(breakeven_10y, 2),
            },
            ok,
        )

    def _get_intermarket_matrix(self) -> tuple[dict, bool]:
        mapping = {
            "DXY": "DX-Y.NYB",
            "USOIL": "CL=F",
            "NASDAQ100": "^IXIC",
            "SP500": "^GSPC",
            "VIX": "^VIX",
        }
        matrix = {}
        hits = 0
        for key, ticker in mapping.items():
            px = self._fetch_yf_last(ticker)
            if px > 0:
                hits += 1
            matrix[key] = {"price": px}
        return matrix, hits >= 3

    def _get_crypto_dominance(self) -> tuple[dict, bool]:
        try:
            r = requests.get("https://api.coingecko.com/api/v3/global", timeout=7)
            if r.status_code == 200:
                g = r.json().get("data", {})
                total_usd = float(g.get("total_market_cap", {}).get("usd", 0))
                mcap_pct = g.get("market_cap_percentage", {})
                btc_d = float(mcap_pct.get("btc", 0))
                eth_d = float(mcap_pct.get("eth", 0))
                usdt_d = float(mcap_pct.get("usdt", 0))

                total_b = round(total_usd / 1e9, 2)
                total2_b = round(total_b * (1.0 - (btc_d / 100.0)), 2)
                total3_b = round(total_b * (1.0 - ((btc_d + eth_d) / 100.0)), 2)
                return (
                    {
                        "TOTAL_B": total_b,
                        "TOTAL2_B": total2_b,
                        "TOTAL3_B": total3_b,
                        "BTC_D": round(btc_d, 2),
                        "USDT_D": round(usdt_d, 2),
                    },
                    total_b > 0,
                )
        except Exception:
            pass
        return {"TOTAL_B": 0.0, "TOTAL2_B": 0.0, "TOTAL3_B": 0.0, "BTC_D": 0.0, "USDT_D": 0.0}, False

    def _get_deriv_onchain_pillar(self) -> tuple[dict, bool]:
        funding_pct = 0.0
        oi_btc = 0.0
        ls_ratio = 1.0
        stable_7d_b = 0.0
        fng_val = 50
        fng_lbl = "Neutral"
        hits = 0

        try:
            fr = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT", timeout=5)
            if fr.status_code == 200:
                funding_pct = round(float(fr.json().get("lastFundingRate", 0)) * 100.0, 4)
                hits += 1
        except Exception:
            pass

        try:
            oi = requests.get("https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT", timeout=5)
            if oi.status_code == 200:
                oi_btc = round(float(oi.json().get("openInterest", 0)), 2)
                hits += 1
        except Exception:
            pass

        try:
            ls = requests.get(
                "https://fapi.binance.com/futures/data/globalLongShortAccountRatio?symbol=BTCUSDT&period=15m&limit=1",
                timeout=5,
            )
            if ls.status_code == 200 and ls.json():
                ls_ratio = round(float(ls.json()[0].get("longShortRatio", 1.0)), 2)
                hits += 1
        except Exception:
            pass

        try:
            sc = requests.get("https://stablecoins.llama.fi/stablecoincharts/all", timeout=7)
            if sc.status_code == 200:
                arr = sc.json()
                if len(arr) >= 8:
                    now_sc = float(arr[-1]["totalCirculatingUSD"]["peggedUSD"])
                    prev_sc = float(arr[-8]["totalCirculatingUSD"]["peggedUSD"])
                    stable_7d_b = round((now_sc - prev_sc) / 1e9, 2)
                    hits += 1
        except Exception:
            pass

        try:
            fg = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
            if fg.status_code == 200:
                item = fg.json().get("data", [{}])[0]
                fng_val = int(item.get("value", 50))
                fng_lbl = str(item.get("value_classification", "Neutral"))
                hits += 1
        except Exception:
            pass

        return (
            {
                "funding_rate_pct": funding_pct,
                "open_interest_btc": oi_btc,
                "long_short_ratio": ls_ratio,
                "stablecoin_7d_flow_b": stable_7d_b,
                "fear_greed_value": fng_val,
                "fear_greed_label": fng_lbl,
            },
            hits >= 2,
        )

    def _get_calendar_and_narratives(self) -> tuple[dict, bool]:
        blackout = False
        upcoming = []
        headlines = []

        try:
            ev_resp = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=7)
            if ev_resp.status_code == 200:
                now_utc = datetime.now(timezone.utc)
                for ev in ev_resp.json():
                    if ev.get("country") == "USD" and ev.get("impact") == "High" and ev.get("date"):
                        ev_dt = datetime.fromisoformat(ev["date"]).astimezone(timezone.utc)
                        diff_min = (ev_dt - now_utc).total_seconds() / 60.0
                        if abs(diff_min) <= 45:
                            blackout = True
                        if -30 <= diff_min <= 240:
                            upcoming.append(f"{ev.get('title')} ({int(diff_min)}m)")
        except Exception:
            pass

        for rss_url in ["https://www.investing.com/rss/news_25.rss", "https://www.investing.com/rss/news_301.rss"]:
            try:
                feed = feedparser.parse(rss_url)
                for entry in feed.entries[:2]:
                    headlines.append(entry.title)
            except Exception:
                pass

        return (
            {
                "news_blackout_active": blackout,
                "high_impact_upcoming": ", ".join(upcoming[:3]) if upcoming else "Tidak ada berita USD merah ±45m",
                "narrative_watch": " | ".join(headlines[:3]) if headlines else "Pantau aliran likuiditas sesi aktif",
            },
            True,
        )

    def get_extended_institutional_pillars(self, btc_binance_price: float) -> dict:
        pillars = {
            "xagusd": 0.0,
            "coinbase_premium_pct": 0.0,
            "taker_buy_sell_ratio": 1.0,
            "open_interest_btc": 0.0,
            "breakeven_10y": self._fetch_fred_latest("T10YIE"),
        }
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

        try:
            r = requests.get("https://api.exchange.coinbase.com/products/BTC-USD/ticker", timeout=5)
            if r.status_code == 200 and btc_binance_price > 0:
                cb_price = float(r.json()["price"])
                pillars["coinbase_premium_pct"] = round(((cb_price - btc_binance_price) / btc_binance_price) * 100.0, 3)
        except Exception:
            pass

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

        return pillars

    def collect_all_8_pillars(self) -> dict:
        xau_meta = self.get_live_price_with_meta("XAUUSD")
        btc_meta = self.get_live_price_with_meta("BTCUSDT")
        macro, ok_macro = self._get_macro_pillar()
        matrix, ok_matrix = self._get_intermarket_matrix()
        crypto_dom, ok_dom = self._get_crypto_dominance()
        deriv_onchain, ok_deriv = self._get_deriv_onchain_pillar()
        calendar_narr, ok_cal = self._get_calendar_and_narratives()
        extended = self.get_extended_institutional_pillars(btc_meta.get("price", 0.0))

        active_count = sum([
            xau_meta["price"] > 0,
            btc_meta["price"] > 0,
            ok_macro,
            ok_matrix,
            ok_dom,
            ok_deriv,
            ok_cal,
            extended["taker_buy_sell_ratio"] > 0,
        ])

        return {
            "xau_price": xau_meta["price"],
            "xau_source": xau_meta["source"],
            "btc_price": btc_meta["price"],
            "btc_source": btc_meta["source"],
            "macro": macro,
            "matrix": matrix,
            "crypto_dom": crypto_dom,
            "deriv_onchain": deriv_onchain,
            "calendar_narr": calendar_narr,
            "extended": extended,
            "telemetry_summary": f"{active_count}/8 Pilar Aktif ({xau_meta['source']} & {btc_meta['source']})",
        }
