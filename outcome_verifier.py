import sqlite3
import yfinance as yf
import pandas as pd

DB_PATH = "quant_cloud_audit.db"


def _harga_terkini(aset: str) -> float:
    """Mengambil harga terakhir instrumen untuk memverifikasi pencapaian TP atau SL."""
    ticker = "BTC-USD" if "BTC" in aset.upper() else "GC=F"
    try:
        df = yf.download(ticker, period="2d", interval="15m", progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna(subset=["Close"])
        if not df.empty:
            return float(df["Close"].iloc[-1])
    except Exception:
        pass
    return 0.0


def verifikasi_outcome_sinyal() -> list:
    """Memeriksa seluruh sinyal berstatus PENDING dan memperbarui status HIT_TP / HIT_SL."""
    hasil_update = []
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("""
            SELECT id, asset, direction, entry_price, sl_price, tp1, tp2, tp3
            FROM audit_signals
            WHERE action_status IN ('BINANCE_DEMO_AUTO', 'MANUAL_CFD_SIGNAL')
              AND (outcome_status IS NULL OR outcome_status = 'PENDING')
        """)
        rows = cur.fetchall()

        for row_id, asset, direction, entry_px, sl_px, tp1, tp2, tp3 in rows:
            last_px = _harga_terkini(asset)
            if last_px <= 0:
                continue

            status_baru = None
            arah = direction.upper()

            if arah == "BUY":
                if last_px <= float(sl_px):
                    status_baru = "HIT_SL"
                elif last_px >= float(tp3):
                    status_baru = "HIT_TP3"
                elif last_px >= float(tp2):
                    status_baru = "HIT_TP2"
                elif last_px >= float(tp1):
                    status_baru = "HIT_TP1"
            elif arah == "SELL":
                if last_px >= float(sl_px):
                    status_baru = "HIT_SL"
                elif last_px <= float(tp3):
                    status_baru = "HIT_TP3"
                elif last_px <= float(tp2):
                    status_baru = "HIT_TP2"
                elif last_px <= float(tp1):
                    status_baru = "HIT_TP1"

            if status_baru:
                cur.execute(
                    "UPDATE audit_signals SET outcome_status = ? WHERE id = ?",
                    (status_baru, row_id),
                )
                hasil_update.append(f"#{row_id} {asset} {arah} -> {status_baru} (@{last_px:.2f})")

        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Verifier warning: {e}")

    return hasil_update


if __name__ == "__main__":
    updates = verifikasi_outcome_sinyal()
    print("Outcome Verifier selesai:", updates if updates else "Tidak ada perubahan status.")
