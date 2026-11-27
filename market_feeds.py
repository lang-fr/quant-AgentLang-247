import os
import re
import time
import datetime
import pytz
import requests
import feedparser
import yfinance as yf
from youtube_transcript_api import YouTubeTranscriptApi


class InstitutionalMarketFeeds:
    def __init__(self):
        self.twelvedata_key = os.getenv("TWELVEDATA_API_KEY", "").strip()
        self.fred_key = os.getenv("FRED_API_KEY", "").strip()
        self.blockworks_key = os.getenv("BLOCKWORKS_API_KEY", "").strip()
        self.metaapi_token = os.getenv("METAAPI_TOKEN", "").strip()
        self.metaapi_account_id = os.getenv("METAAPI_ACCOUNT_ID", "").strip()
        self.wib = pytz.timezone("Asia/Jakarta")

    def get_live_price_with_meta(self, symbol: str) -> dict:
        sym = symbol.upper()
        if "BTC" in sym:
            try:
                r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=5)
                if r.status_code == 200:
                    px = float(r.json().get("price", 0.0))
                    if px > 0:
                        return {"price": round(px, 2), "source": "Binance Spot Live", "realtime": True}
            except Exception:
                pass
            try:
                df = yf.Ticker("BTC-USD").history(period="1d", interval="1m")
                if df is not None and not df.empty:
                    return {"price": round(float(df["Close"].iloc[-1]), 2), "source": "Yahoo BTC-USD", "realtime": True}
            except Exception:
                pass
            return {"price": 0.0, "source": "Offline", "realtime": False}

        # XAUUSD: Prioritas Twelve Data Spot Live -> Fallback Yahoo Finance GC=F / XAUUSD=X
        if self.twelvedata_key:
            try:
                r = requests.get(
                    "https://api.twelvedata.com/price",
                    params={"symbol": "XAU/USD", "apikey": self.twelvedata_key},
                    timeout=6,
                )
                if r.status_code == 200 and "price" in r.json():
                    px = float(r.json()["price"])
                    if px > 0:
                        return {"price": round(px, 2), "source": "TwelveData Spot Live", "realtime": True}
            except Exception:
                pass

        for ticker in ("GC=F", "XAUUSD=X"):
            try:
                df = yf.Ticker(ticker).history(period="2d", interval="1m")
                if df is not None and not df.empty:
                    px = float(df["Close"].iloc[-1])
                    if px > 0:
                        return {"price": round(px, 2), "source": f"Yahoo {ticker}", "realtime": True}
            except Exception:
                continue
        return {"price": 0.0, "source": "Offline", "realtime": False}

    def _fetch_fred_latest(self, series_id: str) -> float:
        if not self.fred_key:
            return 0.0
        try:
            url = (
                f"https://api.stlouisfed.org/fred/series/observations"
                f"?series_id={series_id}&api_key={self.fred_key}&file_type=json&sort_order=desc&limit=2"
            )
            r = requests.get(url, timeout=6)
            if r.status_code == 200:
                for obs in r.json().get("observations", []):
                    val = obs.get("value", ".")
                    if val != ".":
                        return float(val)
        except Exception:
            pass
        return 0.0

    def _fetch_yf_last(self, ticker: str) -> float:
        try:
            df = yf.Ticker(ticker).history(period="5d", interval="1d")
            if df is not None and not df.empty:
                return round(float(df["Close"].iloc[-1]), 2)
        except Exception:
            pass
        return 0.0

    def collect_all_8_pillars(self) -> dict:
        xau_meta = self.get_live_price_with_meta("XAUUSD")
        btc_meta = self.get_live_price_with_meta("BTCUSDT")

        # 1. Makro & Likuiditas (FRED)
        fedfunds = self._fetch_fred_latest("FEDFUNDS")
        dfii10 = self._fetch_fred_latest("DFII10")
        walcl = self._fetch_fred_latest("WALCL") / 1000.0
        tga = self._fetch_fred_latest("WTREGEN")
        rrp = self._fetch_fred_latest("RRPONTSYD")
        net_liq = round(walcl - tga - rrp, 2) if walcl > 0 else 0.0

        # 2. Intermarket Matrix
        dxy = self._fetch_yf_last("DX-Y.NYB")
        us10y = self._fetch_yf_last("^TNX")
        vix = self._fetch_yf_last("^VIX")
        nasdaq = self._fetch_yf_last("^IXIC")
        xagusd = self._fetch_yf_last("SI=F")

        # 3. Stablecoin 7d Flow (DeFiLlama)
        stable_7d_b = 0.0
        try:
            sc = requests.get("https://stablecoins.llama.fi/stablecoincharts/all", timeout=6).json()
            if len(sc) >= 8:
                now_usd = float(sc[-1]["totalCirculatingUSD"]["peggedUSD"])
                old_usd = float(sc[-8]["totalCirculatingUSD"]["peggedUSD"])
                stable_7d_b = round((now_usd - old_usd) / 1e9, 2)
        except Exception:
            pass

        # 4. Taker Buy/Sell Ratio & Funding (Binance Futures)
        taker_ratio = 1.0
        funding_pct = 0.0
        try:
            r_tk = requests.get(
                "https://fapi.binance.com/futures/data/takerlongshortRatio?symbol=BTCUSDT&period=15m&limit=1",
                timeout=5,
            )
            if r_tk.status_code == 200 and r_tk.json():
                taker_ratio = round(float(r_tk.json()[-1].get("buySellRatio", 1.0)), 3)
            r_fr = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT", timeout=5)
            if r_fr.status_code == 200:
                funding_pct = round(float(r_fr.json().get("lastFundingRate", 0.0)) * 100.0, 4)
        except Exception:
            pass

        telemetry_summary = (
            f"XAU:{xau_meta['price']} | BTC:{btc_meta['price']} | DXY:{dxy} | US10Y:{us10y} | "
            f"VIX:{vix} | DFII10:{dfii10} | NetLiq:{net_liq}B | Stable7d:{stable_7d_b}B | "
            f"TakerBS:{taker_ratio} | Funding:{funding_pct}%"
        )

        return {
            "xau_meta": xau_meta,
            "btc_meta": btc_meta,
            "macro": {
                "fedfunds": fedfunds,
                "dfii10": dfii10,
                "net_liquidity_b": net_liq,
            },
            "matrix": {
                "DXY": {"price": dxy},
                "US10Y": {"price": us10y},
                "VIX": {"price": vix},
                "NASDAQ": {"price": nasdaq},
            },
            "crypto_dom": {"USDT_D": 5.2},
            "deriv_onchain": {
                "stablecoin_7d_flow_b": stable_7d_b,
                "funding_rate_pct": funding_pct,
            },
            "extended": {
                "xagusd": xagusd,
                "taker_buy_sell_ratio": taker_ratio,
                "coinbase_premium_pct": 0.0,
            },
            "telemetry_summary": telemetry_summary,
        }

    def check_usd_news_blackout(self) -> tuple[bool, list[str]]:
        """Mengecek jadwal berita merah (High Impact USD) dalam jendela +-45 menit."""
        try:
            events = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=6).json()
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            warnings = []
            for ev in events:
                if ev.get("country") == "USD" and ev.get("impact") == "High" and ev.get("date"):
                    ev_dt = datetime.datetime.fromisoformat(ev["date"]).astimezone(datetime.timezone.utc)
                    diff_min = abs((ev_dt - now_utc).total_seconds()) / 60.0
                    if diff_min <= 45:
                        warnings.append(f"{ev.get('title')} ({int(diff_min)}m)")
            return len(warnings) > 0, warnings
        except Exception:
            return False, []

    def fetch_realtime_breaking_news(self, limit: int = 8) -> list[dict]:
        """
        Radar Breaking News Institusional Real-Time:
        Menggabungkan Financial Juice RSS, ForexLive, Federal Reserve, White House, & CNBC Markets.
        """
        feeds = [
            ("FinancialJuice", "https://www.financialjuice.com/feed.ashx?xy=rss"),
            ("ForexLive", "https://www.forexlive.com/feed/news"),
            ("FederalReserve", "https://www.federalreserve.gov/feeds/press_all.xml"),
            ("WhiteHouse", "https://www.whitehouse.gov/news/feed/"),
            ("CNBC_Markets", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664"),
        ]
        keywords = (
            "fed", "powell", "fomc", "rate", "inflation", "cpi", "ppi", "nfp", "payroll",
            "trump", "tariff", "treasury", "yield", "dollar", "dxy", "gold", "xau",
            "bitcoin", "btc", "crypto", "sec", "etf", "war", "oil", "recession",
        )
        headlines = []
        seen = set()
        for src_name, url in feeds:
            try:
                parsed = feedparser.parse(url)
                for entry in parsed.entries[:6]:
                    title = getattr(entry, "title", "").strip()
                    if not title or title in seen:
                        continue
                    low = title.lower()
                    is_priority = any(k in low for k in keywords)
                    if is_priority or src_name in ("FinancialJuice", "FederalReserve", "WhiteHouse"):
                        seen.add(title)
                        headlines.append(
                            {
                                "source": src_name,
                                "title": title,
                                "published": getattr(entry, "published", "Baru saja"),
                            }
                        )
                    if len(headlines) >= limit:
                        return headlines
            except Exception:
                continue
        return headlines

    def fetch_live_speech_transcript(self, youtube_url_or_id: str) -> str:
        """
        Menarik transkrip teks real-time dari siaran YouTube Live (The Fed FOMC / Pidato Trump)
        untuk diterjemahkan & dianalisis dampaknya ke XAUUSD/BTCUSDT.
        """
        match = re.search(r"(?:v=|youtu\.be/|live/)([A-Za-z0-9_-]{11})", youtube_url_or_id)
        vid_id = match.group(1) if match else youtube_url_or_id.strip()
        if len(vid_id) != 11:
            return ""
        try:
            transcript_list = YouTubeTranscriptApi.get_transcript(vid_id, languages=["en", "id"])
            # Ambil 60 segmen terakhir (~3-5 menit pidato terbaru)
            tail_segments = transcript_list[-60:]
            joined = " ".join(seg.get("text", "") for seg in tail_segments)
            return re.sub(r"\s+", " ", joined).strip()
        except Exception:
            return ""

    def check_all_api_statuses(self, ai_router) -> list[dict]:
        """Menguji koneksi & latensi (ms) seluruh API dan model AI secara real-time."""
        results = []

        def probe(name: str, fn):
            t0 = time.time()
            try:
                ok, detail = fn()
                ms = int((time.time() - t0) * 1000)
                results.append(
                    {
                        "name": name,
                        "status": "✅ AKTIF" if ok else "⚠️ TERBATAS/KOSONG",
                        "latency": f"{ms}ms",
                        "detail": detail,
                    }
                )
            except Exception as e:
                ms = int((time.time() - t0) * 1000)
                results.append(
                    {
                        "name": name,
                        "status": "❌ ERROR",
                        "latency": f"{ms}ms",
                        "detail": str(e)[:60],
                    }
                )

        probe("Gemini 4 Argon (Flagship)", lambda: (
            bool(ai_router._call_gemini("gemini-4-argon", "Balas OK")),
            ai_router.last_route_log.get("gemini-4-argon", "Tidak aktif"),
        ))
        probe("Gemini 3.8 Flash", lambda: (
            bool(ai_router._call_gemini("gemini-3.8-flash", "Balas OK")),
            ai_router.last_route_log.get("gemini-3.8-flash", "Tidak aktif"),
        ))
        probe("Claude Opus 4.7 / Sonnet 4.6", lambda: (
            bool(ai_router._call_claude("claude-sonnet-4-6", "Balas OK")),
            ai_router.last_route_log.get("claude-sonnet-4-6", "ANTHROPIC_API_KEY belum diisi"),
        ))
        probe("Twelve Data (XAU/USD Spot)", lambda: (
            (False, "TWELVEDATA_API_KEY kosong")
            if not self.twelvedata_key
            else (
                "price" in requests.get(
                    "https://api.twelvedata.com/price",
                    params={"symbol": "XAU/USD", "apikey": self.twelvedata_key},
                    timeout=5,
                ).json(),
                "XAU/USD Real-Time Feed",
            )
        ))
        probe("FRED St. Louis Fed", lambda: (
            (False, "FRED_API_KEY kosong")
            if not self.fred_key
            else (self._fetch_fred_latest("FEDFUNDS") > 0, "FEDFUNDS / DFII10 / NetLiq")
        ))
        probe("Binance Spot & Futures", lambda: (
            requests.get("https://api.binance.com/api/v3/ping", timeout=5).status_code == 200,
            "Kline M1-1MON + Orderbook L2 + OI",
        ))
        probe("Yahoo Finance (GC=F/DXY)", lambda: (
            self._fetch_yf_last("GC=F") > 0,
            "Multi-TF & Intermarket Matrix",
        ))
        probe("DeFiLlama Stablecoin Flow", lambda: (
            requests.get("https://stablecoins.llama.fi/stablecoincharts/all", timeout=5).status_code == 200,
            "On-Chain Liquidity 7D",
        ))
        probe("Breaking News & FF Calendar", lambda: (
            requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=5).status_code == 200,
            "FinancialJuice / ForexLive / Fed RSS",
        ))
        probe("MetaApi MT5 Demo Bridge", lambda: (
            (False, "Token/AccountID belum diisi (Mode Simulasi Aktif)")
            if not (self.metaapi_token and self.metaapi_account_id)
            else (
                requests.get(
                    f"https://mt-client-api-v1.agiliumtrade.agiliumtrade.ai/users/current/accounts/{self.metaapi_account_id}",
                    headers={"auth-token": self.metaapi_token},
                    timeout=5,
                ).status_code == 200,
                "Koneksi Eksekusi Demo MT5",
            )
        ))
        return results
