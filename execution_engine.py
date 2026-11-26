import os
import time
import hmac
import hashlib
from urllib.parse import urlencode
import requests

# Muat .env otomatis jika dijalankan di Terminal Lokal
if os.path.exists(".env"):
    with open(".env", "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
BINANCE_SECRET = os.getenv("BINANCE_SECRET", "").strip()

# Endpoint resmi Binance Futures Demo Trading (demo.binance.com)
BINANCE_DEMO_BASE_URLS = [
    "https://demo-fapi.binance.com",
    "https://testnet.binancefuture.com",
]


def _sign_request(params: dict, secret: str) -> str:
    query = urlencode(params)
    signature = hmac.new(
        secret.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{query}&signature={signature}"


def _binance_demo_request(method: str, path: str, params: dict | None = None, signed: bool = True) -> dict:
    if not BINANCE_API_KEY or not BINANCE_SECRET:
        return {"error": "BINANCE_API_KEY atau BINANCE_SECRET kosong di .env"}

    params = dict(params or {})
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    last_err = ""

    for base_url in BINANCE_DEMO_BASE_URLS:
        try:
            req_params = dict(params)
            if signed:
                req_params["timestamp"] = int(time.time() * 1000)
                req_params["recvWindow"] = 10000
                query_str = _sign_request(req_params, BINANCE_SECRET)
                url = f"{base_url}{path}?{query_str}"
                resp = requests.request(method, url, headers=headers, timeout=12)
            else:
                url = f"{base_url}{path}"
                resp = requests.request(method, url, headers=headers, params=req_params, timeout=12)

            data = resp.json()
            if resp.status_code == 200:
                if isinstance(data, dict):
                    data["_base_url"] = base_url
                return data
            last_err = f"{base_url} HTTP {resp.status_code}: {data}"
        except Exception as e:
            last_err = f"{base_url} Error: {str(e)[:80]}"

    return {"error": last_err}


def _cek_spread_futures(symbol: str, default_spread: float) -> float:
    for base_url in BINANCE_DEMO_BASE_URLS:
        try:
            r = requests.get(f"{base_url}/fapi/v1/ticker/bookTicker", params={"symbol": symbol}, timeout=6)
            if r.status_code == 200:
                d = r.json()
                bid = float(d.get("bidPrice", 0))
                ask = float(d.get("askPrice", 0))
                if bid > 0 and ask >= bid:
                    return round(ask - bid, 4)
        except Exception:
            continue
    return default_spread


def _pilih_simbol_emas_binance() -> str:
    for sym in ("XAUUSDT", "PAXGUSDT"):
        for base_url in BINANCE_DEMO_BASE_URLS:
            try:
                r = requests.get(f"{base_url}/fapi/v1/ticker/price", params={"symbol": sym}, timeout=5)
                if r.status_code == 200 and float(r.json().get("price", 0)) > 0:
                    return sym
            except Exception:
                continue
    return "PAXGUSDT"


def ambil_info_akun_dan_spread() -> dict:
    spread_btc = _cek_spread_futures("BTCUSDT", 1.0)
    gold_sym = _pilih_simbol_emas_binance()
    spread_gold = _cek_spread_futures(gold_sym, 0.25)

    acc = _binance_demo_request("GET", "/fapi/v2/account", signed=True)
    if "error" in acc:
        print(f"[Binance Demo Warning] Gagal tarik saldo: {acc['error']}")
        return {
            "connected": False,
            "error": acc["error"],
            "usdt_total": 1000.0,
            "spread_digital_gold": spread_gold,
            "spread_btc": spread_btc,
        }

    # Ambil Total Margin Balance (USDT + USDC + BTC dalam nilai USD)
    margin_balance = float(acc.get("totalMarginBalance", 0.0))
    wallet_balance = float(acc.get("totalWalletBalance", 0.0))
    saldo_aktif = margin_balance if margin_balance > 0 else wallet_balance

    return {
        "connected": True,
        "endpoint": acc.get("_base_url", "https://demo-fapi.binance.com"),
        "usdt_total": round(saldo_aktif, 2),
        "spread_digital_gold": spread_gold,
        "spread_btc": spread_btc,
    }


def _aturan_presisi_simbol(symbol: str) -> tuple[int, int, float]:
    for base_url in BINANCE_DEMO_BASE_URLS:
        try:
            r = requests.get(f"{base_url}/fapi/v1/exchangeInfo", timeout=8)
            if r.status_code == 200:
                for s in r.json().get("symbols", []):
                    if s.get("symbol") == symbol:
                        qty_prec = int(s.get("quantityPrecision", 3))
                        price_prec = int(s.get("pricePrecision", 2))
                        min_qty = 0.001
                        for f in s.get("filters", []):
                            if f.get("filterType") == "LOT_SIZE":
                                min_qty = float(f.get("minQty", 0.001))
                        return qty_prec, price_prec, min_qty
        except Exception:
            continue
    return 3, 2, 0.001


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
    # XAUUSD murni tetap dikirim sebagai sinyal lot CFD manual untuk MT5/Exness
    if aset.upper() == "XAUUSD":
        sl_dist = max(abs(entry_price - sl_price), 1.0)
        # 1 lot standar XAUUSD = $100 per $1 pergerakan harga
        lot_cfd = max(0.01, round(risk_usd / (sl_dist * 100.0), 2))
        return {
            "execution_type": "MANUAL_CFD_SIGNAL",
            "asset": "XAUUSD",
            "direction": arah,
            "suggested_lot_cfd": lot_cfd,
            "entry": entry_price,
            "sl": sl_price,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
        }

    exec_symbol = "BTCUSDT" if aset.upper() == "BTCUSDT" else _pilih_simbol_emas_binance()
    qty_prec, price_prec, min_qty = _aturan_presisi_simbol(exec_symbol)

    # Set leverage awal (1x sesuai SOP keamanan)
    _binance_demo_request("POST", "/fapi/v1/leverage", {"symbol": exec_symbol, "leverage": max(1, int(leverage))}, signed=True)

    sl_dist = max(abs(entry_price - sl_price), entry_price * 0.002)
    total_qty = risk_usd / sl_dist
    per_layer_qty = max(min_qty, round(total_qty / max(1, layers), qty_prec))

    side_entry = "BUY" if arah.upper() == "BUY" else "SELL"
    side_exit = "SELL" if side_entry == "BUY" else "BUY"
    orders_sent = []

    for i in range(max(1, layers)):
        res_order = _binance_demo_request(
            "POST",
            "/fapi/v1/order",
            {
                "symbol": exec_symbol,
                "side": side_entry,
                "type": "MARKET",
                "quantity": per_layer_qty,
            },
            signed=True,
        )
        orders_sent.append({"layer": i + 1, "response": res_order})

    # Pasang Hard Stop Loss & Take Profit 1 otomatis di server Binance Demo
    sl_round = round(sl_price, price_prec)
    tp1_round = round(tp1, price_prec)

    res_sl = _binance_demo_request(
        "POST",
        "/fapi/v1/order",
        {
            "symbol": exec_symbol,
            "side": side_exit,
            "type": "STOP_MARKET",
            "stopPrice": sl_round,
            "closePosition": "true",
        },
        signed=True,
    )
    res_tp = _binance_demo_request(
        "POST",
        "/fapi/v1/order",
        {
            "symbol": exec_symbol,
            "side": side_exit,
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": tp1_round,
            "closePosition": "true",
        },
        signed=True,
    )

    return {
        "execution_type": "BINANCE_DEMO_AUTO",
        "executed_symbol": exec_symbol,
        "direction": side_entry,
        "qty_per_layer": per_layer_qty,
        "orders": orders_sent,
        "sl_order": res_sl,
        "tp1_order": res_tp,
    }


def kelola_auto_breakeven_binance() -> list[str]:
    actions = []
    pos_data = _binance_demo_request("GET", "/fapi/v2/positionRisk", signed=True)
    if not isinstance(pos_data, list):
        return actions

    for p in pos_data:
        amt = float(p.get("positionAmt", 0.0))
        if amt == 0.0:
            continue
        sym = p.get("symbol", "")
        entry = float(p.get("entryPrice", 0.0))
        mark = float(p.get("markPrice", 0.0))
        if entry <= 0 or mark <= 0:
            continue

        profit_pct = ((mark - entry) / entry * 100.0) if amt > 0 else ((entry - mark) / entry * 100.0)
        # Jika posisi sudah profit >= 0.8%, geser SL ke titik impas (Breakeven)
        if profit_pct >= 0.8:
            _, price_prec, _ = _aturan_presisi_simbol(sym)
            be_price = round(entry * (1.0005 if amt > 0 else 0.9995), price_prec)
            side_exit = "SELL" if amt > 0 else "BUY"
            res = _binance_demo_request(
                "POST",
                "/fapi/v1/order",
                {
                    "symbol": sym,
                    "side": side_exit,
                    "type": "STOP_MARKET",
                    "stopPrice": be_price,
                    "closePosition": "true",
                },
                signed=True,
            )
            if "orderId" in res:
                actions.append(f"{sym} SL -> BE ({be_price})")
    return actions
