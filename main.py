import os
import json
import sqlite3
from datetime import datetime, timezone, timedelta
import requests

from market_feeds import (
    ambil_data_makro,
    ambil_metrik_derivatif_dan_sentimen,
    cek_kalender_ekonomi_high_impact,
)
from smc_alpha import analisa_smc_matematis, cek_anti_korelasi_ganda
from execution_engine import ambil_info_akun_dan_spread, proses_eksekusi_sinyal

try:
    from execution_engine import kelola_auto_breakeven_binance
except ImportError:
    def kelola_auto_breakeven_binance():
        return []

# ==============================================================================
# KONFIGURASI ENVIRONMENT & KONSTANTA RISIKO
# ==============================================================================
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
BLOCKWORKS_API_KEY = os.getenv("BLOCKWORKS_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

DB_PATH = "quant_cloud_audit.db"
MIN_CONFLUENCE_SCORE = 75
MAX_DAILY_DRAWDOWN_PCT = 5.0
MAX_ENTRIES_PER_SESSION = 3
MAX_ENTRIES_PER_DAY = 9

WIB = timezone(timedelta(hours=7))
EST = timezone(timedelta(hours=-5))


# ==============================================================================
# 1. DATABASE AUDIT SQLITE, STATE TELEGRAM & RISK GATEKEEPER
# ==============================================================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS audit_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp_wib TEXT,
            date_wib TEXT,
            session_name TEXT,
            asset TEXT,
            direction TEXT,
            total_score INTEGER,
            score_macro INTEGER,
            score_sentiment INTEGER,
            score_ohlc_ote INTEGER,
            score_smc_kz INTEGER,
            action_status TEXT,
            entry_price REAL,
            sl_price REAL,
            tp1 REAL,
            tp2 REAL,
            tp3 REAL,
            outcome_status TEXT DEFAULT 'PENDING',
            notes TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_equity_log (
            date_wib TEXT PRIMARY KEY,
            start_equity REAL,
            last_equity REAL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    conn.close()


def get_bot_state(key: str, default: str = "0") -> str:
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT value FROM bot_state WHERE key = ?", (key,))
        row = cur.fetchone()
        conn.close()
        return row[0] if row else default
    except Exception:
        return default


def set_bot_state(key: str, value: str):
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO bot_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def hitung_dynamic_half_kelly_risk(session_name: str, total_score: int, regime: str = "TRENDING") -> float:
    # Jika pasar RANGING (ADX < 20), turunkan risiko ke 0.5% sesuai SOP institusional
    if regime == "RANGING":
        return 0.005

    base_risk = 0.015 if total_score >= 85 else 0.010
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("""
            SELECT outcome_status FROM audit_signals
            WHERE session_name = ? AND outcome_status IN ('HIT_TP1', 'HIT_TP2', 'HIT_TP3', 'HIT_SL')
            ORDER BY id DESC LIMIT 30
        """, (session_name,))
        rows = [r[0] for r in cur.fetchall()]
        conn.close()

        if len(rows) >= 8:
            wins = sum(1 for r in rows if r.startswith("HIT_TP"))
            win_rate = wins / len(rows)
            rr_ratio = 2.0
            kelly_full = win_rate - ((1.0 - win_rate) / rr_ratio)
            half_kelly = max(0.005, min(0.015, kelly_full * 0.5))
            return round(half_kelly, 4)
    except Exception:
        pass
    return base_risk


def deteksi_sesi_dan_killzone() -> dict:
    now_wib = datetime.now(WIB)
    now_est = datetime.now(EST)
    jam_est = now_est.hour + (now_est.minute / 60.0)

    if 2.0 <= jam_est <= 5.0:
        sesi, in_kz, kz_label = "LONDON", True, "LONDON_OPEN_KILLZONE"
    elif 7.0 <= jam_est <= 11.0:
        sesi, in_kz, kz_label = "NEW_YORK", True, "NEW_YORK_AM_KILLZONE"
    elif 5.0 < jam_est < 7.0:
        sesi, in_kz, kz_label = "LONDON", False, "LONDON_PRE_NY"
    elif 11.0 < jam_est <= 16.0:
        sesi, in_kz, kz_label = "NEW_YORK", False, "NEW_YORK_PM"
    else:
        sesi, in_kz, kz_label = "ASIA", False, "ASIA_CONSOLIDATION"

    return {
        "wib_str": now_wib.strftime("%Y-%m-%d %H:%M WIB"),
        "date_wib": now_wib.strftime("%Y-%m-%d"),
        "est_str": now_est.strftime("%H:%M EST"),
        "session": sesi,
        "in_killzone": in_kz,
        "killzone_label": kz_label,
    }


def cek_batas_risiko_harian(current_equity: float, date_wib: str, session_name: str) -> dict:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("SELECT start_equity FROM daily_equity_log WHERE date_wib = ?", (date_wib,))
    row = cur.fetchone()
    if row is None:
        start_eq = current_equity if current_equity > 0 else 1000.0
        cur.execute(
            "INSERT INTO daily_equity_log (date_wib, start_equity, last_equity) VALUES (?, ?, ?)",
            (date_wib, start_eq, start_eq),
        )
    else:
        start_eq = float(row[0])
        cur.execute(
            "UPDATE daily_equity_log SET last_equity = ? WHERE date_wib = ?",
            (current_equity, date_wib),
        )

    dd_pct = ((start_eq - current_equity) / start_eq * 100.0) if start_eq > 0 else 0.0

    cur.execute(
        "SELECT COUNT(*) FROM audit_signals WHERE date_wib = ? AND action_status IN ('BINANCE_DEMO_AUTO', 'MANUAL_CFD_SIGNAL')",
        (date_wib,),
    )
    total_hari_ini = int(cur.fetchone()[0])

    cur.execute(
        "SELECT COUNT(*) FROM audit_signals WHERE date_wib = ? AND session_name = ? AND action_status IN ('BINANCE_DEMO_AUTO', 'MANUAL_CFD_SIGNAL')",
        (date_wib, session_name),
    )
    total_sesi_ini = int(cur.fetchone()[0])

    conn.commit()
    conn.close()

    if dd_pct >= MAX_DAILY_DRAWDOWN_PCT:
        return {"allowed": False, "reason": f"KILL SWITCH AKTIF: Daily drawdown {dd_pct:.2f}% >= {MAX_DAILY_DRAWDOWN_PCT}%", "dd_pct": round(dd_pct, 2)}
    if total_hari_ini >= MAX_ENTRIES_PER_DAY:
        return {"allowed": False, "reason": f"Batas harian tercapai ({total_hari_ini}/{MAX_ENTRIES_PER_DAY} entry)", "dd_pct": round(dd_pct, 2)}
    if total_sesi_ini >= MAX_ENTRIES_PER_SESSION:
        return {"allowed": False, "reason": f"Batas sesi {session_name} tercapai ({total_sesi_ini}/{MAX_ENTRIES_PER_SESSION} entry)", "dd_pct": round(dd_pct, 2)}

    return {
        "allowed": True,
        "dd_pct": round(dd_pct, 2),
        "entries_today": total_hari_ini,
        "entries_session": total_sesi_ini,
    }


def simpan_log_audit(record: dict):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO audit_signals (
            timestamp_wib, date_wib, session_name, asset, direction,
            total_score, score_macro, score_sentiment, score_ohlc_ote, score_smc_kz,
            action_status, entry_price, sl_price, tp1, tp2, tp3, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        record["timestamp_wib"], record["date_wib"], record["session_name"], record["asset"], record["direction"],
        record["total_score"], record["score_macro"], record["score_sentiment"], record["score_ohlc_ote"], record["score_smc_kz"],
        record["action_status"], record["entry_price"], record["sl_price"], record["tp1"], record["tp2"], record["tp3"], record["notes"]
    ))
    conn.commit()
    conn.close()


# ==============================================================================
# 2. MULTI-LLM ENGINE: CLAUDE (ANALISA) & GEMINI 4 HYDROGEN (CHAT) + GROQ
# ==============================================================================
def _ekstrak_json_bersih(teks: str) -> dict:
    teks = teks.strip()
    if "```" in teks:
        bagian = teks.split("```")
        for b in bagian:
            b_clean = b.replace("json", "", 1).strip()
            if b_clean.startswith("{") and b_clean.endswith("}"):
                return json.loads(b_clean)
    awal = teks.find("{")
    akhir = teks.rfind("}")
    if awal != -1 and akhir != -1:
        return json.loads(teks[awal:akhir + 1])
    return json.loads(teks)


def _panggil_claude(prompt: str, max_tokens: int = 2048) -> tuple[str, str]:
    if not ANTHROPIC_API_KEY:
        return "", ""
    for model_claude in ("claude-sonnet-4-5", "claude-3-5-sonnet-latest"):
        try:
            url = "https://api.anthropic.com/v1/messages"
            headers = {
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            payload = {
                "model": model_claude,
                "max_tokens": max_tokens,
                "temperature": 0.15,
                "messages": [{"role": "user", "content": prompt}],
            }
            resp = requests.post(url, headers=headers, json=payload, timeout=30)
            if resp.status_code == 200:
                txt = resp.json()["content"][0]["text"].strip()
                if txt:
                    return txt, f"Anthropic {model_claude}"
        except Exception:
            continue
    return "", ""


def _panggil_gemini_hydrogen(prompt: str, mode_json: bool = False, mode_biasa: bool = True) -> tuple[str, str]:
    if not GEMINI_API_KEY:
        return "", ""
    try:
        url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-4-hydrogen:generateContent"
        headers = {
            "x-goog-api-key": GEMINI_API_KEY,
            "Content-Type": "application/json",
        }
        gen_cfg = {
            "temperature": 0.2 if mode_biasa else 0.15,
            "maxOutputTokens": 4096 if mode_biasa else 16384,
        }
        if not mode_biasa:
            gen_cfg["thinkingConfig"] = {"thinkingLevel": "HIGH"}
        if mode_json:
            gen_cfg["responseMimeType"] = "application/json"

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": gen_cfg,
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=35)
        if resp.status_code == 200:
            parts = resp.json()["candidates"][0]["content"]["parts"]
            teks_hasil = "".join(
                p.get("text", "") for p in parts if not p.get("thought", False)
            ).strip()
            if teks_hasil:
                label = "Gemini 4 Hydrogen (Chat)" if mode_biasa else "Gemini 4 Hydrogen (Failover Analisa)"
                return teks_hasil, label
    except Exception:
        pass
    return "", ""


def _panggil_groq(prompt: str, mode_json: bool = False) -> tuple[str, str]:
    if not GROQ_API_KEY:
        return "", ""
    try:
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "llama-3.3-70b-versatile",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.15,
        }
        if mode_json:
            payload["response_format"] = {"type": "json_object"}
        resp = requests.post(url, headers=headers, json=payload, timeout=20)
        if resp.status_code == 200:
            txt = resp.json()["choices"][0]["message"]["content"].strip()
            if txt:
                return txt, "Groq Llama-3.3-70B"
    except Exception:
        pass
    return "", ""


def panggil_llm_teks(prompt: str, mode_json: bool = False) -> tuple[str, str]:
    """
    - mode_json=True (ANALISA 3-AGEN): Claude -> Gemini 4 Hydrogen -> Groq
    - mode_json=False (CHATBOT TELEGRAM): Gemini 4 Hydrogen (model biasa) -> Claude -> Groq
    """
    if mode_json:
        for fn in (
            lambda: _panggil_claude(prompt, max_tokens=2048),
            lambda: _panggil_gemini_hydrogen(prompt, mode_json=True, mode_biasa=False),
            lambda: _panggil_groq(prompt, mode_json=True),
        ):
            txt, model_name = fn()
            if txt:
                return txt, model_name
    else:
        for fn in (
            lambda: _panggil_gemini_hydrogen(prompt, mode_json=False, mode_biasa=True),
            lambda: _panggil_claude(prompt, max_tokens=1500),
            lambda: _panggil_groq(prompt, mode_json=False),
        ):
            txt, model_name = fn()
            if txt:
                return txt, model_name

    return "", "Fallback Deterministik"


def ambil_riset_blockworks() -> str:
    if not BLOCKWORKS_API_KEY:
        return "BELUM DITARIK"
    try:
        headers = {"Authorization": f"Bearer {BLOCKWORKS_API_KEY}"}
        r = requests.get("https://api.blockworks.com/v1/research?limit=2", headers=headers, timeout=8)
        if r.status_code == 200:
            items = r.json().get("data", [])
            if items:
                judul = [it.get("title", "") for it in items if it.get("title")]
                return " | ".join(judul)[:220]
    except Exception:
        pass
    return "API Aktif (Tidak ada rilis baru)"


def evaluasi_critical_thinking_3_agen(
    makro: dict,
    derivatif: dict,
    kalender: dict,
    smc_xau: dict,
    smc_xyz: dict,
    smc_btc: dict,
    blockworks_info: str,
) -> dict:
    konteks = {
        "makro_intermarket": makro,
        "sentimen_onchain_cot": derivatif,
        "kalender_ekonomi": kalender,
        "blockworks_intel": blockworks_info,
        "xauusd_smc": smc_xau,
        "xyz_gold_smc": smc_xyz,
        "btcusdt_smc": smc_btc,
    }

    prompt = f"""
Kamu adalah Komite Investasi Kuantitatif Institusional (Alpha Proposer + Devil's Advocate + Risk Judge).
Analisa data live pasar di bawah ini secara kritis, objektif, dan tanpa mengarang angka:

{json.dumps(konteks)}

Balas HANYA dalam JSON valid dengan struktur persis berikut:
{{
  "gold_bias": "BUY" atau "SELL" atau "NEUTRAL",
  "gold_sentiment_score": angka bulat 0 sampai 20,
  "gold_veto": true atau false,
  "gold_reason": "Alasan utama arah Emas (struktur SMC/FVG/OB/Asian Sweep, ADX regime, DXY/US10Y, CFTC COT) + sebutkan sumber API-nya dalam 2 kalimat padat",
  "gold_invalidation": "Syarat harga/makro yang membatalkan setup Emas dalam 1 kalimat",
  "btc_bias": "BUY" atau "SELL" atau "NEUTRAL",
  "btc_sentiment_score": angka bulat 0 sampai 20,
  "btc_veto": true atau false,
  "btc_reason": "Alasan utama arah BTCUSDT (struktur SMC/FVG/Asian Sweep, ADX regime, Binance Orderflow OI/Taker, BTC.D/USDT.D, NASDAQ) + sebutkan sumber API-nya dalam 2 kalimat padat",
  "btc_invalidation": "Syarat harga/makro yang membatalkan setup BTCUSDT dalam 1 kalimat",
  "kritik_devils_advocate": "Kritik terkuat Agen Skeptis terhadap risiko jebakan likuiditas / divergensi makro sesi ini",
  "ringkasan_risk_judge": "Keputusan akhir Risk Judge (eksekusi atau tahan) beserta alasan utamanya"
}}
"""
    txt, model_dipakai = panggil_llm_teks(prompt, mode_json=True)
    if txt:
        try:
            hasil_llm = _ekstrak_json_bersih(txt)
            if "gold_bias" in hasil_llm:
                hasil_llm["model_used"] = model_dipakai
                return hasil_llm
        except Exception:
            pass

    dxy_chg = makro.get("DXY", {}).get("chg_pct", 0.0)
    us10y_last = makro.get("US10Y", {}).get("last", 0.0)
    cot_bias = derivatif.get("cftc_cot", {}).get("gold_cot_bias", "NEUTRAL")
    bias_fallback = "SELL" if dxy_chg > 0 else "BUY"
    return {
        "model_used": "Fallback Deterministik",
        "gold_bias": bias_fallback,
        "gold_sentiment_score": 12,
        "gold_veto": False,
        "gold_reason": f"DXY bergerak {dxy_chg:+.2f}% dengan US10Y di {us10y_last}% (Sumber: Yahoo Finance DX-Y.NYB/^TNX) dan CFTC COT {cot_bias} (Sumber: CFTC Socrata API).",
        "gold_invalidation": "Batal jika DXY berbalik arah menembus pembukaan harian dan candle H1 menutup di luar zona FVG/OB.",
        "btc_bias": bias_fallback,
        "btc_sentiment_score": 12,
        "btc_veto": False,
        "btc_reason": f"Mengikuti korelasi DXY {dxy_chg:+.2f}% serta dominasi BTC.D/USDT.D (Sumber: CoinGecko Global API & Binance Public Orderflow).",
        "btc_invalidation": "Batal jika Taker Buy/Sell Ratio dan Open Interest berbalik berlawanan dari struktur H1.",
        "kritik_devils_advocate": "Waspadai false breakout (liquidity sweep) di batas sesi London-NY sebelum konfirmasi MSS.",
        "ringkasan_risk_judge": f"Konsensus mengikuti arus DXY ({dxy_chg:+.2f}%) sambil menunggu skor konfluensi >= {MIN_CONFLUENCE_SCORE}.",
    }


# ==============================================================================
# 3. MESIN PEMBOBOTAN SKOR KONFLUENSI (0 - 100 POIN) + REGIME FILTER
# ==============================================================================
def hitung_skor_konfluensi(
    aset: str,
    smc_data: dict,
    makro: dict,
    derivatif: dict,
    llm_view: dict,
    kz_info: dict,
    spread_usd: float,
) -> dict:
    is_gold = aset.upper() in ("XAUUSD", "XYZ:GOLD")
    reason_ai = llm_view.get("gold_reason" if is_gold else "btc_reason", "Data belum lengkap")
    inval_ai = llm_view.get("gold_invalidation" if is_gold else "btc_invalidation", "Tembus level SL")

    if "error" in smc_data or "error" in smc_data.get("daily_ohlc", {}):
        err_msg = smc_data.get("error", "Data chart tidak lengkap")
        return {
            "asset": aset,
            "data_source": smc_data.get("data_source", "ERROR"),
            "regime": "UNKNOWN",
            "adx_14": 0.0,
            "direction": "WAIT",
            "total_score": 0,
            "breakdown": {"macro": 0, "sentiment": 0, "ohlc_ote": 0, "smc_kz": 0},
            "sweep_mss": "NONE/NONE",
            "ote_pos": "UNKNOWN",
            "fvg_zone": None,
            "ob_zone": None,
            "valid_setup": False,
            "blocked_reason": err_msg,
            "reason_utama": f"[PERLU DATA LIVE / BELUM DITARIK] {err_msg}",
            "invalidation": inval_ai,
            "entry": 0.0,
            "sl": 0.0,
            "tp1": 0.0,
            "tp2": 0.0,
            "tp3": 0.0,
        }

    ohlc = smc_data["daily_ohlc"]
    regime_info = smc_data.get("market_regime", {"adx_14": 22.0, "atr_14": 0.0, "regime": "TRENDING"})
    ote = smc_data.get("fibonacci_ote_h1", {})
    fvg15 = smc_data.get("fvg_m15", {})
    fvgh1 = smc_data.get("fvg_h1", {})
    obh1 = smc_data.get("order_block_h1", {})
    sweep_mss = smc_data.get("sweep_and_mss", {})

    bias_llm = llm_view.get("gold_bias" if is_gold else "btc_bias", "NEUTRAL").upper()
    skor_sentimen_raw = int(llm_view.get("gold_sentiment_score" if is_gold else "btc_sentiment_score", 10))
    veto_judge = bool(llm_view.get("gold_veto" if is_gold else "btc_veto", False))
    bias_ohlc = ohlc.get("bias_ohlc", "BUY").upper()

    arah = bias_ohlc if bias_llm in ("NEUTRAL", bias_ohlc) else bias_llm

    # --- PILAR 1: MAKRO, INTERMARKET & COT/ORDERFLOW (MAKS 25 POIN) ---
    skor_makro = 0
    dxy_chg = makro.get("DXY", {}).get("chg_pct", 0.0)
    us10y_chg = makro.get("US10Y", {}).get("chg_pct", 0.0)
    vix_last = makro.get("VIX", {}).get("last", 18.0)

    if is_gold:
        silver_chg = makro.get("SILVER", {}).get("chg_pct", 0.0)
        cot_bias = derivatif.get("cftc_cot", {}).get("gold_cot_bias", "NEUTRAL")
        if (arah == "BUY" and dxy_chg < 0) or (arah == "SELL" and dxy_chg > 0):
            skor_makro += 7
        if (arah == "BUY" and us10y_chg < 0) or (arah == "SELL" and us10y_chg > 0):
            skor_makro += 6
        if (arah == "BUY" and silver_chg > 0) or (arah == "SELL" and silver_chg < 0):
            skor_makro += 4
        if (arah == "BUY" and vix_last >= 17.0) or (arah == "SELL" and vix_last < 17.0):
            skor_makro += 4
        if (arah == "BUY" and "BULLISH" in cot_bias) or (arah == "SELL" and "BEARISH" in cot_bias):
            skor_makro += 4
    else:
        nq_chg = makro.get("NASDAQ", {}).get("chg_pct", 0.0)
        sp_chg = makro.get("SP500", {}).get("chg_pct", 0.0)
        of_bias = derivatif.get("btc_orderflow", {}).get("orderflow_bias", "NEUTRAL")
        if (arah == "BUY" and dxy_chg < 0) or (arah == "SELL" and dxy_chg > 0):
            skor_makro += 6
        if (arah == "BUY" and (nq_chg > 0 or sp_chg > 0)) or (arah == "SELL" and (nq_chg < 0 or sp_chg < 0)):
            skor_makro += 6
        if (arah == "BUY" and vix_last < 22.0) or (arah == "SELL" and vix_last >= 22.0):
            skor_makro += 5
        if (arah == "BUY" and "BUY" in of_bias) or (arah == "SELL" and "SELL" in of_bias):
            skor_makro += 8

    # --- PILAR 2: FUNDAMENTAL & KEPUTUSAN RISK JUDGE 3-AGEN (MAKS 20 POIN) ---
    if veto_judge:
        skor_sentimen = 0
    elif bias_llm == arah:
        skor_sentimen = max(0, min(20, skor_sentimen_raw))
    elif bias_llm == "NEUTRAL":
        skor_sentimen = 10
    else:
        skor_sentimen = 3

    # --- PILAR 3: KUANTITATIF OHLC & FIBONACCI OTE (MAKS 25 POIN) ---
    skor_ohlc_ote = 0
    if bias_ohlc == arah:
        skor_ohlc_ote += 10

    pos_ote = ote.get("price_position", "")
    if arah == "BUY" and pos_ote in ("IN_BUY_OTE_DISCOUNT", "DISCOUNT_ZONE"):
        skor_ohlc_ote += 10 if pos_ote == "IN_BUY_OTE_DISCOUNT" else 6
    elif arah == "SELL" and pos_ote in ("IN_SELL_OTE_PREMIUM", "PREMIUM_ZONE"):
        skor_ohlc_ote += 10 if pos_ote == "IN_SELL_OTE_PREMIUM" else 6

    last_px = float(ohlc["last_price"])
    nl_px = float(ohlc["NL_support"])
    nh_px = float(ohlc["NH_resisten"])
    rentang_harian = max(abs(nh_px - nl_px), last_px * 0.005)

    if arah == "BUY" and abs(last_px - nl_px) <= (0.35 * rentang_harian):
        skor_ohlc_ote += 5
    elif arah == "SELL" and abs(nh_px - last_px) <= (0.35 * rentang_harian):
        skor_ohlc_ote += 5

    # --- PILAR 4: SMC (FVG, OB, ASIAN/PDL SWEEP, MSS) & KILL ZONE (MAKS 30 POIN) ---
    skor_smc_kz = 0
    sweep = sweep_mss.get("sweep_signal", "NONE")
    mss = sweep_mss.get("mss_signal", "NONE")
    asian_sw = sweep_mss.get("asian_sweep", "NONE")

    if arah == "BUY":
        fvg_aktif = fvg15.get("bullish_fvg") or fvgh1.get("bullish_fvg")
        ob_aktif = obh1.get("bullish_ob")
        if fvg_aktif:
            skor_smc_kz += 8
        if ob_aktif:
            skor_smc_kz += 6
        if sweep == "BULLISH_LIQUIDITY_SWEEP" or mss == "BULLISH_MSS" or asian_sw == "BULLISH_ASIAN_SWEEP":
            skor_smc_kz += 6
    else:
        fvg_aktif = fvg15.get("bearish_fvg") or fvgh1.get("bearish_fvg")
        ob_aktif = obh1.get("bearish_ob")
        if fvg_aktif:
            skor_smc_kz += 8
        if ob_aktif:
            skor_smc_kz += 6
        if sweep == "BEARISH_LIQUIDITY_SWEEP" or mss == "BEARISH_MSS" or asian_sw == "BEARISH_ASIAN_SWEEP":
            skor_smc_kz += 6

    batas_spread = 1.50 if is_gold else 15.0
    if spread_usd <= batas_spread:
        skor_smc_kz += 3

    if kz_info["in_killzone"]:
        skor_smc_kz += 7
    elif aset.upper() != "XAUUSD":
        skor_smc_kz += 2

    # Penalti Filter Rezim Pasar: Jika RANGING (ADX < 20), kurangi 10 poin agar tidak terjebak whipsaw
    penalti_regime = 10 if regime_info.get("regime") == "RANGING" else 0
    total_skor = max(0, (skor_makro + skor_sentimen + skor_ohlc_ote + skor_smc_kz) - penalti_regime)

    # Kalkulasi Entry, Hard Stop-Loss, dan 3 Target Profit (1.5R, 2.5R, 4.0R)
    jarak_sl_dasar = max(rentang_harian * 0.25, last_px * (0.0025 if is_gold else 0.006))
    if arah == "BUY":
        sl_px = round(min(nl_px - (jarak_sl_dasar * 0.2), last_px - jarak_sl_dasar), 2)
        risk_dist = abs(last_px - sl_px)
        tp1 = round(last_px + (1.5 * risk_dist), 2)
        tp2 = round(last_px + (2.5 * risk_dist), 2)
        tp3 = round(last_px + (4.0 * risk_dist), 2)
    else:
        sl_px = round(max(nh_px + (jarak_sl_dasar * 0.2), last_px + jarak_sl_dasar), 2)
        risk_dist = abs(sl_px - last_px)
        tp1 = round(last_px - (1.5 * risk_dist), 2)
        tp2 = round(last_px - (2.5 * risk_dist), 2)
        tp3 = round(last_px - (4.0 * risk_dist), 2)

    valid = (total_skor >= MIN_CONFLUENCE_SCORE) and (not veto_judge)
    if veto_judge:
        blocked_reason = "Veto Risk Judge aktif"
    elif regime_info.get("regime") == "RANGING" and total_skor < MIN_CONFLUENCE_SCORE:
        blocked_reason = f"Pasar RANGING (ADX {regime_info.get('adx_14')} < 20) & Skor {total_skor}/100 < {MIN_CONFLUENCE_SCORE}"
    else:
        blocked_reason = f"Skor {total_skor}/100 < {MIN_CONFLUENCE_SCORE}"

    reason_lengkap = (
        f"{reason_ai} | Teknikal: Regime={regime_info.get('regime')} (ADX {regime_info.get('adx_14')}), "
        f"OHLC Bias={bias_ohlc} (Pivot {ohlc['pivot']}), Zona={pos_ote}, "
        f"Sweep/MSS={sweep}/{mss} (Asian={asian_sw}), FVG={fvg_aktif}, OB={ob_aktif} "
        f"[Sumber Chart: {smc_data.get('data_source', 'API')}]."
    )

    return {
        "asset": aset,
        "data_source": smc_data.get("data_source", "UNKNOWN"),
        "regime": regime_info.get("regime", "TRENDING"),
        "adx_14": regime_info.get("adx_14", 0.0),
        "direction": arah,
        "total_score": total_skor,
        "breakdown": {
            "macro": skor_makro,
            "sentiment": skor_sentimen,
            "ohlc_ote": skor_ohlc_ote,
            "smc_kz": skor_smc_kz,
        },
        "sweep_mss": f"{sweep}/{mss}",
        "ote_pos": pos_ote,
        "fvg_zone": fvg_aktif,
        "ob_zone": ob_aktif,
        "valid_setup": valid,
        "blocked_reason": "" if valid else blocked_reason,
        "reason_utama": reason_lengkap,
        "invalidation": inval_ai,
        "entry": round(last_px, 2),
        "sl": sl_px,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
    }


# ==============================================================================
# 4. TELEGRAM INTERAKTIF (GEMINI 4 HYDROGEN CHAT) & NOTIFIKASI LAPORAN
# ==============================================================================
def kirim_telegram(pesan: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(pesan)
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        potongan = [pesan[i:i + 3900] for i in range(0, len(pesan), 3900)]
        for part in potongan:
            requests.post(
                url,
                json={"chat_id": TELEGRAM_CHAT_ID, "text": part},
                timeout=12,
            )
    except Exception as e:
        print(f"Gagal kirim Telegram: {e}")


def balas_chat_telegram_interaktif(ringkasan_data_live: str):
    """Membaca pesan terbaru dari user di Telegram dan membalasnya via Gemini 4 Hydrogen (model biasa)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    last_offset = int(get_bot_state("tg_last_update_id", "0"))
    try:
        url_get = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset={last_offset + 1}&timeout=3"
        resp = requests.get(url_get, timeout=10).json()
        updates = resp.get("result", [])
        if not updates:
            return

        pesan_terakhir = None
        max_uid = last_offset
        for upd in updates:
            uid = upd.get("update_id", 0)
            if uid > max_uid:
                max_uid = uid
            msg = upd.get("message", {})
            if str(msg.get("chat", {}).get("id")) == str(TELEGRAM_CHAT_ID) and msg.get("text"):
                pesan_terakhir = msg["text"]

        set_bot_state("tg_last_update_id", str(max_uid))

        if not pesan_terakhir:
            return

        prompt_chat = f"""
Kamu adalah Chief Quantitative Strategist & Institutional SMC/Orderflow Analyst untuk sistem AgentLang.
Jawab pertanyaan user di Telegram secara lugas, kritis, dan analitis berdasarkan DATA LIVE AKTUAL berikut:

{ringkasan_data_live}

PERTANYAAN USER: "{pesan_terakhir}"

ATURAN WAJIB:
1. Sebutkan arah bias (BULLISH / BEARISH / WAIT) secara tegas.
2. Wajib sertakan REASON UTAMA (struktur harga/SMC/FVG/OB/Asian Sweep, ADX regime, orderflow/OI/funding, makro DXY/US10Y, kalender ekonomi).
3. Wajib cantumkan BUKTI SUMBER DATA spesifik dari data di atas (atau beri label [PERLU DATA LIVE / BELUM DITARIK] jika datanya kosong—jangan mengarang angka).
4. Sertakan SKENARIO & INVALIDASI (area pantau entry, SL, TP1-TP3, batas risiko/layer, dan syarat yang membatalkan setup).
5. Jangan gunakan karakter markdown rumit (* atau _) agar tidak error di parser Telegram.
"""
        jawaban, model_chat = panggil_llm_teks(prompt_chat, mode_json=False)
        if jawaban:
            kirim_telegram(
                f"RESPON CHAT AGENTLANG ({model_chat})\n"
                f"Pertanyaan: {pesan_terakhir}\n"
                f"----------------------------------------\n"
                f"{jawaban}"
            )
    except Exception as e:
        print(f"Telegram chat listener warning: {e}")


def jalankan_analisa_lengkap():
    init_db()
    kz = deteksi_sesi_dan_killzone()

    # 1. Kelola Auto-Breakeven posisi aktif di Binance Demo
    be_actions = kelola_auto_breakeven_binance()

    # 2. Tarik Info Akun, Ekuitas, dan Spread Binance Demo
    akun = ambil_info_akun_dan_spread()
    equity = float(akun.get("usdt_total", 1000.0))
    spread_gold = float(akun.get("spread_digital_gold", 0.25))
    spread_btc = float(akun.get("spread_btc", 1.00))

    # 3. Cek Risk Gatekeeper & Filter Kalender Ekonomi Red Folder
    risk_gate = cek_batas_risiko_harian(equity, kz["date_wib"], kz["session"])
    kalender = cek_kalender_ekonomi_high_impact(window_menit=60)

    # 4. Tarik Data Multi-Pilar: Makro, Derivatif/COT/Orderflow, Blockworks, dan Teknikal
    makro = ambil_data_makro()
    derivatif = ambil_metrik_derivatif_dan_sentimen()
    bw_info = ambil_riset_blockworks()
    smc_xau = analisa_smc_matematis("XAUUSD")
    smc_xyz = analisa_smc_matematis("XYZ:GOLD")
    smc_btc = analisa_smc_matematis("BTCUSDT")
    korelasi = cek_anti_korelasi_ganda(threshold=0.80)

    # 5. Debat Critical Thinking 3-Agen (Claude -> Gemini 4 Hydrogen -> Groq)
    llm_view = evaluasi_critical_thinking_3_agen(
        makro, derivatif, kalender, smc_xau, smc_xyz, smc_btc, bw_info
    )

    # 6. Hitung Skor Konfluensi (0 - 100) untuk Ketiga Instrumen
    hasil_skor = [
        hitung_skor_konfluensi("XAUUSD", smc_xau, makro, derivatif, llm_view, kz, 0.25),
        hitung_skor_konfluensi("XYZ:GOLD", smc_xyz, makro, derivatif, llm_view, kz, spread_gold),
        hitung_skor_konfluensi("BTCUSDT", smc_btc, makro, derivatif, llm_view, kz, spread_btc),
    ]

    # 7. Filter Anti-Korelasi Ganda (|corr| >= 0.80 -> ambil hanya 1 skor tertinggi)
    kandidat_lolos = [h for h in hasil_skor if h["valid_setup"]]
    if korelasi.get("double_exposure_blocked") and len(kandidat_lolos) > 1:
        kandidat_lolos.sort(key=lambda x: x["total_score"], reverse=True)
        aset_terpilih = kandidat_lolos[0]["asset"]
        for h in hasil_skor:
            if h["valid_setup"] and h["asset"] != aset_terpilih:
                h["valid_setup"] = False
                h["blocked_reason"] = f"Ditahan filter Anti-Korelasi ({korelasi.get('corr_30d')})"

    # 8. Proses Routing Eksekusi & Audit Logging
    laporan_eksekusi = []
    for h in hasil_skor:
        status_aksi = "NO_TRADE"
        catatan = h.get("blocked_reason", "")

        if h["valid_setup"]:
            if kalender.get("news_blackout"):
                status_aksi = "BLOCKED_NEWS"
                catatan = f"Red Folder News Blackout: {', '.join(kalender.get('high_impact_events', []))}"
            elif not risk_gate["allowed"]:
                status_aksi = "BLOCKED_RISK"
                catatan = risk_gate["reason"]
            else:
                risk_pct = hitung_dynamic_half_kelly_risk(kz["session"], h["total_score"], h.get("regime", "TRENDING"))
                risk_usd = round(equity * risk_pct, 2)
                jml_layer = 2 if h["total_score"] >= 85 else 1

                res_exec = proses_eksekusi_sinyal(
                    aset=h["asset"],
                    arah=h["direction"],
                    risk_usd=risk_usd,
                    entry_price=h["entry"],
                    sl_price=h["sl"],
                    tp1=h["tp1"],
                    tp2=h["tp2"],
                    tp3=h["tp3"],
                    layers=jml_layer,
                    leverage=1,
                )
                status_aksi = res_exec.get("execution_type", "PROCESSED")
                catatan = json.dumps(res_exec)
                laporan_eksekusi.append((h, res_exec, risk_usd, risk_pct))

        simpan_log_audit({
            "timestamp_wib": kz["wib_str"],
            "date_wib": kz["date_wib"],
            "session_name": kz["session"],
            "asset": h["asset"],
            "direction": h["direction"],
            "total_score": h["total_score"],
            "score_macro": h["breakdown"]["macro"],
            "score_sentiment": h["breakdown"]["sentiment"],
            "score_ohlc_ote": h["breakdown"]["ohlc_ote"],
            "score_smc_kz": h["breakdown"]["smc_kz"],
            "action_status": status_aksi,
            "entry_price": h["entry"],
            "sl_price": h["sl"],
            "tp1": h["tp1"],
            "tp2": h["tp2"],
            "tp3": h["tp3"],
            "notes": catatan,
        })

    # 9. Susun Laporan Executive Summary Lengkap dengan Reason & Sumber API
    btc_of = derivatif.get("btc_orderflow", {})
    cot = derivatif.get("cftc_cot", {})
    liq = derivatif.get("crypto_liquidity", {})
    news_status = (
        f"BLACKOUT ({', '.join(kalender['high_impact_events'])})"
        if kalender.get("news_blackout")
        else "AMAN (Tidak ada Red Folder +/-60m | Sumber: ForexFactory JSON)"
    )

    lines = [
        f"QUANT AI CONFLUENCE & 3-AGENT REPORT",
        f"Waktu: {kz['wib_str']} | Sesi: {kz['session']} ({kz['killzone_label']})",
        f"AI Analisa: {llm_view.get('model_used', 'Claude/Multi-LLM')} | Binance Demo: ${equity:,.2f} (DD: {risk_gate['dd_pct']}%)",
        f"Kalender Ekonomi: {news_status}",
        f"----------------------------------------",
        f"1. MAKRO, ORDERFLOW & CFTC COT",
        f"- DXY: {makro.get('DXY', {}).get('last')} ({makro.get('DXY', {}).get('chg_pct'):+.2f}%) | US10Y: {makro.get('US10Y', {}).get('last')} | USOIL: {makro.get('USOIL', {}).get('last')} [Yahoo Finance]",
        f"- BTC Orderflow: {btc_of.get('orderflow_bias')} (OI {btc_of.get('oi_change_pct'):+.2f}%, Taker {btc_of.get('taker_buy_sell_ratio')}) [Binance Public API]",
        f"- CFTC Gold COT: {cot.get('gold_cot_bias')} (Net {cot.get('gold_managed_money_net'):,}) [CFTC Socrata API]",
        f"- Likuiditas Kripto: BTC.D {liq.get('btc_dominance')}% | USDT.D {liq.get('usdt_dominance')}% | TOTAL3 ${liq.get('total3_usd_b')}B [CoinGecko API]",
        f"- Blockworks Intel: {bw_info}",
        f"----------------------------------------",
        f"2. SIDANG CRITICAL THINKING (3-AGEN)",
        f"- Devil's Advocate: {llm_view.get('kritik_devils_advocate')}",
        f"- Risk Judge: {llm_view.get('ringkasan_risk_judge')}",
        f"----------------------------------------",
        f"3. ANALISA SETUP, REASON & LEVEL HARGA PER ASET",
    ]

    for h in hasil_skor:
        b = h["breakdown"]
        status_label = "LOLOS EKSEKUSI" if h["valid_setup"] else f"WAIT / WATCHLIST ({h['blocked_reason']})"
        mode_tag = "Manual CFD" if h["asset"] == "XAUUSD" else "Auto Binance Demo"
        lines.extend([
            f"",
            f"[{h['asset']} | {mode_tag}] -> BIAS: {h['direction']} | SKOR: {h['total_score']}/100",
            f"Status: {status_label}",
            f"Rincian: Makro {b['macro']}/25 | Judge {b['sentiment']}/20 | OHLC/OTE {b['ohlc_ote']}/25 | SMC/KZ {b['smc_kz']}/30",
            f"Reason Utama: {h['reason_utama']}",
            f"Plan Harga: Entry {h['entry']} | SL {h['sl']} | TP1 {h['tp1']} | TP2 {h['tp2']} | TP3 {h['tp3']}",
            f"Invalidasi: {h['invalidation']}",
        ])

    if be_actions:
        lines.append("")
        lines.append("Auto-Breakeven Aktif: " + ", ".join(be_actions))

    if laporan_eksekusi:
        lines.append("")
        lines.append("----------------------------------------")
        lines.append("4. DETAIL EKSEKUSI TERKIRIM")
        for h, res, r_usd, r_pct in laporan_eksekusi:
            if res.get("execution_type") == "MANUAL_CFD_SIGNAL":
                lines.append(
                    f"MANUAL CFD {h['asset']} ({h['direction']}): Lot {res.get('suggested_lot_cfd')} (Risk {r_pct*100:.2f}% / ${r_usd}) | Entry {h['entry']} | SL {h['sl']} | TP1 {h['tp1']}"
                )
            else:
                lines.append(
                    f"AUTO BINANCE DEMO {h['asset']} -> {res.get('executed_symbol')} ({h['direction']}): Risk {r_pct*100:.2f}% (${r_usd}) | {len(res.get('orders', []))} layer terkirim"
                )

    laporan_teks = "\n".join(lines)
    kirim_telegram(laporan_teks)

    # 10. Cek pesan chat baru user di Telegram dan balas via Gemini 4 Hydrogen (model biasa)
    balas_chat_telegram_interaktif(laporan_teks)


if __name__ == "__main__":
    jalankan_analisa_lengkap()
