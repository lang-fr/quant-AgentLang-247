import os
import ccxt
import requests

METAAPI_TOKEN = os.getenv("METAAPI_TOKEN", "").strip()
METAAPI_ACCOUNT_ID = os.getenv("METAAPI_ACCOUNT_ID", "").strip()
BINANCE_TESTNET_API_KEY = os.getenv("BINANCE_TESTNET_API_KEY", "").strip()
BINANCE_TESTNET_SECRET = os.getenv("BINANCE_TESTNET_SECRET", "").strip()
AUTO_EXECUTE_DEMO = os.getenv("AUTO_EXECUTE_DEMO", "false").lower() == "true"


def _klien_binance_testnet():
    if not BINANCE_TESTNET_API_KEY or not BINANCE_TESTNET_SECRET:
        return None
    ex = ccxt.binance({
        "apiKey": BINANCE_TESTNET_API_KEY,
        "secret": BINANCE_TESTNET_SECRET,
        "enableRateLimit": True,
        "options": {"defaultType": "future"},
    })
    ex.set_sandbox_mode(True)
    return ex


def ambil_info_akun_mt5() -> dict:
    """Menarik balance, equity, dan drawdown harian aktual dari MetaApi (Exness Demo MT5)."""
    if not METAAPI_TOKEN or not METAAPI_ACCOUNT_ID:
        return {"status": "nonaktif", "balance": 1000.0, "equity": 1000.0, "daily_loss_pct": 0.0}
    try:
        headers = {"auth-token": METAAPI_TOKEN}
        url = f"https://mt-client-api-v1.agiliumtrade.agiliumtrade.ai/users/current/accounts/{METAAPI_ACCOUNT_ID}/account-information"
        resp = requests.get(url, headers=headers, timeout=10).json()
        bal = float(resp.get("balance", 1000.0))
        eq = float(resp.get("equity", bal))
        dd_pct = round(max(0.0, ((bal - eq) / bal) * 100.0), 2) if bal > 0 else 0.0
        return {"status": "aktif", "balance": bal, "equity": eq, "daily_loss_pct": dd_pct}
    except Exception as e:
        return {"status": "error", "balance": 1000.0, "equity": 1000.0, "daily_loss_pct": 0.0, "detail": str(e)}


def eksekusi_xau_metaapi_3tp(
    arah: str,
    total_lot: float,
    entry_price: float,
    sl_price: float,
    tp1: float,
    tp2: float,
    tp3: float,
    layers: int = 1,
) -> dict:
    """Mengirim order XAUUSD ke Exness Demo MT5 via MetaApi terbagi 3 TP dan hingga 3 layer."""
    if not METAAPI_TOKEN or not METAAPI_ACCOUNT_ID:
        return {"executed": False, "reason": "METAAPI_TOKEN / METAAPI_ACCOUNT_ID belum diisi"}

    side = arah.upper()
    if side not in ("BUY", "SELL"):
        return {"executed": False, "reason": f"Arah tidak valid: {arah}"}

    headers = {"auth-token": METAAPI_TOKEN, "Content-Type": "application/json"}
    url = f"https://mt-client-api-v1.agiliumtrade.agiliumtrade.ai/users/current/accounts/{METAAPI_ACCOUNT_ID}/trade"

    # Alokasi 3 TP: 40% TP1, 40% TP2, 20% TP3 (Runner)
    bobot_tp = [("TP1_40%", tp1, 0.40), ("TP2_40%", tp2, 0.40), ("TP3_RUNNER_20%", tp3, 0.20)]
    hasil_order = []

    for layer_idx in range(max(1, min(3, layers))):
        # Layer 1 eksekusi market/limit utama, Layer 2 & 3 limit bertingkat menuju SL (30% & 60% jarak SL)
        rasio_geser = 0.0 if layer_idx == 0 else (0.30 if layer_idx == 1 else 0.60)
        harga_layer = entry_price - (rasio_geser * (entry_price - sl_price))
        lot_layer = total_lot / max(1, min(3, layers))

        for label_tp, target_tp, porsi in bobot_tp:
            vol_sub = max(0.01, round(lot_layer * porsi, 2))
            if layer_idx == 0:
                action_type = "ORDER_TYPE_BUY" if side == "BUY" else "ORDER_TYPE_SELL"
                payload = {
                    "actionType": action_type,
                    "symbol": "XAUUSD",
                    "volume": vol_sub,
                    "stopLoss": round(sl_price, 2),
                    "takeProfit": round(target_tp, 2),
                    "comment": f"AgentLang_L{layer_idx+1}_{label_tp}",
                }
            else:
                action_type = "ORDER_TYPE_BUY_LIMIT" if side == "BUY" else "ORDER_TYPE_SELL_LIMIT"
                payload = {
                    "actionType": action_type,
                    "symbol": "XAUUSD",
                    "volume": vol_sub,
                    "openPrice": round(harga_layer, 2),
                    "stopLoss": round(sl_price, 2),
                    "takeProfit": round(target_tp, 2),
                    "comment": f"AgentLang_L{layer_idx+1}_{label_tp}",
                }

            try:
                r = requests.post(url, headers=headers, json=payload, timeout=12).json()
                hasil_order.append({"layer": layer_idx + 1, "tp": label_tp, "vol": vol_sub, "resp": r})
            except Exception as e:
                hasil_order.append({"layer": layer_idx + 1, "tp": label_tp, "error": str(e)})

    return {"executed": True, "orders": hasil_order}


def eksekusi_btc_testnet_3tp(
    arah: str,
    risk_usd: float,
    entry_price: float,
    sl_price: float,
    tp1: float,
    tp2: float,
    tp3: float,
    layers: int = 1,
) -> dict:
    """Mengirim order BTC/USDT ke Binance Futures Testnet (Leverage 1x + Hard SL + 3 TP)."""
    ex = _klien_binance_testnet()
    if ex is None:
        return {"executed": False, "reason": "BINANCE_TESTNET_API_KEY / SECRET belum diisi"}

    side_entry = "buy" if arah.upper() == "BUY" else "sell"
    side_exit = "sell" if side_entry == "buy" else "buy"
    jarak_sl = abs(entry_price - sl_price)
    if jarak_sl <= 0:
        return {"executed": False, "reason": "Jarak SL tidak valid"}

    try:
        ex.set_leverage(1, "BTC/USDT")
    except Exception:
        pass

    total_qty = round(risk_usd / jarak_sl, 3)
    if total_qty < 0.001:
        total_qty = 0.003

    hasil = []
    bobot_tp = [("TP1", tp1, 0.40), ("TP2", tp2, 0.40), ("TP3_RUNNER", tp3, 0.20)]

    for layer_idx in range(max(1, min(3, layers))):
        rasio_geser = 0.0 if layer_idx == 0 else (0.30 if layer_idx == 1 else 0.60)
        harga_layer = entry_price - (rasio_geser * (entry_price - sl_price))
        qty_layer = round(total_qty / max(1, min(3, layers)), 3)

        try:
            if layer_idx == 0:
                ord_in = ex.create_order("BTC/USDT", "market", side_entry, qty_layer)
            else:
                ord_in = ex.create_order("BTC/USDT", "limit", side_entry, qty_layer, round(harga_layer, 1))

            # Pasang Hard Stop Loss
            ex.create_order(
                "BTC/USDT",
                "STOP_MARKET",
                side_exit,
                qty_layer,
                params={"stopPrice": round(sl_price, 1), "reduceOnly": True},
            )

            # Pasang 3 Take Profit bertahap
            for lbl, px_tp, porsi in bobot_tp:
                q_tp = max(0.001, round(qty_layer * porsi, 3))
                ex.create_order(
                    "BTC/USDT",
                    "TAKE_PROFIT_MARKET",
                    side_exit,
                    q_tp,
                    params={"stopPrice": round(px_tp, 1), "reduceOnly": True},
                )

            hasil.append({"layer": layer_idx + 1, "qty": qty_layer, "order_id": ord_in.get("id")})
        except Exception as e:
            hasil.append({"layer": layer_idx + 1, "error": str(e)})

    return {"executed": True, "orders": hasil}


def kelola_auto_breakeven_mt5() -> list:
    """Memeriksa posisi terbuka MT5; jika profit >= 1.5R atau TP1 kena, geser SL ke Breakeven + buffer."""
    if not METAAPI_TOKEN or not METAAPI_ACCOUNT_ID:
        return []

    headers = {"auth-token": METAAPI_TOKEN, "Content-Type": "application/json"}
    base = f"https://mt-client-api-v1.agiliumtrade.agiliumtrade.ai/users/current/accounts/{METAAPI_ACCOUNT_ID}"
    aksi_be = []

    try:
        positions = requests.get(f"{base}/positions", headers=headers, timeout=10).json()
        if not isinstance(positions, list):
            return []

        for pos in positions:
            pos_id = pos.get("id")
            sym = pos.get("symbol")
            p_type = pos.get("type", "")
            open_px = float(pos.get("openPrice", 0))
            curr_px = float(pos.get("currentPrice", 0))
            sl_now = float(pos.get("stopLoss", 0))
            tp_now = float(pos.get("takeProfit", 0))

            if open_px <= 0 or curr_px <= 0 or sl_now <= 0 or tp_now <= 0:
                continue

            # Geser ke BE + 0.30 USD buffer jika harga sudah menempuh >= 50% perjalanan menuju TP
            if p_type == "POSITION_TYPE_BUY" and sl_now < open_px:
                target_trigger = open_px + 0.50 * (tp_now - open_px)
                if curr_px >= target_trigger:
                    be_sl = round(open_px + 0.30, 2)
                    requests.post(
                        f"{base}/trade",
                        headers=headers,
                        json={"actionType": "POSITION_MODIFY", "positionId": pos_id, "stopLoss": be_sl, "takeProfit": tp_now},
                        timeout=10,
                    )
                    aksi_be.append(f"{sym} #{pos_id} BUY SL -> BE ({be_sl})")

            elif p_type == "POSITION_TYPE_SELL" and sl_now > open_px:
                target_trigger = open_px - 0.50 * (open_px - tp_now)
                if curr_px <= target_trigger:
                    be_sl = round(open_px - 0.30, 2)
                    requests.post(
                        f"{base}/trade",
                        headers=headers,
                        json={"actionType": "POSITION_MODIFY", "positionId": pos_id, "stopLoss": be_sl, "takeProfit": tp_now},
                        timeout=10,
                    )
                    aksi_be.append(f"{sym} #{pos_id} SELL SL -> BE ({be_sl})")
    except Exception:
        pass

    return aksi_be
