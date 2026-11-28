import os
import time
import hmac
import hashlib
import logging
from urllib.parse import urlencode
from typing import Dict, Any, Optional
import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("InstitutionalExecutionEngine")


class InstitutionalExecutionEngine:
    """
    Eksekutor Institusional Binance USD-M Futures (Testnet / Live):
    - Membaca saldo USDT Futures secara real-time (/fapi/v2/balance)
    - Menggabungkan rencana 1-3 layer menjadi 1 posisi net tunggal (kompatibel One-Way Mode Binance)
    - Memasang Hard Stop Loss (STOP_MARKET) dan Take Profit (TAKE_PROFIT_MARKET) closePosition=true
    """

    def __init__(self):
        self.use_testnet = os.getenv("BINANCE_USE_TESTNET", "true").strip().lower() != "false"
        if self.use_testnet:
            self.api_key = os.getenv("BINANCE_TESTNET_API_KEY", "").strip()
            self.api_secret = os.getenv("BINANCE_TESTNET_SECRET", "").strip()
            self.base_urls = ["https://demo-fapi.binance.com", "https://testnet.binancefuture.com"]
        else:
            self.api_key = os.getenv("BINANCE_API_KEY", "").strip()
            self.api_secret = os.getenv("BINANCE_API_SECRET", "").strip()
            self.base_urls = ["https://fapi.binance.com"]

        self.active_base_url = self.base_urls[0]
        self.max_leverage = int(os.getenv("BINANCE_MAX_LEVERAGE", "3"))

    def _sign_params(self, params: Dict[str, Any]) -> str:
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        query = urlencode(params)
        sig = hmac.new(self.api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{query}&signature={sig}"

    def _request(
        self, method: str, path: str, params: Optional[Dict[str, Any]] = None, signed: bool = True
    ) -> Dict[str, Any]:
        if not self.api_key or not self.api_secret:
            return {"ok": False, "error": "BINANCE_API_KEY_OR_SECRET_MISSING"}

        headers = {"X-MBX-APIKEY": self.api_key}
        req_params = dict(params or {})

        for base in self.base_urls:
            try:
                if signed:
                    qs = self._sign_params(dict(req_params))
                    url = f"{base}{path}?{qs}"
                    r = requests.request(method, url, headers=headers, timeout=12)
                else:
                    url = f"{base}{path}"
                    r = requests.request(method, url, params=req_params, headers=headers, timeout=12)

                data = r.json()
                if r.status_code == 200:
                    self.active_base_url = base
                    return {"ok": True, "data": data, "endpoint": base}
                logger.warning(f"Binance {base}{path} HTTP {r.status_code}: {data}")
            except Exception as e:
                logger.warning(f"Binance {base}{path} error: {e}")

        return {"ok": False, "error": "BINANCE_REQUEST_FAILED"}

    def get_binance_usdt_balance(self) -> Dict[str, Any]:
        res = self._request("GET", "/fapi/v2/balance", signed=True)
        if not res.get("ok"):
            return {
                "connected": False,
                "mode": "TESTNET" if self.use_testnet else "LIVE",
                "usdt_balance": 0.0,
                "available_balance": 0.0,
                "detail": res.get("error", "Auth/Network Error"),
            }

        for item in res.get("data", []):
            if item.get("asset") == "USDT":
                return {
                    "connected": True,
                    "mode": "TESTNET" if self.use_testnet else "LIVE",
                    "endpoint": res.get("endpoint"),
                    "usdt_balance": round(float(item.get("balance", 0.0)), 2),
                    "available_balance": round(float(item.get("availableBalance", 0.0)), 2),
                }

        return {
            "connected": True,
            "mode": "TESTNET" if self.use_testnet else "LIVE",
            "usdt_balance": 0.0,
            "available_balance": 0.0,
            "detail": "Wallet USDT Futures belum terisi saldo",
        }

    def get_balance_summary(self) -> str:
        b = self.get_binance_usdt_balance()
        if not b.get("connected"):
            return f"Binance {b['mode']}: Belum Terhubung ({b.get('detail', 'Cek API Key')})"
        return f"Binance {b['mode']}: Total ${b['usdt_balance']:.2f} USDT (Tersedia: ${b['available_balance']:.2f})"

    def set_leverage(self, symbol: str, leverage: int = 2) -> None:
        lev = max(1, min(int(leverage), self.max_leverage))
        self._request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": lev}, signed=True)

    def execute_net_futures_order(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        sl_price: float,
        tp_price: float,
        risk_pct: float = 0.01,
    ) -> str:
        clean_sym = "BTCUSDT" if "BTC" in symbol.upper() else symbol.upper()
        if clean_sym == "XAUUSD":
            return "XAUUSD adalah instrumen CFD/Metal (gunakan bridge MT5/Exness, bukan Binance Futures)."

        bal_info = self.get_binance_usdt_balance()
        avail = float(bal_info.get("available_balance", 0.0))
        if not bal_info.get("connected") or avail < 15.0:
            return (
                f"Eksekusi Binance ({bal_info.get('mode')}) dibatalkan: "
                f"Saldo USDT Futures tersedia ${avail:.2f} (minimal $15.00 untuk margin & SL)."
            )

        side = "BUY" if ("BUY" in direction.upper() or "LONG" in direction.upper()) else "SELL"
        opp_side = "SELL" if side == "BUY" else "BUY"

        stop_dist = abs(float(entry_price) - float(sl_price))
        if stop_dist <= 0:
            return "Eksekusi dibatalkan: Jarak Stop Loss tidak valid (0)."

        # Hitung ukuran 1 posisi net berdasarkan risiko modal aktual (1% dari saldo Futures)
        risk_usd = avail * min(max(risk_pct, 0.005), 0.02)
        raw_qty = risk_usd / stop_dist
        qty = round(max(raw_qty, 0.002), 3)

        self.set_leverage(clean_sym, leverage=2)

        # 1. Entry Net Position tunggal (kompatibel One-Way Mode Binance)
        entry_res = self._request(
            "POST",
            "/fapi/v1/order",
            {"symbol": clean_sym, "side": side, "type": "MARKET", "quantity": qty},
            signed=True,
        )
        if not entry_res.get("ok"):
            return f"Order Entry Binance gagal: {entry_res.get('error')}"

        # 2. Pasang Hard Stop Loss (STOP_MARKET)
        self._request(
            "POST",
            "/fapi/v1/order",
            {
                "symbol": clean_sym,
                "side": opp_side,
                "type": "STOP_MARKET",
                "stopPrice": round(float(sl_price), 1),
                "closePosition": "true",
            },
            signed=True,
        )

        # 3. Pasang Take Profit (TAKE_PROFIT_MARKET)
        self._request(
            "POST",
            "/fapi/v1/order",
            {
                "symbol": clean_sym,
                "side": opp_side,
                "type": "TAKE_PROFIT_MARKET",
                "stopPrice": round(float(tp_price), 1),
                "closePosition": "true",
            },
            signed=True,
        )

        return (
            f"EKSEKUSI BINANCE {bal_info.get('mode')} SUKSES (1 Net Position)\n"
            f"- Simbol: {clean_sym} | Arah: {side} | Qty: {qty} BTC (Leverage 2x)\n"
            f"- Saldo Awal: ${avail:.2f} | Risiko: ${risk_usd:.2f} (1%)\n"
            f"- Hard SL: {round(float(sl_price), 1)} | Target TP: {round(float(tp_price), 1)}"
        )

    # Alias kompatibilitas untuk pemanggilan dari main.py
    def execute_setup(self, setup: Dict[str, Any]) -> str:
        return self.execute_net_futures_order(
            symbol=str(setup.get("symbol", "BTCUSDT")),
            direction=str(setup.get("direction", "BUY")),
            entry_price=float(setup.get("entry", 0.0)),
            sl_price=float(setup.get("sl", 0.0)),
            tp_price=float(setup.get("tp1", setup.get("tp", 0.0))),
        )

    def execute_trade(self, symbol: str, direction: str, entry: float, sl: float, tp: float) -> str:
        return self.execute_net_futures_order(symbol, direction, entry, sl, tp)
