import os
import sys
import html
import time
import json
import sqlite3
import datetime
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytz
import requests
from dotenv import load_dotenv

from ai_router import UnifiedAIRouter
from market_feeds import InstitutionalMarketFeeds
from smc_alpha import InstitutionalSMCAlphaEngine

# Muat variabel dari file .env jika dijalankan secara lokal / Google Colab
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
ACCOUNT_BALANCE = float(os.getenv("ACCOUNT_BALANCE", "10000"))
RISK_PER_TRADE_PCT = float(os.getenv("RISK_PER_TRADE_PCT", "1.0"))
MAX_ENTRIES_PER_SESSION = int(os.getenv("MAX_ENTRIES_PER_SESSION", "3"))

WIB = pytz.timezone("Asia/Jakarta")
DB_PATH = "quant_cloud_audit.db"

# Satu router AI terpadu untuk Report Otomatis & Co-Pilot Live Chat
ai_router = UnifiedAIRouter()
feeds = InstitutionalMarketFeeds()
alpha_engine = InstitutionalSMCAlphaEngine()


# =====================================================================
# 1. DATABASE AUDIT & MANAJEMEN KUOTA SESI
# =====================================================================
def init_audit_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS signal_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at_wib TEXT,
            session_name TEXT,
            ai_model TEXT,
            xau_price REAL,
            btc_price REAL,
            macro_bias TEXT,
            audit_passed INTEGER,
            status TEXT,
            payload_json TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def save_signal_record(session_name: str, ai_model: str, xau_price: float, btc_price: float,
                       macro_bias: str, audit_passed: bool, payload: dict) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    now_str = datetime.datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        """
        INSERT INTO signal_audit_log
        (created_at_wib, session_name, ai_model, xau_price, btc_price, macro_bias, audit_passed, status, payload_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now_str,
            session_name,
            ai_model,
            xau_price,
            btc_price,
            macro_bias,
            1 if audit_passed else 0,
            "PENDING_REVIEW" if audit_passed else "BLOCKED_BY_GATE",
            json.dumps(payload),
        ),
    )
    record_id = cur.lastrowid
    conn.commit()
    conn.close()
    return record_id


def update_signal_status(record_id: int, new_status: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE signal_audit_log SET status = ? WHERE id = ?", (new_status, record_id))
    conn.commit()
    conn.close()


def get_session_quota_info() -> tuple[str, int]:
    now_wib = datetime.datetime.now(WIB)
    hour = now_wib.hour
    if 7 <= hour < 14:
        session = "ASIA_RANGE (07:00-14:00 WIB)"
    elif 14 <= hour < 19:
        session = "LONDON_KILLZONE (14:00-19:00 WIB)"
    elif 19 <= hour <= 23:
        session = "NEW_YORK_KILLZONE (19:00-24:00 WIB)"
    else:
        session = "DEAD_ZONE_ROLLOVER (00:00-07:00 WIB)"

    today_prefix = now_wib.strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM signal_audit_log WHERE created_at_wib LIKE ? AND session_name = ? AND status = 'APPROVED'",
        (f"{today_prefix}%", session),
    )
    used = cur.fetchone()[0]
    conn.close()
    remaining = max(0, MAX_ENTRIES_PER_SESSION - used)
    return session, remaining


# =====================================================================
# 2. PENGIRIM PESAN TELEGRAM (HTML + ANTI-ERROR FALLBACK)
# =====================================================================
def send_telegram_message(text: str, chat_id: str = None, reply_markup: dict = None):
    target_chat = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not target_chat:
        print("[PERINGATAN] TELEGRAM_BOT_TOKEN atau TELEGRAM_CHAT_ID belum diisi.")
        print(text)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    # Telegram membatasi 4096 karakter per pesan; pecah otomatis jika melebihi batas
    chunks = [text[i: i + 3900] for i in range(0, len(text), 3900)]

    for idx, chunk in enumerate(chunks):
        payload = {
            "chat_id": target_chat,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup and idx == len(chunks) - 1:
            payload["reply_markup"] = reply_markup

        try:
            resp = requests.post(url, json=payload, timeout=20)
            if resp.status_code == 400:
                # Jika ada tag HTML tidak valid, kirim ulang sebagai teks biasa agar tetap masuk
                payload.pop("parse_mode", None)
                requests.post(url, json=payload, timeout=20)
        except Exception as exc:
            print(f"[ERROR TELEGRAM] Gagal mengirim pesan: {exc}")


# =====================================================================
# 3. PEMFORMAT MAPPING SNIPER (M1 -> 1MON MONTHLY)
# =====================================================================
def _format_asset_mapping(asset_title: str, bundle: dict) -> str:
    live = bundle.get("live_price", 0.0)
    setups = bundle.get("setups", {})
    risk_usd = round(ACCOUNT_BALANCE * (RISK_PER_TRADE_PCT / 100.0), 2)

    blocks = [f"<b>{html.escape(asset_title)} (Live: {live}) — Risk/Trade: ${risk_usd} ({RISK_PER_TRADE_PCT}%)</b>"]
    for horizon_label, s in setups.items():
        blocks.append(
            f"\n🎯 <b>{html.escape(horizon_label)}</b> — [<b>{s['direction']}</b>] (RR: 1:{s['rr']})\n"
            f"• Rantai MTF: <code>{html.escape(s['tf_chain'])}</code> ({s['aligned_count']})\n"
            f"• Sniper Limit Entry (1-3 Layer): <code>{s['entry']}</code>\n"
            f"• Tight Stop Loss: <code>{s['sl']}</code>\n"
            f"• TP1 (Geser SL ke BEP+1): <code>{s['tp1']}</code>\n"
            f"• TP2 / TP3 Runner: <code>{s['tp2']}</code> | <code>{s['tp3']}</code>"
        )
    return "\n".join(blocks)


# =====================================================================
# 4. GENERATOR LAPORAN UTAMA (8 PILAR + M1-1MON CASCADE + AUDIT GATE)
# =====================================================================
def build_and_send_institutional_report(target_chat_id: str = None):
    now_str = datetime.datetime.now(WIB).strftime("%d %b %Y %H:%M WIB")
    session_name, quota_left = get_session_quota_info()

    # Langkah A: Tarik seluruh 8 Pilar Data sebelum menyentuh chart/orderflow
    intel = feeds.collect_all_8_pillars()
    macro = intel["macro"]
    matrix = intel["matrix"]
    dom = intel["crypto_dom"]
    deriv = intel["deriv_onchain"]
    cal = intel["calendar_narr"]

    macro_bias = "BEARISH" if (macro["net_liquidity_b"] < 0 and macro["dfii10"] > 2.0) else "BULLISH"

    # Langkah B: Jalankan Multi-Timeframe Cascade (M1 Menit 1 -> 1MON Monthly)
    xau_bundle = alpha_engine.build_cascade_setups("XAUUSD", feeds, macro_bias)
    btc_bundle = alpha_engine.build_cascade_setups("BTCUSD", feeds, macro_bias)

    # Langkah C: Pre-Send Verification Gate (Cek 6 syarat keamanan)
    xau_score = 84 if macro_bias == "BEARISH" else 80
    btc_score = 80 if deriv["stablecoin_7d_flow_b"] <= 0 else 78

    xau_ok, xau_err = alpha_engine.verify_setup_before_send(xau_bundle, cal["news_blackout_active"], xau_score)
    btc_ok, btc_err = alpha_engine.verify_setup_before_send(btc_bundle, cal["news_blackout_active"], btc_score)
    all_ok = xau_ok and btc_ok

    # Langkah D: Ringkasan Analitis via UnifiedAIRouter (Claude -> Gemini 4 Hydrogen -> Flash)
    sys_prompt = (
        "Anda adalah Institutional Quant Strategist. Tulis analisis 3 poin padat dalam Bahasa Indonesia "
        "(1. Makro & Likuiditas, 2. Konfluensi Intermarket, On-Chain & Orderflow, 3. Skeptic Veto & Risiko Pembatalan). "
        "Jangan gunakan simbol < atau >."
    )
    usr_prompt = (
        f"Sesi: {session_name}. Makro: NetLiq={macro['net_liquidity_b']}B, DFII10={macro['dfii10']}%, "
        f"US02Y={macro['us02y']}%, US10Y={macro['us10y']}%, FEDFUNDS={macro['fedfunds']}%, "
        f"DXY={matrix['DXY']['price']}, USOIL={matrix['USOIL']['price']}, NDX={matrix['NASDAQ100']['price']}, "
        f"SPX={matrix['SP500']['price']}, VIX={matrix['VIX']['price']}. "
        f"Crypto Dom: TOTAL={dom['TOTAL_B']}B, TOTAL2={dom['TOTAL2_B']}B, TOTAL3={dom['TOTAL3_B']}B, "
        f"BTC.D={dom['BTC_D']}%, USDT.D={dom['USDT_D']}%. "
        f"Derivatif/On-Chain: Funding={deriv['funding_rate_pct']}%, OI={deriv['open_interest_btc']}, "
        f"L/S={deriv['long_short_ratio']}, Stable7D={deriv['stablecoin_7d_flow_b']}B, "
        f"FnG={deriv['fear_greed_value']} ({deriv['fear_greed_label']}). "
        f"Kalender Merah: {cal['high_impact_upcoming']}. Narasi: {cal['narrative_watch']}."
    )
    narrative_raw, active_model = ai_router.generate(sys_prompt, usr_prompt, max_tokens=750)
    narrative_safe = html.escape(narrative_raw)

    # Simpan ke database audit
    record_id = save_signal_record(
        session_name=session_name,
        ai_model=active_model,
        xau_price=xau_bundle.get("live_price", 0.0),
        btc_price=btc_bundle.get("live_price", 0.0),
        macro_bias=macro_bias,
        audit_passed=all_ok,
        payload={"xau": xau_bundle, "btc": btc_bundle},
    )

    audit_status = (
        "✅ TERVERIFIKASI AMAN (6/6 Audit Gate Lolos)"
        if all_ok
        else f"⚠️ DITAHAN AUDIT GATE: {html.escape(', '.join(xau_err + btc_err))}"
    )

    report_header = (
        f"🏛 <b>AGENT QUANT LANG - SNIPER ENTRY INSTITUTIONAL ORDERFLOW</b>\n"
        f"🕒 {now_str}\n"
        f"🧠 AI Engine: <code>{html.escape(active_model)}</code>\n"
        f"🌐 Sesi: {session_name} | Kuota Sesi: <b>{quota_left}/{MAX_ENTRIES_PER_SESSION}</b>\n"
        f"📡 Telemetry API: {html.escape(intel['telemetry_summary'])}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>RINGKASAN KONFIRMASI &amp; 8 PILAR INTERMARKET</b>\n"
        f"🥇 <b>XAUUSD (Live: {xau_bundle.get('live_price')})</b> — Bias: <b>{macro_bias}</b> | Skor: <b>{xau_score}/100</b>\n"
        f"₿ <b>BTCUSDT (Live: {btc_bundle.get('live_price')})</b> — Bias: <b>{macro_bias}</b> | Skor: <b>{btc_score}/100</b>\n\n"
        f"• <b>Makro &amp; Yields:</b> NetLiq <code>${macro['net_liquidity_b']}B</code> | DFII10 <code>{macro['dfii10']}%</code> | "
        f"US02Y <code>{macro['us02y']}%</code> | US10Y <code>{macro['us10y']}%</code> | FEDFUNDS <code>{macro['fedfunds']}%</code>\n"
        f"• <b>Lawan Pair:</b> DXY <code>{matrix['DXY']['price']}</code> | USOIL <code>{matrix['USOIL']['price']}</code> | "
        f"NDX <code>{matrix['NASDAQ100']['price']}</code> | SPX <code>{matrix['SP500']['price']}</code> | VIX <code>{matrix['VIX']['price']}</code>\n"
        f"• <b>Crypto Cap &amp; Dom:</b> TOTAL <code>${dom['TOTAL_B']}B</code> | TOTAL2 <code>${dom['TOTAL2_B']}B</code> | "
        f"TOTAL3 <code>${dom['TOTAL3_B']}B</code> | BTC.D <code>{dom['BTC_D']}%</code> | USDT.D <code>{dom['USDT_D']}%</code>\n"
        f"• <b>On-Chain &amp; Orderflow:</b> Stable7D <code>${deriv['stablecoin_7d_flow_b']}B</code> | "
        f"Funding <code>{deriv['funding_rate_pct']}%</code> | L/S <code>{deriv['long_short_ratio']}</code> | "
        f"FnG <code>{deriv['fear_greed_value']} ({deriv['fear_greed_label']})</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{narrative_safe}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{_format_asset_mapping('🥇 MAPPING SNIPER XAUUSD (ZERO FLOATING)', xau_bundle)}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{_format_asset_mapping('₿ MAPPING SNIPER BTCUSDT (ZERO FLOATING)', btc_bundle)}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🛡 <b>Pre-Send Verification Gate (ID #{record_id}):</b> {audit_status}"
    )

    inline_buttons = {
        "inline_keyboard": [
            [
                {"text": "✅ Approve Sniper Setup", "callback_data": f"approve_{record_id}"},
                {"text": "❌ Reject / Skip", "callback_data": f"reject_{record_id}"},
            ]
        ]
    }
    send_telegram_message(report_header, chat_id=target_chat_id, reply_markup=inline_buttons)


# =====================================================================
# 5. CO-PILOT LIVE CHAT (MEMAKAI UNIFIED AI ROUTER YANG SAMA)
# =====================================================================
def handle_copilot_live_chat(user_message: str) -> str:
    session_name, quota_left = get_session_quota_info()
    sys_prompt = (
        "Anda adalah QUANT CO-PILOT LIVE untuk sistem AGENT QUANT LANG - SNIPER ENTRY INSTITUTIONAL ORDERFLOW. "
        "Anda selalu menggunakan Multi-Timeframe dari M1 (Menit 1) membesar ke 1MON (1 Bulan), SMC, Orderflow, "
        "dan manajemen risiko ketat 0.5% - 2% per trade. Jawab ringkas, tajam, dan terstruktur dalam Bahasa Indonesia."
    )
    context_prompt = (
        f"[Konteks Sesi Aktif: {session_name} | Sisa Kuota Sesi: {quota_left}/{MAX_ENTRIES_PER_SESSION}]\n"
        f"Pesan CEO/Trader: {user_message}"
    )
    reply_raw, active_model = ai_router.generate(sys_prompt, context_prompt, max_tokens=900)
    reply_safe = html.escape(reply_raw)

    return (
        f"💬 <b>AGENT QUANT LANG — CO-PILOT LIVE</b> (<code>{html.escape(active_model)}</code>)\n"
        f"🌐 Sesi: {session_name} | Kuota: <b>{quota_left}/{MAX_ENTRIES_PER_SESSION}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{reply_safe}"
    )


# =====================================================================
# 6. LISTENER TELEGRAM 24/7 & HEALTH CHECK SERVER (UNTUK COLAB / CLOUD)
# =====================================================================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"AGENT QUANT LANG 24/7 ONLINE")

    def log_message(self, format, *args):
        return


def start_health_server():
    port = int(os.getenv("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"[SERVER] Health-check server aktif di port {port}")


def run_247_polling_and_scheduler():
    if not TELEGRAM_BOT_TOKEN:
        print("[ERROR] TELEGRAM_BOT_TOKEN kosong. Isi di .env atau Secrets terlebih dahulu.")
        return

    offset = 0
    last_sent_slot = ""
    print("[BOT] AGENT QUANT LANG 24/7 aktif mendengarkan Telegram & jadwal sesi...")

    while True:
        # Cek jadwal laporan otomatis (08:00 Asia, 14:00 London, 19:00 New York WIB)
        now_wib = datetime.datetime.now(WIB)
        slot_key = now_wib.strftime("%Y-%m-%d_%H")
        if now_wib.hour in (8, 14, 19) and now_wib.minute == 0 and slot_key != last_sent_slot:
            last_sent_slot = slot_key
            try:
                build_and_send_institutional_report()
            except Exception as exc:
                print(f"[ERROR REPORT] {exc}")

        # Polling pesan masuk & klik tombol Telegram
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            resp = requests.get(url, params={"offset": offset, "timeout": 25}, timeout=30).json()
            for update in resp.get("result", []):
                offset = update["update_id"] + 1

                # 1. Jika tombol Approve / Reject ditekan
                if "callback_query" in update:
                    cb = update["callback_query"]
                    cb_data = cb.get("data", "")
                    cb_chat = str(cb["message"]["chat"]["id"])
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                        json={"callback_query_id": cb["id"]},
                        timeout=10,
                    )
                    if cb_data.startswith("approve_"):
                        rec_id = int(cb_data.split("_")[1])
                        update_signal_status(rec_id, "APPROVED")
                        send_telegram_message(
                            f"✅ <b>Setup #{rec_id} DISETUJUI.</b> Pasang Limit Order sesuai zona M1-1MON & aktifkan auto BEP+1 saat TP1 tersentuh.",
                            chat_id=cb_chat,
                        )
                    elif cb_data.startswith("reject_"):
                        rec_id = int(cb_data.split("_")[1])
                        update_signal_status(rec_id, "REJECTED")
                        send_telegram_message(
                            f"❌ <b>Setup #{rec_id} DILEWATI.</b> Kuota sesi tetap utuh dan aman.",
                            chat_id=cb_chat,
                        )

                # 2. Jika ada pesan teks masuk (Perintah / Live Chat Co-Pilot)
                elif "message" in update and "text" in update["message"]:
                    msg_text = update["message"]["text"].strip()
                    chat_id = str(update["message"]["chat"]["id"])

                    if msg_text.lower() in ("/report", "report", "analisa", "sinyal"):
                        send_telegram_message("⏳ Menarik 8 Pilar Data & memindai Cascade M1 → 1MON...", chat_id=chat_id)
                        build_and_send_institutional_report(target_chat_id=chat_id)
                    else:
                        reply = handle_copilot_live_chat(msg_text)
                        send_telegram_message(reply, chat_id=chat_id)

        except Exception as exc:
            print(f"[POLLING RETRY] {exc}")
            time.sleep(3)


# =====================================================================
# 7. ENTRY POINT UTAMA
# =====================================================================
if __name__ == "__main__":
    init_audit_db()

    # Mode sekali jalan untuk GitHub Actions (Cron Job) atau argumen --report
    is_github_actions = os.getenv("GITHUB_ACTIONS", "").lower() == "true"
    is_report_flag = "--report" in sys.argv or os.getenv("RUN_MODE", "").lower() == "report"

    if is_github_actions or is_report_flag:
        print("[MODE] Menjalankan Laporan Institusional Sekali Jalan...")
        build_and_send_institutional_report()
        print("[SELESAI] Laporan berhasil diproses.")
    else:
        start_health_server()
        run_247_polling_and_scheduler()
