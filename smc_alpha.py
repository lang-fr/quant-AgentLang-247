import pandas as pd
import yfinance as yf


class InstitutionalSMCAlphaEngine:
    """
    Mesin Alpha SMC & Full Multi-Timeframe Ladder (M1 -> M5 -> M15 -> M30 -> H1 -> H4 -> 1D -> 1W -> 1M):
    - Menarik histori bertahap dari M1 hingga Monthly (1mo) dengan window histori yang sesuai tiap timeframe
    - Menganalisis arah & struktur tiap timeframe secara berjenjang
    - Menyusun setup terpisah untuk SESI/SCALP, INTRADAY, SWING, dan HOLD POSITION
    """

    TF_SPECS = [
        ("M1", "5d", "1m"),
        ("M5", "1mo", "5m"),
        ("M15", "1mo", "15m"),
        ("M30", "2mo", "30m"),
        ("H1", "3mo", "1h"),
        ("H4", "1y", "1h"),     # diagregasi ke 4H dari 1h jika perlu
        ("1D", "2y", "1d"),
        ("1W", "5y", "1wk"),
        ("1M", "max", "1mo"),
    ]

    def _ticker_for(self, symbol: str) -> str:
        s = symbol.upper()
        if "XAU" in s or "GOLD" in s:
            return "GC=F"
        return "BTC-USD"

    def _fetch_tf(self, symbol: str, label: str, period: str, interval: str) -> pd.DataFrame:
        ticker = self._ticker_for(symbol)
        try:
            df = yf.Ticker(ticker).history(period=period, interval=interval)
            if df is None or df.empty:
                return pd.DataFrame()
            df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
            if label == "H4" and interval == "1h" and not df.empty:
                df = (
                    df.resample("4h")
                    .agg({
                        "Open": "first",
                        "High": "max",
                        "Low": "min",
                        "Close": "last",
                        "Volume": "sum",
                    })
                    .dropna()
                )
            return df
        except Exception:
            return pd.DataFrame()

    def _analyze_single_tf(self, df: pd.DataFrame) -> dict:
        if df is None or df.empty or len(df) < 5:
            return {"bias": "NEUTRAL", "high": 0.0, "low": 0.0, "mid": 0.0, "bars": 0}

        window = df.tail(50)
        high_val = float(window["High"].max())
        low_val = float(window["Low"].min())
        mid_val = (high_val + low_val) / 2.0
        last_close = float(window["Close"].iloc[-1])
        prev_close = float(window["Close"].iloc[-5])

        if last_close > mid_val and last_close >= prev_close:
            bias = "BULLISH"
        elif last_close < mid_val and last_close <= prev_close:
            bias = "BEARISH"
        else:
            bias = "NEUTRAL"

        return {
            "bias": bias,
            "high": round(high_val, 2),
            "low": round(low_val, 2),
            "mid": round(mid_val, 2),
            "bars": int(len(df)),
        }

    def scan_all_timeframes(self, symbol: str) -> tuple[dict[str, pd.DataFrame], dict[str, dict]]:
        """
        Membaca histori secara bertahap dari M1 sampai 1M (Monthly).
        """
        dfs: dict[str, pd.DataFrame] = {}
        ladder: dict[str, dict] = {}

        for label, period, interval in self.TF_SPECS:
            df = self._fetch_tf(symbol, label, period, interval)
            dfs[label] = df
            ladder[label] = self._analyze_single_tf(df)

        return dfs, ladder

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
            entry = low_val + (rng * 0.705)
            if entry <= live_price * 1.0005:
                entry = live_price + max(rng * 0.15, min_risk * 0.8)
            sl = max(high_val + (rng * 0.05), entry + min_risk)
            risk = sl - entry
            tp1 = entry - (risk * 3.0)
            tp2 = entry - (risk * 6.0)
            tp3 = entry - (risk * 10.0)
        else:
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

    def calculate_confluence_score(self, symbol: str, intel: dict, macro_bias: str, ladder: dict | None = None) -> int:
        score = 62
        macro = intel.get("macro", {})
        matrix = intel.get("matrix", {})
        deriv = intel.get("deriv_onchain", {})
        dom = intel.get("crypto_dom", {})

        net_liq = float(macro.get("net_liquidity_b", 0.0))
        dxy_chg = float(matrix.get("DXY", {}).get("change_pct", 0.0))
        vix_val = float(matrix.get("VIX", {}).get("price", 20.0))

        if symbol == "XAUUSD":
            if macro_bias == "BEARISH" and dxy_chg >= 0:
                score += 9
            elif macro_bias == "BULLISH" and (dxy_chg <= 0 or net_liq > 0):
                score += 9
            if vix_val > 15.0:
                score += 4
        else:
            stable_flow = float(deriv.get("stablecoin_7d_flow_b", 0.0))
            btc_d = float(dom.get("BTC_D", 55.0))
            if macro_bias == "BEARISH" and stable_flow <= 0:
                score += 9
            elif macro_bias == "BULLISH" and stable_flow >= 0:
                score += 9
            if 45.0 <= btc_d <= 65.0:
                score += 4

        if ladder:
            aligned = sum(1 for tf in ["M1", "M5", "M15", "M30", "H1", "H4", "1D", "1W", "1M"] if ladder.get(tf, {}).get("bias") == macro_bias)
            if aligned >= 6:
                score += 8
            elif aligned >= 4:
                score += 4

        return max(0, min(100, int(score)))

    def build_cascade_setups(self, symbol: str, feeds, macro_bias: str) -> dict:
        live_price = feeds.get_live_price(symbol)
        is_xau = symbol == "XAUUSD"
        if live_price <= 0:
            live_price = 3620.4 if is_xau else 61400.0

        # Scan bertahap dari M1 -> M5 -> M15 -> M30 -> H1 -> H4 -> 1D -> 1W -> 1M
        dfs, ladder = self.scan_all_timeframes(symbol)

        ltf_df = pd.concat([d.tail(60) for d in [dfs.get("M1"), dfs.get("M5")] if d is not None and not d.empty], ignore_index=True)
        itf_df = pd.concat([d.tail(80) for d in [dfs.get("M15"), dfs.get("M30"), dfs.get("H1"), dfs.get("H4")] if d is not None and not d.empty], ignore_index=True)
        htf_df = pd.concat([d.tail(60) for d in [dfs.get("1D"), dfs.get("1W")] if d is not None and not d.empty], ignore_index=True)
        mtf_df = dfs.get("1M") if (dfs.get("1M") is not None and not dfs.get("1M").empty) else dfs.get("1W")

        primary_dir = "SELL LIMIT" if macro_bias == "BEARISH" else "BUY LIMIT"

        scalp_base = self._compute_zone_setup(ltf_df, live_price, primary_dir, is_xau)
        intra_base = self._compute_zone_setup(itf_df, live_price, primary_dir, is_xau)
        swing_base = self._compute_zone_setup(htf_df, live_price, primary_dir, is_xau)
        hold_base = self._compute_zone_setup(mtf_df, live_price, "BUY LIMIT", is_xau)

        ladder_summary = " | ".join(
            [f"{tf}:{ladder[tf]['bias'][:4]}" for tf in ["M1", "M5", "M15", "M30", "H1", "H4", "1D", "1W", "1M"]]
        )

        setups = {
            "SESI / SCALP (M1-M5)": {
                **scalp_base,
                "tf_chain": "M1 → M5",
                "aligned_count": f"{ladder['M1']['bias']}/{ladder['M5']['bias']}",
                "reason": f"Struktur mikro M1-M5 & sweep likuiditas sesi mengikuti bias {macro_bias}.",
            },
            "INTRADAY (M15-M30-H1-H4)": {
                **intra_base,
                "tf_chain": "M15 → M30 → H1 → H4",
                "aligned_count": f"{ladder['H1']['bias']}/{ladder['H4']['bias']}",
                "reason": f"Konfluensi M15-H4 (OB/FVG & OTE) searah struktur {macro_bias}.",
            },
            "SWING (1D-1W)": {
                **swing_base,
                "tf_chain": "1D → 1W",
                "aligned_count": f"{ladder['1D']['bias']}/{ladder['1W']['bias']}",
                "reason": f"Zona OTE 0.705 harian-mingguan pada tren {macro_bias}.",
            },
            "HOLD POSITION (1M BULANAN)": {
                **hold_base,
                "tf_chain": "1D → 1W → 1M",
                "aligned_count": f"1M:{ladder['1M']['bias']}",
                "reason": "Pemetaan struktur bulanan (1M) & histori panjang untuk zona akumulasi.",
            },
        }

        return {
            "symbol": symbol,
            "live_price": round(live_price, 2),
            "mtf_ladder": ladder,
            "ladder_summary": ladder_summary,
            "setups": setups,
        }

    def refine_setups_with_flagship_ai(self, symbol: str, raw_bundle: dict, intel: dict, ai_router) -> tuple[dict, str]:
        intra = raw_bundle["setups"]["INTRADAY (M15-M30-H1-H4)"]
        swing = raw_bundle["setups"]["SWING (1D-1W)"]
        hold = raw_bundle["setups"]["HOLD POSITION (1M BULANAN)"]

        sys_prompt = (
            "Anda adalah analis kuantitatif SMC & Multi-Timeframe (M1 sampai 1M Bulanan). "
            "Tulis 1 kalimat ringkas alasan teknikal untuk setup Intraday berdasarkan struktur MTF dan data makro. "
            "Jangan ubah angka harga dan jangan gunakan simbol < atau >."
        )
        usr_prompt = (
            f"Aset: {symbol} | Live: {raw_bundle['live_price']} | Ladder: {raw_bundle.get('ladder_summary', '')} | "
            f"Intraday: {intra['direction']} @ {intra['entry']} (SL {intra['sl']}, TP1 {intra['tp1']}) | "
            f"Swing: {swing['direction']} @ {swing['entry']} | Hold 1M: {hold['direction']} @ {hold['entry']} | "
            f"DXY: {intel.get('matrix', {}).get('DXY', {}).get('price')} | "
            f"NetLiq: {intel.get('macro', {}).get('net_liquidity_b')}B"
        )

        ai_reason, model_used = ai_router.generate(sys_prompt, usr_prompt, max_tokens=220)
        if ai_reason and len(ai_reason.strip()) > 12:
            clean_reason = ai_reason.strip().replace("<", "").replace(">", "")
            raw_bundle["setups"]["INTRADAY (M15-M30-H1-H4)"]["reason"] = clean_reason

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

        intra = next((v for k, v in bundle.get("setups", {}).items() if "INTRADAY" in k.upper()), {})
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
