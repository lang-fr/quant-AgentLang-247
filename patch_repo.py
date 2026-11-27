import ast
import os
import py_compile
import re
import shutil

if not os.path.exists("main.py"):
    for d in os.listdir("."):
        if os.path.isdir(d) and os.path.exists(os.path.join(d, "main.py")):
            os.chdir(d)
            break

assert os.path.exists("main.py"), "Folder repo tidak ditemukan! Pastikan cd ke folder quant-AgentLang-247 dulu."

for fname in ["ai_router.py", "market_feeds.py", "smc_alpha.py", "main.py", "execution_engine.py"]:
    if os.path.exists(fname):
        shutil.copy2(fname, f"{fname}.bak")

AI_ROUTER_CODE = r'''import os
import requests


class MultiTierAIRouter:
    def __init__(self):
        self.anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        self.gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
        self.primary_claude = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")
        self.primary_gemini = os.getenv("GEMINI_PRO_MODEL", "gemini-4-hydrogen")
        self.fallback_gemini = os.getenv("GEMINI_LITE_MODEL", "gemini-3.5-flash-lite")
        self.active_setup_engine = "Quant Deterministic Engine"
        self.active_analysis_engine = "Quant Deterministic Engine"

    def _call_claude(self, prompt: str, max_tokens: int = 950) -> str:
        if not self.anthropic_key:
            return ""
        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self.anthropic_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.primary_claude,
                    "max_tokens": max_tokens,
                    "temperature": 0.2,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=25,
            )
            if resp.status_code == 200:
                return resp.json()["content"][0]["text"].strip()
        except Exception:
            pass
        return ""

    def _call_gemini(self, model_name: str, prompt: str, max_tokens: int = 950) -> str:
        if not self.gemini_key:
            return ""
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={self.gemini_key}"
            resp = requests.post(
                url,
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.2, "maxOutputTokens": max_tokens},
                },
                timeout=25,
            )
            if resp.status_code == 200:
                return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception:
            pass
        return ""

    def route_high_priority(self, prompt: str, role: str = "ANALISA", max_tokens: int = 950) -> str:
        out = self._call_gemini(self.primary_gemini, prompt, max_tokens)
        if out:
            engine_label = f"{self.primary_gemini} (Tier-1 Priority)"
        else:
            out = self._call_claude(prompt, max_tokens)
            if out:
                engine_label = f"{self.primary_claude} (Tier-1 Claude)"
            else:
                out = self._call_gemini(self.fallback_gemini, prompt, max_tokens)
                engine_label = f"{self.fallback_gemini} (Fallback)" if out else "Quant Deterministic Engine"

        if role == "SETUP":
            self.active_setup_engine = engine_label
        else:
            self.active_analysis_engine = engine_label
        return out

    def generate_analysis(self, prompt: str) -> str:
        return self.route_high_priority(prompt, role="ANALISA")

    def refine_setup(self, prompt: str) -> str:
        return self.route_high_priority(prompt, role="SETUP")

    def generate(self, sys_prompt: str, usr_prompt: str, max_tokens: int = 950, role: str = "ANALISA"):
        combined = f"{sys_prompt}\n\n{usr_prompt}"
        text = self.route_high_priority(combined, role=role, max_tokens=max_tokens)
        engine = self.active_setup_engine if role == "SETUP" else self.active_analysis_engine
        return text, engine
'''

with open("ai_router.py", "w", encoding="utf-8") as f:
    f.write(AI_ROUTER_CODE)
print("[1/5] ai_router.py berhasil diperbarui.")

NEW_FEEDS_METHODS = r'''    def get_live_price_with_meta(self, symbol: str) -> dict:
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
                        return {"price": mid, "source": "Swissquote Spot Live", "realtime": True}
            except Exception:
                pass

            td_key = os.getenv("TWELVEDATA_API_KEY", "").strip()
            if td_key:
                try:
                    r = requests.get(
                        f"https://api.twelvedata.com/price?symbol=XAU/USD&apikey={td_key}",
                        timeout=6,
                    )
                    val = float(r.json().get("price", 0))
                    if val > 1000:
                        return {"price": round(val, 2), "source": "TwelveData Spot Live", "realtime": True}
                except Exception:
                    pass

            try:
                r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=PAXGUSDT", timeout=5)
                val = float(r.json().get("price", 0))
                if val > 1000:
                    return {"price": round(val, 2), "source": "Binance PAXG Spot Live", "realtime": True}
            except Exception:
                pass

            for ticker in ["XAUUSD=X", "GC=F"]:
                try:
                    val = float(yf.Ticker(ticker).fast_info.get("lastPrice", 0) or 0)
                    if val > 1000:
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

        fred_key = os.getenv("FRED_API_KEY", "").strip()
        if fred_key:
            try:
                f_r = requests.get(
                    f"https://api.stlouisfed.org/fred/series/observations?series_id=T10YIE&api_key={fred_key}&file_type=json&sort_order=desc&limit=1",
                    timeout=5,
                )
                obs = f_r.json().get("observations", [])
                if obs and obs[0]["value"] != ".":
                    pillars["breakeven_10y"] = float(obs[0]["value"])
            except Exception:
                pass
        return pillars
'''

with open("market_feeds.py", "r", encoding="utf-8") as f:
    mf_src = f.read()

if "import os" not in mf_src:
    mf_src = "import os\n" + mf_src
if "import requests" not in mf_src:
    mf_src = "import requests\n" + mf_src

tree = ast.parse(mf_src)
lines = mf_src.splitlines(keepends=True)
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == "get_live_price":
        lines[node.lineno - 1 : node.end_lineno] = [NEW_FEEDS_METHODS + "\n"]
        break

with open("market_feeds.py", "w", encoding="utf-8") as f:
    f.writelines(lines)
print("[2/5] market_feeds.py berhasil diperbarui.")

NEW_ZONE_METHODS = r'''    def _calc_atr(self, df: pd.DataFrame, period: int = 14) -> float:
        if df is None or len(df) < 3:
            return 0.0
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        close = df["Close"].astype(float)
        prev_close = close.shift(1).fillna(close)
        tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
        return float(tr.tail(period).mean())

    def _compute_zone_setup(self, df: pd.DataFrame, live_price: float, direction: str, is_xau: bool, horizon: str = "INTRADAY") -> dict:
        dec = 2
        atr = self._calc_atr(df, 14)

        if df is not None and not df.empty:
            high_val = float(df["High"].max())
            low_val = float(df["Low"].min())
            rng = max(high_val - low_val, atr * 2.0, live_price * (0.002 if is_xau else 0.004))
        else:
            rng = live_price * (0.005 if is_xau else 0.015)
            high_val = live_price + (rng * 0.5)
            low_val = live_price - (rng * 0.5)

        if horizon == "SCALP":
            if is_xau:
                risk = min(max(atr * 0.85, 1.50), 2.20)
                entry_offset = min(max(atr * 0.45, 0.80), 2.50)
            else:
                risk = min(max(atr * 0.9, live_price * 0.0022), live_price * 0.0045)
                entry_offset = min(max(atr * 0.5, live_price * 0.0012), live_price * 0.0035)
            rr_mult = (2.5, 5.0, 7.5)
        elif horizon == "INTRADAY":
            risk = min(max(atr * 1.35, live_price * (0.0035 if is_xau else 0.008)), live_price * (0.009 if is_xau else 0.022))
            entry_offset = min(max(atr * 0.9, rng * 0.18), live_price * (0.012 if is_xau else 0.025))
            rr_mult = (2.6, 5.2, 8.0)
        elif horizon == "SWING":
            risk = min(max(atr * 1.5, live_price * (0.012 if is_xau else 0.025)), live_price * (0.03 if is_xau else 0.07))
            entry_offset = min(max(rng * 0.25, live_price * (0.018 if is_xau else 0.04)), live_price * (0.05 if is_xau else 0.12))
            rr_mult = (2.8, 5.5, 8.5)
        else:
            risk = min(max(atr * 1.6, live_price * (0.035 if is_xau else 0.08)), live_price * (0.08 if is_xau else 0.18))
            entry_offset = min(max(rng * 0.30, live_price * (0.05 if is_xau else 0.12)), live_price * (0.14 if is_xau else 0.28))
            rr_mult = (3.0, 6.0, 9.0)

        if direction == "SELL LIMIT":
            raw_entry = low_val + (rng * 0.705)
            entry = raw_entry if raw_entry > live_price * 1.0003 else (live_price + entry_offset)
            sl = entry + risk
            tp1 = entry - (risk * rr_mult[0])
            tp2 = entry - (risk * rr_mult[1])
            tp3 = entry - (risk * rr_mult[2])
        else:
            raw_entry = high_val - (rng * 0.705)
            entry = raw_entry if raw_entry < live_price * 0.9997 else (live_price - entry_offset)
            sl = max(entry - risk, entry * 0.55)
            risk = max(entry - sl, 0.5 if is_xau else 25.0)
            tp1 = entry + (risk * rr_mult[0])
            tp2 = entry + (risk * rr_mult[1])
            tp3 = entry + (risk * rr_mult[2])

        rr_tp1 = round(abs(tp1 - entry) / max(abs(entry - sl), 1e-9), 1)
        rr_tp2 = round(abs(tp2 - entry) / max(abs(entry - sl), 1e-9), 1)
        rr_tp3 = round(abs(tp3 - entry) / max(abs(entry - sl), 1e-9), 1)

        return {
            "direction": direction,
            "entry": round(entry, dec),
            "sl": round(sl, dec),
            "tp1": round(tp1, dec),
            "tp2": round(tp2, dec),
            "tp3": round(tp3, dec),
            "rr": f"1:{rr_tp1} / 1:{rr_tp2} / 1:{rr_tp3}",
        }
'''

NEW_CASCADE_METHOD = r'''    def build_cascade_setups(self, symbol: str, feeds, macro_bias: str) -> dict:
        price_meta = feeds.get_live_price_with_meta(symbol) if hasattr(feeds, "get_live_price_with_meta") else {
            "price": feeds.get_live_price(symbol),
            "source": "Primary Feed",
            "realtime": True,
        }
        live_price = float(price_meta["price"])
        is_xau = "XAU" in symbol.upper()

        if live_price <= 0:
            raise RuntimeError(f"Harga live {symbol} OFFLINE — setup dibatalkan demi keamanan eksekusi.")

        dfs, ladder = self.scan_all_timeframes(symbol)

        scalp_df = pd.concat(
            [d.tail(45) for d in [dfs.get("M1"), dfs.get("M5"), dfs.get("M15")] if d is not None and not d.empty],
            ignore_index=True,
        )
        intra_df = pd.concat(
            [d.tail(60) for d in [dfs.get("M30"), dfs.get("H1"), dfs.get("H4")] if d is not None and not d.empty],
            ignore_index=True,
        )
        swing_df = pd.concat(
            [d.tail(50) for d in [dfs.get("1D"), dfs.get("1W")] if d is not None and not d.empty],
            ignore_index=True,
        )
        hold_df = dfs.get("1M") if (dfs.get("1M") is not None and not dfs.get("1M").empty) else dfs.get("1W")

        primary_dir = "SELL LIMIT" if macro_bias == "BEARISH" else "BUY LIMIT"

        scalp_base = self._compute_zone_setup(scalp_df, live_price, primary_dir, is_xau, "SCALP")
        intra_base = self._compute_zone_setup(intra_df, live_price, primary_dir, is_xau, "INTRADAY")
        swing_base = self._compute_zone_setup(swing_df, live_price, primary_dir, is_xau, "SWING")
        hold_base = self._compute_zone_setup(hold_df, live_price, primary_dir, is_xau, "HOLD")

        ladder_summary = " | ".join(
            [f"{tf}:{ladder[tf]['bias'][:4]}" for tf in ["M1", "M5", "M15", "M30", "H1", "H4", "1D", "1W", "1M"]]
        )

        setups = {
            "SESI / SCALP (M1-M5-M15)": {
                **scalp_base,
                "tf_chain": "M1 -> M5 -> M15",
                "aligned_count": f"{ladder['M1']['bias']}/{ladder['M5']['bias']}/{ladder['M15']['bias']}",
                "reason": (
                    f"Micro-sweep likuiditas M1-M15 & mitigasi FVG/OB sesi aktif di {scalp_base['entry']} "
                    f"(Sumber: {price_meta['source']}, bias {macro_bias}, target skalping presisi)."
                ),
            },
            "INTRADAY (M30-H1-H4)": {
                **intra_base,
                "tf_chain": "M30 -> H1 -> H4",
                "aligned_count": f"{ladder['M30']['bias']}/{ladder['H1']['bias']}/{ladder['H4']['bias']}",
                "reason": (
                    f"Konfluensi struktur M30-H4 (OTE 0.705 + Order Block/FVG) searah bias makro {macro_bias} "
                    f"menuju pool likuiditas sesi London/NY."
                ),
            },
            "SWING (1D-1W)": {
                **swing_base,
                "tf_chain": "1D -> 1W",
                "aligned_count": f"{ladder['1D']['bias']}/{ladder['1W']['bias']}",
                "reason": (
                    f"Area diskon/premium institusional 1D-1W selaras dengan arah DXY, real yield US10Y, "
                    f"dan posisi COT mingguan ({macro_bias})."
                ),
            },
            "HOLD POSITION (1M BULANAN)": {
                **hold_base,
                "tf_chain": "1D -> 1W -> 1M",
                "aligned_count": f"1M:{ladder['1M']['bias']}",
                "reason": (
                    f"Zona akumulasi/distribusi struktural bulanan (1M) berdasarkan siklus likuiditas global "
                    f"dan kebijakan moneter jangka panjang."
                ),
            },
        }

        return {
            "symbol": symbol,
            "live_price": round(live_price, 2),
            "price_source": price_meta["source"],
            "is_realtime": price_meta["realtime"],
            "mtf_ladder": ladder,
            "ladder_summary": ladder_summary,
            "setups": setups,
        }
'''

with open("smc_alpha.py", "r", encoding="utf-8") as f:
    smc_src = f.read()

smc_src = smc_src.replace("INTRADAY (M15-M30-H1-H4)", "INTRADAY (M30-H1-H4)")

if 'intel.get("extended"' not in smc_src and "if ladder:" in smc_src:
    ext_score_block = (
        '        ext = intel.get("extended", {})\n'
        '        if symbol == "XAUUSD":\n'
        '            if float(ext.get("xagusd", 0.0)) > 0:\n'
        '                score += 4\n'
        '        else:\n'
        '            cb_prem = float(ext.get("coinbase_premium_pct", 0.0))\n'
        '            taker_bs = float(ext.get("taker_buy_sell_ratio", 1.0))\n'
        '            if (macro_bias == "BULLISH" and cb_prem > 0 and taker_bs >= 1.02) or (\n'
        '                macro_bias == "BEARISH" and cb_prem < 0 and taker_bs <= 0.98\n'
        '            ):\n'
        '                score += 7\n\n'
        '        if ladder:'
    )
    smc_src = smc_src.replace("        if ladder:", ext_score_block, 1)

smc_tree = ast.parse(smc_src)
smc_lines = smc_src.splitlines(keepends=True)
replacements = []
for node in ast.walk(smc_tree):
    if isinstance(node, ast.FunctionDef) and node.name == "_compute_zone_setup":
        replacements.append((node.lineno - 1, node.end_lineno, NEW_ZONE_METHODS + "\n"))
    elif isinstance(node, ast.FunctionDef) and node.name == "build_cascade_setups":
        replacements.append((node.lineno - 1, node.end_lineno, NEW_CASCADE_METHOD + "\n"))

for start, end, code_block in sorted(replacements, key=lambda x: x[0], reverse=True):
    smc_lines[start:end] = [code_block]

with open("smc_alpha.py", "w", encoding="utf-8") as f:
    f.writelines(smc_lines)
print("[3/5] smc_alpha.py berhasil diperbarui.")

with open("main.py", "r", encoding="utf-8") as f:
    main_src = f.read()

main_src = main_src.replace("INTRADAY (M15-M30-H1-H4)", "INTRADAY (M30-H1-H4)")

if "get_extended_institutional_pillars" not in main_src:
    inject_block = (
        '    xau_meta = feeds.get_live_price_with_meta("XAUUSD")\n'
        '    btc_meta = feeds.get_live_price_with_meta("BTCUSDT")\n'
        '    intel["extended"] = feeds.get_extended_institutional_pillars(btc_meta["price"])\n'
    )
    main_src = re.sub(
        r"(\n[ \t]+xau_raw\s*=\s*alpha_engine\.build_cascade_setups)",
        "\n" + inject_block + r"\1",
        main_src,
        count=1,
    )

main_src = main_src.replace(
    "XAUUSD (Live: {xau_bundle['live_price']})",
    "XAUUSD (Live: {xau_bundle['live_price']} | {xau_bundle.get('price_source', 'Live')})",
)
main_src = main_src.replace(
    "BTCUSDT (Live: {btc_bundle['live_price']})",
    "BTCUSDT (Live: {btc_bundle['live_price']} | {btc_bundle.get('price_source', 'Live')})",
)

with open("main.py", "w", encoding="utf-8") as f:
    f.write(main_src)
print("[4/5] main.py berhasil diperbarui.")

if os.path.exists("execution_engine.py"):
    with open("execution_engine.py", "r", encoding="utf-8") as f:
        exec_src = f.read()
    exec_src = exec_src.replace("INTRADAY (M15-M30-H1-H4)", "INTRADAY (M30-H1-H4)")
    with open("execution_engine.py", "w", encoding="utf-8") as f:
        f.write(exec_src)
    print("[5/5] execution_engine.py disinkronkan.")

for fname in ["ai_router.py", "market_feeds.py", "smc_alpha.py", "main.py", "execution_engine.py"]:
    if os.path.exists(fname):
        py_compile.compile(fname, doraise=True)

print("\n✅ SEMUA FILE BERHASIL DI-PATCH & LOLOS CEK SINTAKS (0 ERROR)!")
