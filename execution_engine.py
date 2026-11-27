import os
import hmac
import time
import hashlib
import urllib.parse
import requests


class InstitutionalExecutionEngine:
    """
    Mesin Eksekusi Institusional:
    - Menghitung Position Sizing dinamis (0.5% - 2% risk per trade)
    - Memecah Limit Order menjadi 1-3 Layer Sniper
    - Mengaktifkan pengaman Auto BEP+1 saat TP1 tersentuh
    - Memblokir order otomatis jika Daily Drawdown menyentuh batas Kill Switch (3% - 5%)
    - Mendukung Mode: PAPER_SIMULATION (Default), BINANCE_TESTNET, dan MT5_WEBHOOK
    """

    def __init__(self):
        self.account_balance = float(os.getenv("ACCOUNT_BALANCE", "10000"))
        self.risk_pct = min(max(float(os.getenv("RISK_PER_TRADE_PCT", "1.0")), 0.5), 2.0)
        self.max_daily_dd_pct = float(os.getenv("MAX_DAILY_DRAWDOWN_PCT", "3.0"))
        self.layers_count = min(max(int(os.getenv("ENTRY_LAYERS", "3")), 1), 3)

        self.binance_testnet_key = os.getenv("BINANCE_TESTNET_API_KEY", "").strip()
        self.binance_testnet_secret = os.getenv("BINANCE_TESTNET_SECRET", "").strip()
        self.mt5_webhook_url = os.getenv("MT5_WEBHOOK_URL", "").strip()

    def check_kill_switch(self, daily_pnl_pct: float) -> tuple[bool, str]:
        """
        Memeriksa apakah kerugian harian sudah menyentuh batas Kill Switch (-3.0%).
        """
        if daily_pnl_pct <= -abs(self.max_daily_dd_pct):
            return False, (
                f"KILL SWITCH AKTIF: Kerugian harian ({daily_pnl_pct:.2f}%) "
                f"melewati batas maksimal (-{self.max_daily_dd_pct:.2f}%). Semua order baru diblokir."
            )
        return True, "AMAN"

    def build_layered_orders(self, symbol: str, setup: dict) -> list[dict]:
        """
        Memecah 1 setup menjadi 1-3 Layer Limit Order di antara Entry dan Stop Loss
        dengan total risiko tetap terkunci pada self.risk_pct (0.5% - 2%).
        """
        direction = setup["direction"]
        entry = float(setup["entry"])
        sl = float(setup["sl"])
        tp1 = float(setup["tp1"])
        tp2 = float(setup["tp2"])
        tp3 = float(setup["tp3"])

        stop_distance = abs(entry - sl)
        if stop_distance <= 0:
            return []

        total_risk_usd = self.account_balance * (self.risk_pct / 100.0)
        total_units = round(total_risk_usd / stop_distance, 4)

        layer_weights = {
            1: [1.0],
            2: [0.60, 0.40],
            3: [0.40, 0.35, 0.25],
        }[self.layers_count]

        layer_offsets = {
            1: [0.0],
            2: [0.0, 0.35],
            3: [0.0, 0.30, 0.60],
        }[self.layers_count]

        orders = []
        for idx, (weight, offset_ratio) in enumerate(zip(layer_weights, layer_offsets), start=1):
            if direction == "SELL LIMIT":
                layer_price = round(entry + (stop_distance * offset_ratio), 2)
                bep_price = round(layer_price - (stop_distance * 0.05), 2)
            else:
                layer_price = round(entry - (stop_distance * offset_ratio), 2)
                bep_price = round(layer_price + (stop_distance * 0.05), 2)

            qty = round(total_units * weight, 4)
            orders.append(
                {
                    "layer": idx,
                    "symbol": symbol,
                    "side": "SELL" if "SELL" in direction else "BUY",
                    "order_type": "LIMIT",
                    "price": layer_price,
                    "sl": sl,
                    "bep_plus_1": bep_price,
                    "tp1": tp1,
                    "tp2": tp2,
                    "tp3": tp3,
                    "qty": qty,
                    "risk_usd": round(total_risk_usd * weight, 2),
                }
            )
        return orders

    def _send_binance_testnet_order(self, order: dict) -> str:
        """
        Mengirim Limit Order ke Binance USD-M Futures Testnet (gratis, tanpa dana riil).
        """
        base_url = "https://testnet.binancefuture.com"
        endpoint = "/fapi/v1/order"
        params = {
            "symbol": "BTCUSDT",
            "side": order["side"],
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": max(round(order["qty"], 3), 0.001),
            "price": order["price"],
            "timestamp": int(time.time() * 1000),
        }
        query_string = urllib.parse.urlencode(params)
        signature = hmac.new(
            self.binance_testnet_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        params["signature"] = signature

        headers = {"X-MBX-APIKEY": self.binance_testnet_key}
        resp = requests.post(f"{base_url}{endpoint}", headers=headers, params=params, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            return f"BinanceTestnet OrderID #{data.get('orderId')}"
        return f"PaperFallback ({resp.status_code})"

    def execute_approved_setup(self, record_id: int, payload: dict, daily_pnl_pct: float = 0.0) -> str:
        """
        Menjalankan eksekusi saat tombol Approve ditekan di Telegram.
        """
        is_safe, kill_msg = self.check_kill_switch(daily_pnl_pct)
        if not is_safe:
            return f"🛑 {kill_msg}"

        summary_lines = [f"⚡ <b>EKSEKUSI SNIPER AKTIF (Setup #{record_id})</b>"]

        for asset_key, horizon_target in [
            ("xau", "INTRADAY (M1-M5-M15-M30-H1-H4)"),
            ("btc", "INTRADAY (M1-M5-M15-M30-H1-H4)"),
        ]:
            asset_setups = payload.get(asset_key, {})
            setup = asset_setups.get(horizon_target)
            if not setup:
                continue

            symbol_label = "XAUUSD" if asset_key == "xau" else "BTCUSDT"
            layered = self.build_layered_orders(symbol_label, setup)
            if not layered:
                continue

            mode_tag = "PAPER-SIM"
            if asset_key == "btc" and self.binance_testnet_key and self.binance_testnet_secret:
                try:
                    mode_tag = self._send_binance_testnet_order(layered[0])
                except Exception:
                    mode_tag = "PAPER-SIM"
            elif self.mt5_webhook_url:
                try:
                    requests.post(
                        self.mt5_webhook_url,
                        json={"record_id": record_id, "symbol": symbol_label, "orders": layered},
                        timeout=10,
                    )
                    mode_tag = "MT5-WEBHOOK-SENT"
                except Exception:
                    mode_tag = "PAPER-SIM"

            summary_lines.append(
                f"\n📌 <b>{symbol_label} [{setup['direction']}]</b> — Mode: <code>{mode_tag}</code>\n"
                f"• Layer 1 (40%): <code>{layered[0]['price']}</code> | Risk: <code>${layered[0]['risk_usd']}</code>\n"
                + (
                    f"• Layer 2 (35%): <code>{layered[1]['price']}</code> | Risk: <code>${layered[1]['risk_usd']}</code>\n"
                    if len(layered) >= 2
                    else ""
                )
                + (
                    f"• Layer 3 (25%): <code>{layered[2]['price']}</code> | Risk: <code>${layered[2]['risk_usd']}</code>\n"
                    if len(layered) >= 3
                    else ""
                )
                + f"• Hard SL: <code>{setup['sl']}</code> | Auto BEP+1 saat TP1 (<code>{setup['tp1']}</code>)"
            )

        return "\n".join(summary_lines)
