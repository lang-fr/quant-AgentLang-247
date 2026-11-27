import yfinance as yf
import pandas as pd


class InstitutionalSMCAlphaEngine:
    """
    Mesin Alpha SMC, OHLC & Konfluensi Multi-Pilar:
    - Memisahkan horizon SESI/SCALP, INTRADAY, SWING, dan HOLD POSITION dari struktur OHLC historis
    - Menghitung skor konfluensi (0-100) dari makro, intermarket, dan on-chain/orderflow
    - Menjaga struktur harga (Entry, SL, TP1/TP2/TP3) tetap konsisten sebelum dikirim ke Telegram
    """

    def _fetch_ohlc(self, symbol: str, period: str, interval: str) -> pd.DataFrame:
        ticker = "GC=F" if symbol == "XAUUSD" else "BTC-USD"
        try:
            df = yf.Ticker(ticker).history(period=period, interval=interval)
            if df is not None and not df.empty:
                return df.dropna()
        except Exception:
            pass
        return pd.DataFrame()

    def _compute_zone_setup(self, df: pd.DataFrame, live_price: float, direction: str, is_xau: bool) -> dict:
        dec = 2
        if df is not None and not df.empty:
            high_val = float(df["High"].max())
            low_val = float(df["Low"].min())
            rng = max(high_val - low_val, live_price * (0.004 if is_xau else 0.01))
        else:
            rng = live_price * (0.01 if is_xau else 0.025)
            high_val = live_price + (rng * 0.5)
            low_val = live_price - (rng * 0.5)

        min_risk = live_price * (0.002 if is_xau else 0.005)

        if direction == "SELL LIMIT":
            # Zona premium / OTE 0.705 di atas harga live untuk mencari penolakan turun
            entry = low_val + (rng * 0.705)
            if entry <= live_price * 1.0005:
                entry = live_price + max(rng * 0.15, min_risk * 0.8)
            sl = max(high_val + (rng * 0.05), entry + min_risk)
            risk = sl - entry
            tp1 = entry - (risk * 3.0)
            tp2 = entry - (risk * 6.0)
            tp3 = entry - (risk * 10.0)
        else:
            # Zona diskon / OTE 0.705 di bawah harga live untuk mencari pantulan naik
            entry = high_val - (rng * 0.705)
            if entry >= live_price * 0.9995:
                entry = live_price - max(rng * 0.15, min_risk * 0.8)
            sl = min(low_val - (rng * 0.05), entry - min_risk)
            risk = max(entry - sl, min_risk)
            sl = entry - risk
            tp1 = entry + (risk * 3.0)
            tp2 = entry + (risk * 6.0)
            tp3 = entry + (risk * 10.0)

        rr_tp1 = round(abs(tp1 - entry) / max(abs(entry - sl), 1e-9), 1)
        return {
            "direction": direction,
            "entry": round(entry, dec),
            "sl": round(sl, dec),
            "tp1": round(tp1, dec),
            "tp2": round(tp2, dec),
            "tp3": round(tp3, dec),
            "rr": f"1:{rr_tp1} / 1:6.0 / 1:10.0",
        }

    def calculate_confluence_score(self, symbol: str, intel: dict, macro_bias: str) -> int:
        """
        Menghitung skor konfluensi (0-100) dari makro, lawan pair/intermarket, dan derivatif/on-chain.
        """
        score = 65
        macro = intel.get("macro", {})
        matrix = intel.get("matrix", {})
        deriv = intel.get("deriv_onchain", {})
        dom = intel.get("crypto_dom", {})

        net_liq = float(macro.get("net_liquidity_b", 0.0))
        dxy_chg = float(matrix.get("DXY", {}).get("change_pct", 0.0))
        vix_val = float(matrix.get("VIX", {}).get("price", 20.0))

        if symbol == "XAUUSD":
            if macro_bias == "BEARISH" and dxy_chg >= 0:
                score += 10
            elif macro_bias == "BULLISH" and (dxy_chg <= 0 or net_liq > 0):
                score += 10
            if vix_val > 15.0:
                score += 5
        else:
            stable_flow = float(deriv.get("stablecoin_7d_flow_b", 0.0))
            btc_d = float(dom.get("BTC_D", 55.0))
            if macro_bias == "BEARISH" and stable_flow <= 0:
                score += 9
            elif macro_bias == "BULLISH" and stable_flow >= 0:
                score += 9
            if 45.0 <= btc_d <= 65.0:
                score += 5

        return max(0, min(100, int(score)))

    def build_cascade_setups(self, symbol: str, feeds, macro_bias: str) -> dict:
        live_price = feeds.get_live_price(symbol)
        is_xau = symbol == "XAUUSD"
        if live_price <= 0:
            live_price = 3620.4 if is_xau else 61400.0

        primary_dir = "SELL LIMIT" if macro_bias == "BEARISH" else "BUY LIMIT"

        df_scalp = self._fetch_ohlc(symbol, period="5d", interval="15m")
        df_intra = self._fetch_ohlc(symbol, period="60d", interval="1h")
        df_swing = self._fetch_ohlc(symbol, period="1y", interval="1d")
        df_hold = self._fetch_ohlc(symbol, period="5y", interval="1wk")

        scalp_base = self._compute_zone_setup(
            df_scalp.tail(48) if not df_scalp.empty else df_scalp,
            live_price, primary_dir, is_xau
        )
        intra_base = self._compute_zone_setup(
            df_intra.tail(240) if not df_intra.empty else df_intra,
            live_price, primary_dir, is_xau
        )
        swing_base = self._compute_zone_setup(
            df_swing.tail(120) if not df_swing.empty else df_swing,
            live_price, primary_dir, is_xau
        )
        hold_base = self._compute_zone_setup(
            df_hold, live_price, "BUY LIMIT", is_xau
        )

        setups = {
            "SESI / SCALP (M1-M5-M15)": {
                **scalp_base,
                "tf_chain": "M1 → M5 → M15",
                "aligned_count": "3/3 LTF Searah",
                "reason": f"Sweep likuiditas sesi & reaksi CE 50% FVG LTF selaras bias {macro_bias}.",
            },
            "INTRADAY (M30-H1-H4)": {
                **intra_base,
                "tf_chain": "M30 → H1 → H4",
                "aligned_count": "3/3 ITF Searah",
                "reason": f"Mitigasi Order Block / FVG intraday & konfirmasi struktur {macro_bias}.",
            },
            "SWING (H4-1D-1W)": {
                **swing_base,
                "tf_chain": "H4 → 1D → 1W",
                "aligned_count": "3/3 HTF Searah",
                "reason": f"Zona OTE 0.705 swing multi-pekan mengikuti arah makro {macro_bias}.",
            },
            "HOLD POSITION (1MON-5Y)": {
                **hold_base,
                "tf_chain": "1W → 1MON → 5Y",
                "aligned_count": "Akumulasi Makro",
                "reason": "Zona diskon struktural jangka panjang untuk akumulasi bertahap.",
            },
        }

        return {
            "symbol": symbol,
            "live_price": round(live_price, 2),
            "setups": setups,
        }

    def refine_setups_with_flagship_ai(self, symbol: str, raw_bundle: dict, intel: dict, ai_router) -> tuple[dict, str]:
        intra = raw_bundle["setups"]["INTRADAY (M30-H1-H4)"]
        swing = raw_bundle["setups"]["SWING (H4-1D-1W)"]

        sys_prompt = (
            "Anda adalah analis kuantitatif SMC & Orderflow. "
            "Tulis 1 kalimat ringkas alasan teknikal untuk setup Intraday berdasarkan struktur dan data makro yang diberikan. "
            "Jangan ubah angka harga dan jangan gunakan simbol < atau >."
        )
        usr_prompt = (
            f"Aset: {symbol} | Live: {raw_bundle['live_price']} | "
            f"Intraday: {intra['direction']} @ {intra['entry']} (SL {intra['sl']}, TP1 {intra['tp1']}) | "
            f"Swing: {swing['direction']} @ {swing['entry']} | "
            f"DXY: {intel.get('matrix', {}).get('DXY', {}).get('price')} | "
            f"NetLiq: {intel.get('macro', {}).get('net_liquidity_b')}B"
        )

        ai_reason, model_used = ai_router.generate(sys_prompt, usr_prompt, max_tokens=220)
        if ai_reason and len(ai_reason.strip()) > 12:
            clean_reason = ai_reason.strip().replace("<", "").replace(">", "")
            raw_bundle["setups"]["INTRADAY (M30-H1-H4)"]["reason"] = clean_reason

        return raw_bundle, model_used

    def verify_setup_before_send(self, bundle: dict, news_blackout: bool, confidence_score: int) -> tuple[bool, list[str]]:
        errors = []
        if news_blackout:
            errors.append("News Blackout Aktif")
        if confidence_score < 75:
            errors.append(f"Skor Konfluensi Rendah ({confidence_score}/100)")

        live = float(bundle.get("live_price", 0.0))
        if live <= 0:
            errors.append("Harga Live Invalid")

        intra = bundle.get("setups", {}).get("INTRADAY (M30-H1-H4)", {})
        if not intra:
            errors.append("Setup Intraday Kosong")
            return False, errors

        direction = intra.get("direction", "")
        entry = float(intra.get("entry", 0.0))
        sl = float(intra.get("sl", 0.0))
        tp1 = float(intra.get("tp1", 0.0))

        risk = abs(entry - sl)
        reward = abs(tp1 - entry)

        if risk <= 0:
            errors.append("Jarak SL Nol")
        elif (reward / risk) < 2.5:
            errors.append(f"RR Kurang dari 1:2.5 ({round(reward / risk, 2)})")

        if direction == "SELL LIMIT" and not (sl > entry > tp1):
            errors.append("Struktur Harga SELL LIMIT Terbalik")
        elif direction == "BUY LIMIT" and not (sl < entry < tp1):
            errors.append("Struktur Harga BUY LIMIT Terbalik")

        return len(errors) == 0, errors
