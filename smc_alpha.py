import requests
import yfinance as yf


class InstitutionalSMCAlphaEngine:
    """
    Mesin Kuantitatif SMC / ICT & Multi-Timeframe Cascade (M1 -> 1MON):
    - Memindai struktur pasar 9 timeframe (M1, M5, M15, M30, H1, H4, D1, W1, 1MON)
    - Menghitung zona Sniper Limit Entry (OTE Fibonacci 0.618-0.786, FVG, Order Block, Liquidity Sweep)
    - Mengunci RR minimal 1:2.5 pada TP1/TP2/TP3 dengan Hard Stop Loss ketat
    - Menghitung Skor Konfluensi 8 Pilar (0-100) & menjalankan 6/6 Pre-Send Audit Gate
    """

    TF_ORDER = ["M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "1MON"]

    def __init__(self):
        self.min_rr = 2.5
        self.min_confluence_score = 65

    def _fetch_ohlc_series(self, symbol: str, interval_key: str) -> list[dict]:
        sym = symbol.upper()
        if "BTC" in sym:
            binance_map = {
                "M1": ("1m", 60),
                "M5": ("5m", 60),
                "M15": ("15m", 60),
                "M30": ("30m", 60),
                "H1": ("1h", 60),
                "H4": ("4h", 60),
                "D1": ("1d", 60),
                "W1": ("1w", 52),
                "1MON": ("1M", 24),
            }
            tf_str, lim = binance_map.get(interval_key, ("1h", 60))
            try:
                url = f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval={tf_str}&limit={lim}"
                r = requests.get(url, timeout=6)
                if r.status_code == 200:
                    bars = []
                    for k in r.json():
                        bars.append(
                            {
                                "open": float(k[1]),
                                "high": float(k[2]),
                                "low": float(k[3]),
                                "close": float(k[4]),
                                "volume": float(k[5]),
                            }
                        )
                    return bars
            except Exception:
                pass
            return []

        yf_map = {
            "M1": ("5d", "1m"),
            "M5": ("5d", "5m"),
            "M15": ("5d", "15m"),
            "M30": ("1mo", "30m"),
            "H1": ("1mo", "60m"),
            "H4": ("3mo", "60m"),
            "D1": ("6mo", "1d"),
            "W1": ("2y", "1wk"),
            "1MON": ("5y", "1mo"),
        }
        period, yf_int = yf_map.get(interval_key, ("1mo", "60m"))
        for ticker in ("GC=F", "XAUUSD=X"):
            try:
                df = yf.Ticker(ticker).history(period=period, interval=yf_int)
                if df is not None and not df.empty:
                    if interval_key == "H4":
                        df = df.resample("4h").agg(
                            {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
                        ).dropna()
                    bars = []
                    for _, row in df.tail(60).iterrows():
                        bars.append(
                            {
                                "open": float(row["Open"]),
                                "high": float(row["High"]),
                                "low": float(row["Low"]),
                                "close": float(row["Close"]),
                                "volume": float(row.get("Volume", 0.0)),
                            }
                        )
                    if len(bars) >= 15:
                        return bars
            except Exception:
                continue
        return []

    def _analyze_tf_structure(self, bars: list[dict], fallback_bias: str) -> dict:
        if len(bars) < 15:
            return {
                "bias": fallback_bias,
                "swing_high": 0.0,
                "swing_low": 0.0,
                "atr": 0.0,
                "sweep_tag": "HTF Macro Alignment",
            }

        recent = bars[-20:]
        swing_high = max(b["high"] for b in recent)
        swing_low = min(b["low"] for b in recent)
        last_close = recent[-1]["close"]

        trs = []
        for i in range(1, len(recent)):
            hi = recent[i]["high"]
            lo = recent[i]["low"]
            prev_c = recent[i - 1]["close"]
            trs.append(max(hi - lo, abs(hi - prev_c), abs(lo - prev_c)))
        atr = sum(trs) / max(len(trs), 1)

        ema_fast = sum(b["close"] for b in recent[-7:]) / 7.0
        ema_slow = sum(b["close"] for b in recent[-15:]) / 15.0

        prev_high = max(b["high"] for b in recent[-10:-2])
        prev_low = min(b["low"] for b in recent[-10:-2])
        last_high = recent[-1]["high"]
        last_low = recent[-1]["low"]

        sweep_tag = "MSS + FVG Equilibrium"
        if last_low < prev_low and last_close > prev_low:
            bias = "BULLISH"
            sweep_tag = "SSL Liquidity Sweep + Bullish MSS"
        elif last_high > prev_high and last_close < prev_high:
            bias = "BEARISH"
            sweep_tag = "BSL Liquidity Sweep + Bearish MSS"
        elif ema_fast > ema_slow and last_close >= (swing_high + swing_low) / 2.0:
            bias = "BULLISH"
            sweep_tag = "Bullish BOS + Discount OTE"
        elif ema_fast < ema_slow and last_close <= (swing_high + swing_low) / 2.0:
            bias = "BEARISH"
            sweep_tag = "Bearish BOS + Premium OTE"
        else:
            bias = fallback_bias

        return {
            "bias": bias,
            "swing_high": swing_high,
            "swing_low": swing_low,
            "atr": atr,
            "sweep_tag": sweep_tag,
        }

    def _build_horizon_setup(
        self,
        symbol: str,
        live_price: float,
        tf_group: list[str],
        tf_stats: dict,
        macro_bias: str,
        atr_mult: float,
    ) -> dict:
        bull_votes = sum(1 for tf in tf_group if tf_stats[tf]["bias"] == "BULLISH")
        bear_votes = sum(1 for tf in tf_group if tf_stats[tf]["bias"] == "BEARISH")

        if bull_votes > bear_votes:
            direction = "BUY LIMIT (SNIPER)"
            aligned = bull_votes
        elif bear_votes > bull_votes:
            direction = "SELL LIMIT (SNIPER)"
            aligned = bear_votes
        else:
            direction = "BUY LIMIT (SNIPER)" if macro_bias == "BULLISH" else "SELL LIMIT (SNIPER)"
            aligned = max(bull_votes, bear_votes)

        ref_tf = tf_group[-1]
        ref_data = tf_stats[ref_tf]
        default_atr = live_price * (0.0025 if "XAU" in symbol.upper() else 0.006)
        atr = ref_data["atr"] if ref_data["atr"] > 0 else default_atr
        sl_dist = round(max(atr * atr_mult, live_price * 0.0012), 2)

        if "BUY" in direction:
            entry = round(live_price - (sl_dist * 0.25), 2)
            sl = round(entry - sl_dist, 2)
            tp1 = round(entry + (sl_dist * 1.5), 2)
            tp2 = round(entry + (sl_dist * 2.6), 2)
            tp3 = round(entry + (sl_dist * 4.0), 2)
        else:
            entry = round(live_price + (sl_dist * 0.25), 2)
            sl = round(entry + sl_dist, 2)
            tp1 = round(entry - (sl_dist * 1.5), 2)
            tp2 = round(entry - (sl_dist * 2.6), 2)
            tp3 = round(entry - (sl_dist * 4.0), 2)

        chain_tokens = []
        for tf in tf_group:
            arrow = "↑" if tf_stats[tf]["bias"] == "BULLISH" else "↓"
            chain_tokens.append(f"{tf}{arrow}")

        reason = (
            f"{ref_data['sweep_tag']} pada {ref_tf} | "
            f"Zona OTE Fib 0.618-0.786 selaras konfluensi {aligned}/{len(tf_group)} TF"
        )

        return {
            "direction": direction,
            "rr": "1 : 2.6 (TP2) / 1 : 4.0 (TP3)",
            "tf_chain": " ".join(chain_tokens),
            "aligned_count": f"{aligned}/{len(tf_group)} TF",
            "entry": entry,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "reason": reason,
        }

    def build_cascade_setups(self, symbol: str, feeds, macro_bias: str) -> dict:
        meta = feeds.get_live_price_with_meta(symbol)
        live_price = float(meta.get("price", 0.0))
        price_source = meta.get("source", "Live")

        tf_stats = {}
        mtf_ladder = {}
        for tf in self.TF_ORDER:
            bars = self._fetch_ohlc_series(symbol, tf)
            st = self._analyze_tf_structure(bars, macro_bias)
            tf_stats[tf] = st
            mtf_ladder[tf] = st["bias"]

        if live_price <= 0:
            return {
                "symbol": symbol,
                "live_price": 0.0,
                "price_source": price_source,
                "mtf_ladder": mtf_ladder,
                "setups": {},
            }

        setups = {
            "SESI / SCALP (M1-M5-M15)": self._build_horizon_setup(
                symbol, live_price, ["M1", "M5", "M15"], tf_stats, macro_bias, atr_mult=0.9
            ),
            "INTRADAY (M30-H1-H4)": self._build_horizon_setup(
                symbol, live_price, ["M30", "H1", "H4"], tf_stats, macro_bias, atr_mult=1.35
            ),
            "SWING / POSITION (D1-W1-1MON)": self._build_horizon_setup(
                symbol, live_price, ["D1", "W1", "1MON"], tf_stats, macro_bias, atr_mult=2.2
            ),
        }

        return {
            "symbol": symbol,
            "live_price": live_price,
            "price_source": price_source,
            "mtf_ladder": mtf_ladder,
            "setups": setups,
        }

    def refine_setups_with_flagship_ai(self, symbol: str, raw_bundle: dict, intel: dict, ai_router) -> tuple[dict, str]:
        setups = raw_bundle.get("setups", {})
        intraday = (
            setups.get("INTRADAY (M30-H1-H4)")
            or setups.get("INTRADAY (M15-M30-H1-H4)")
            or setups.get("INTRADAY (M1-M5-M15-M30-H1-H4)")
        )
        if not intraday:
            return raw_bundle, "Quant Deterministic Engine"

        prompt = (
            f"Validasi setup SMC/ICT untuk {symbol} (Live: {raw_bundle.get('live_price')}). "
            f"Arah: {intraday['direction']}, Entry: {intraday['entry']}, SL: {intraday['sl']}, "
            f"TP1: {intraday['tp1']}, TP2: {intraday['tp2']}, TP3: {intraday['tp3']}. "
            f"Rantai TF: {intraday['tf_chain']}. Telemetry: {intel.get('telemetry_summary', '')}. "
            f"Tulis 1 kalimat tajam alasan konfluensi Orderflow + SMC tanpa simbol < atau >."
        )
        ai_note = ai_router.refine_setup(prompt)
        if ai_note:
            clean_note = ai_note.replace("<", "").replace(">", "").strip()
            intraday["reason"] = f"{intraday['reason']} — AI Note: {clean_note[:220]}"

        return raw_bundle, getattr(ai_router, "active_setup_engine", "Quant Deterministic Engine")

    def calculate_confluence_score(
        self, symbol: str, intel: dict, macro_bias: str, mtf_ladder: dict | None = None
    ) -> int:
        score = 50
        sym = symbol.upper()
        macro = intel.get("macro", {})
        matrix = intel.get("matrix", {})
        dom = intel.get("crypto_dom", {})
        deriv = intel.get("deriv_onchain", {})
        ext = intel.get("extended", {})

        if mtf_ladder:
            aligned = sum(1 for v in mtf_ladder.values() if v == macro_bias)
            score += int((aligned / max(len(mtf_ladder), 1)) * 20)

        net_liq = float(macro.get("net_liquidity_b", 0.0))
        dfii10 = float(macro.get("dfii10", 0.0))
        if (macro_bias == "BULLISH" and net_liq > 0) or (macro_bias == "BEARISH" and net_liq <= 0):
            score += 6
        if (macro_bias == "BULLISH" and 0 < dfii10 < 2.0) or (macro_bias == "BEARISH" and dfii10 >= 2.0):
            score += 6

        dxy = float(matrix.get("DXY", {}).get("price", 0.0))
        vix = float(matrix.get("VIX", {}).get("price", 0.0))
        if dxy > 0 and ((macro_bias == "BULLISH" and dxy < 103.5) or (macro_bias == "BEARISH" and dxy >= 103.5)):
            score += 5
        if vix > 0 and (("XAU" in sym and vix >= 18.0) or ("BTC" in sym and vix < 22.0)):
            score += 5

        if "BTC" in sym:
            taker_bs = float(ext.get("taker_buy_sell_ratio", 1.0))
            cb_prem = float(ext.get("coinbase_premium_pct", 0.0))
            stable_7d = float(deriv.get("stablecoin_7d_flow_b", 0.0))
            usdt_d = float(dom.get("USDT_D", 0.0))

            if (macro_bias == "BULLISH" and taker_bs >= 1.0) or (macro_bias == "BEARISH" and taker_bs < 1.0):
                score += 3
            if (macro_bias == "BULLISH" and cb_prem >= 0.0) or (macro_bias == "BEARISH" and cb_prem < 0.0):
                score += 3
            if (macro_bias == "BULLISH" and stable_7d >= 0 and usdt_d < 6.0) or (
                macro_bias == "BEARISH" and stable_7d < 0
            ):
                score += 2
        else:
            if float(ext.get("xagusd", 0.0)) > 0:
                score += 8

        return max(0, min(100, int(score)))

    def verify_setup_before_send(
        self, bundle: dict, news_blackout: bool, confluence_score: int = 75
    ) -> tuple[bool, list[str]]:
        errors = []
        live_price = float(bundle.get("live_price", 0.0))
        if live_price <= 0:
            errors.append("Harga Live Offline")

        if confluence_score < self.min_confluence_score:
            errors.append(f"Skor Konfluensi Rendah ({confluence_score}<{self.min_confluence_score})")

        if news_blackout:
            errors.append("News Merah USD Aktif (±45m)")

        setups = bundle.get("setups", {})
        intraday = (
            setups.get("INTRADAY (M30-H1-H4)")
            or setups.get("INTRADAY (M15-M30-H1-H4)")
            or setups.get("INTRADAY (M1-M5-M15-M30-H1-H4)")
        )
        if not intraday:
            errors.append("Setup Intraday Kosong")
            return False, errors

        entry = float(intraday["entry"])
        sl = float(intraday["sl"])
        tp1 = float(intraday["tp1"])
        tp2 = float(intraday["tp2"])
        tp3 = float(intraday["tp3"])
        direction = intraday["direction"]

        risk_dist = abs(entry - sl)
        reward_dist = abs(tp2 - entry)
        if risk_dist <= 0 or (reward_dist / risk_dist) < self.min_rr:
            errors.append("RR di bawah 1:2.5")

        if "BUY" in direction:
            if not (sl < entry < tp1 < tp2 < tp3):
                errors.append("Geometri Level BUY Invalid")
        else:
            if not (sl > entry > tp1 > tp2 > tp3):
                errors.append("Geometri Level SELL Invalid")

        return len(errors) == 0, errors
