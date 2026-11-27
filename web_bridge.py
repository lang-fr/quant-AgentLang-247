import os
import json
import threading
import datetime
import pytz
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()
WIB = pytz.timezone("Asia/Jakarta")

app = Flask(__name__)
_ALERT_CALLBACK = None


def register_webhook_callback(fn):
    """Mendaftarkan fungsi pemroses alert TradingView dari main.py."""
    global _ALERT_CALLBACK
    _ALERT_CALLBACK = fn


@app.route("/", methods=["GET"])
@app.route("/health", methods=["GET"])
def health_check():
    """Endpoint ping 24/7 untuk UptimeRobot, Koyeb, dan Render agar instance tidak sleep."""
    now_wib = datetime.datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S WIB")
    return jsonify(
        {
            "status": "healthy",
            "service": "quant-AgentLang-247",
            "timestamp_wib": now_wib,
        }
    ), 200


@app.route("/webhook", methods=["POST"])
def tradingview_webhook():
    """
    Menerima alert JSON dari TradingView:
    Contoh payload: {"secret": "...", "symbol": "XAUUSD", "action": "BUY", "price": 3680.5, "tf": "M15", "note": "FVG Sweep"}
    """
    expected_secret = os.getenv("WEBHOOK_SECRET", "").strip()
    data = request.get_json(silent=True) or {}

    if expected_secret and str(data.get("secret", "")).strip() != expected_secret:
        return jsonify({"status": "unauthorized"}), 401

    if _ALERT_CALLBACK is not None:
        threading.Thread(target=_ALERT_CALLBACK, args=(data,), daemon=True).start()

    return jsonify({"status": "accepted", "received": data}), 200


def start_web_bridge_in_background():
    """Menjalankan server Flask di thread daemon tanpa memblokir Telegram polling."""
    port = int(os.getenv("PORT", "8080"))

    def _run():
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t
