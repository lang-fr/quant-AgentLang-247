import json
import sqlite3
import datetime
import pandas as pd
import pytz
import yfinance as yf


class OutcomeVerifierEngine:
    """
    Memverifikasi hasil riil dari setiap sinyal yang disetujui (APPROVED):
    - Mengaudit XAUUSD dan BTCUSDT secara terpisah berdasarkan candle SETELAH waktu sinyal dibuat
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

    def _get_history_df(self, yf_ticker: str) -> pd.DataFrame:
        try:
            df = yf.Ticker(yf_ticker).history(period="5d", interval="15m")
            if df is None or df.empty:
                return pd.DataFrame()
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC").tz_convert(self.wib)
            else:
                df.index = df.index.tz_convert(self.wib)
            return df
        except Exception:
            return pd.DataFrame()

    def _extract_setup(self, asset_dict: dict) -> dict | None:
        for key in ("INTRADAY (M30-H1-H4)", "INTRADAY (M1-M5-M15-M30-H1-H4)", "SESI / SCALP (M1-M5-M15)"):
            if key in asset_dict and asset_dict[key]:
                return asset_dict[key]
        return None

    def _evaluate_single_asset(self, setup: dict, df_after: pd.DataFrame) -> tuple[str, float]:
        if not setup or df_after.empty:
            return "OPEN", 0.0

        direction = setup.get("direction", "")
        entry = float(setup["entry"])
        sl = float(setup["sl"])
        tp1 = float(setup["tp1"])
        tp2 = float(setup["tp2"])
        tp3 = float(setup["tp3"])

        risk_dist = max(abs(entry - sl), 1e-6)
        r_tp1 = round(abs(tp1 - entry) / risk_dist, 2)
        r_tp2 = round(abs(tp2 - entry) / risk_dist, 2)
        r_tp3 = round(abs(tp3 - entry) / risk_dist, 2)

        triggered = False
        bep_active = False

        for _, bar in df_after.iterrows():
            hi = float(bar["High"])
            lo = float(bar["Low"])

            if "SELL" in direction:
                if not triggered and hi >= entry:
                    triggered = True
                if triggered:
                    if hi >= sl and not bep_active:
                        return "SL_LOSS", -1.0
                    if lo <= tp3:
                        return "TP3_WON", r_tp3
                    if lo <= tp2:
                        bep_active = True
                    elif lo <= tp1:
                        bep_active = True
                    if bep_active and hi >= entry:
                        return "BEP_CLOSED", round(r_tp1 * 0.5, 2)
            else:
                if not triggered and lo <= entry:
                    triggered = True
                if triggered:
                    if lo <= sl and not bep_active:
                        return "SL_LOSS", -1.0
                    if hi >= tp3:
                        return "TP3_WON", r_tp3
                    if hi >= tp2:
                        bep_active = True
                    elif hi >= tp1:
                        bep_active = True
                    if bep_active and lo <= entry:
                        return "BEP_CLOSED", round(r_tp1 * 0.5, 2)

        if bep_active:
            return "TP1_HIT_BEP_ACTIVE", r_tp1
        return "OPEN", 0.0

    def verify_all_approved_setups(self) -> dict:
        self._ensure_columns()
        xau_df = self._get_history_df("GC=F")
        btc_df = self._get_history_df("BTC-USD")

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
                created_dt = self.wib.localize(datetime.datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S"))
            except Exception:
                continue

            xau_setup = self._extract_setup(payload.get("xau", {}))
            btc_setup = self._extract_setup(payload.get("btc", {}))

            xau_after = xau_df[xau_df.index >= created_dt] if not xau_df.empty else pd.DataFrame()
            btc_after = btc_df[btc_df.index >= created_dt] if not btc_df.empty else pd.DataFrame()

            xau_res, xau_r = self._evaluate_single_asset(xau_setup, xau_after)
            btc_res, btc_r = self._evaluate_single_asset(btc_setup, btc_after)

            candidates = [(xau_res, xau_r), (btc_res, btc_r)]
            closed = [c for c in candidates if c[0] != "OPEN"]
            if not closed:
                continue

            new_outcome = " / ".join([c[0] for c in closed]) if len(closed) > 1 else closed[0][0]
            new_r = round(sum(c[1] for c in closed), 2)

            if new_outcome != outcome_status or new_r != float(realized_r or 0.0):
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

        cur.execute("SELECT COUNT(*) FROM signal_audit_log WHERE status = 'APPROVED' AND realized_r > 0")
        wins = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM signal_audit_log WHERE status = 'APPROVED' AND realized_r < 0")
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
