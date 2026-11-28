import os
import time
import hmac
import hashlib
from urllib.parse import urlencode
import requests


class InstitutionalExecutionEngine:
    def __init__(self):
        self.balance = float(os.getenv("ACCOUNT_BALANCE", "10000.0"))
        self.risk_pct = float(os.getenv("RISK_PER_TRADE_PCT", "1.0"))
        self.max_layers = int(os.getenv("MAX_LAYERS_PER_SETUP", "3"))
        self.metaapi_token = os.getenv("METAAPI_TOKEN", "").strip()
        self.metaapi_account_id = os.getenv("METAAPI_ACCOUNT_ID", "").strip()

        # Tambahan: Konfigurasi Binance USD-M Futures (Testnet / Live)
        self.binance_use_testnet = os.getenv("BINANCE_USE_TESTNET", "true").strip().lower() != "false"
        if self.binance_use_testnet:
            self.binance_api_key = os.getenv("BINANCE_TESTNET_API_KEY", "").strip()
            self.binance_api_secret = os.getenv("BINANCE_TESTNET_SECRET", "").strip()
            self.binance_base_urls = ["https://demo-fapi.binance.com", "https://testnet.binancefuture.com"]
        else:
            self.binance_api_key = os.getenv("BINANCE_API_KEY", "").strip()
            self.binance_api_secret = os.getenv("BINANCE_API_SECRET", "").strip()
            self.binance_base_urls = ["https://fapi.binance.com"]
        self.binance_leverage = int(os.getenv("BINANCE_MAX_LEVERAGE", "2"))

    def calculate_position_layers(self, symbol: str, setup: dict) -> dict:
        entry = float(setup["entry"])
        sl = float(setup["sl"])
        tp1 = float(setup["tp1"])
        tp2 = float(setup["tp2"])
        tp3 = float(setup["tp3"])
        direction = setup["direction"]

        risk_usd = round(self.balance * (self.risk_pct / 100.0), 2)
        sl_dist = max(abs(entry - sl), 0.01)

        is_xau = "XAU" in symbol.upper()
        # Kontrak standar: XAUUSD 1 lot = $100 per $1 gerak; BTCUSDT 1 lot = $1 per $1 gerak
        contract_mult = 100.0 if is_xau else 1.0
        total_lot = max(0.01, round(risk_usd / (sl_dist * contract_mult), 2))

        layers_count = min(self.max_layers, 3) if total_lot >= 0.03 else 1
        lot_per_layer = max(0.01, round(total_lot / layers_count, 2))

        step = round(sl_dist * 0.18, 2)
        is_buy = "BUY" in direction
        tps = [tp1, tp2, tp3]
        layers = []

        for idx in range(layers_count):
            px = round(entry - (idx * step), 2) if is_buy else round(entry + (idx * step), 2)
            layers.append(
                {
                    "layer": idx + 1,
                    "order_type": "BUY LIMIT" if is_buy else "SELL LIMIT",
                    "price": px,
                    "sl": sl,
                    "tp": tps[idx % len(tps)],
                    "lot": lot_per_layer,
                }
            )

        return {
            "symbol": symbol,
            "direction": direction,
            "risk_usd": risk_usd,
            "total_lot": total_lot,
            "layers": layers,
        }

    # =========================================================================
    # TAMBAHAN: HELPER SALDO & EKSEKUSI 1 POSISI NET BINANCE FUTURES
    # =========================================================================
    def _binance_request(self, method: str, path: str, params: dict | None = None) -> dict:
        if not (self.binance_api_key and self.binance_api_secret):
            return {"ok": False, "error": "BINANCE_KEY_MISSING"}

        headers = {"X-MBX-APIKEY": self.binance_api_key}
        base_params = dict(params or {})

        for base in self.binance_base_urls:
            try:
                p = dict(base_params)
                p["timestamp"] = int(time.time() * 1000)
                p["recvWindow"] = 5000
                qs = urlencode(p)
                sig = hmac.new(self.binance_api_secret.encode("utf-8"), qs.encode("utf-8"), hashlib.sha256).hexdigest()
                url = f"{base}{path}?{qs}&signature={sig}"
                r = requests.request(method, url, headers=headers, timeout=12)
                data = r.json()
                if r.status_code == 200:
                    return {"ok": True, "data": data, "endpoint": base}
            except Exception:
                continue
        return {"ok": False, "error": "BINANCE_API_UNREACHABLE"}

    def get_binance_usdt_balance(self) -> dict:
        mode = "TESTNET" if self.binance_use_testnet else "LIVE"
        res = self._binance_request("GET", "/fapi/v2/balance")
        if not res.get("ok"):
            return {"connected": False, "mode": mode, "usdt_balance": 0.0, "available_balance": 0.0, "detail": res.get("error")}

        for item in res.get("data", []):
            if item.get("asset") == "USDT":
                return {
                    "connected": True,
                    "mode": mode,
                    "usdt_balance": round(float(item.get("balance", 0.0)), 2),
                    "available_balance": round(float(item.get("availableBalance", 0.0)), 2),
                }
        return {"connected": True, "mode": mode, "usdt_balance": 0.0, "available_balance": 0.0, "detail": "Saldo USDT 0"}

    def get_balance_summary(self) -> str:
        b = self.get_binance_usdt_balance()
        if not b.get("connected"):
            return f"Binance {b['mode']}: Belum Terhubung ({b.get('detail', 'Cek API Key')})"
        return f"Binance {b['mode']}: Total ${b['usdt_balance']:.2f} USDT (Tersedia: ${b['available_balance']:.2f})"

    def _execute_binance_net_horizon(self, symbol: str, horizon_label: str, setup: dict, plan: dict) -> dict:
        clean_sym = "BTCUSDT"
        bal = self.get_binance_usdt_balance()
        avail = float(bal.get("available_balance", 0.0))
        mode_label = f"BINANCE_FUTURES_{bal['mode']}"

        if not bal.get("connected") or avail < 15.0:
            return {
                "mode": mode_label,
                "horizon": horizon_label,
                "plan": plan,
                "message": (
                    f"Eksekusi Binance {bal['mode']} ({horizon_label}) ditahan: "
                    f"Saldo USDT Futures tersedia ${avail:.2f} (minimal $15.00)."
                ),
            }

        side = "BUY" if "BUY" in plan["direction"].upper() else "SELL"
        opp_side = "SELL" if side == "BUY" else "BUY"
        entry_px = float(setup["entry"])
        sl_px = float(setup["sl"])
        tp1_px = float(setup["tp1"])
        sl_dist = max(abs(entry_px - sl_px), 1.0)

        # Gabungkan 3 layer menjadi 1 posisi net tunggal berbasis saldo aktual Binance Futures
        risk_usd = round(avail * (self.risk_pct / 100.0), 2)
        net_qty = round(max(risk_usd / sl_dist, 0.002), 3)

        self._binance_request("POST", "/fapi/v1/leverage", {"symbol": clean_sym, "leverage": self.binance_leverage})

        order_res = self._binance_request(
            "POST",
            "/fapi/v1/order",
            {"symbol": clean_sym, "side": side, "type": "MARKET", "quantity": net_qty},
        )
        if not order_res.get("ok"):
            return {
                "mode": mode_label,
                "horizon": horizon_label,
                "plan": plan,
                "message": f"Order Binance {bal['mode']} gagal: {order_res.get('error')}",
            }

        # Pasang Hard SL & TP1 (closePosition=true agar aman pada 1 net position)
        self._binance_request(
            "POST",
            "/fapi/v1/order",
            {"symbol": clean_sym, "side": opp_side, "type": "STOP_MARKET", "stopPrice": round(sl_px, 1), "closePosition": "true"},
        )
        self._binance_request(
            "POST",
            "/fapi/v1/order",
            {"symbol": clean_sym, "side": opp_side, "type": "TAKE_PROFIT_MARKET", "stopPrice": round(tp1_px, 1), "closePosition": "true"},
        )

        return {
            "mode": mode_label,
            "horizon": horizon_label,
            "plan": plan,
            "message": (
                f"Eksekusi Binance {bal['mode']} ({horizon_label}): 1 Posisi Net {side} {net_qty} BTC "
                f"(gabungan {len(plan['layers'])} layer | Saldo: ${avail:.2f} | Risk: ${risk_usd} | "
                f"Hard SL: {round(sl_px, 1)} | TP1: {round(tp1_px, 1)})."
            ),
        }

    # =========================================================================
    # FUNGSI UTAMA ASLI (DIPERTAHANKAN 100% + ROUTING OTOMATIS BTC KE BINANCE)
    # =========================================================================
    def execute_horizon_setup(self, symbol: str, horizon_label: str, setup: dict) -> dict:
        plan = self.calculate_position_layers(symbol, setup)

        # Jika simbol BTCUSDT dan API Binance terpasang -> eksekusi 1 posisi net di Binance Futures
        if "BTC" in symbol.upper() and (self.binance_api_key and self.binance_api_secret):
            return self._execute_binance_net_horizon(symbol, horizon_label, setup, plan)

        if not (self.metaapi_token and self.metaapi_account_id):
            return {
                "mode": "SIMULATION_JOURNAL",
                "horizon": horizon_label,
                "plan": plan,
                "message": (
                    f"Order {horizon_label} ({plan['direction']}) dicatat ke Jurnal Audit & Outcome Verifier "
                    f"(Total {plan['total_lot']} lot dalam {len(plan['layers'])} layer | Risiko ${plan['risk_usd']})."
                ),
            }

        url = f"https://mt-client-api-v1.agiliumtrade.agiliumtrade.ai/users/current/accounts/{self.metaapi_account_id}/trade"
        headers = {"auth-token": self.metaapi_token, "Content-Type": "application/json"}
        placed = []
        errors = []

        for lyr in plan["layers"]:
            action_type = "ORDER_TYPE_BUY_LIMIT" if "BUY" in lyr["order_type"] else "ORDER_TYPE_SELL_LIMIT"
            payload = {
                "actionType": action_type,
                "symbol": symbol,
                "volume": lyr["lot"],
                "openPrice": lyr["price"],
                "stopLoss": lyr["sl"],
                "takeProfit": lyr["tp"],
                "comment": f"QuantLang-{horizon_label[:5]}-L{lyr['layer']}",
            }
            try:
                r = requests.post(url, headers=headers, json=payload, timeout=10)
                if r.status_code in (200, 201):
                    placed.append(f"L{lyr['layer']}@{lyr['price']}")
                else:
                    errors.append(f"L{lyr['layer']}:HTTP{r.status_code}")
            except Exception as e:
                errors.append(f"L{lyr['layer']}:{str(e)[:40]}")

        return {
            "mode": "METAAPI_MT5_DEMO",
            "horizon": horizon_label,
            "plan": plan,
            "placed": placed,
            "errors": errors,
            "message": (
                f"Eksekusi MetaApi MT5 Demo ({horizon_label}): Sukses {len(placed)}/{len(plan['layers'])} layer "
                f"({', '.join(placed) if placed else 'Gagal: ' + ', '.join(errors)})"
            ),
        }
