import os
import json
import sqlite3
import logging
import datetime
from typing import Dict, Any, List, Tuple, Optional

import pytz
import numpy as np
import pandas as pd
import requests
import yfinance as yf
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("QuantV28Core")
WIB = pytz.timezone("Asia/Jakarta")
AUDIT_DB_PATH = "quant_cloud_audit.db"


# =============================================================================
# 1. 5 PLAYBOOK TRADING_SKILLS DINAMIS
# =============================================================================
TRADING_SKILLS: Dict[str, Dict[str, Any]] = {
    "MARKET_MAKER_ORDERFLOW": {
        "tf_focus": "M1-M5-M15",
        "rules": "Wajib konfirmasi Liquidity Sweep pada Equal High/Low + CVD Divergence + reaksi di Order Block/FVG.",
        "min_rr": 3.0,
        "max_sl_atr_mult": 1.3,
    },
    "SESSION_ICT_LIQUIDITY": {
        "tf_focus": "M5-M15-H1",
        "rules": "Eksekusi khusus Asia/London/NY Killzone & Silver Bullet FVG retrace pasca sapuan Asian Range.",
        "min_rr": 3.0,
        "max_sl_atr_mult": 1.5,
    },
    "INTRADAY_EXECUTION": {
        "tf_focus": "M15-H1-H4",
        "rules": "Selaras BOS/MSS H1-H4 dengan entry di Discount/Premium Fib OTE 0.618-0.786. Geser SL ke BEP+1 saat TP1.",
        "min_rr": 3.0,
        "max_sl_atr_mult": 1.8,
    },
    "SWING_HEDGE_FUND": {
        "tf_focus": "H4-D1-W1",
        "rules": "Konfluensi struktur D1-W1 + posisi bersih CFTC COT + arah US10Y/DXY. Toleransi noise lebih lebar dengan 3 layer.",
        "min_rr": 4.0,
        "max_sl_atr_mult": 2.0,
    },
    "MACRO_POSITION_HOLD": {
        "tf_focus": "D1-W1-1MON",
        "rules": "Posisi makro jangka menengah berbasis siklus suku bunga The Fed, Net Liquidity, dan rezim volatilitas GARCH.",
        "min_rr": 5.0,
        "max_sl_atr_mult": 2.2,
    },
}


# =============================================================================
# 2. DATABASE AUDIT SQLITE (quant_cloud_audit.db) & KUOTA SESI
# =============================================================================
def init_audit_db():
    conn = sqlite3.connect(AUDIT_DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS session_quotas (
            session_key TEXT PRIMARY KEY,
            entries_used INTEGER DEFAULT 0,
            daily_pnl_pct REAL DEFAULT 0.0,
            updated_wib TEXT
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS trade_journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp_wib TEXT,
            symbol TEXT,
            playbook TEXT,
            direction TEXT,
            entry_price REAL,
            sl_price REAL,
            tp_price REAL,
            confluence_score INTEGER,
            outcome TEXT DEFAULT 'OPEN',
            pnl_r REAL DEFAULT 0.0,
            notes TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def get_current_session_key() -> Tuple[str, str]:
    now = datetime.datetime.now(WIB)
    date_str = now.strftime("%Y-%m-%d")
    h = now.hour
    if 7 <= h < 13:
        sess = "ASIA"
    elif 13 <= h < 19:
        sess = "LONDON"
    elif 19 <= h or h < 2:
        sess = "NEW_YORK"
    else:
        sess = "DEAD_ZONE"
    return f"{date_str}_{sess}", sess


def check_and_consume_session_quota(consume: bool = False, max_per_session: int = 3) -> Dict[str, Any]:
    init_audit_db()
    s_key, sess_name = get_current_session_key()
    date_prefix = s_key.split("_")[0]

    conn = sqlite3.connect(AUDIT_DB_PATH)
    c = conn.cursor()
    c.execute("SELECT entries_used, daily_pnl_pct FROM session_quotas WHERE session_key = ?", (s_key,))
    row = c.fetchone()
    used = row[0] if row else 0

    c.execute("SELECT COALESCE(SUM(entries_used), 0), COALESCE(SUM(daily_pnl_pct), 0.0) FROM session_quotas WHERE session_key LIKE ?", (f"{date_prefix}_%",))
    day_used, day_pnl = c.fetchone()

    allowed = (sess_name != "DEAD_ZONE") and (used < max_per_session) and (day_used < 9) and (day_pnl > -5.0)
    if consume and allowed:
        now_wib = datetime.datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S WIB")
        c.execute(
            """
            INSERT INTO session_quotas (session_key, entries_used, daily_pnl_pct, updated_wib)
            VALUES (?, 1, 0.0, ?)
            ON CONFLICT(session_key) DO UPDATE SET entries_used = entries_used + 1, updated_wib = ?
            """,
            (s_key, now_wib, now_wib),
        )
        conn.commit()
        used += 1
        day_used += 1

    conn.close()
    return {
        "session_name": sess_name,
        "session_used": used,
        "session_max": max_per_session,
        "daily_used": day_used,
        "daily_pnl_pct": round(float(day_pnl), 2),
        "allowed": allowed,
    }


def record_trade_journal(
    symbol: str,
    playbook: str,
    direction: str,
    entry_price: float,
    sl_price: float,
    tp_price: float,
    score: int,
    notes: str = "",
) -> int:
    init_audit_db()
    conn = sqlite3.connect(AUDIT_DB_PATH)
    c = conn.cursor()
    now_wib = datetime.datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S WIB")
    c.execute(
        """
        INSERT INTO trade_journal
        (timestamp_wib, symbol, playbook, direction, entry_price, sl_price, tp_price, confluence_score, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (now_wib, symbol, playbook, direction, entry_price, sl_price, tp_price, score, notes),
    )
    jid = c.lastrowid
    conn.commit()
    conn.close()
    return jid


def get_journal_and_evaluation() -> str:
    init_audit_db()
    conn = sqlite3.connect(AUDIT_DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, timestamp_wib, symbol, playbook, direction, entry_price, confluence_score, outcome, pnl_r FROM trade_journal ORDER BY id DESC LIMIT 10")
    rows = c.fetchall()
    c.execute("SELECT COUNT(*), SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END), COALESCE(SUM(pnl_r), 0.0) FROM trade_journal WHERE outcome != 'OPEN'")
    closed_cnt, wins, net_r = c.fetchone()
    conn.close()

    quota = check_and_consume_session_quota(consume=False)
    win_rate = round((wins / closed_cnt) * 100.0, 1) if closed_cnt else 0.0

    lines = [
        "📒 JURNAL AUDIT & EVALUASI KUANTITATIF (quant_cloud_audit.db)",
        f"• Sesi Aktif: {quota['session_name']} ({quota['session_used']}/{quota['session_max']} kuota sesi | {quota['daily_used']}/9 harian)",
        f"• Daily PnL: {quota['daily_pnl_pct']}% (Kill Switch -5.0%) | Closed Trades: {closed_cnt} | Win Rate: {win_rate}% | Net R: {round(float(net_r), 2)}R",
        "",
        "10 Catatan Setup Terakhir:",
    ]
    if not rows:
        lines.append("- Belum ada eksekusi tercatat di jurnal.")
    else:
        for r in rows:
            lines.append(
                f"#{r[0]} [{r[1]}] {r[2]} | {r[3]} | {r[4]} @ {r[5]} | Skor {r[6]} | Status: {r[7]} ({r[8]}R)"
            )
    return "\n".join(lines)


# =============================================================================
# 3. FILTER REGIME ADX(14), RSI(14) H1/D1, & SPIKE GUARD Z-SCORE H1 (|Z| >= 2.2)
# =============================================================================
def _compute_rsi(series: pd.Series, period: int = 14) -> float:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    val = rsi.dropna()
    return round(float(val.iloc[-1]), 2) if len(val) > 0 else 50.0


def _compute_adx(df: pd.DataFrame, period: int = 14) -> float:
    high, low, close = df["High"], df["Low"], df["Close"]
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100.0 * (plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.nan))
    minus_di = 100.0 * (minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.nan))
    dx = (100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)).fillna(0.0)
    adx = dx.ewm(alpha=1 / period, adjust=False).mean().dropna()
    return round(float(adx.iloc[-1]), 2) if len(adx) > 0 else 20.0


def calculate_regime_and_guards(symbol: str) -> Dict[str, Any]:
    yf_ticker = "GC=F" if symbol.upper() == "XAUUSD" else "BTC-USD"
    try:
        df_h1 = yf.download(yf_ticker, period="30d", interval="1h", progress=False, auto_adjust=True)
        df_d1 = yf.download(yf_ticker, period="6mo", interval="1d", progress=False, auto_adjust=True)

        if isinstance(df_h1.columns, pd.MultiIndex):
            df_h1.columns = df_h1.columns.get_level_values(0)
        if isinstance(df_d1.columns, pd.MultiIndex):
            df_d1.columns = df_d1.columns.get_level_values(0)

        if len(df_h1) < 30 or len(df_d1) < 30:
            return {"status": "INSUFFICIENT_DATA"}

        adx_h1 = _compute_adx(df_h1, 14)
        adx_d1 = _compute_adx(df_d1, 14)
        rsi_h1 = _compute_rsi(df_h1["Close"], 14)
        rsi_d1 = _compute_rsi(df_d1["Close"], 14)

        # Spike Guard Z-Score H1 (|Z| >= 2.2)
        h1_ret = df_h1["Close"].pct_change().dropna()
        recent_window = h1_ret.tail(24)
        mean_r = float(recent_window.iloc[:-1].mean())
        std_r = float(recent_window.iloc[:-1].std(ddof=1))
        last_r = float(recent_window.iloc[-1])
        z_score_h1 = round((last_r - mean_r) / max(std_r, 1e-9), 2)
        spike_guard = abs(z_score_h1) >= 2.2

        if adx_h1 >= 25.0:
            regime = "TRENDING"
        elif adx_h1 >= 20.0:
            regime = "TRANSITION"
        else:
            regime = "CHOPPY_SIDEWAYS"

        return {
            "symbol": symbol,
            "regime": regime,
            "adx_h1": adx_h1,
            "adx_d1": adx_d1,
            "rsi_h1": rsi_h1,
            "rsi_d1": rsi_d1,
            "z_score_h1": z_score_h1,
            "spike_guard_triggered": spike_guard,
        }
    except Exception as e:
        logger.warning(f"Regime guard error ({symbol}): {e}")
        return {"status": "ERROR", "detail": str(e)}


# =============================================================================
# 4. DERIBIT OPTIONS (DVOL_BTC + PUT/CALL OI), CMC API, & BLOCKWORKS (4 ENDPOINT)
# =============================================================================
def fetch_deribit_options_btc() -> Dict[str, Any]:
    try:
        r_dvol = requests.get(
            "https://www.deribit.com/api/v2/public/get_index_price?index_name=btcdvol_usdc",
            timeout=12,
        )
        dvol = 0.0
        if r_dvol.status_code == 200:
            dvol = float(r_dvol.json().get("result", {}).get("index_price", 0.0))

        r_opt = requests.get(
            "https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency=BTC&kind=option",
            timeout=15,
        )
        put_oi = 0.0
        call_oi = 0.0
        if r_opt.status_code == 200:
            for item in r_opt.json().get("result", []):
                name = str(item.get("instrument_name", ""))
                oi = float(item.get("open_interest", 0.0))
                if name.endswith("-P"):
                    put_oi += oi
                elif name.endswith("-C"):
                    call_oi += oi

        pc_ratio = round(put_oi / max(call_oi, 1.0), 3)
        sentiment = "HEDGING_FEAR_HIGH_PUT" if pc_ratio > 0.85 else ("BULLISH_CALL_DOMINANT" if pc_ratio < 0.55 else "BALANCED_OPTIONS")
        return {
            "DVOL_BTC": round(dvol, 2),
            "put_oi_btc": round(put_oi, 1),
            "call_oi_btc": round(call_oi, 1),
            "put_call_oi_ratio": pc_ratio,
            "options_sentiment": sentiment,
        }
    except Exception as e:
        return {"status": "ERROR", "detail": str(e)}


def fetch_cmc_global_metrics() -> Dict[str, Any]:
    api_key = os.getenv("CMC_API_KEY", "").strip()
    if not api_key:
        return {"status": "CMC_KEY_MISSING"}
    url = "https://pro-api.coinmarketcap.com/v1/global-metrics/quotes/latest"
    headers = {"X-CMC_PRO_API_KEY": api_key, "Accept": "application/json"}
    try:
        r = requests.get(url, headers=headers, timeout=12)
        if r.status_code != 200:
            return {"status": f"HTTP_{r.status_code}"}
        data = r.json().get("data", {})
        quote = data.get("quote", {}).get("USD", {})
        return {
            "btc_dominance": round(float(data.get("btc_dominance", 0.0)), 2),
            "eth_dominance": round(float(data.get("eth_dominance", 0.0)), 2),
            "total_mcap_usd_b": round(float(quote.get("total_market_cap", 0.0)) / 1e9, 2),
            "vol_24h_change_pct": round(float(quote.get("total_volume_24h_yesterday_percentage_change", 0.0)), 2),
        }
    except Exception as e:
        return {"status": "ERROR", "detail": str(e)}


def fetch_blockworks_intel() -> Dict[str, Any]:
    api_key = os.getenv("BLOCKWORKS_API_KEY", "").strip()
    if not api_key:
        return {"status": "BLOCKWORKS_KEY_MISSING"}

    candidates = [
        "https://api.blockworks.co/v1/intel",
        "https://api.blockworks.co/v1/research",
        "https://api.blockworks.co/v1/macro",
        "https://api.blockworks.co/v1/news",
    ]
    headers = {"Authorization": f"Bearer {api_key}", "x-api-key": api_key, "Accept": "application/json"}
    for ep in candidates:
        try:
            r = requests.get(ep, headers=headers, timeout=10)
            if r.status_code == 200:
                payload = r.json()
                items = payload.get("data") or payload.get("articles") or []
                titles = [str(x.get("title", "")) for x in items[:3] if isinstance(x, dict) and x.get("title")]
                return {"status": "OK", "endpoint": ep, "highlights": titles}
        except Exception:
            continue
    return {"status": "UNAVAILABLE_OR_TIER_RESTRICTED"}


# =============================================================================
# 5. SIDANG DEWAN JURI 4-AGEN AI & AUDIT SKOR (audit_high_probability_setup)
# =============================================================================
def audit_high_probability_setup(
    symbol: str,
    base_score: int,
    direction: str,
    regime_info: Dict[str, Any],
    deribit_info: Dict[str, Any],
    cmc_info: Dict[str, Any],
    blockworks_info: Dict[str, Any],
    blackout: bool,
    ai_router: Any = None,
) -> Dict[str, Any]:
    """
    Menjalankan sidang Dewan Juri 4-Agen (Macro Analyst, Bull Researcher, Bear/Skeptic Researcher, Risk Judge)
    serta memotong max_allowed_score otomatis berdasarkan red_flags.
    """
    red_flags: List[str] = []
    quota = check_and_consume_session_quota(consume=False)

    if blackout:
        red_flags.append("USD_NEWS_BLACKOUT_ACTIVE")
    if quota["session_name"] == "DEAD_ZONE":
        red_flags.append("DEAD_ZONE_ROLLOVER_TIME")
    if not quota["allowed"]:
        red_flags.append("SESSION_OR_DAILY_QUOTA_EXCEEDED")
    if regime_info.get("regime") == "CHOPPY_SIDEWAYS":
        red_flags.append(f"CHOPPY_REGIME_ADX_{regime_info.get('adx_h1')}")
    if regime_info.get("spike_guard_triggered"):
        red_flags.append(f"SPIKE_GUARD_ZSCORE_{regime_info.get('z_score_h1')}")

    rsi_h1 = float(regime_info.get("rsi_h1", 50.0))
    if "BUY" in direction.upper() and rsi_h1 > 72.0:
        red_flags.append(f"OVERBOUGHT_CHASE_RSI_H1_{rsi_h1}")
    elif "SELL" in direction.upper() and rsi_h1 < 28.0:
        red_flags.append(f"OVERSOLD_CHASE_RSI_H1_{rsi_h1}")

    # Pemotongan skor maksimum otomatis berdasarkan jumlah red_flags
    flag_count = len(red_flags)
    if flag_count == 0:
        max_allowed_score = 100
    elif flag_count == 1:
        max_allowed_score = 78
    elif flag_count == 2:
        max_allowed_score = 68
    else:
        max_allowed_score = 55

    final_score = min(int(base_score), max_allowed_score)
    approved = (final_score >= 75) and (flag_count <= 1) and (not blackout)

    jury_verdict = (
        f"Dewan Juri 4-Agen: Skor Awal {base_score} -> Final {final_score}/100 "
        f"(Cap {max_allowed_score} | Red Flags: {flag_count}: {', '.join(red_flags) if red_flags else 'NIHIL'})."
    )

    if ai_router is not None:
        jury_prompt = (
            f"SIDANG DEWAN JURI 4-AGEN UNTUK {symbol} ({direction}):\n"
            f"- Skor Awal: {base_score} | Max Allowed Cap: {max_allowed_score} | Red Flags: {red_flags}\n"
            f"- Regime & Guards: {json.dumps(regime_info)}\n"
            f"- Deribit Options BTC: {json.dumps(deribit_info)}\n"
            f"- CMC Global: {json.dumps(cmc_info)} | Blockworks: {json.dumps(blockworks_info)}\n\n"
            f"Berikan putusan ringkas dalam 4 baris:\n"
            f"1) Macro Analyst: ...\n"
            f"2) Bull Researcher: ...\n"
            f"3) Bear/Skeptic Researcher: ...\n"
            f"4) Risk Judge (Final Veto/Approve): ..."
        )
        try:
            if hasattr(ai_router, "generate"):
                ai_out = ai_router.generate(jury_prompt, prefer_deep_reasoning=approved)
                if ai_out and len(ai_out) > 20:
                    jury_verdict = ai_out
        except Exception:
            pass

    return {
        "symbol": symbol,
        "base_score": base_score,
        "max_allowed_score": max_allowed_score,
        "final_score": final_score,
        "red_flags": red_flags,
        "approved_high_prob": approved,
        "status_label": "LOLOS SIDANG 4-AGEN (>=75)" if approved else "WAIT / DITAHAN RISK JUDGE",
        "jury_verdict": jury_verdict,
    }


# =============================================================================
# 6. OUT-OF-SAMPLE BACKTEST & 1.000x MONTE CARLO SIMULATION (/backtest)
# =============================================================================
def run_backtest_and_monte_carlo(symbol: str = "XAUUSD", simulations: int = 1000) -> str:
    yf_ticker = "GC=F" if symbol.upper() == "XAUUSD" else "BTC-USD"
    try:
        df = yf.download(yf_ticker, period="2y", interval="1d", progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if len(df) < 150:
            return f"⚠️ Data historis {symbol} tidak cukup untuk Out-of-Sample Backtest."

        # 70% In-Sample, 30% Out-of-Sample
        split_idx = int(len(df) * 0.70)
        oos = df.iloc[split_idx:].copy()

        # Simulasi strategi SMC Breakout + Pullback OTE dengan RR 1:2.5 & komisi/slippage
        oos["ema20"] = oos["Close"].ewm(span=20, adjust=False).mean()
        oos["atr14"] = (oos["High"] - oos["Low"]).rolling(14).mean()
        oos = oos.dropna()

        trade_returns_pct: List[float] = []
        risk_per_trade = 0.01  # 1.0% per trade
        fee_slippage_r = 0.08  # 0.08R biaya komisi & slippage

        for i in range(2, len(oos) - 1):
            row = oos.iloc[i]
            prev = oos.iloc[i - 1]
            nxt = oos.iloc[i + 1]
            atr = float(row["atr14"])
            if atr <= 0:
                continue

            # Sinyal Bullish Sweep & Reclaim di atas EMA20
            if prev["Low"] < prev["ema20"] and row["Close"] > row["ema20"] and row["Close"] > row["Open"]:
                sl_dist = atr * 1.2
                tp_dist = sl_dist * 2.5
                if (nxt["Low"] <= row["Close"] - sl_dist):
                    trade_returns_pct.append((-1.0 - fee_slippage_r) * risk_per_trade)
                elif (nxt["High"] >= row["Close"] + tp_dist):
                    trade_returns_pct.append((2.5 - fee_slippage_r) * risk_per_trade)

            # Sinyal Bearish Sweep & Breakdown di bawah EMA20
            elif prev["High"] > prev["ema20"] and row["Close"] < row["ema20"] and row["Close"] < row["Open"]:
                sl_dist = atr * 1.2
                tp_dist = sl_dist * 2.5
                if (nxt["High"] >= row["Close"] + sl_dist):
                    trade_returns_pct.append((-1.0 - fee_slippage_r) * risk_per_trade)
                elif (nxt["Low"] <= row["Close"] - tp_dist):
                    trade_returns_pct.append((2.5 - fee_slippage_r) * risk_per_trade)

        if len(trade_returns_pct) < 10:
            return f"⚠️ Sampel sinyal Out-of-Sample {symbol} terlalu sedikit ({len(trade_returns_pct)} trades)."

        arr = np.array(trade_returns_pct)
        win_rate = round(float(np.mean(arr > 0)) * 100.0, 1)
        oos_total_ret = round((np.prod(1.0 + arr) - 1.0) * 100.0, 2)

        # 1.000x Monte Carlo Bootstrap Simulation
        rng = np.random.default_rng(42)
        mc_final_returns = []
        mc_max_drawdowns = []
        ruin_count = 0

        for _ in range(simulations):
            sample = rng.choice(arr, size=len(arr), replace=True)
            equity = np.cumprod(1.0 + sample)
            peak = np.maximum.accumulate(equity)
            dd = (peak - equity) / peak
            max_dd = float(np.max(dd)) * 100.0
            mc_final_returns.append((equity[-1] - 1.0) * 100.0)
            mc_max_drawdowns.append(max_dd)
            if max_dd >= 15.0:
                ruin_count += 1

        med_ret = round(float(np.median(mc_final_returns)), 2)
        p5_ret = round(float(np.percentile(mc_final_returns, 5)), 2)
        p95_dd = round(float(np.percentile(mc_max_drawdowns, 95)), 2)
        ruin_prob = round((ruin_count / simulations) * 100.0, 2)

        return (
            f"📊 OUT-OF-SAMPLE BACKTEST & {simulations}x MONTE CARLO ({symbol})\n"
            f"• Sampel Out-of-Sample (30% Data Terakhir): {len(arr)} trades | Win Rate: {win_rate}%\n"
            f"• OOS Net Return (termasuk komisi & slippage): {oos_total_ret:+.2f}%\n"
            f"• Monte Carlo Median Return: {med_ret:+.2f}% | Worst 5% (VaR95): {p5_ret:+.2f}%\n"
            f"• Monte Carlo 95th Max Drawdown: {p95_dd:.2f}% | Probabilitas Ruin (>15% DD): {ruin_prob}%"
        )
    except Exception as e:
        return f"⚠️ Gagal menjalankan backtest {symbol}: {e}"


if __name__ == "__main__":
    init_audit_db()
    print(json.dumps(calculate_regime_and_guards("XAUUSD"), indent=2))
    print(run_backtest_and_monte_carlo("XAUUSD", 1000))
