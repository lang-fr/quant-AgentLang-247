import numpy as np
import pandas as pd


class InstitutionalSMCAlphaEngine:
    """
    Mesin Multi-Timeframe Cascade (dari M1 Menit 1 membesar hingga 1MON Monthly 1 Bulan)
    + SMC/Orderflow + Pre-Send Verification Gate.
    """

    CASCADE_CONFIG = {
        "PER SESSION (M1-M5-M15)": [
            "M1",   # Menit 1
            "M5",   # Menit 5
            "M15",  # Menit 15
        ],
        "INTRADAY (M1-M5-M15-M30-H1-H4)": [
            "M1",   # Menit 1
            "M5",   # Menit 5
            "M15",  # Menit 15
            "M30",  # Menit 30
            "H1",   # 1 Jam
            "H4",   # 4 Jam
        ],
        "SWING (M1-M5-M15-M30-H1-H4-D1-W1)": [
            "M1",   # Menit 1
            "M5",   # Menit 5
            "M15",  # Menit 15
            "M30",  # Menit 30
            "H1",   # 1 Jam
            "H4",   # 4 Jam
            "D1",   # Daily / 1 Hari
            "W1",   # Weekly / 1 Minggu
        ],
        "HOLD POSITION (M1-M5-M15-M30-H1-H4-D1-W1-1MON)": [
            "M1",    # Menit 1
            "M5",    # Menit 5
            "M15",   # Menit 15
            "M30",   # Menit 30
            "H1",    # 1 Jam
            "H4",    # 4 Jam
            "D1",    # Daily / 1 Hari
            "W1",    # Weekly / 1 Minggu
            "1MON",  # Monthly / 1 Bulan (bukan menit)
        ],
    }

    def _analyze_single_tf(self, df: pd.DataFrame) -> dict:
        if df is None or len(df) < 20:
            return {"valid": False}

        close = df["Close"].values
        high = df["High"].values
        low = df["Low"].values

        ema20 = pd.Series(close).ewm(span=20).mean().iloc[-1]
        ema50 = pd.Series(close).ewm(span=50).mean().iloc[-1]
        bias = "BULLISH" if close[-1] > ema20 and ema20 >= ema50 else "BEARISH"

        tr = np.maximum(
            high[1:] - low[1:],
            np.maximum(np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1])),
        )
        atr = float(np.mean(tr[-14:])) if len(tr) >= 14 else float(np.mean(tr))

        swing_high = float(np.max(high[-30:]))
        swing_low = float(np.min(low[-30:]))
        eq_50 = (swing_high + swing_low) / 2.0
        ote_sell_705 = swing_low + 0.705 * (swing_high - swing_low)
        ote_buy_705 = swing_high - 0.705 * (swing_high - swing_low)

        fvg_zone = None
        for i in range(len(df) - 3, max(len(df) - 20, 1), -1):
            if low[i + 2] > high[i]:
                fvg_zone = ("BULLISH_FVG", round(float(high[i]), 2), round(float(low[i + 2]), 2))
                break
            if high[i + 2] < low[i]:
                fvg_zone = ("BEARISH_FVG", round(float(high[i + 2]), 2), round(float(low[i]), 2))
                break

        return {
            "valid": True,
            "price": round(float(close[-1]), 2),
            "bias": bias,
            "atr": round(atr, 4),
            "swing_high": round(swing_high, 2),
            "swing_low": round(swing_low, 2),
            "eq_50": round(eq_50, 2),
            "ote_sell_705": round(ote_sell_705, 2),
            "ote_buy_705": round(ote_buy_705, 2),
            "fvg": fvg_zone,
        }

    def build_cascade_setups(self, symbol: str, feeds, macro_bias: str) -> dict:
        """
        Membangun setup untuk keempat horizon (Per Session, Intraday, Swing, Hold Position)
        dimulai dari M1 (Menit 1) hingga 1MON (Monthly / 1 Bulan).
        """
        tf_cache = {}
        all_tfs = self.CASCADE_CONFIG["HOLD POSITION (M1-M5-M15-M30-H1-H4-D1-W1-1MON)"]
        for tf_code in all_tfs:
            df = feeds.fetch_mtf_candles(symbol, tf_code)
            tf_cache[tf_code] = self._analyze_single_tf(df)

        m1_data = tf_cache.get("M1", {})
        live_price = m1_data.get("price") or tf_cache.get("M15", {}).get("price", 0.0)
        m1_atr = m1_data.get("atr", live_price * 0.0005)

        setups = {}
        for label, tf_chain in self.CASCADE_CONFIG.items():
            valid_nodes = [tf_cache[tf] for tf in tf_chain if tf_cache.get(tf, {}).get("valid")]
            if not valid_nodes:
                continue

            micro = valid_nodes[0]        # M1 (Menit 1) untuk presisi titik entry zero floating
            macro_node = valid_nodes[-1]  # TF puncak horizon (M15 / H4 / W1 / 1MON)

            bull_votes = sum(1 for n in valid_nodes if n["bias"] == "BULLISH")
            bear_votes = len(valid_nodes) - bull_votes
            is_hold = "HOLD POSITION" in label

            direction = (
                "BUY LIMIT"
                if (is_hold or (bull_votes > bear_votes and macro_bias == "BULLISH"))
                else "SELL LIMIT"
            )

            if direction == "SELL LIMIT":
                anchor = macro_node["ote_sell_705"] if "SWING" in label else max(micro["price"], macro_node["eq_50"])
                if "PER SESSION" in label or "INTRADAY" in label:
                    anchor = round(live_price + (m1_atr * 0.8), 2)
                sl_dist = max(m1_atr * 2.2, (macro_node["swing_high"] - anchor) * 0.25, live_price * 0.0012)
                if "SWING" in label:
                    sl_dist = max(sl_dist, live_price * 0.015)
                sl = round(anchor + sl_dist, 2)
                risk = sl - anchor
                tp1 = round(anchor - (risk * 3.0), 2)
                tp2 = round(anchor - (risk * 6.0), 2)
                tp3 = round(anchor - (risk * 10.0), 2)
            else:
                anchor = macro_node["ote_buy_705"] if is_hold else min(micro["price"], macro_node["eq_50"])
                if "PER SESSION" in label or "INTRADAY" in label:
                    anchor = round(live_price - (m1_atr * 0.8), 2)
                sl_dist = max(m1_atr * 2.2, (anchor - macro_node["swing_low"]) * 0.25, live_price * 0.0012)
                if is_hold:
                    sl_dist = max(sl_dist, live_price * 0.06)
                sl = round(anchor - sl_dist, 2)
                risk = anchor - sl
                tp1 = round(anchor + (risk * 3.0), 2)
                tp2 = round(anchor + (risk * 6.0), 2)
                tp3 = round(anchor + (risk * 11.0), 2)

            rr_ratio = round(abs(tp3 - anchor) / max(risk, 1e-6), 1)
            tf_sequence_str = " → ".join(tf_chain)

            setups[label] = {
                "direction": direction,
                "entry": anchor,
                "sl": sl,
                "tp1": tp1,
                "tp2": tp2,
                "tp3": tp3,
                "rr": rr_ratio,
                "tf_chain": tf_sequence_str,
                "aligned_count": f"{max(bull_votes, bear_votes)}/{len(valid_nodes)} TF",
                "fvg_micro": micro.get("fvg"),
            }

        return {"live_price": live_price, "setups": setups}

    def verify_setup_before_send(self, setup_bundle: dict, news_blackout: bool, confidence_score: int) -> tuple[bool, list]:
        """
        Pre-Send Audit Gate: memeriksa 6 syarat mutlak sebelum sinyal dikirim ke Telegram.
        """
        reasons = []
        if confidence_score < 75:
            reasons.append(f"Skor konfluensi ({confidence_score}) di bawah ambang minimum 75.")
        if news_blackout:
            reasons.append("News Blackout aktif (berita merah USD <= 15 menit).")
        if not setup_bundle.get("setups"):
            reasons.append("Data Multi-Timeframe tidak lengkap.")

        for name, s in setup_bundle.get("setups", {}).items():
            if s["direction"] == "SELL LIMIT" and not (s["sl"] > s["entry"] > s["tp1"] > s["tp2"] > s["tp3"]):
                reasons.append(f"Struktur harga SL/Entry/TP tidak valid pada {name}.")
            if s["direction"] == "BUY LIMIT" and not (s["sl"] < s["entry"] < s["tp1"] < s["tp2"] < s["tp3"]):
                reasons.append(f"Struktur harga SL/Entry/TP tidak valid pada {name}.")
            if s["rr"] < 3.0:
                reasons.append(f"Risk/Reward {name} ({s['rr']}) di bawah 1:3.")

        is_safe = len(reasons) == 0
        return is_safe, reasons
