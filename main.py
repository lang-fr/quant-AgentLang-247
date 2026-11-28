import os
import sys
import json
import time
import sqlite3
import threading
import datetime
from typing import Dict, Any, List, Optional, Tuple

import pytz
import requests
from dotenv import load_dotenv

from ai_router import FlagshipAIRouter
from market_feeds import InstitutionalMarketFeeds
from smc_alpha import InstitutionalSMCAlphaEngine
from execution_engine import InstitutionalExecutionEngine
from quant_v28_core import (
    TRADING_SKILLS,
    init_audit_db,
    check_and_consume_session_quota,
    record_trade_journal,
    get_journal_and_evaluation,
    calculate_regime_and_guards,
    fetch_deribit_options_btc,
    fetch_cmc_global_metrics,
    fetch_blockworks_intel,
    audit_high_probability_setup,
    run_backtest_and_monte_carlo,
)
from web_bridge import register_webhook_callback, start_web_bridge_in_background
from quant_derivatives_mm import QuantStatDerivativesMMEngine
quant_dmm_engine = QuantStatDerivativesMMEngine()

try:
    from quant_intel import QuantIntelEngine
except Exception as _qi_err:
    QuantIntelEngine = None
    print(f"[WARN] quant_intel.py belum tersedia: {_qi_err}")

try:
    import risk_engine as risk_mod
except Exception as _re_err:
    risk_mod = None
    print(f"[WARN] risk_engine.py belum tersedia: {_re_err}")

try:
    from outcome_verifier import OutcomeVerifierEngine
except Exception as _ov_err:
    OutcomeVerifierEngine = None
    print(f"[WARN] outcome_verifier.py belum tersedia: {_ov_err}")
try:
    from outcome_verifier import OutcomeVerifierEngine
except Exception as _ov_err:
    OutcomeVerifierEngine = None
    print(f"[WARN] outcome_verifier.py belum tersedia: {_ov_err}")
try:
    from quant_unified_hub import QuantUnifiedHub
    unified_hub = QuantUnifiedHub()
except Exception as _quh_err:
    unified_hub = None
    print(f"[WARN] quant_unified_hub.py belum aktif: {_quh_err}")
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = str(os.getenv("TELEGRAM_CHAT_ID", "")).strip()
AUTO_SCAN_INTERVAL_SEC = int(os.getenv("AUTO_SCAN_INTERVAL_SEC", "14400"))
NEWS_POLL_INTERVAL_SEC = int(os.getenv("NEWS_POLL_INTERVAL_SEC", "60"))
MIN_CONFLUENCE_SCORE = int(os.getenv("MIN_CONFLUENCE_SCORE", "75"))
AUTO_EXECUTE_ENABLED = os.getenv("AUTO_EXECUTE_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
DB_PATH = "quant_agentlang_247.db"
WIB = pytz.timezone("Asia/Jakarta")

ai_router = FlagshipAIRouter()
feeds = InstitutionalMarketFeeds()
alpha_engine = InstitutionalSMCAlphaEngine()
exec_engine = InstitutionalExecutionEngine()
quant_intel = QuantIntelEngine() if QuantIntelEngine is not None else None
outcome_verifier = OutcomeVerifierEngine() if OutcomeVerifierEngine is not None else None

LATEST_CONTEXT_CACHE = {"updated": "Belum ada scan", "summary": "Belum ada data pasar tersimpan."}
SPEECH_WATCH_STATE = {"active": False, "url": "", "last_tail": ""}


# =============================================================================
# DATABASE & STATE HELPERS
# =============================================================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS setups_store (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_wib TEXT,
            symbol TEXT,
            live_price REAL,
            confluence_score INTEGER,
            bundle_json TEXT,
            status TEXT
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_news (
            news_hash TEXT PRIMARY KEY,
            source TEXT,
            title TEXT,
            created_wib TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def is_news_seen_and_mark(news_hash: str, source: str, title: str) -> bool:
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM seen_news WHERE news_hash = ?", (news_hash,))
    exists = c.fetchone() is not None
    if not exists:
        now_wib = datetime.datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S WIB")
        c.execute(
            "INSERT INTO seen_news (news_hash, source, title, created_wib) VALUES (?, ?, ?, ?)",
            (news_hash, source, title, now_wib),
        )
        conn.commit()
    conn.close()
    return exists


def save_setup_bundle(symbol: str, live_price: float, score: int, bundle: dict) -> int:
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now_wib = datetime.datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S WIB")
    c.execute(
        "INSERT INTO setups_store (created_wib, symbol, live_price, confluence_score, bundle_json, status) VALUES (?, ?, ?, ?, ?, ?)",
        (now_wib, symbol, live_price, score, json.dumps(bundle), "PENDING"),
    )
    sid = c.lastrowid
    conn.commit()
    conn.close()
    return sid


def get_setup_bundle(setup_id: int) -> Optional[dict]:
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT symbol, bundle_json, status FROM setups_store WHERE id = ?", (setup_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    return {"symbol": row[0], "bundle": json.loads(row[1]), "status": row[2]}


def update_setup_status(setup_id: int, new_status: str):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE setups_store SET status = ? WHERE id = ?", (new_status, setup_id))
    conn.commit()
    conn.close()


# =============================================================================
# AI ROUTER ADAPTER & TELEGRAM SENDER
# =============================================================================
def ask_ai_router(prompt: str, system_instruction: str = "", deep: bool = False) -> Tuple[str, str]:
    """Adapter kompatibel untuk semua versi ai_router.py tanpa AttributeError."""
    if hasattr(ai_router, "generate_with_flagship_cascade"):
        try:
            res, eng = ai_router.generate_with_flagship_cascade(prompt)
            if res:
                return res, eng
        except Exception:
            pass

    if deep and hasattr(ai_router, "generate_institutional_analysis"):
        try:
            res, eng = ai_router.generate_institutional_analysis(prompt, system_instruction)
            if res:
                return res, eng
        except Exception:
            pass

    if hasattr(ai_router, "generate"):
        try:
            res = ai_router.generate(
                prompt=prompt,
                system_instruction=system_instruction,
                prefer_deep_reasoning=deep,
                role="analysis" if deep else "setup",
            )
            eng = getattr(ai_router, "analysis_engine", "Flagship AI")
            return res, eng
        except Exception:
            pass

    return (
        "1. Makro & Likuiditas: Pantau arah DXY dan yield US10Y sebagai penentu arus modal.\n"
        "2. Konfluensi 8 Pilar: Validasi struktur MTF, level Pivot NH/NL, dan arus CFTC COT.\n"
        "3. Skeptic Veto: Wajib tunggu konfirmasi rejection di POI dan patuhi batas risiko CVaR.",
        "Quant Deterministic Engine",
    )


def send_telegram(text: str, reply_markup: Optional[dict] = None) -> None:
    from ai_router import clean_plain_text
    text = clean_plain_text(text)
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    clean_text = text.replace("<", "").replace(">", "")
    chunks = [clean_text[i : i + 3900] for i in range(0, len(clean_text), 3900)]
    for idx, chunk in enumerate(chunks):
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": chunk}
        if reply_markup and idx == len(chunks) - 1:
            payload["reply_markup"] = json.dumps(reply_markup)
        try:
            requests.post(url, json=payload, timeout=15)
        except Exception as e:
            print("Telegram send error:", e)


# =============================================================================
# SESI KILLZONE & RISK ENGINE SANITY SANITIZER (TAHAP 1 + TAHAP 2 BRIDGE)
# =============================================================================
def get_active_killzone_wib() -> Tuple[str, bool]:
    now_wib = datetime.datetime.now(WIB)
    hour = now_wib.hour
    if 7 <= hour < 13:
        return "ASIA TOKYO KILLZONE (07:00 - 13:00 WIB)", True
    elif 13 <= hour < 19:
        return "LONDON OPEN KILLZONE (13:00 - 19:00 WIB)", True
    elif 19 <= hour or hour < 2:
        return "NEW YORK / LONDON OVERLAP KILLZONE (19:00 - 02:00 WIB)", True
    else:
        return "DEAD ZONE / BANK ROLLOVER (02:00 - 07:00 WIB)", False


def _parse_first_float(val: Any, fallback: float) -> float:
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        cleaned = val.replace(",", " ").replace("|", " ").replace("/", " ").replace("-", " ")
        for token in cleaned.split():
            try:
                num = float(token)
                if num > 0:
                    return num
            except ValueError:
                continue
    return fallback


def sanitize_and_enrich_setup_with_risk_engine(
    symbol: str,
    live_price: float,
    horizon_title: str,
    st: Dict[str, Any],
    garch_info: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Menghubungkan setup mentah ke risk_engine.py sekaligus mencegah anomali SL melebar
    (misal SL Swing 900 poin pada XAUUSD) serta menghitung lot per layer dan BEP+1.
    """
    direction = str(st.get("direction", "BUY")).upper()
    entry_ref = _parse_first_float(st.get("entry"), live_price)
    sl_raw = _parse_first_float(st.get("sl"), live_price)

    # Batas jarak SL maksimal per horizon agar RR realistis & anti-margin call
    if "SCALP" in horizon_title.upper():
        max_sl_pct = 0.0045 if symbol == "XAUUSD" else 0.0080
    elif "INTRADAY" in horizon_title.upper():
        max_sl_pct = 0.0110 if symbol == "XAUUSD" else 0.0180
    else:
        max_sl_pct = 0.0220 if symbol == "XAUUSD" else 0.0380

    # Penyesuaian volatilitas GARCH jika ekstrem
    vol_regime = garch_info.get("volatility_regime", "NORMAL_VOLATILITY")
    size_multiplier = 0.65 if vol_regime == "EXTREME_VOLATILITY_REDUCE_SIZE" else 1.0

    sl_dist = abs(entry_ref - sl_raw)
    max_dist = entry_ref * max_sl_pct
    min_dist = entry_ref * 0.0012

    sl_corrected = False
    if sl_dist > max_dist or sl_dist < min_dist:
        sl_dist = round(max_dist * 0.85, 2)
        sl_corrected = True
        if "SELL" in direction:
            new_sl = round(entry_ref + sl_dist, 2)
            new_tp1 = round(entry_ref - (sl_dist * 1.5), 2)
            new_tp2 = round(entry_ref - (sl_dist * 2.5), 2)
            new_tp3 = round(entry_ref - (sl_dist * 4.0), 2)
        else:
            new_sl = round(entry_ref - sl_dist, 2)
            new_tp1 = round(entry_ref + (sl_dist * 1.5), 2)
            new_tp2 = round(entry_ref + (sl_dist * 2.5), 2)
            new_tp3 = round(entry_ref + (sl_dist * 4.0), 2)
        st["sl"] = f"{new_sl} (Risk-Capped)"
        st["tp1"] = str(new_tp1)
        st["tp2"] = str(new_tp2)
        st["tp3"] = str(new_tp3)
        st["rr"] = "1:2.5 (Max 1:4.0)"

    # Kalkulasi Risk USD & Lot 3-Layer (30% / 40% / 30%)
    risk_usd = round(exec_engine.balance * (exec_engine.risk_pct / 100.0) * size_multiplier, 2)
    contract_size = 100.0 if symbol == "XAUUSD" else 1.0
    total_lot = max(0.01, round(risk_usd / max(sl_dist * contract_size, 1.0), 2))
    l1 = max(0.01, round(total_lot * 0.30, 2))
    l2 = max(0.01, round(total_lot * 0.40, 2))
    l3 = max(0.01, round(total_lot * 0.30, 2))

    # Panggil risk_engine.py jika tersedia untuk validasi tambahan
    re_note = ""
    if risk_mod is not None:
        try:
            for cls_name in ("InstitutionalRiskEngine", "RiskEngine"):
                if hasattr(risk_mod, cls_name):
                    re_inst = getattr(risk_mod, cls_name)()
                    if hasattr(re_inst, "evaluate_setup"):
                        re_out = re_inst.evaluate_setup(
                            symbol=symbol,
                            entry=entry_ref,
                            sl=_parse_first_float(st.get("sl"), entry_ref),
                            balance=exec_engine.balance,
                            risk_pct=exec_engine.risk_pct,
                        )
                        if isinstance(re_out, dict) and re_out.get("lot_per_layer"):
                            re_note = f" | RiskEngine Lot/Layer: {re_out['lot_per_layer']}"
                    break
        except Exception:
            pass

    bep_offset = 1.0 if symbol == "XAUUSD" else 25.0
    bep_price = round(entry_ref + bep_offset if "BUY" in direction else entry_ref - bep_offset, 2)
    st["risk_summary"] = (
        f"Total Lot: {total_lot} (L1:{l1} | L2:{l2} | L3:{l3}) | "
        f"Risk: ${risk_usd} | BEP+1: {bep_price}{re_note}"
        + (" [SL Dikalibrasi RiskEngine]" if sl_corrected else "")
    )
    return st


# =============================================================================
# FORMATTER PESAN #1 (KONFIRMASI UTAMA 1.950+ KARAKTER) & PESAN #2/#3 (ASET)
# =============================================================================
def build_master_confirmation_message(
    trigger: str,
    intel: Dict[str, Any],
    q_scan: Dict[str, Any],
    blackout: bool,
    news_list: List[Any],
    breaking: List[Dict[str, Any]],
) -> str:
    now_wib = datetime.datetime.now(WIB).strftime("%Y-%m-%d %H:%M WIB")
    killzone_label, is_kz_active = get_active_killzone_wib()
    matrix = intel.get("matrix", {})

    def _px(key: str) -> str:
        item = matrix.get(key, {})
        p = item.get("price", 0)
        chg = item.get("change_pct", 0)
        return f"{p} ({chg:+.2f}%)" if p else "N/A"

    inter_corr = q_scan.get("intermarket_correlation", {})
    levels = inter_corr.get("latest_levels", {})
    xau_q = q_scan.get("xauusd", {})
    btc_q = q_scan.get("btcusdt", {})

    xau_ohlc = xau_q.get("ohlc_strategy", {})
    xau_garch = xau_q.get("garch_cvar_risk", {})
    xau_cot = xau_q.get("cftc_cot", {})

    btc_ohlc = btc_q.get("ohlc_strategy", {})
    btc_garch = btc_q.get("garch_cvar_risk", {})
    btc_cot = btc_q.get("cftc_cot", {})
    btc_defi = btc_q.get("defillama_liquidity", {})

    # Susun konteks lengkap untuk 3 Poin Analisa Institutional Quant Strategist
    analyst_prompt = (
        f"Anda adalah Institutional Quant Strategist (SMC, ICT, Orderflow, OHLC Pivot, GARCH/CVaR, Makro).\n"
        f"Data Pasar Terkini ({now_wib} | Sesi: {killzone_label}):\n"
        f"- Telemetry 8 Pilar: {intel.get('telemetry_summary', '')}\n"
        f"- Intermarket Levels: DXY={levels.get('DXY', _px('DXY'))}, US02Y={levels.get('US02Y', 'N/A')}, "
        f"US10Y={levels.get('US10Y', 'N/A')}, SP500={levels.get('SP500', 'N/A')}, NASDAQ100={levels.get('NASDAQ100', 'N/A')}\n"
        f"- Filter Makro: XAU={inter_corr.get('xauusd_macro_verdict', 'N/A')} | BTC={inter_corr.get('btcusd_macro_verdict', 'N/A')}\n"
        f"- XAUUSD OHLC Pivot: NH(Sell)={xau_ohlc.get('new_high_NH', 'N/A')}, NL(Buy)={xau_ohlc.get('new_low_NL', 'N/A')}, "
        f"Status={xau_ohlc.get('spatial_status', 'N/A')} | GARCH Vol={xau_garch.get('next_day_volatility_pct', 'N/A')}% "
        f"(CVaR95={xau_garch.get('cvar_95_daily_pct', 'N/A')}%) | CFTC COT={xau_cot.get('cot_institutional_bias', 'N/A')} "
        f"(Net={xau_cot.get('noncomm_net', 'N/A')})\n"
        f"- BTCUSDT OHLC Pivot: NH(Sell)={btc_ohlc.get('new_high_NH', 'N/A')}, NL(Buy)={btc_ohlc.get('new_low_NL', 'N/A')}, "
        f"Status={btc_ohlc.get('spatial_status', 'N/A')} | GARCH Vol={btc_garch.get('next_day_volatility_pct', 'N/A')}% "
        f"(CVaR95={btc_garch.get('cvar_95_daily_pct', 'N/A')}%) | DeFiLlama Stablecoin 7D={btc_defi.get('net_flow_7d_usd_b', 'N/A')}B\n"
        f"- News Blackout USD: {blackout} {news_list}\n"
        f"- Breaking Headlines: {' | '.join(h['title'] for h in breaking[:3])}\n\n"
        f"Tulis analisa tajam dalam Bahasa Indonesia dengan tepat 3 poin bernomor:\n"
        f"1. Makro, Yields & Likuiditas Global:\n"
        f"2. Konfluensi 8 Pilar Intermarket, OHLC Pivot (NH/NL) & CFTC COT:\n"
        f"3. Skeptic Veto & Manajemen Tail Risk (GARCH/CVaR):"
    )
    strategist_text, strategist_engine = ask_ai_router(analyst_prompt, deep=True)

    news_headline_str = " | ".join(h["title"] for h in breaking[:2]) if breaking else "Tidak ada headline ekstrem baru"
    blackout_str = f"🚨 AKTIF ({news_list})" if blackout else "✅ AMAN (Tidak ada rilis High-Impact ±45m)"

    header_lines = [
        f"🏛️ AGENT QUANT LANG — INSTITUTIONAL REPORT ({now_wib})",
        f"Trigger: {trigger} | AI Strategist: {strategist_engine}",
        f"Sesi Killzone: {'🟢' if is_kz_active else '🟡'} {killzone_label}",
        f"Dompet Eksekusi: {exec_engine.get_balance_summary()}",
        f"Rem Darurat News USD: {blackout_str}",
        "",
        "📌 RINGKASAN KONFIRMASI & 8 PILAR INTERMARKET",
        f"• Pilar 1-2 (Makro & Yields): DXY {_px('DXY')} | US02Y {levels.get('US02Y', 'N/A')}% | US10Y {levels.get('US10Y', 'N/A')}%",
        f"• Pilar 3-4 (Indeks & Risk): S&P500 {levels.get('SP500', 'N/A')} | NASDAQ100 {levels.get('NASDAQ100', 'N/A')} | Filter: {inter_corr.get('xauusd_macro_verdict', 'NETRAL')} / {inter_corr.get('btcusd_macro_verdict', 'NETRAL')}",
        f"• Pilar 5 (OHLC Pivot XAU): NH (Sell): {xau_ohlc.get('new_high_NH', 'N/A')} | Mid: {xau_ohlc.get('equilibrium_mid', 'N/A')} | NL (Buy): {xau_ohlc.get('new_low_NL', 'N/A')} [{xau_ohlc.get('spatial_status', 'N/A')}]",
        f"• Pilar 6 (OHLC Pivot BTC): NH (Sell): {btc_ohlc.get('new_high_NH', 'N/A')} | Mid: {btc_ohlc.get('equilibrium_mid', 'N/A')} | NL (Buy): {btc_ohlc.get('new_low_NL', 'N/A')} [{btc_ohlc.get('spatial_status', 'N/A')}]",
        f"• Pilar 7 (GARCH & CVaR): XAU Vol {xau_garch.get('next_day_volatility_pct', 'N/A')}% (CVaR95: {xau_garch.get('cvar_95_daily_pct', 'N/A')}%) | BTC Vol {btc_garch.get('next_day_volatility_pct', 'N/A')}% (CVaR95: {btc_garch.get('cvar_95_daily_pct', 'N/A')}%)",
        f"• Pilar 8 (CFTC COT & On-Chain): Gold COT Net {xau_cot.get('noncomm_net', 'N/A')} ({xau_cot.get('cot_institutional_bias', 'N/A')}) | Stablecoin 7D: {btc_defi.get('net_flow_7d_usd_b', 'N/A')}B ({btc_defi.get('liquidity_regime', 'N/A')})",
        f"• Radar Headline: {news_headline_str[:180]}",
        "",
        "🧠 ANALISA INSTITUTIONAL QUANT STRATEGIST (3 PILAR KEPUTUSAN)",
        strategist_text,
    ]
    return "\n".join(header_lines)


def format_asset_report(
    symbol: str,
    bundle: dict,
    score: int,
    engine_name: str,
    passed: bool,
    errors: list[str],
    asset_quant: Dict[str, Any],
) -> str:
    setups = bundle.get("setups", {})
    risk_usd = round(exec_engine.balance * (exec_engine.risk_pct / 100.0), 1)
    ohlc = asset_quant.get("ohlc_strategy", {})
    garch = asset_quant.get("garch_cvar_risk", {})

    gate_ok = passed and (score >= MIN_CONFLUENCE_SCORE)
    if not gate_ok and score < MIN_CONFLUENCE_SCORE:
        errors = list(errors) + [f"Skor {score} < {MIN_CONFLUENCE_SCORE} (WAIT / NO SNIPER)"]

    status_badge = "✅ LOLOS AUDIT & RISK GATE" if gate_ok else f"⛔ WAIT / HOLD ({', '.join(errors)})"

    lines = [
        f"🎯 MAPPING SNIPER {symbol} (Live: {bundle['live_price']} | {bundle['price_source']})",
        f"Modal: ${exec_engine.balance} | Risk/Trade: ${risk_usd} ({exec_engine.risk_pct}%) | Skor: {score}/100",
        f"Status Gate: {status_badge}",
        f"OHLC Daily Pivot: NH(Sell)={ohlc.get('new_high_NH', 'N/A')} | NL(Buy)={ohlc.get('new_low_NL', 'N/A')} | Vol GARCH: {garch.get('next_day_volatility_pct', 'N/A')}% ({garch.get('volatility_regime', 'NORMAL')})",
        "",
    ]

    for horizon_title, raw_st in setups.items():
        st = sanitize_and_enrich_setup_with_risk_engine(
            symbol=symbol,
            live_price=float(bundle["live_price"]),
            horizon_title=horizon_title,
            st=dict(raw_st),
            garch_info=garch,
        )
        lines.extend(
            [
                f"📌 {horizon_title} — [{st['direction']}] (RR: {st['rr']})",
                f"• Rantai MTF: {st['tf_chain']} ({st['aligned_count']})",
                f"• Sniper Limit Entry (1-3 Layer): {st['entry']}",
                f"• Tight Stop Loss: {st['sl']}",
                f"• TP1 (Geser SL ke BEP+1): {st['tp1']}",
                f"• TP2 / TP3 Runner: {st['tp2']} | {st['tp3']}",
                f"• Position Sizing: {st['risk_summary']}",
                f"• Alasan Presisi: {st['reason']}",
                "",
            ]
        )
        
        dmm = quant_dmm_engine.run_all_three_pillars(symbol)
    qs = dmm.get("quant_statistics", {})
    dv = dmm.get("derivatives_microstructure", {})
    mm = dmm.get("market_making_orderbook", {})
    lines.extend(
        [
            f"📊 Quant Stat: Hurst {qs.get('hurst_exponent', 'N/A')} ({qs.get('stat_regime', 'N/A')}) | Z20D: {qs.get('price_zscore_20d', 'N/A')} | PairZ: {qs.get('pair_spread_zscore_30d', 'N/A')}",
            f"⚡ Derivatif: FundAPR {dv.get('funding_apr_pct', 'N/A')}% | Basis {dv.get('spot_perp_basis_bps', 'N/A')}bps | OI ${dv.get('open_interest_usd_m', 'N/A')}M | {dv.get('squeeze_regime', 'N/A')}",
            f"🏦 Market Making: OBI {mm.get('orderbook_imbalance_obi', 'N/A')} | MicroPx {mm.get('stoikov_micro_price', 'N/A')} | Quote Bid/Ask: {mm.get('avellaneda_optimal_bid_limit', 'N/A')} / {mm.get('avellaneda_optimal_ask_limit', 'N/A')}",
            "",
        ]
    )
    lines.append(f"AI Setup Engine: {engine_name}")
    return "\n".join(lines)


# =============================================================================
# PIPELINE UTAMA LAPORAN 24/7
# =============================================================================
def build_and_send_institutional_report(trigger: str = "Scheduled"):
    if outcome_verifier is not None:
        try:
            for m_name in ("verify_all_pending_and_approved", "verify_pending", "run_verification", "verify_all"):
                if hasattr(outcome_verifier, m_name):
                    getattr(outcome_verifier, m_name)()
                    break
        except Exception as e:
            print("Outcome verifier warning:", e)

    intel = feeds.collect_all_8_pillars()
    blackout, news_list = feeds.check_usd_news_blackout()
    breaking = feeds.fetch_realtime_breaking_news(limit=5)
    q_scan = quant_intel.run_full_quant_scan() if quant_intel is not None else {}

    master_msg = build_master_confirmation_message(
        trigger=trigger,
        intel=intel,
        q_scan=q_scan,
        blackout=blackout,
        news_list=news_list,
        breaking=breaking,
    )
    send_telegram(master_msg)

    dxy = intel["matrix"]["DXY"]["price"]
    macro_bias = "BULLISH" if (dxy == 0 or dxy < 104.0) else "BEARISH"
    summary_parts = [master_msg[:1800]]

    for sym in ("XAUUSD", "BTCUSDT"):
        raw_bundle = alpha_engine.build_cascade_setups(sym, feeds, macro_bias)
        refined_bundle, engine_name = alpha_engine.refine_setups_with_flagship_ai(sym, raw_bundle, intel, ai_router)
        score = alpha_engine.calculate_confluence_score(sym, intel, macro_bias, refined_bundle.get("mtf_ladder"))
        passed, errors = alpha_engine.verify_setup_before_send(refined_bundle, blackout, score)

        asset_q = q_scan.get(sym.lower(), {})
        intraday_setup = refined_bundle.get("setups", {}).get("INTRADAY (M1-M5-M15-M30-H1-H4)", {})
        final_score = score
        try:
            jury_audit = audit_high_probability_setup(
                symbol=sym,
                direction=intraday_setup.get("direction", "BUY"),
                entry=_parse_first_float(intraday_setup.get("entry"), refined_bundle["live_price"]),
                sl=_parse_first_float(intraday_setup.get("sl"), refined_bundle["live_price"]),
                tp2=_parse_first_float(intraday_setup.get("tp2"), refined_bundle["live_price"]),
                raw_score=score,
                intel=intel,
                q_scan=q_scan,
                dmm=quant_dmm_engine.run_all_three_pillars(sym),
                news_blackout=blackout,
            )
            if isinstance(jury_audit, dict):
                final_score = int(jury_audit.get("final_score", score))
                if not jury_audit.get("approved", True):
                    passed = False
                    errors = list(errors) + jury_audit.get("red_flags", [])[:2]
        except Exception as _ja_err:
            print("Jury audit fallback:", _ja_err)

        setup_id = save_setup_bundle(sym, refined_bundle["live_price"], final_score, refined_bundle)
        msg = format_asset_report(sym, refined_bundle, final_score, engine_name, passed, errors, asset_q)
            if unified_hub is not None:
            try:
                hub_res = unified_hub.run_unified_analysis(
                    symbol=sym,
                    feeds=feeds,
                    intel=intel,
                    ai_router=ai_router,
                    account_balance=exec_engine.balance,
                    consume_quota_on_approve=False,
                )
                j_audit = hub_res.get("jury_audit", {})
                add_guard = hub_res.get("additive_guard", {})
                r_sent = hub_res.get("realtime_sentiment", {})
                reg_g = hub_res.get("regime_and_guards", {})
                r_verd = hub_res.get("risk_verdict", {})

                if isinstance(j_audit.get("final_score"), (int, float)):
                    final_score = int(j_audit["final_score"])
                if not j_audit.get("approved_high_prob", True):
                    passed = False
                    errors = list(errors) + list(j_audit.get("red_flags", []))[:2]
                if not add_guard.get("additive_pass", True):
                    passed = False
                    errors = list(errors) + list(add_guard.get("extra_warnings", []))

                extra_lines = [
                    "",
                    "🛡️ KONFIRMASI TAMBAHAN UNIFIED HUB 360°:",
                    f"• Playbook & Sesi WIB: {hub_res.get('active_playbook', '-')} | {add_guard.get('wib_session', '-')}",
                    f"• Regime ADX/RSI: {reg_g.get('regime', '-')} (ADX H1: {reg_g.get('adx_h1', '-')} | RSI H1: {reg_g.get('rsi_h1', '-')} | Spike Z: {reg_g.get('z_score_h1', '-')})",
                    f"• Sentimen Realtime: Fear & Greed {r_sent.get('fear_greed_value', '-')} ({r_sent.get('fear_greed_label', '-')}) | OKX L/S: {r_sent.get('okx_btc_long_short_ratio', 'N/A')}",
                    f"• Dewan Juri 4-Agen: Skor Final {final_score}/100 ({j_audit.get('status_label', '-')}) | Red Flags: {', '.join(j_audit.get('red_flags', [])) or 'NIHIL'}",
                    f"• Risk Engine CRO: {r_verd.get('reason', '-')}",
                ]
                msg = msg + "\n" + "\n".join(extra_lines)
            except Exception as _hub_err:
                print(f"[WARN] UnifiedHub enrichment {sym}: {_hub_err}")
        buttons = {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve Scalp", "callback_data": f"approve_scalp_{setup_id}"},
                    {"text": "✅ Approve Intraday", "callback_data": f"approve_intraday_{setup_id}"},
                ],
                [
                    {"text": "✅ Approve Swing", "callback_data": f"approve_swing_{setup_id}"},
                    {"text": "❌ Reject", "callback_data": f"reject_{setup_id}"},
                ],
            ]
        }
        send_telegram(msg, reply_markup=buttons)

        if AUTO_EXECUTE_ENABLED and passed and final_score >= MIN_CONFLUENCE_SCORE and not blackout and intraday_setup:
            try:
                q_res = check_and_consume_session_quota(sym, "INTRADAY")
                quota_ok, quota_msg = (q_res[0], str(q_res[1])) if isinstance(q_res, tuple) else (bool(q_res), "Kuota Sesi OK")
            except Exception:
                quota_ok, quota_msg = True, "Kuota Sesi OK"

            if quota_ok:
                res = exec_engine.execute_horizon_setup(sym, "INTRADAY", intraday_setup)
                update_setup_status(setup_id, "AUTO_EXECUTED_INTRADAY")
                try:
                    record_trade_journal(
                        symbol=sym,
                        horizon="INTRADAY",
                        direction=intraday_setup.get("direction", "BUY"),
                        entry=_parse_first_float(intraday_setup.get("entry"), refined_bundle["live_price"]),
                        sl=_parse_first_float(intraday_setup.get("sl"), refined_bundle["live_price"]),
                        tp1=_parse_first_float(intraday_setup.get("tp1"), refined_bundle["live_price"]),
                        tp2=_parse_first_float(intraday_setup.get("tp2"), refined_bundle["live_price"]),
                        tp3=_parse_first_float(intraday_setup.get("tp3"), refined_bundle["live_price"]),
                        score=final_score,
                        mode=f"AUTO_{res.get('mode', 'SIM')}",
                        notes=f"Auto-Executed Setup #{setup_id}",
                    )
                except Exception as _rj_err:
                    print("Record journal warning:", _rj_err)

                layers_txt = "\n".join(
                    f"  - Layer {l['layer']}: {l['order_type']} @ {l['price']} | Lot: {l['lot']} | SL: {l['sl']} | TP: {l['tp']}"
                    for l in res["plan"]["layers"]
                )
                send_telegram(
                    f"⚡ AUTO-EXECUTE INTRADAY AKTIF — {sym} (Setup #{setup_id} | Skor {final_score}/100)\n"
                    f"Mode: {res['mode']} | {quota_msg}\n"
                    f"{layers_txt}\n"
                    f"Status: {res['message']}"
                )

        summary_parts.append(f"{sym} Score {final_score}/100: {json.dumps(refined_bundle.get('setups', {}))[:450]}")

    LATEST_CONTEXT_CACHE["updated"] = datetime.datetime.now(WIB).strftime("%Y-%m-%d %H:%M WIB")
    LATEST_CONTEXT_CACHE["summary"] = "\n".join(summary_parts)

# =============================================================================
# WATCHER BERITA REAL-TIME, PIDATO LIVE & INTERAKSI TELEGRAM
# =============================================================================
def auto_breaking_news_watcher_loop():
    initial = feeds.fetch_realtime_breaking_news(limit=10)
    for item in initial:
        is_news_seen_and_mark(item["hash"], item["source"], item["title"])

    while True:
        time.sleep(NEWS_POLL_INTERVAL_SEC)
        try:
            latest_items = feeds.fetch_realtime_breaking_news(limit=8)
            fresh_batch = []
            for item in latest_items:
                if not is_news_seen_and_mark(item["hash"], item["source"], item["title"]):
                    fresh_batch.append(item)

            if fresh_batch:
                raw_lines = "\n".join(f"- [{b['source']}] {b['title']}" for b in fresh_batch)
                prompt = (
                    f"BREAKING NEWS DADAKAN BARU TERDETEKSI:\n{raw_lines}\n\n"
                    f"1. Terjemahkan ke Bahasa Indonesia secara singkat dan jelas.\n"
                    f"2. Sebutkan apakah sentimennya BULLISH atau BEARISH untuk XAUUSD (Emas), DXY, dan BTCUSDT."
                )
                ai_note, eng = ask_ai_router(prompt, deep=False)
                now_str = datetime.datetime.now(WIB).strftime("%H:%M:%S WIB")
                alert_msg = (
                    f"🚨 BREAKING NEWS OTOMATIS ({now_str})\n\n"
                    f"{raw_lines}\n\n"
                    f"📊 DAMPAK PASAR ({eng}):\n{ai_note}"
                )
                send_telegram(alert_msg)
        except Exception as e:
            print("News watcher error:", e)


def live_speech_continuous_worker(youtube_url: str):
    SPEECH_WATCH_STATE["active"] = True
    SPEECH_WATCH_STATE["url"] = youtube_url
    SPEECH_WATCH_STATE["last_tail"] = ""

    send_telegram(
        "🎙️ MONITOR PIDATO LIVE AKTIF (Otomatis tiap 45 detik)\n"
        "Sistem akan otomatis menerjemahkan setiap ucapan baru The Fed / Trump dan menyimpulkan arah XAUUSD & BTCUSDT.\n"
        "Ketik /stop_pidato kapan saja untuk menghentikan pantauan."
    )

    empty_streak = 0
    for _ in range(120):
        if not SPEECH_WATCH_STATE["active"]:
            break

        _, current_text = feeds.fetch_live_speech_transcript(youtube_url, tail_count=45)
        if not current_text:
            empty_streak += 1
            if empty_streak >= 8:
                send_telegram("⏹️ Siaran pidato berakhir atau subtitle live tidak lagi tersedia. Monitor pidato dihentikan.")
                break
            time.sleep(45)
            continue

        empty_streak = 0
        if current_text != SPEECH_WATCH_STATE["last_tail"]:
            SPEECH_WATCH_STATE["last_tail"] = current_text
            snippet = current_text[-1200:]
            prompt = (
                f"Potongan ucapan terbaru dari pidato live The Fed / Trump:\n\"{snippet}\"\n\n"
                f"1. Terjemahkan inti ucapan ini ke Bahasa Indonesia (2-3 kalimat padat).\n"
                f"2. Klasifikasikan nada: HAWKISH (USD naik, XAUUSD turun) atau DOVISH (USD turun, XAUUSD naik) atau NETRAL.\n"
                f"3. Arah proyeksi XAUUSD & BTCUSDT saat pidato berlanjut/selesai."
            )
            ai_summary, eng = ask_ai_router(prompt, deep=False)
            now_str = datetime.datetime.now(WIB).strftime("%H:%M:%S WIB")
            send_telegram(f"🎙️ UPDATE PIDATO LIVE ({now_str} | {eng}):\n\n{ai_summary or snippet[:600]}")

        time.sleep(45)

    SPEECH_WATCH_STATE["active"] = False


def handle_status_api():
    send_telegram("Memeriksa status & latensi seluruh API dan Model AI Flagship...")
    statuses = feeds.check_all_api_statuses(ai_router)
    lines = [f"STATUS KONEKSI API & AI REAL-TIME ({datetime.datetime.now(WIB).strftime('%H:%M WIB')})\n"]
    for item in statuses:
        lines.append(f"{item['status']} {item['name']} ({item['latency']})\n   └ {item['detail']}")
    send_telegram("\n".join(lines))


def handle_breaking_news():
    headlines = feeds.fetch_realtime_breaking_news(limit=8)
    blackout, active_ev = feeds.check_usd_news_blackout()
    if not headlines:
        send_telegram("Belum ada headline breaking news baru yang tertangkap dari feed RSS.")
        return

    raw_text = "\n".join(f"- [{h['source']}] {h['title']}" for h in headlines)
    prompt = (
        f"Ini adalah headline breaking news real-time terbaru:\n{raw_text}\n\n"
        f"Terjemahkan poin terpenting ke Bahasa Indonesia dan simpulkan dalam 3 bullet singkat "
        f"arah dampaknya terhadap XAUUSD (Emas), DXY, dan BTCUSDT."
    )
    ai_summary, eng = ask_ai_router(prompt, deep=False)
    header = (
        f"RADAR BREAKING NEWS REAL-TIME ({datetime.datetime.now(WIB).strftime('%H:%M WIB')})\n"
        f"Rem Darurat News USD (±45m): {'AKTIF ' + str(active_ev) if blackout else 'AMAN'}\n\n"
        f"{raw_text}\n\n"
        f"ANALISA DAMPAK HARGA ({eng}):\n{ai_summary}"
    )
    send_telegram(header)


def handle_speech_transcript(url_arg: str):
    if not url_arg:
        send_telegram("Kirim format: /transkrip <link_youtube> atau /pantau_pidato <link_youtube>")
        return
    send_telegram("Menyedot transkrip live pidato & menganalisis arah pergerakan XAUUSD/BTCUSDT...")
    _, transcript = feeds.fetch_live_speech_transcript(url_arg, tail_count=60)
    if not transcript:
        send_telegram("Transkrip belum tersedia pada video tersebut (pastikan Closed Captions/CC YouTube aktif).")
        return

    prompt = (
        f"Berikut potongan transkrip langsung pidato The Fed / pejabat AS:\n\"{transcript[:3500]}\"\n\n"
        f"1. Terjemahkan inti pernyataan terpenting ke Bahasa Indonesia.\n"
        f"2. Tentukan apakah nadanya HAWKISH (USD naik, XAUUSD turun) atau DOVISH (USD turun, XAUUSD naik).\n"
        f"3. Berikan proyeksi pergerakan setelah pidato selesai untuk XAUUSD dan BTCUSDT."
    )
    analysis, eng = ask_ai_router(prompt, deep=True)
    send_telegram(f"TRANSKRIP & ANALISA PIDATO LIVE ({eng}):\n\n{analysis or transcript[:1500]}")


def process_callback_query(cb: dict):
    cb_id = cb["id"]
    data = cb.get("data", "")
    from_id = str(cb.get("from", {}).get("id", ""))

    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
        json={"callback_query_id": cb_id, "text": "Memproses perintah..."},
        timeout=8,
    )

    if TELEGRAM_CHAT_ID and from_id != TELEGRAM_CHAT_ID:
        return

    parts = data.split("_")
    if len(parts) == 2 and parts[0] == "reject":
        sid = int(parts[1])
        update_setup_status(sid, "REJECTED")
        send_telegram(f"❌ Setup #{sid} DITOLAK & diabaikan.")
        return

    if len(parts) == 3 and parts[0] == "approve":
        horizon_code = parts[1].upper()
        sid = int(parts[2])
        record = get_setup_bundle(sid)
        if not record:
            send_telegram(f"⚠️ Data Setup #{sid} tidak ditemukan di database.")
            return

        setups = record["bundle"].get("setups", {})
        horizon_key_map = {
            "SCALP": "SESI / SCALP (M1-M5-M15)",
            "INTRADAY": "INTRADAY (M1-M5-M15-M30-H1-H4)",
            "SWING": "SWING / POSITION (M1-M5-M15-M30-H1-H4-D1-W1-1MON)",
        }
        chosen_key = horizon_key_map.get(horizon_code, "")
        chosen_setup = setups.get(chosen_key)
        if not chosen_setup:
            send_telegram(f"⚠️ Horizon {horizon_code} pada Setup #{sid} kosong.")
            return

        res = exec_engine.execute_horizon_setup(record["symbol"], horizon_code, chosen_setup)
        update_setup_status(sid, f"APPROVED_{horizon_code}")
        live_px = float(record["bundle"].get("live_price", 0.0))
        try:
            record_trade_journal(
                symbol=record["symbol"],
                horizon=horizon_code,
                direction=chosen_setup.get("direction", "BUY"),
                entry=_parse_first_float(chosen_setup.get("entry"), live_px),
                sl=_parse_first_float(chosen_setup.get("sl"), live_px),
                tp1=_parse_first_float(chosen_setup.get("tp1"), live_px),
                tp2=_parse_first_float(chosen_setup.get("tp2"), live_px),
                tp3=_parse_first_float(chosen_setup.get("tp3"), live_px),
                score=0,
                mode=f"MANUAL_{res.get('mode', 'SIM')}",
                notes=f"Approved via Telegram Button #{sid}",
            )
        except Exception:
            pass
        layers_txt = "\n".join(
            f"  - Layer {l['layer']}: {l['order_type']} @ {l['price']} | Lot: {l['lot']} | SL: {l['sl']} | TP: {l['tp']}"
            for l in res["plan"]["layers"]
        )
        send_telegram(
            f"✅ EKSEKUSI {horizon_code} DISETUJUI — {record['symbol']} (Setup #{sid})\n"
            f"Mode: {res['mode']}\n"
            f"{layers_txt}\n\n"
            f"Status: {res['message']}"
        )


def telegram_polling_loop():
    if not TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN kosong, listener tidak dijalankan.")
        return

    base_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    try:
        requests.get(f"{base_url}/deleteWebhook", params={"drop_pending_updates": False}, timeout=10)
    except Exception:
        pass

    offset = 0
    while True:
        try:
            resp = requests.get(f"{base_url}/getUpdates", params={"offset": offset, "timeout": 30}, timeout=35).json()
            for item in resp.get("result", []):
                offset = item["update_id"] + 1

                if "callback_query" in item:
                    threading.Thread(target=process_callback_query, args=(item["callback_query"],), daemon=True).start()
                    continue

                msg = item.get("message", {})
                text = msg.get("text", "").strip()
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if not text or (TELEGRAM_CHAT_ID and chat_id != TELEGRAM_CHAT_ID):
                    continue

                low = text.lower()
                if low in ("/start", "/help"):
                    send_telegram(
                        "🤖 QUANT AGENTLANG 24/7 AKTIF (8 Pilar + QuantIntel + RiskEngine)\n\n"
                        "Fitur Otomatis Berjalan 24/7:\n"
                        "• 🏛️ Laporan Konfirmasi 8 Pilar Intermarket + OHLC Pivot + GARCH/CVaR + CFTC COT\n"
                        "• 🚨 Auto Breaking News Push (Financial Juice, ForexLive, Fed, White House tiap 60 detik)\n"
                        "• 📊 Auto Mapping M1-1MON & 3 Tombol Approve (Scalp / Intraday / Swing)\n\n"
                        "Perintah Manual:\n"
                        "• /report atau /cek — Scan M1-1MON + 8 Pilar sekarang\n"
                        "• /status_api — Cek status & latensi semua API + Model AI\n"
                        "• /news — Rangkuman breaking news terbaru\n"
                        "• /pantau_pidato <link_youtube> — Auto-translate pidato live Fed/Trump tiap 45 detik\n"
                        "• /stop_pidato — Hentikan pantauan pidato live\n"
                        "• Chat bebas kapan saja — Ketik pertanyaan apa saja untuk ngobrol langsung dengan AI!"
                    )
                elif low in ("/report", "/cek"):
                    send_telegram("⏳ Memindai 9 timeframe (M1-1MON), 8 Pilar Intermarket, OHLC Pivot & GARCH/CVaR...")
                    threading.Thread(
                        target=build_and_send_institutional_report, args=("Manual /report",), daemon=True
                    ).start()
                elif low == "/status_api":
                    threading.Thread(target=handle_status_api, daemon=True).start()
                elif low == "/news":
                    threading.Thread(target=handle_breaking_news, daemon=True).start()
                elif low.startswith("/pantau_pidato"):
                    arg = text[len("/pantau_pidato") :].strip()
                    if not arg:
                        send_telegram("Kirim format: /pantau_pidato <link_youtube_live_fed_atau_trump>")
                    else:
                        SPEECH_WATCH_STATE["active"] = False
                        time.sleep(1)
                        threading.Thread(target=live_speech_continuous_worker, args=(arg,), daemon=True).start()
                elif low == "/stop_pidato":
                    SPEECH_WATCH_STATE["active"] = False
                    send_telegram("⏹️ Pantauan pidato live dihentikan.")
                elif low.startswith("/transkrip"):
                    arg = text[len("/transkrip") :].strip()
                    threading.Thread(target=handle_speech_transcript, args=(arg,), daemon=True).start()

                elif low.startswith("/backtest"):
                    sym_arg = "BTCUSDT" if "btc" in low else "XAUUSD"
                    send_telegram(f"⏳ Menjalankan Out-of-Sample Backtest & 1.000x Monte Carlo untuk {sym_arg}...")
                    threading.Thread(
                        target=lambda s=sym_arg: send_telegram(run_backtest_and_monte_carlo(s, 1000)),
                        daemon=True,
                    ).start()
                elif low in ("/jurnal", "/evaluasi"):
                    send_telegram(get_journal_and_evaluation())
                elif low == "/skills":
                    skills_txt = "\n".join(
                        f"• {k} ({v['tf_focus']} | Min RR 1:{v['min_rr']}): {v['rules']}"
                        for k, v in TRADING_SKILLS.items()
                    )
                    send_telegram(f"📚 5 PLAYBOOK TRADING_SKILLS AKTIF:\n\n{skills_txt}")
                elif low == "/auto_on":
                    globals()["AUTO_EXECUTE_ENABLED"] = True
                    send_telegram("⚡ AUTO-EXECUTE 24/7 DIAKTIFKAN. Setup yang lolos filter & sidang 4-Agen akan langsung dieksekusi.")
                elif low == "/auto_off":
                    globals()["AUTO_EXECUTE_ENABLED"] = False
                    send_telegram("🛑 AUTO-EXECUTE DIMATIKAN. Mode berubah ke Semi-Otomatis (menunggu klik tombol Approve).")
                elif low == "/audit":
                    if outcome_verifier is not None:
                        summary_audit = None
                        for m_name in ("verify_all_pending_and_approved", "verify_pending", "run_verification", "verify_all"):
                            if hasattr(outcome_verifier, m_name):
                                summary_audit = getattr(outcome_verifier, m_name)()
                                break
                        send_telegram(f"🔍 HASIL AUDIT AKURASI SINYAL:\n{json.dumps(summary_audit, indent=2, default=str)}")
                    else:
                        send_telegram("Modul outcome_verifier.py belum aktif.")
                elif low.startswith("/hub"):
                    if unified_hub is None:
                        send_telegram("Modul quant_unified_hub.py belum aktif.")
                    else:
                        sym_hub = "BTCUSDT" if "btc" in low else "XAUUSD"
                        send_telegram(f"⏳ Menjalankan Unified Hub 360° (SMC + Quant + MM + 4-Agent Jury) untuk {sym_hub}...")
                        def run_hub_cmd(s=sym_hub):
                            rep = unified_hub.run_unified_analysis(
                                symbol=s,
                                feeds=feeds,
                                ai_router=ai_router,
                                account_balance=exec_engine.balance,
                            )
                            send_telegram(unified_hub.format_institutional_brief(rep))
                        threading.Thread(target=run_hub_cmd, daemon=True).start()
                else:
                    def reply_chat(q=text):
                        if LATEST_CONTEXT_CACHE["updated"] == "Belum ada scan":
                            snap = feeds.collect_all_8_pillars()
                            LATEST_CONTEXT_CACHE["summary"] = snap["telemetry_summary"]
                        if hasattr(ai_router, "chat_live"):
                            ans, eng = ai_router.chat_live(q, LATEST_CONTEXT_CACHE["summary"])
                        else:
                            chat_prompt = f"Konteks Pasar Terkini:\n{LATEST_CONTEXT_CACHE['summary']}\n\nPertanyaan User: {q}"
                            ans, eng = ask_ai_router(chat_prompt, deep=False)
                        send_telegram(f"{ans}\n\n[{eng}]")

                    threading.Thread(target=reply_chat, daemon=True).start()
        except Exception as e:
            print("Polling error:", e)
            time.sleep(4)


def scheduled_loop():
    time.sleep(10)
    while True:
        try:
            build_and_send_institutional_report("Auto-Scan 24/7")
        except Exception as e:
            print("Scheduler error:", e)
        time.sleep(AUTO_SCAN_INTERVAL_SEC)


def drain_telegram_updates_briefly(duration_sec: int = 25):
    if not TELEGRAM_BOT_TOKEN:
        return
    base_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    offset = 0
    deadline = time.time() + duration_sec
    while time.time() < deadline:
        try:
            resp = requests.get(f"{base_url}/getUpdates", params={"offset": offset, "timeout": 5}, timeout=8).json()
            for item in resp.get("result", []):
                offset = item["update_id"] + 1
                if "callback_query" in item:
                    process_callback_query(item["callback_query"])
        except Exception:
            pass
        time.sleep(1)


if __name__ == "__main__":
    init_db()
    init_audit_db()
    if "--report" in sys.argv:
        build_and_send_institutional_report("GitHub Actions Scheduled")
        drain_telegram_updates_briefly(25)
    else:
        register_webhook_callback(
            lambda payload: send_telegram(f"📡 TRADINGVIEW WEBHOOK MASUK:\n{json.dumps(payload, indent=2)}")
        )
        start_web_bridge_in_background()
        threading.Thread(target=scheduled_loop, daemon=True).start()
        threading.Thread(target=auto_breaking_news_watcher_loop, daemon=True).start()
        telegram_polling_loop()
