import os
import ccxt

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
BINANCE_SECRET = os.getenv("BINANCE_SECRET", "").strip()
BINANCE_DEMO_MODE = os.getenv("BINANCE_DEMO_MODE", "true").lower() == "true"
AUTO_EXECUTE_BINANCE = os.getenv("AUTO_EXECUTE_BINANCE", "true").lower() == "true"


def _klien_binance_futures():
    """Inisialisasi klien Binance USDⓈ-M Futures (Demo Trading atau Live)."""
    if not BINANCE_API_KEY or not BINANCE_SECRET:
        return None

    ex = ccxt.binanceusdm({
        "apiKey": BINANCE_API_KEY,
        "secret": BINANCE_SECRET,
        "enableRateLimit": True,
        "options": {
            "defaultType": "future",
            "adjustForTimeDifference": True,
        },
    })

    if BINANCE_DEMO_MODE:
        if hasattr(ex, "enable_demo_trading"):
            ex.enable_demo_trading(True)
        else:
            ex.set_sandbox_mode(True)

    return ex


def _pilih_simbol_digital_gold(ex) -> str:
    """Memilih kontrak emas digital di Binance Futures (XAU/USDT atau fallback PAXG/USDT)."""
    try:
        markets = ex.load_markets()
        for kandidat in ("XAU/USDT:USDT", "XAU/USDT", "PAXG/USDT:USDT", "PAXG/USDT"):
            if kandidat in markets:
                return kandidat
    except Exception:
        pass
    return "PAXG/USDT:USDT"


def ambil_info_akun_dan_spread() -> dict:
    """Menarik saldo USDT aktual dan spread real-time Binance Demo untuk Digital Gold & BTC."""
    ex = _klien_binance_futures()
    if ex is None:
        return {
            "status": "nonaktif",
            "mode": "DEMO" if BINANCE_DEMO_MODE else "LIVE",
            "usdt_total": 1000.0,
            "spread_digital_gold": 0.25,
            "spread_btc": 1.00,
        }

    try:
        bal = ex.fetch_balance()
        usdt_total = float(bal.get("USDT", {}).get("total", 1000.0))
        usdt_free = float(bal.get("USDT", {}).get("free", usdt_total))

        sym_gold = _pilih_simbol_digital_gold(ex)
        ob_gold = ex.fetch_order_book(sym_gold, limit=5)
        spread_gold = round(ob_gold["asks"][0][0] - ob_gold["bids"][0][0], 3) if ob_gold["asks"] and ob_gold["bids"] else 0.25

        ob_btc = ex.fetch_order_book("BTC/USDT:USDT", limit=5)
        spread_btc = round(ob_btc["asks"][0][0] - ob_btc["bids"][0][0], 2) if ob_btc["asks"] and ob_btc["bids"] else 1.00

        return {
            "status": "aktif",
            "mode": "DEMO" if BINANCE_DEMO_MODE else "LIVE",
            "usdt_total": round(usdt_total, 2),
            "usdt_free": round(usdt_free, 2),
            "binance_gold_symbol": sym_gold,
            "spread_digital_gold": spread_gold,
            "spread_btc": spread_btc,
        }
    except Exception as e:
        return {"status": "error", "detail": str(e), "usdt_total": 1000.0}


def proses_eksekusi_sinyal(
    aset: str,
    arah: str,
    risk_usd: float,
    entry_price: float,
    sl_price: float,
    tp1: float,
    tp2: float,
    tp3: float,
    layers: int = 1,
    leverage: int = 1,
) -> dict:
    """
    Routing eksekusi:
    - 'XAUUSD' -> MANUAL_CFD_ONLY (kirim sinyal manual ke Telegram, tidak masuk order otomatis)
    - 'XYZ:GOLD' / 'BTCUSDT' -> AUTO_EXECUTE di Binance Demo Futures
    """
    aset_up = aset.upper().strip()

    # 1. Jalur Broker CFD (Manual Only)
    if aset_up == "XAUUSD":
        jarak_sl = abs(entry_price - sl_price)
        lot_cfd = max(0.01, round(risk_usd / (jarak_sl * 100.0), 2)) if jarak_sl > 0 else 0.01
        return {
            "executed": False,
            "execution_type": "MANUAL_CFD_SIGNAL",
            "symbol": "OANDA:XAUUSD",
            "side": arah.upper(),
            "suggested_lot_cfd": lot_cfd,
            "entry": entry_price,
            "sl": sl_price,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "note": "Eksekusi manual di broker CFD sesuai chart OANDA/TradingView.",
        }

    # 2. Jalur Binance Demo (Otomatis untuk XYZ:GOLD & BTCUSDT)
    if not AUTO_EXECUTE_BINANCE:
        return {"executed": False, "execution_type": "DISABLED", "reason": "AUTO_EXECUTE_BINANCE=false"}

    ex = _klien_binance_futures()
    if ex is None:
        return {"executed": False, "execution_type": "ERROR", "reason": "BINANCE_API_KEY / SECRET belum diisi"}

    side_entry = "buy" if arah.upper() == "BUY" else "sell"
    side_exit = "sell" if side_entry == "buy" else "buy"
    jarak_sl = abs(entry_price - sl_price)
    if jarak_sl <= 0:
        return {"executed": False, "execution_type": "ERROR", "reason": "Jarak SL tidak valid"}

    if aset_up in ("XYZ:GOLD", "XYZGOLD", "GOLD_DIGITAL", "PAXGUSDT"):
        sym = _pilih_simbol_digital_gold(ex)
    else:
        sym = "BTC/USDT:USDT"

    try:
        ex.load_markets()
        ex.set_leverage(max(1, min(5, leverage)), sym)
    except Exception:
        pass

    # Sesuaikan harga entry jika simbol Binance memakai PAXG/USDT dengan selisih kecil dari xyz:GOLD
    ticker = ex.fetch_ticker(sym)
    harga_binance_now = float(ticker.get("last") or entry_price)
    offset_harga = harga_binance_now - entry_price

    total_qty_raw = risk_usd / jarak_sl
    bobot_tp = [
        ("TP1_40%", tp1 + offset_harga, 0.40),
        ("TP2_40%", tp2 + offset_harga, 0.40),
        ("TP3_RUNNER_20%", tp3 + offset_harga, 0.20),
    ]
    hasil = []
    jml_layer = max(1, min(3, layers))

    for layer_idx in range(jml_layer):
        rasio_geser = 0.0 if layer_idx == 0 else (0.30 if layer_idx == 1 else 0.60)
        harga_layer = (entry_price + offset_harga) - (rasio_geser * (entry_price - sl_price))
        qty_layer_raw = total_qty_raw / jml_layer

        try:
            qty_layer = float(ex.amount_to_precision(sym, qty_layer_raw))
            if qty_layer <= 0:
                continue

            if layer_idx == 0:
                ord_in = ex.create_order(sym, "market", side_entry, qty_layer)
            else:
                px_lim = float(ex.price_to_precision(sym, harga_layer))
                ord_in = ex.create_order(sym, "limit", side_entry, qty_layer, px_lim)

            px_sl = float(ex.price_to_precision(sym, sl_price + offset_harga))
            ex.create_order(
                sym,
                "STOP_MARKET",
                side_exit,
                qty_layer,
                params={"stopPrice": px_sl, "reduceOnly": True},
            )

            for lbl, px_tp_raw, porsi in bobot_tp:
                q_tp = float(ex.amount_to_precision(sym, qty_layer * porsi))
                if q_tp <= 0:
                    continue
                px_tp = float(ex.price_to_precision(sym, px_tp_raw))
                ex.create_order(
                    sym,
                    "TAKE_PROFIT_MARKET",
                    side_exit,
                    q_tp,
                    params={"stopPrice": px_tp, "reduceOnly": True},
                )

            hasil.append({"symbol": sym, "layer": layer_idx + 1, "qty": qty_layer, "order_id": ord_in.get("id")})
        except Exception as e:
            hasil.append({"symbol": sym, "layer": layer_idx + 1, "error": str(e)})

    return {
        "executed": True,
        "execution_type": "BINANCE_DEMO_AUTO",
        "mode": "DEMO" if BINANCE_DEMO_MODE else "LIVE",
        "source_signal": aset_up,
        "executed_symbol": sym,
        "orders": hasil,
    }
