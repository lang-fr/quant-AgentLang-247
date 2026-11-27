import json
import sqlite3
import datetime
import pytz
import yfinance as yf


class OutcomeVerifierEngine:
    """
    Memverifikasi hasil riil dari setiap sinyal yang disetujui (APPROVED):
    - Mengecek apakah harga menyentuh TP1 (geser SL ke BEP+1), TP2, TP3, atau SL
    - Menghitung Win Rate historis dan Daily Realized PnL (%) untuk Kill Switch
    """

    def __init__(self, db_path: str = "quant_cloud_audit.db"):
        self.db_path = db_path
        self.wib = pytz.timezone("Asia/Jakarta")

    def _ensure_columns(self):
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(signal_audit_log)")
        cols = [row[1] for row in cur.fetchall()]
        if "outcome_status" not in cols:
            cur.execute("ALTER TABLE signal_audit_log ADD COLUMN outcome_status TEXT DEFAULT 'OPEN'")
        if "realized_r" not in cols:
            cur.execute("ALTER TABLE signal_audit_log ADD COLUMN realized_r REAL DEFAULT 0.0")
        conn.commit()
        conn.close()

    def _get_recent_high_low(self, yf_ticker: str) -> tuple[float, float, float]:
        try:
            df = yf.Ticker(yf_ticker).history(period="2d", interval="15m")
            if df is None or df.empty:
                return 0.0, 0.0, 0.0
            return float(df["High"].max()), float(df["Low"].min()), float(df["Close"].iloc[-1])
        except Exception:
            return 0.0, 0.0, 0.0

    def verify_all_approved_setups(self) -> dict:
        self._ensure_columns()
        xau_hi, xau_lo, xau_last = self._get_recent_high_low("GC=F")
        btc_hi, btc_lo, btc_last = self._get_recent_high_low("BTC-USD")

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, created_at_wib, status, outcome_status, realized_r, payload_json
            FROM signal_audit_log
            WHERE status = 'APPROVED'
            """
        )
        rows = cur.fetchall()

        updated_count = 0
        for row_id, created_at, status, outcome_status, realized_r, payload_str in rows:
            if outcome_status in ("TP3_WON", "SL_LOSS", "BEP_CLOSED"):
                continue

            try:
                payload = json.loads(payload_str)
            except Exception:
                continue

            setup = payload.get("xau", {}).get("INTRADAY (M1-M5-M15-M30-H1-H4)")
            hi, lo = xau_hi, xau_lo
            if not setup or hi <= 0:
                setup = payload.get("btc", {}).get("INTRADAY (M1-M5-M15-M30-H1-H4)")
                hi, lo = btc_hi, btc_lo

            if not setup or hi <= 0:
                continue

            direction = setup.get("direction", "")
            entry = float(setup["entry"])
            sl = float(setup["sl"])
            tp1 = float(setup["tp1"])
            tp2 = float(setup["tp2"])
            tp3 = float(setup["tp3"])

            new_outcome = outcome_status or "OPEN"
            new_r = float(realized_r or 0.0)

            if direction == "SELL LIMIT" and hi >= entry:
                if lo <= tp3:
                    new_outcome, new_r = "TP3_WON", 10.0
                elif lo <= tp2:
                    new_outcome, new_r = "TP2_HIT_BEP_ACTIVE", 6.0
                elif lo <= tp1:
                    new_outcome, new_r = "TP1_HIT_BEP_ACTIVE", 3.0
                elif hi >= sl:
                    if "BEP_ACTIVE" in new_outcome:
                        new_outcome, new_r = "BEP_CLOSED", max(new_r, 0.2)
                    else:
                        new_outcome, new_r = "SL_LOSS", -1.0

            elif direction == "BUY LIMIT" and lo <= entry:
                if hi >= tp3:
                    new_outcome, new_r = "TP3_WON", 11.0
                elif hi >= tp2:
                    new_outcome, new_r = "TP2_HIT_BEP_ACTIVE", 6.0
                elif hi >= tp1:
                    new_outcome, new_r = "TP1_HIT_BEP_ACTIVE", 3.0
                elif lo <= sl:
                    if "BEP_ACTIVE" in new_outcome:
                        new_outcome, new_r = "BEP_CLOSED", max(new_r, 0.2)
                    else:
                        new_outcome, new_r = "SL_LOSS", -1.0

            if new_outcome != outcome_status:
                cur.execute(
                    "UPDATE signal_audit_log SET outcome_status = ?, realized_r = ? WHERE id = ?",
                    (new_outcome, new_r, row_id),
                )
                updated_count += 1

        conn.commit()
        stats = self.get_performance_summary(conn)
        conn.close()
        stats["newly_updated"] = updated_count
        return stats

    def get_performance_summary(self, existing_conn=None) -> dict:
        self._ensure_columns()
        conn = existing_conn or sqlite3.connect(self.db_path)
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) FROM signal_audit_log WHERE status = 'APPROVED'")
        total_approved = cur.fetchone()[0]

        cur.execute(
            """
            SELECT COUNT(*) FROM signal_audit_log
            WHERE status = 'APPROVED' AND outcome_status IN ('TP1_HIT_BEP_ACTIVE', 'TP2_HIT_BEP_ACTIVE', 'TP3_WON', 'BEP_CLOSED')
            """
        )
        wins = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM signal_audit_log WHERE status = 'APPROVED' AND outcome_status = 'SL_LOSS'")
        losses = cur.fetchone()[0]

        cur.execute("SELECT COALESCE(SUM(realized_r), 0.0) FROM signal_audit_log WHERE status = 'APPROVED'")
        total_r = float(cur.fetchone()[0])

        today_prefix = datetime.datetime.now(self.wib).strftime("%Y-%m-%d")
        cur.execute(
            """
            SELECT COALESCE(SUM(realized_r), 0.0) FROM signal_audit_log
            WHERE status = 'APPROVED' AND created_at_wib LIKE ?
            """,
            (f"{today_prefix}%",),
        )
        today_r = float(cur.fetchone()[0])

        if not existing_conn:
            conn.close()

        closed_trades = wins + losses
        win_rate = round((wins / closed_trades) * 100.0, 1) if closed_trades > 0 else 0.0
        daily_pnl_pct = round(today_r * 1.0, 2)

        return {
            "total_approved": total_approved,
            "wins": wins,
            "losses": losses,
            "win_rate_pct": win_rate,
            "total_r": round(total_r, 2),
            "daily_pnl_pct": daily_pnl_pct,
        }
