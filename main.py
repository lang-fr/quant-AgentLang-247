import os
import sys
import json
import time
import sqlite3
import threading
import datetime
import pytz
import requests
from dotenv import load_dotenv

from ai_router import FlagshipAIRouter
from market_feeds import InstitutionalMarketFeeds
from smc_alpha import InstitutionalSMCAlphaEngine
from execution_engine import InstitutionalExecutionEngine

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = str(os.getenv("TELEGRAM_CHAT_ID", "")).strip()
AUTO_SCAN_INTERVAL_SEC = int(os.getenv("AUTO_SCAN_INTERVAL_SEC", "14400"))
NEWS_POLL_INTERVAL_SEC = int(os.getenv("NEWS_POLL_INTERVAL_SEC", "60"))
DB_PATH = "quant_agentlang_247.db"
WIB = pytz.timezone("Asia/Jakarta")

ai_router = FlagshipAIRouter()
feeds = InstitutionalMarketFeeds()
alpha_engine = InstitutionalSMCAlphaEngine()
exec_engine = InstitutionalExecutionEngine()

LATEST_CONTEXT_CACHE = {"updated": "Belum ada scan", "summary": "Belum ada data pasar tersimpan."}
SPEECH_WATCH_STATE = {"active": False, "url": "", "last_tail": ""}


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


def get_setup_bundle(setup_id: int) -> dict | None:
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


def send_telegram(text: str, reply_markup: dict | None = None):
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


def format_asset_report(symbol: str, bundle: dict, score: int, engine_name: str, passed: bool, errors: list[str]) -> str:
    setups = bundle.get("setups", {})
    risk_usd = round(exec_engine.balance * (exec_engine.risk_pct / 100.0), 1)
    lines = [
        f"MAPPING SNIPER {symbol} (ZERO FLOATING) (Live: {bundle['live_price']} | {bundle['price_source']}) — Risk/Trade: ${risk_usd} ({exec_engine.risk_pct}%)",
        f"Skor Konfluensi: {score}/100 | Audit Gate: {'LOLOS 6/6' if passed else 'PERHATIAN: ' + ', '.join(errors)}",
        "",
    ]
    for horizon_title, st in setups.items():
        lines.extend(
            [
                f"🎯 {horizon_title} — [{st['direction']}] (RR: {st['rr']})",
                f"- Rantai MTF: {st['tf_chain']} ({st['aligned_count']})",
                f"- Sniper Limit Entry (1-3 Layer): {st['entry']}",
                f"- Tight Stop Loss: {st['sl']}",
                f"- TP1 (Geser SL ke BEP+1): {st['tp1']}",
                f"- TP2 / TP3 Runner: {st['tp2']} | {st['tp3']}",
                f"- Alasan Presisi: {st['reason']}",
                "",
            ]
        )
    lines.append(f"AI Engine Aktif: {engine_name}")
    return "\n".join(lines)


def build_and_send_institutional_report(trigger: str = "Scheduled"):
    intel = feeds.collect_all_8_pillars()
    blackout, news_list = feeds.check_usd_news_blackout()
    breaking = feeds.fetch_realtime_breaking_news(limit=5)

    dxy = intel["matrix"]["DXY"]["price"]
    macro_bias = "BULLISH" if (dxy == 0 or dxy < 104.0) else "BEARISH"

    summary_parts = [
        f"Update ({trigger}): {intel['telemetry_summary']}",
        f"News Blackout: {blackout} {news_list}",
        "Breaking Headlines: " + " | ".join(h["title"] for h in breaking[:3]),
    ]

    for sym in ("XAUUSD", "BTCUSDT"):
        raw_bundle = alpha_engine.build_cascade_setups(sym, feeds, macro_bias)
        refined_bundle, engine_name = alpha_engine.refine_setups_with_flagship_ai(sym, raw_bundle, intel, ai_router)
        score = alpha_engine.calculate_confluence_score(sym, intel, macro_bias, refined_bundle.get("mtf_ladder"))
        passed, errors = alpha_engine.verify_setup_before_send(refined_bundle, blackout, score)

        setup_id = save_setup_bundle(sym, refined_bundle["live_price"], score, refined_bundle)
        msg = format_asset_report(sym, refined_bundle, score, engine_name, passed, errors)

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
        summary_parts.append(f"{sym} Score {score}/100: {json.dumps(refined_bundle.get('setups', {}))[:450]}")

    LATEST_CONTEXT_CACHE["updated"] = datetime.datetime.now(WIB).strftime("%Y-%m-%d %H:%M WIB")
    LATEST_CONTEXT_CACHE["summary"] = "\n".join(summary_parts)


def auto_breaking_news_watcher_loop():
    """
    Berjalan 24/7 setiap 60 detik: mendeteksi berita dadakan baru dari Financial Juice,
    ForexLive, Federal Reserve, & White House, lalu otomatis mengirim terjemahan & dampak harga.
    """
    # Seed awal saat startup agar tidak membanjiri berita lama
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
                ai_note, eng = ai_router.generate_with_flagship_cascade(prompt)
                now_str = datetime.datetime.now(WIB).strftime("%H:%M:%S WIB")
                alert_msg = (
                    f"🚨 BREAKING NEWS OTOMATIS ({now_str})\n\n"
                    f"{raw_lines}\n\n"
                    f"📊 DAMPAK PASAR ({eng}):\n{ai_note or 'Pantau volatilitas XAUUSD & BTCUSDT.'}"
                )
                send_telegram(alert_msg)
        except Exception as e:
            print("News watcher error:", e)


def live_speech_continuous_worker(youtube_url: str):
    """
    Memantau pidato live The Fed / Trump secara otomatis tiap 45 detik, menerjemahkan
    kalimat baru, dan menyimpulkan arah pergerakan saat pidato berjalan maupun selesai.
    """
    SPEECH_WATCH_STATE["active"] = True
    SPEECH_WATCH_STATE["url"] = youtube_url
    SPEECH_WATCH_STATE["last_tail"] = ""

    send_telegram(
        "🎙️ MONITOR PIDATO LIVE AKTIF (Otomatis tiap 45 detik)\n"
        "Sistem akan otomatis menerjemahkan setiap ucapan baru The Fed / Trump dan menyimpulkan arah XAUUSD & BTCUSDT.\n"
        "Ketik /stop_pidato kapan saja untuk menghentikan pantauan."
    )

    empty_streak = 0
    max_cycles = 120  # Maks 90 menit
    for _ in range(max_cycles):
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
        prev_tail = SPEECH_WATCH_STATE["last_tail"]
        if current_text != prev_tail:
            SPEECH_WATCH_STATE["last_tail"] = current_text
            # Ambil bagian terbaru (~1200 karakter terakhir)
            snippet = current_text[-1200:]
            prompt = (
                f"Potongan ucapan terbaru dari pidato live The Fed / Trump:\n\"{snippet}\"\n\n"
                f"1. Terjemahkan inti ucapan ini ke Bahasa Indonesia (2-3 kalimat padat).\n"
                f"2. Klasifikasikan nada: HAWKISH (USD naik, XAUUSD turun) atau DOVISH (USD turun, XAUUSD naik) atau NETRAL.\n"
                f"3. Arah proyeksi XAUUSD & BTCUSDT saat pidato berlanjut/selesai."
            )
            ai_summary, eng = ai_router.generate_with_flagship_cascade(prompt)
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
    ai_summary, eng = ai_router.generate_with_flagship_cascade(prompt)
    header = (
        f"RADAR BREAKING NEWS REAL-TIME ({datetime.datetime.now(WIB).strftime('%H:%M WIB')})\n"
        f"Rem Darurat News USD (±45m): {'AKTIF ' + str(active_ev) if blackout else 'AMAN'}\n\n"
        f"{raw_text}\n\n"
    )
    if ai_summary:
        header += f"ANALISA DAMPAK HARGA ({eng}):\n{ai_summary}"
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
    analysis, eng = ai_router.generate_with_flagship_cascade(prompt)
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
                        "🤖 QUANT AGENTLANG 24/7 AKTIF (Auto Breaking News + Live Chat)\n\n"
                        "Fitur Otomatis Berjalan 24/7:\n"
                        "• 🚨 Auto Breaking News Push (cek Financial Juice, ForexLive, Fed, White House tiap 60 detik)\n"
                        "• 📊 Auto Mapping M1-1MON & 3 Tombol Approve (Scalp / Intraday / Swing)\n\n"
                        "Perintah Manual:\n"
                        "• /report atau /cek — Scan M1-1MON sekarang\n"
                        "• /status_api — Cek status & latensi semua API + Gemini 4 Argon / Claude\n"
                        "• /news — Rangkuman breaking news terbaru\n"
                        "• /pantau_pidato <link_youtube> — Auto-translate pidato live Fed/Trump tiap 45 detik\n"
                        "• /stop_pidato — Hentikan pantauan pidato live\n"
                        "• Chat bebas kapan saja — Ketik pertanyaan apa saja untuk ngobrol langsung dengan AI!"
                    )
                elif low in ("/report", "/cek"):
                    send_telegram("⏳ Memindai 9 timeframe (M1-1MON), Orderflow, Heatmap & SMT Divergence...")
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
                else:
                    def reply_chat(q=text):
                        if LATEST_CONTEXT_CACHE["updated"] == "Belum ada scan":
                            snap = feeds.collect_all_8_pillars()
                            LATEST_CONTEXT_CACHE["summary"] = snap["telemetry_summary"]
                        ans, eng = ai_router.chat_live(q, LATEST_CONTEXT_CACHE["summary"])
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


if __name__ == "__main__":
    init_db()
    if "--report" in sys.argv:
        build_and_send_institutional_report("GitHub Actions Scheduled")
    else:
        threading.Thread(target=scheduled_loop, daemon=True).start()
        threading.Thread(target=auto_breaking_news_watcher_loop, daemon=True).start()
        telegram_polling_loop()
