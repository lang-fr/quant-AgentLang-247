import math
import requests
import yfinance as yf


class InstitutionalSMCAlphaEngine:
    """
    Mesin Kuantitatif Institusional Full-Stack (M1 -> 1MON):
    1. Quant Statistical: ATR(14), Session VWAP, & Volume Z-Score Displacement
    2. Orderflow: Candle Volume Delta, Cumulative Volume Delta (CVD), & Absorption
    3. Liquidity Heatmap: Orderbook L2 Bid/Ask Wall + Unswept EQH/EQL Liquidity Clusters
    4. SMC / ICT: BSL/SSL Sweep, MSS, FVG, Unmitigated Order Block, & Real Fib OTE (0.618-0.786)
    5. Supply & Demand: DBR/RBD Unmitigated Zones + RBS/SBR Flip Levels
    """

    TF_ORDER = ["M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "1MON"]

    def __init__(self):
        self.min_rr = 2.5
        self.min_confluence_score = 65

    def _fetch_ohlc_series(self, symbol: str, interval_key: str) -> list[dict]:
        sym = symbol.upper()
        if "BTC" in sym:
            binance_map = {
                "M1": ("1m", 80),
                "M5": ("5m", 80),
                "M15": ("15m", 80),
                "M30": ("30m", 80),
                "H1": ("1h", 80),
                "H4": ("4h", 80),
                "D1": ("1d", 80),
                "W1": ("1w", 60),
                "1MON": ("1M", 36),
            }
            tf_str, lim = binance_map.get(interval_key, ("1h", 80))
            try:
                url = f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval={tf_str}&limit={lim}"
                r = requests.get(url, timeout=6)
                if r.status_code == 200:
                    return [
                        {
                            "open": float(k[1]),
                            "high": float(k[2]),
                            "low": float(k[3]),
                            "close": float(k[4]),
                            "volume": float(k[5]),
                        }
                        for k in r.json()
                    ]
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
            "D1": ("1y", "1d"),
            "W1": ("3y", "1wk"),
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
                    bars = [
                        {
                            "open": float(row["Open"]),
                            "high": float(row["High"]),
                            "low": float(row["Low"]),
                            "close": float(row["Close"]),
                            "volume": float(row.get("Volume", 0.0)),
                        }
                        for _, row in df.tail(80).iterrows()
                    ]
                    if len(bars) >= 15:
                        return bars
            except Exception:
                continue
        return []

    def _fetch_orderbook_heatmap(self, symbol: str, live_price: float) -> dict:
        """
        Liquidity Heatmap L2 Orderbook (Binance Depth) untuk mendeteksi tembok likuiditas
        Bid Wall (support institusi) & Ask Wall (resistance institusi) di dekat harga live.
        """
        if "BTC" not in symbol.upper() or live_price <= 0:
            return {"bid_wall": 0.0, "ask_wall": 0.0, "ob_imbalance": 0.0}

        try:
            url = "https://api.binance.com/api/v3/depth?symbol=BTCUSDT&limit=100"
            r = requests.get(url, timeout=4)
            if r.status_code == 200:
                data = r.json()
                bids = [(float(p), float(q)) for p, q in data.get("bids", []) if float(p) < live_price]
                asks = [(float(p), float(q)) for p, q in data.get("asks", []) if float(p) > live_price]
                total_bid = sum(q for _, q in bids)
                total_ask = sum(q for _, q in asks)
                denom = total_bid + total_ask
                imbalance = round((total_bid - total_ask) / denom, 3) if denom > 0 else 0.0
                best_bid_wall = max(bids, key=lambda x: x[1])[0] if bids else 0.0
                best_ask_wall = max(asks, key=lambda x: x[1])[0] if asks else 0.0
                return {
                    "bid_wall": round(best_bid_wall, 2),
                    "ask_wall": round(best_ask_wall, 2),
                    "ob_imbalance": imbalance,
                }
        except Exception:
            pass
        return {"bid_wall": 0.0, "ask_wall": 0.0, "ob_imbalance": 0.0}

    def _compute_orderflow_and_quant(self, recent: list[dict]) -> dict:
        """Menghitung Candle Volume Delta, CVD, Volume Z-Score, Absorption, & VWAP."""
        deltas = []
        vols = []
        pv_sum = 0.0
        vol_sum = 0.0

        for b in recent:
            hi, lo, cl = b["high"], b["low"], b["close"]
            vol = max(b.get("volume", 0.0), 1.0)
            rng = max(hi - lo, 1e-6)
            # Rumus Orderflow Delta berbasis tekanan closing dalam range candle
            delta = vol * (((cl - lo) - (hi - cl)) / rng)
            deltas.append(delta)
            vols.append(vol)
            typical = (hi + lo + cl) / 3.0
            pv_sum += typical * vol
            vol_sum += vol

        cvd = sum(deltas)
        vwap = (pv_sum / vol_sum) if vol_sum > 0 else recent[-1]["close"]

        mean_v = sum(vols) / max(len(vols), 1)
        var_v = sum((v - mean_v) ** 2 for v in vols) / max(len(vols), 1)
        std_v = math.sqrt(var_v) if var_v > 0 else 1.0
        vol_zscore = (vols[-1] - mean_v) / std_v

        last = recent[-1]
        body = abs(last["close"] - last["open"])
        rng = max(last["high"] - last["low"], 1e-6)
        absorption = (vol_zscore >= 1.2) and ((body / rng) < 0.35)

        return {
            "cvd": cvd,
            "vwap": vwap,
            "vol_zscore": round(vol_zscore, 2),
            "absorption": absorption,
        }

    def _detect_smc_snd_and_heatmap(self, recent: list[dict], atr: float) -> dict:
        """
        Mendeteksi:
        - FVG (Fair Value Gap) Bullish & Bearish
        - Unmitigated Order Block (OB) & Supply/Demand (DBR / RBD)
        - RBS (Resistance Become Support) & SBR (Support Become Resistance)
        - Liquidity Heatmap Clusters (Unswept EQH & EQL)
        - Real Fibonacci OTE (0.618 - 0.786 / 0.705 Sweet Spot)
        """
        last_close = recent[-1]["close"]
        fvg_bull, fvg_bear = 0.0, 0.0
        ob_demand, ob_supply = 0.0, 0.0

        # 1. Scan FVG + Unmitigated Order Block / Supply & Demand (DBR / RBD)
        for i in range(len(recent) - 2, 1, -1):
            c_base = recent[i - 1]
            c_imp = recent[i]
            c_next = recent[i + 1]
            imp_body = abs(c_imp["close"] - c_imp["open"])

            # Bullish FVG & Demand / Bullish OB (Drop-Base-Rally)
            if fvg_bull == 0.0 and c_next["low"] > c_base["high"]:
                mid_fvg = (c_next["low"] + c_base["high"]) / 2.0
                if mid_fvg < last_close:
                    fvg_bull = round(mid_fvg, 2)

            if ob_demand == 0.0 and c_imp["close"] > c_imp["open"] and imp_body >= max(atr * 1.1, 1e-6):
                zone_top = max(c_base["open"], c_base["close"])
                zone_bot = c_base["low"]
                # Cek apakah zona masih unmitigated (belum ditembus ke bawah setelah terbentuk)
                min_after = min(b["low"] for b in recent[i + 1 :])
                if zone_top < last_close and min_after >= zone_bot:
                    ob_demand = round((zone_top + zone_bot) / 2.0, 2)

            # Bearish FVG & Supply / Bearish OB (Rally-Base-Drop)
            if fvg_bear == 0.0 and c_next["high"] < c_base["low"]:
                mid_fvg = (c_base["low"] + c_next["high"]) / 2.0
                if mid_fvg > last_close:
                    fvg_bear = round(mid_fvg, 2)

            if ob_supply == 0.0 and c_imp["close"] < c_imp["open"] and imp_body >= max(atr * 1.1, 1e-6):
                zone_bot = min(c_base["open"], c_base["close"])
                zone_top = c_base["high"]
                max_after = max(b["high"] for b in recent[i + 1 :])
                if zone_bot > last_close and max_after <= zone_top:
                    ob_supply = round((zone_top + zone_bot) / 2.0, 2)

        # 2. RBS / SBR Flip Detection dari struktur pivot tengah
        mid_idx = len(recent) // 2
        early_high = max(b["high"] for b in recent[:mid_idx])
        early_low = min(b["low"] for b in recent[:mid_idx])
        rbs_level = round(early_high, 2) if last_close > early_high else 0.0
        sbr_level = round(early_low, 2) if last_close < early_low else 0.0

        # 3. Liquidity Heatmap Clusters: Unswept Equal Highs (EQH) & Equal Lows (EQL)
        tol = max(atr * 0.25, last_close * 0.0005)
        eqh_pool, eql_pool = 0.0, 0.0
        highs = [b["high"] for b in recent[:-1]]
        lows = [b["low"] for b in recent[:-1]]
        for idx_h in range(len(highs) - 1, 0, -1):
            if highs[idx_h] > last_close and abs(highs[idx_h] - highs[idx_h - 1]) <= tol:
                eqh_pool = round(max(highs[idx_h], highs[idx_h - 1]), 2)
                break
        for idx_l in range(len(lows) - 1, 0, -1):
            if lows[idx_l] < last_close and abs(lows[idx_l] - lows[idx_l - 1]) <= tol:
                eql_pool = round(min(lows[idx_l], lows[idx_l - 1]), 2)
                break

        # 4. Real Fibonacci OTE (0.618 - 0.786, Sweet Spot 0.705)
        swing_high = max(b["high"] for b in recent)
        swing_low = min(b["low"] for b in recent)
        leg = max(swing_high - swing_low, 1e-6)
        fib_ote_bull = round(swing_high - (0.705 * leg), 2)
        fib_ote_bear = round(swing_low + (0.705 * leg), 2)

        return {
            "fvg_bull": fvg_bull,
            "fvg_bear": fvg_bear,
            "ob_demand": ob_demand,
            "ob_supply": ob_supply,
            "rbs_level": rbs_level,
            "sbr_level": sbr_level,
            "eqh_pool": eqh_pool,
            "eql_pool": eql_pool,
            "fib_ote_bull": fib_ote_bull,
            "fib_ote_bear": fib_ote_bear,
        }

    def _analyze_tf_structure(self, bars: list[dict], fallback_bias: str) -> dict:
        if len(bars) < 15:
            return {
                "bias": fallback_bias,
                "swing_high": 0.0,
                "swing_low": 0.0,
                "atr": 0.0,
                "fvg_bull": 0.0,
                "fvg_bear": 0.0,
                "ob_demand": 0.0,
                "ob_supply": 0.0,
                "rbs_level": 0.0,
                "sbr_level": 0.0,
                "eqh_pool": 0.0,
                "eql_pool": 0.0,
                "fib_ote_bull": 0.0,
                "fib_ote_bear": 0.0,
                "cvd": 0.0,
                "vwap": 0.0,
                "vol_zscore": 0.0,
                "absorption": False,
                "sweep_tag": "HTF Macro Alignment",
            }

        recent = bars[-24:]
        swing_high = max(b["high"] for b in recent)
        swing_low = min(b["low"] for b in recent)
        last_close = recent[-1]["close"]

        trs = []
        for i in range(1, len(recent)):
            hi = recent[i]["high"]
            lo = recent[i]["low"]
            prev_c = recent[i - 1]["close"]
            trs.append(max(hi - lo, abs(hi - prev_c), abs(lo - prev_c)))
        atr = sum(trs[-14:]) / max(len(trs[-14:]), 1)

        ema_fast = sum(b["close"] for b in bars[-9:]) / 9.0
        ema_slow = sum(b["close"] for b in bars[-21:]) / min(len(bars), 21)

        qf = self._compute_orderflow_and_quant(recent)
        smc_snd = self._detect_smc_snd_and_heatmap(recent, atr)

        prev_high = max(b["high"] for b in recent[-12:-2])
        prev_low = min(b["low"] for b in recent[-12:-2])
        last_high = recent[-1]["high"]
        last_low = recent[-1]["low"]

        sweep_tag = "BOS + Fib OTE 0.705"
        if last_low < prev_low and last_close > prev_low:
            bias = "BULLISH"
            sweep_tag = "SSL Sweep + Bullish MSS" + (" + Absorption" if qf["absorption"] else "")
        elif last_high > prev_high and last_close < prev_high:
            bias = "BEARISH"
            sweep_tag = "BSL Sweep + Bearish MSS" + (" + Absorption" if qf["absorption"] else "")
        elif (ema_fast > ema_slow and last_close >= qf["vwap"]) or (qf["cvd"] > 0 and last_close > recent[0]["close"]):
            bias = "BULLISH"
            sweep_tag = "Bullish BOS + Positive CVD"
        elif (ema_fast < ema_slow and last_close <= qf["vwap"]) or (qf["cvd"] < 0 and last_close < recent[0]["close"]):
            bias = "BEARISH"
            sweep_tag = "Bearish BOS + Negative CVD"
        else:
            bias = fallback_bias

        return {
            "bias": bias,
            "swing_high": swing_high,
            "swing_low": swing_low,
            "atr": atr,
            "sweep_tag": sweep_tag,
            **qf,
            **smc_snd,
        }

    def _get_sniper_sl_bounds(self, symbol: str, horizon_mode: str, live_price: float) -> tuple[float, float, float]:
        """Mengunci batas (min_sl, max_sl, max_pullback) agar Stop Loss selalu tipis & presisi."""
        is_xau = "XAU" in symbol.upper()
        if is_xau:
            if horizon_mode == "SCALP":
                return (1.8, 3.0, 3.5)      # SL terkunci ketat 18-30 pips (1.8 - 3.0 USD)
            if horizon_mode == "INTRADAY":
                return (4.5, 9.5, 12.0)     # SL terkunci ketat 45-95 pips (4.5 - 9.5 USD)
            return (12.0, 22.0, 28.0)       # Swing: SL terkunci ketat 120-220 pips (12.0 - 22.0 USD)

        if horizon_mode == "SCALP":
            return (live_price * 0.0015, live_price * 0.0030, live_price * 0.0035)
        if horizon_mode == "INTRADAY":
            return (live_price * 0.0040, live_price * 0.0085, live_price * 0.0110)
        return (live_price * 0.0095, live_price * 0.0190, live_price * 0.0250)

    def _refine_m1_m5_entry(
        self,
        direction: str,
        live_price: float,
        sl_dist: float,
        max_pullback: float,
        poi_tf: str,
        tf_stats: dict,
        ob_heatmap: dict,
    ) -> tuple[float, str]:
        """
        Micro-Refinement Konfluensi 5-Lapis pada M1 -> M5 -> POI TF:
        1. Unmitigated Order Block / Demand-Supply Zone (DBR / RBD)
        2. Micro Fair Value Gap (FVG M1 / M5)
        3. RBS / SBR Flip Level
        4. Real Fibonacci OTE 0.705 (0.618-0.786)
        5. L2 Orderbook Liquidity Wall (Bid/Ask Wall)
        """
        is_buy = "BUY" in direction
        candidates = []

        for micro_tf in ("M1", "M5", poi_tf):
            st = tf_stats.get(micro_tf, {})
            if is_buy:
                checks = [
                    (st.get("ob_demand", 0.0), f"Unmitigated Demand/OB {micro_tf}"),
                    (st.get("fvg_bull", 0.0), f"Micro-FVG {micro_tf}"),
                    (st.get("rbs_level", 0.0), f"RBS Flip {micro_tf}"),
                    (st.get("fib_ote_bull", 0.0), f"Real Fib OTE 0.705 {micro_tf}"),
                ]
                for lvl, lbl in checks:
                    if lvl > 0 and 0.3 <= (live_price - lvl) <= max_pullback:
                        candidates.append((lvl, f"{lbl} (POI {poi_tf})"))
            else:
                checks = [
                    (st.get("ob_supply", 0.0), f"Unmitigated Supply/OB {micro_tf}"),
                    (st.get("fvg_bear", 0.0), f"Micro-FVG {micro_tf}"),
                    (st.get("sbr_level", 0.0), f"SBR Flip {micro_tf}"),
                    (st.get("fib_ote_bear", 0.0), f"Real Fib OTE 0.705 {micro_tf}"),
                ]
                for lvl, lbl in checks:
                    if lvl > 0 and 0.3 <= (lvl - live_price) <= max_pullback:
                        candidates.append((lvl, f"{lbl} (POI {poi_tf})"))

        # Cek L2 Orderbook Wall (Liquidity Heatmap)
        wall_lvl = ob_heatmap.get("bid_wall", 0.0) if is_buy else ob_heatmap.get("ask_wall", 0.0)
        if wall_lvl > 0:
            dist_w = (live_price - wall_lvl) if is_buy else (wall_lvl - live_price)
            if 0.3 <= dist_w <= max_pullback:
                candidates.append((wall_lvl, f"L2 Orderbook Liquidity Wall (POI {poi_tf})"))

        if candidates:
            best_lvl, best_lbl = candidates[0]
            return round(best_lvl, 2), best_lbl

        pullback = round(min(max_pullback * 0.50, sl_dist * 0.75), 2)
        if is_buy:
            return round(live_price - pullback, 2), f"M1-M5 Discount OTE 0.705 (POI {poi_tf})"
        return round(live_price + pullback, 2), f"M1-M5 Premium OTE 0.705 (POI {poi_tf})"

    def _build_horizon_setup(
        self,
        symbol: str,
        live_price: float,
        tf_group: list[str],
        anchor_tfs: list[str],
        poi_tf: str,
        horizon_mode: str,
        tf_stats: dict,
        macro_bias: str,
        ob_heatmap: dict,
    ) -> dict:
        anchor_bull = sum(1 for tf in anchor_tfs if tf_stats[tf]["bias"] == "BULLISH")
        anchor_bear = sum(1 for tf in anchor_tfs if tf_stats[tf]["bias"] == "BEARISH")

        if anchor_bull > anchor_bear:
            direction = "BUY LIMIT (SNIPER)"
            target_bias = "BULLISH"
        elif anchor_bear > anchor_bull:
            direction = "SELL LIMIT (SNIPER)"
            target_bias = "BEARISH"
        else:
            direction = "BUY LIMIT (SNIPER)" if macro_bias == "BULLISH" else "SELL LIMIT (SNIPER)"
            target_bias = macro_bias

        aligned = sum(1 for tf in tf_group if tf_stats[tf]["bias"] == target_bias)

        m1_atr = tf_stats["M1"]["atr"]
        m5_atr = tf_stats["M5"]["atr"]
        poi_atr = tf_stats[poi_tf]["atr"]
        micro_base = max(m1_atr * 2.2, m5_atr * 1.1, poi_atr * 0.35)

        min_sl, max_sl, max_pullback = self._get_sniper_sl_bounds(symbol, horizon_mode, live_price)
        raw_sl = micro_base if micro_base > 0 else min_sl
        sl_dist = round(max(min_sl, min(raw_sl, max_sl)), 2)

        rr_tp1, rr_tp2, rr_tp3 = (2.0, 4.0, 7.0) if horizon_mode == "SWING" else (1.5, 3.0, 5.0)

        entry, poi_label = self._refine_m1_m5_entry(
            direction=direction,
            live_price=live_price,
            sl_dist=sl_dist,
            max_pullback=max_pullback,
            poi_tf=poi_tf,
            tf_stats=tf_stats,
            ob_heatmap=ob_heatmap,
        )

        # Target Likuiditas (Draw on Liquidity dari Heatmap EQH/EQL jika searah & valid)
        poi_data = tf_stats[poi_tf]
        if "BUY" in direction:
            sl = round(entry - sl_dist, 2)
            tp1 = round(entry + (sl_dist * rr_tp1), 2)
            tp2 = round(entry + (sl_dist * rr_tp2), 2)
            tp3 = round(entry + (sl_dist * rr_tp3), 2)
            liq_target = poi_data.get("eqh_pool", 0.0)
            liq_note = f" | Draw Liquidity EQH: {liq_target}" if liq_target > tp1 else ""
        else:
            sl = round(entry + sl_dist, 2)
            tp1 = round(entry - (sl_dist * rr_tp1), 2)
            tp2 = round(entry - (sl_dist * rr_tp2), 2)
            tp3 = round(entry - (sl_dist * rr_tp3), 2)
            liq_target = poi_data.get("eql_pool", 0.0)
            liq_note = f" | Draw Liquidity EQL: {liq_target}" if 0 < liq_target < tp1 else ""

        chain_tokens = [
            f"{tf}{'↑' if tf_stats[tf]['bias'] == 'BULLISH' else '↓'}"
            for tf in tf_group
        ]

        lead_anchor = anchor_tfs[-1]
        m1_st = tf_stats["M1"]
        of_tag = f"CVD:{'POS' if m1_st['cvd'] >= 0 else 'NEG'} Z:{m1_st['vol_zscore']}"
        reason = (
            f"Bias {lead_anchor} ({tf_stats[lead_anchor]['sweep_tag']}) + Orderflow M1 ({of_tag}) | "
            f"Entry: {poi_label}{liq_note} | Tight SL {sl_dist} pts ({aligned}/{len(tf_group)} TF)"
        )

        return {
            "direction": direction,
            "rr": f"1 : {rr_tp2} (TP2) / 1 : {rr_tp3} (TP3)",
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

        ob_heatmap = self._fetch_orderbook_heatmap(symbol, live_price)

        if live_price <= 0:
            return {
                "symbol": symbol,
                "live_price": 0.0,
                "price_source": price_source,
                "mtf_ladder": mtf_ladder,
                "tf_stats": tf_stats,
                "ob_heatmap": ob_heatmap,
                "setups": {},
            }

        setups = {
            "SESI / SCALP (M1-M5-M15)": self._build_horizon_setup(
                symbol=symbol,
                live_price=live_price,
                tf_group=["M1", "M5", "M15"],
                anchor_tfs=["M15"],
                poi_tf="M5",
                horizon_mode="SCALP",
                tf_stats=tf_stats,
                macro_bias=macro_bias,
                ob_heatmap=ob_heatmap,
            ),
            "INTRADAY (M1-M5-M15-M30-H1-H4)": self._build_horizon_setup(
                symbol=symbol,
                live_price=live_price,
                tf_group=["M1", "M5", "M15", "M30", "H1", "H4"],
                anchor_tfs=["H1", "H4"],
                poi_tf="M15",
                horizon_mode="INTRADAY",
                tf_stats=tf_stats,
                macro_bias=macro_bias,
                ob_heatmap=ob_heatmap,
            ),
            "SWING / POSITION (M1-M5-M15-M30-H1-H4-D1-W1-1MON)": self._build_horizon_setup(
                symbol=symbol,
                live_price=live_price,
                tf_group=["M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "1MON"],
                anchor_tfs=["D1", "W1", "1MON"],
                poi_tf="H1",
                horizon_mode="SWING",
                tf_stats=tf_stats,
                macro_bias=macro_bias,
                ob_heatmap=ob_heatmap,
            ),
        }

        return {
            "symbol": symbol,
            "live_price": live_price,
            "price_source": price_source,
            "mtf_ladder": mtf_ladder,
            "tf_stats": tf_stats,
            "ob_heatmap": ob_heatmap,
            "setups": setups,
        }

    def refine_setups_with_flagship_ai(self, symbol: str, raw_bundle: dict, intel: dict, ai_router) -> tuple[dict, str]:
        setups = raw_bundle.get("setups", {})
        intraday = (
            setups.get("INTRADAY (M1-M5-M15-M30-H1-H4)")
            or setups.get("INTRADAY (M30-H1-H4)")
            or setups.get("INTRADAY (M15-M30-H1-H4)")
        )
        if not intraday:
            return raw_bundle, "Quant Deterministic Engine"

        prompt = (
            f"Validasi setup Quant + Orderflow + Heatmap + SMC/ICT + SnD untuk {symbol} (Live: {raw_bundle.get('live_price')}). "
            f"Arah: {intraday['direction']}, Entry: {intraday['entry']}, Tight SL: {intraday['sl']}, "
            f"TP1: {intraday['tp1']}, TP2: {intraday['tp2']}, TP3: {intraday['tp3']}. "
            f"Rantai TF: {intraday['tf_chain']}. Telemetry: {intel.get('telemetry_summary', '')}. "
            f"Tulis 1 kalimat tajam alasan konfluensi Orderflow + Heatmap + SMC tanpa simbol < atau >."
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
            setups.get("INTRADAY (M1-M5-M15-M30-H1-H4)")
            or setups.get("INTRADAY (M30-H1-H4)")
            or setups.get("INTRADAY (M15-M30-H1-H4)")
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
