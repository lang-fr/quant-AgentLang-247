import os
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
import requests
from dotenv import load_dotenv

load_dotenv()

# ==============================================================================
# KONFIGURASI LOGGING AUDIT RISIKO
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("risk_audit.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("InstitutionalRiskEngine")


class InstitutionalRiskEngine:
    """
    Penjaga Gawang Modal (Chief Risk Officer) untuk Sistem AI Quant 24/7.
    Mencegat setiap sinyal dari Alpha/AI Engine sebelum dikirim ke Execution Engine.
    """

    def __init__(self, db_path: str = "quant_risk_state.db"):
        self.db_path = db_path
        self.default_risk_pct = float(os.getenv("DEFAULT_RISK_PCT", "1.0"))
        self.choppy_risk_pct = float(os.getenv("CHOPPY_RISK_PCT", "0.5"))
        self.max_confluence_risk_pct = float(os.getenv("MAX_CONFLUENCE_RISK_PCT", "2.0"))
        self.max_daily_drawdown_pct = float(os.getenv("MAX_DAILY_DRAWDOWN_PCT", "3.0"))
        self.max_entries_per_session = int(os.getenv("MAX_ENTRIES_PER_SESSION", "3"))
        self.max_entries_per_day = int(os.getenv("MAX_ENTRIES_PER_DAY", "9"))
        self.max_layers_per_setup = int(os.getenv("MAX_LAYERS_PER_SETUP", "3"))
        self.min_rr_ratio = float(os.getenv("MIN_RR_RATIO", "2.5"))
        self.max_spread_xau = float(os.getenv("MAX_SPREAD_PIPS_XAU", "3.0"))
        self.max_spread_btc = float(os.getenv("MAX_SPREAD_PIPS_BTC", "25.0"))
        self.news_block_window_min = int(os.getenv("NEWS_BLOCK_WINDOW_MIN", "45"))
        self._init_db()

    def _init_db(self) -> None:
        try:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS risk_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp_utc TEXT,
                    date_utc TEXT,
                    session_name TEXT,
                    symbol TEXT,
                    direction TEXT,
                    approved INTEGER,
                    reason TEXT,
                    risk_pct REAL,
                    lot_per_layer REAL,
                    layers INTEGER,
                    entry_price REAL,
                    stop_loss REAL,
                    tp1 REAL,
                    tp2 REAL,
                    tp3 REAL
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_equity_tracker (
                    date_utc TEXT PRIMARY KEY,
                    starting_balance REAL,
                    current_balance REAL,
                    kill_switch_active INTEGER DEFAULT 0
                )
                """
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Gagal inisialisasi database risiko: {e}")

    @staticmethod
    def get_current_session(now_utc: Optional[datetime] = None) -> str:
        now = now_utc or datetime.now(timezone.utc)
        hour = now.hour
        if 0 <= hour < 7:
            return "ASIA"
        elif 7 <= hour < 13:
            return "LONDON"
        elif 13 <= hour < 21:
            return "NEW_YORK"
        return "OFF_SESSION"

    def update_and_check_daily_drawdown(self, current_balance: float) -> Dict:
        """
        Memantau ekuitas harian dan mengaktifkan Kill Switch jika rugi >= MAX_DAILY_DRAWDOWN_PCT.
        """
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute(
                "SELECT starting_balance, kill_switch_active FROM daily_equity_tracker WHERE date_utc = ?",
                (today_str,),
            )
            row = c.fetchone()

            if row is None:
                starting_balance = current_balance
                kill_switch = 0
                c.execute(
                    """
                    INSERT INTO daily_equity_tracker (date_utc, starting_balance, current_balance, kill_switch_active)
                    VALUES (?, ?, ?, ?)
                    """,
                    (today_str, starting_balance, current_balance, kill_switch),
                )
            else:
                starting_balance, kill_switch = float(row[0]), int(row[1])
                drawdown_pct = (
                    ((starting_balance - current_balance) / starting_balance) * 100.0
                    if starting_balance > 0
                    else 0.0
                )
                if drawdown_pct >= self.max_daily_drawdown_pct:
                    kill_switch = 1
                c.execute(
                    """
                    UPDATE daily_equity_tracker
                    SET current_balance = ?, kill_switch_active = ?
                    WHERE date_utc = ?
                    """,
                    (current_balance, kill_switch, today_str),
                )

            conn.commit()
            conn.close()

            drawdown_pct = (
                max(0.0, ((starting_balance - current_balance) / starting_balance) * 100.0)
                if starting_balance > 0
                else 0.0
            )
            return {
                "kill_switch_active": bool(kill_switch),
                "starting_balance": round(starting_balance, 2),
                "current_balance": round(current_balance, 2),
                "daily_drawdown_pct": round(drawdown_pct, 2),
            }
        except Exception as e:
            logger.error(f"Error pengecekan drawdown harian: {e}")
            return {"kill_switch_active": False, "error": str(e)}

    def get_quota_usage(self) -> Dict:
        """
        Menghitung jumlah entry yang sudah disetujui pada sesi aktif dan hari ini.
        """
        now_utc = datetime.now(timezone.utc)
        today_str = now_utc.strftime("%Y-%m-%d")
        session_name = self.get_current_session(now_utc)
        try:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute(
                """
                SELECT COUNT(*) FROM risk_decisions
                WHERE date_utc = ? AND session_name = ? AND approved = 1
                """,
                (today_str, session_name),
            )
            session_used = int(c.fetchone()[0])

            c.execute(
                """
                SELECT COUNT(*) FROM risk_decisions
                WHERE date_utc = ? AND approved = 1
                """,
                (today_str,),
            )
            daily_used = int(c.fetchone()[0])
            conn.close()

            return {
                "session_name": session_name,
                "session_used": session_used,
                "session_remaining": max(0, self.max_entries_per_session - session_used),
                "daily_used": daily_used,
                "daily_remaining": max(0, self.max_entries_per_day - daily_used),
            }
        except Exception as e:
            logger.error(f"Error pengecekan kuota sesi: {e}")
            return {
                "session_name": session_name,
                "session_used": 0,
                "session_remaining": self.max_entries_per_session,
                "daily_used": 0,
                "daily_remaining": self.max_entries_per_day,
            }

    def check_tier1_usd_news(self) -> Dict:
        """
        Mengecek jadwal berita High-Impact USD gratis dari feed ForexFactory mingguan.
        Memblokir entry jika ada rilis berita dalam ±NEWS_BLOCK_WINDOW_MIN menit.
        """
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        try:
            resp = requests.get(url, timeout=8)
            resp.raise_for_status()
            events = resp.json()
            now_utc = datetime.now(timezone.utc)
            blocking_events = []

            for ev in events:
                if ev.get("country") == "USD" and ev.get("impact") == "High":
                    ev_date = ev.get("date", "")
                    if ev_date:
                        ev_dt = datetime.fromisoformat(ev_date).astimezone(timezone.utc)
                        diff_min = abs((ev_dt - now_utc).total_seconds()) / 60.0
                        if diff_min <= self.news_block_window_min:
                            blocking_events.append(f"{ev.get('title')} ({int(diff_min)}m)")

            return {
                "blocked": len(blocking_events) > 0,
                "events": blocking_events,
            }
        except Exception as e:
            logger.warning(f"Feed kalender berita tidak terjangkau (lanjut dengan hati-hati): {e}")
            return {"blocked": False, "events": [], "warning": str(e)}

    def calculate_dynamic_risk_pct(
        self,
        win_rate: float = 0.55,
        rr_ratio: float = 2.5,
        confluence_score: int = 75,
        market_regime: str = "TRENDING",
    ) -> float:
        """
        Menghitung persentase risiko per trade menggunakan Half-Kelly Criterion
        yang dikalibrasi dengan Market Regime dan Skor Konfluensi Multi-Engine.
        """
        if market_regime.upper() in ["CHOPPY", "CHOPPY_SIDEWAYS", "RANGING"]:
            return self.choppy_risk_pct

        # Formula Half-Kelly: f* = 0.5 * (W - (1 - W) / R)
        safe_rr = max(1.0, rr_ratio)
        kelly_fraction = 0.5 * (win_rate - ((1.0 - win_rate) / safe_rr))
        kelly_pct = round(kelly_fraction * 100.0, 2)

        if confluence_score >= 85 and kelly_pct >= self.max_confluence_risk_pct:
            return self.max_confluence_risk_pct

        # Klem aman antara CHOPPY_RISK_PCT (0.5%) dan DEFAULT_RISK_PCT (1.0%)
        return max(self.choppy_risk_pct, min(self.default_risk_pct, kelly_pct if kelly_pct > 0 else self.default_risk_pct))

    def _log_decision(
        self,
        symbol: str,
        direction: str,
        approved: bool,
        reason: str,
        risk_pct: float = 0.0,
        lot_per_layer: float = 0.0,
        layers: int = 1,
        entry_price: float = 0.0,
        stop_loss: float = 0.0,
        tp1: float = 0.0,
        tp2: float = 0.0,
        tp3: float = 0.0,
    ) -> None:
        now_utc = datetime.now(timezone.utc)
        try:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            c.execute(
                """
                INSERT INTO risk_decisions (
                    timestamp_utc, date_utc, session_name, symbol, direction,
                    approved, reason, risk_pct, lot_per_layer, layers,
                    entry_price, stop_loss, tp1, tp2, tp3
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now_utc.isoformat(),
                    now_utc.strftime("%Y-%m-%d"),
                    self.get_current_session(now_utc),
                    symbol,
                    direction,
                    1 if approved else 0,
                    reason,
                    risk_pct,
                    lot_per_layer,
                    layers,
                    entry_price,
                    stop_loss,
                    tp1,
                    tp2,
                    tp3,
                ),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Gagal menyimpan log keputusan risiko: {e}")

    def evaluate_trade_signal(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        take_profit_final: float,
        account_balance: float,
        pip_value_per_lot: float = 1.0,
        pip_size: float = 0.1,
        spread_pips: float = 1.5,
        layers: int = 1,
        confluence_score: int = 80,
        market_regime: str = "TRENDING",
        z_score_h1: float = 0.0,
        historical_win_rate: float = 0.58,
        open_positions: Optional[List[Dict]] = None,
        correlation_matrix: Optional[Dict[str, float]] = None,
    ) -> Dict:
        """
        Gerbang validasi utama sebelum eksekusi order.
        Mengembalikan dict lengkap berisi status 'approved', 'lot_per_layer', dan level TP/BEP.
        """
        symbol_clean = symbol.upper().strip()
        dir_clean = direction.upper().strip()
        open_positions = open_positions or []
        correlation_matrix = correlation_matrix or {}

        # 1. Validasi Arah & Hard Stop-Loss
        if dir_clean not in ["BUY", "SELL"]:
            msg = f"DITOLAK: Arah sinyal tidak valid ({direction})."
            self._log_decision(symbol_clean, dir_clean, False, msg)
            return {"approved": False, "reason": msg}

        if stop_loss <= 0 or entry_price <= 0 or take_profit_final <= 0:
            msg = "DITOLAK: Entry, Hard Stop-Loss, dan Take-Profit wajib > 0."
            self._log_decision(symbol_clean, dir_clean, False, msg)
            return {"approved": False, "reason": msg}

        if dir_clean == "BUY" and not (stop_loss < entry_price < take_profit_final):
            msg = "DITOLAK: Struktur harga BUY tidak valid (wajib SL < Entry < TP)."
            self._log_decision(symbol_clean, dir_clean, False, msg)
            return {"approved": False, "reason": msg}

        if dir_clean == "SELL" and not (take_profit_final < entry_price < stop_loss):
            msg = "DITOLAK: Struktur harga SELL tidak valid (wajib TP < Entry < SL)."
            self._log_decision(symbol_clean, dir_clean, False, msg)
            return {"approved": False, "reason": msg}

        # 2. Daily Drawdown Kill Switch (Batas Kerugian Harian)
        dd_state = self.update_and_check_daily_drawdown(account_balance)
        if dd_state.get("kill_switch_active"):
            msg = (
                f"KILL SWITCH AKTIF: Drawdown harian {dd_state.get('daily_drawdown_pct')}% "
                f"mencapai batas {self.max_daily_drawdown_pct}%. Sistem hibernasi."
            )
            logger.critical(msg)
            self._log_decision(symbol_clean, dir_clean, False, msg)
            return {"approved": False, "reason": msg}

        # 3. Kalender Berita Tier-1 USD (±45 Menit)
        news_state = self.check_tier1_usd_news()
        if news_state.get("blocked"):
            msg = f"CIRCUIT BREAKER BERITA: Rilis High-Impact USD aktif {news_state.get('events')}."
            logger.warning(msg)
            self._log_decision(symbol_clean, dir_clean, False, msg)
            return {"approved": False, "reason": msg}

        # 4. Z-Score Spike Guard (Anomali Volatilitas Candle)
        if abs(z_score_h1) >= 2.5:
            msg = f"SPIKE GUARD: Z-Score H1 ekstrem ({z_score_h1}), tunda entry."
            logger.warning(msg)
            self._log_decision(symbol_clean, dir_clean, False, msg)
            return {"approved": False, "reason": msg}

        # 5. Kuota Sesi & Kuota Harian (Overtrading Protection)
        quota = self.get_quota_usage()
        if quota["session_remaining"] <= 0:
            msg = f"BATAS SESI ({quota['session_name']}): Kuota {self.max_entries_per_session} entry habis."
            self._log_decision(symbol_clean, dir_clean, False, msg)
            return {"approved": False, "reason": msg}

        if quota["daily_remaining"] <= 0:
            msg = f"BATAS HARIAN: Kuota {self.max_entries_per_day} entry hari ini habis."
            self._log_decision(symbol_clean, dir_clean, False, msg)
            return {"approved": False, "reason": msg}

        # 6. Validasi Jumlah Layer (1 - 3 Layer)
        if not (1 <= layers <= self.max_layers_per_setup):
            msg = f"BATAS LAYER: Jumlah layer ({layers}) di luar rentang 1-{self.max_layers_per_setup}."
            self._log_decision(symbol_clean, dir_clean, False, msg)
            return {"approved": False, "reason": msg}

        # 7. Filter Spread Broker
        max_allowed_spread = self.max_spread_btc if "BTC" in symbol_clean else self.max_spread_xau
        if spread_pips > max_allowed_spread:
            msg = f"FILTER SPREAD: Spread {symbol_clean} ({spread_pips} pips) > batas {max_allowed_spread} pips."
            self._log_decision(symbol_clean, dir_clean, False, msg)
            return {"approved": False, "reason": msg}

        # 8. Filter Korelasi Portofolio (> 80%)
        for pos in open_positions:
            existing_sym = str(pos.get("symbol", "")).upper()
            corr_val = abs(float(correlation_matrix.get(existing_sym, 0.0)))
            if existing_sym != symbol_clean and corr_val > 0.80:
                msg = f"FILTER KORELASI: {symbol_clean} berkorelasi {corr_val:.2f} (>0.80) dengan posisi aktif {existing_sym}."
                self._log_decision(symbol_clean, dir_clean, False, msg)
                return {"approved": False, "reason": msg}

        # 9. Kalkulasi Risk/Reward Ratio & Jarak SL
        sl_distance = abs(entry_price - stop_loss)
        tp_distance = abs(take_profit_final - entry_price)
        rr_ratio = round(tp_distance / sl_distance, 2) if sl_distance > 0 else 0.0

        if rr_ratio < self.min_rr_ratio:
            msg = f"FILTER R:R: Risk/Reward ({rr_ratio}) di bawah minimum institusional ({self.min_rr_ratio})."
            self._log_decision(symbol_clean, dir_clean, False, msg)
            return {"approved": False, "reason": msg}

        # 10. Hitung Ukuran Posisi (Position Sizing) & 3 Level Take Profit + Auto-BEP
        risk_pct = self.calculate_dynamic_risk_pct(
            win_rate=historical_win_rate,
            rr_ratio=rr_ratio,
            confluence_score=confluence_score,
            market_regime=market_regime,
        )
        total_risk_usd = account_balance * (risk_pct / 100.0)
        risk_per_layer_usd = total_risk_usd / layers
        sl_pips = sl_distance / pip_size

        raw_lot = risk_per_layer_usd / max(0.0001, (sl_pips * pip_value_per_lot))
        lot_per_layer = max(0.01, round(raw_lot, 2))

        sign = 1.0 if dir_clean == "BUY" else -1.0
        tp1 = round(entry_price + sign * (sl_distance * 1.5), 4)
        tp2 = round(entry_price + sign * (sl_distance * 2.5), 4)
        tp3 = round(take_profit_final, 4)
        breakeven_sl = round(entry_price + sign * (pip_size * 1.0), 4)  # Entry + 1 pip buffer

        msg = (
            f"APPROVED [{quota['session_name']}]: Risk {risk_pct}% (${total_risk_usd:.2f}) | "
            f"{layers} layer x {lot_per_layer} lot | R:R 1:{rr_ratio}"
        )
        logger.info(msg)
        self._log_decision(
            symbol_clean,
            dir_clean,
            True,
            msg,
            risk_pct=risk_pct,
            lot_per_layer=lot_per_layer,
            layers=layers,
            entry_price=entry_price,
            stop_loss=stop_loss,
            tp1=tp1,
            tp2=tp2,
            tp3=tp3,
        )

        return {
            "approved": True,
            "reason": msg,
            "session": quota["session_name"],
            "risk_pct": risk_pct,
            "total_risk_usd": round(total_risk_usd, 2),
            "layers": layers,
            "lot_per_layer": lot_per_layer,
            "rr_ratio": rr_ratio,
            "stop_loss": round(stop_loss, 4),
            "breakeven_sl_after_tp1": breakeven_sl,
            "take_profits": {
                "TP1_partial_50pct": tp1,
                "TP2_partial_30pct": tp2,
                "TP3_runner_20pct": tp3,
            },
        }


# ==============================================================================
# UJI MANDIRI OFFLINE (JALANKAN: python risk_engine.py)
# ==============================================================================
if __name__ == "__main__":
    engine = InstitutionalRiskEngine(db_path="test_risk_state.db")
    sample_verdict = engine.evaluate_trade_signal(
        symbol="XAUUSD",
        direction="BUY",
        entry_price=3895.00,
        stop_loss=3880.00,
        take_profit_final=3940.00,
        account_balance=1000.0,
        pip_value_per_lot=1.0,
        pip_size=0.1,
        spread_pips=1.8,
        layers=2,
        confluence_score=86,
        market_regime="TRENDING",
        z_score_h1=0.65,
    )
    print(json.dumps(sample_verdict, indent=2))
