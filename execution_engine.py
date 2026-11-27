import os
import requests


class InstitutionalExecutionEngine:
    def __init__(self):
        self.balance = float(os.getenv("ACCOUNT_BALANCE", "10000.0"))
        self.risk_pct = float(os.getenv("RISK_PER_TRADE_PCT", "1.0"))
        self.max_layers = int(os.getenv("MAX_LAYERS_PER_SETUP", "3"))
        self.metaapi_token = os.getenv("METAAPI_TOKEN", "").strip()
        self.metaapi_account_id = os.getenv("METAAPI_ACCOUNT_ID", "").strip()

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

    def execute_horizon_setup(self, symbol: str, horizon_label: str, setup: dict) -> dict:
        plan = self.calculate_position_layers(symbol, setup)
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
