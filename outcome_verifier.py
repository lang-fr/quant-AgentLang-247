import sqlite3
from datetime import datetime, timedelta, timezone
import pandas as pd
import yfinance as yf

DB_PATH = "quant_cloud_audit.db"


def migrasi_kolom_outcome():
    """Menambahkan kolom koordinat harga & hasil TP/SL ke tabel audit_log secara aman."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    kolom_baru = [
        ("entry_price", "REAL DEFAULT 0.0"),
        ("sl_price", "REAL DEFAULT 0.0"),
        ("tp1_price", "REAL DEFAULT 0.0"),
        ("tp2_price", "REAL DEFAULT 0.0"),
        ("outcome", "TEXT DEFAULT 'OPEN'"),
        ("pnl_r", "REAL DEFAULT 0.0"),
    ]
    for nama_kol, tipe_kol in kolom_baru:
        try:
            c.execute(f"ALTER TABLE audit_log ADD COLUMN {nama_kol} {tipe_kol}")
        except sqlite3.OperationalError:
            pass  # Kolom sudah ada
    conn.commit()
    conn.close()


def simpan_koordinat_sinyal(log_id: int, entry_px: float, sl_px: float, tp1_px: float, tp2_px: float):
    """Menyimpan level angka Entry, SL, TP1, TP2 untuk diverifikasi pada run berikutnya."""
    migrasi_kolom_outcome()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        UPDATE audit_log
        SET entry_price = ?, sl_price = ?, tp1_price = ?, tp2_price = ?, outcome = 'OPEN'
        WHERE id = ?
        """,
        (entry_px, sl_px, tp1_px, tp2_px, log_id),
    )
    conn.commit()
    conn.close()


def verifikasi_hasil_sinyal_sebelumnya() -> list:
    """Memeriksa candle M15 setelah sinyal terbit untuk menentukan apakah kena TP atau SL terlebih dahulu."""
    migrasi_kolom_outcome()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    batas_waktu = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()

    c.execute(
        """
        SELECT id, timestamp_utc, asset, bias, entry_price, sl_price, tp1_price, tp2_price
        FROM audit_log
        WHERE outcome = 'OPEN' AND entry_price > 0 AND sl_price > 0 AND timestamp_utc >= ?
        """,
        (batas_waktu,),
    )
    rows = c.fetchall()
    if not rows:
        conn.close()
        return []

    cache_ohlc = {}
    for sym_yf in ("GC=F", "BTC-USD"):
        df = yf.download(sym_yf, period="5d", interval="15m", progress=False)
        if not df.empty:
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            idx_utc = df.index.tz_convert("UTC") if df.index.tz is not None else df.index.tz_localize("UTC")
            df.index = idx_utc
            cache_ohlc[sym_yf] = df

    hasil_update = []
    for row in rows:
        log_id, ts_str, asset, bias, entry_px, sl_px, tp1_px, tp2_px = row
        sym_yf = "BTC-USD" if "BTC" in asset.upper() else "GC=F"
        df = cache_ohlc.get(sym_yf)
        if df is None or df.empty:
            continue

        try:
            ts_signal = datetime.fromisoformat(ts_str).astimezone(timezone.utc)
        except Exception:
            continue

        sub = df[df.index > ts_signal]
        if sub.empty:
            continue

        is_buy = "BULL" in bias.upper() or "BUY" in bias.upper()
        kena_tp1 = False
        status_final = "OPEN"
        pnl_r = 0.0

        for _, candle in sub.iterrows():
            hi = float(candle["High"])
            lo = float(candle["Low"])

            if is_buy:
                if lo <= sl_px and not kena_tp1:
                    status_final = "HIT_SL"
                    pnl_r = -1.0
                    break
                if tp2_px > 0 and hi >= tp2_px:
                    status_final = "HIT_TP2"
                    pnl_r = 3.0
                    break
                if tp1_px > 0 and hi >= tp1_px:
                    kena_tp1 = True
                if kena_tp1 and lo <= entry_px:
                    status_final = "HIT_TP1_BE"
                    pnl_r = 0.8  # 40% tutup di 2R + sisa BE
                    break
            else:
                if hi >= sl_px and not kena_tp1:
                    status_final = "HIT_SL"
                    pnl_r = -1.0
                    break
                if tp2_px > 0 and lo <= tp2_px:
                    status_final = "HIT_TP2"
                    pnl_r = 3.0
                    break
                if tp1_px > 0 and lo <= tp1_px:
                    kena_tp1 = True
                if kena_tp1 and hi >= entry_px:
                    status_final = "HIT_TP1_BE"
                    pnl_r = 0.8
                    break

        if status_final != "OPEN":
            c.execute(
                "UPDATE audit_log SET outcome = ?, pnl_r = ? WHERE id = ?",
                (status_final, pnl_r, log_id),
            )
            hasil_update.append(f"#{log_id} {asset}: {status_final} ({pnl_r:+}R)")

    conn.commit()
    conn.close()
    return hasil_update


def ambil_statistik_winrate_sesi() -> str:
    """Ringkasan performa historis nyata per sesi (Asia/London/NY) untuk bahan audit Risk Judge."""
    migrasi_kolom_outcome()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        SELECT session_name,
               COUNT(*) as total,
               SUM(CASE WHEN outcome LIKE 'HIT_TP%' THEN 1 ELSE 0 END) as wins,
               SUM(pnl_r) as net_r
        FROM audit_log
        WHERE outcome != 'OPEN'
        GROUP BY session_name
        """
    )
    rows = c.fetchall()
    conn.close()

    if not rows:
        return "Belum ada sinyal dengan koordinat harga yang selesai diverifikasi TP/SL."

    baris = []
    for sesi, tot, win, net_r in rows:
        wr = round((win / tot) * 100.0, 1) if tot > 0 else 0.0
        baris.append(f"{sesi}: WinRate {wr}% ({win}/{tot}) | Net {round(float(net_r or 0), 2):+}R")
    return " | ".join(baris)
