import os
import time
import json
import logging
import datetime
from typing import Dict, Any, Optional, Tuple

import pytz
import requests
from dotenv import load_dotenv

# Mengimpor seluruh mesin yang sudah terpasang di repositori (100% tanpa mengubah isinya)
from smc_alpha import InstitutionalSMCAlphaEngine
from quant_derivatives_mm import QuantStatDerivativesMMEngine
from quant_intel import QuantIntelEngine
from risk_engine import InstitutionalRiskEngine
from quant_v28_core import (
    TRADING_SKILLS,
    init_audit_db,
    check_and_consume_session_quota,
    record_trade_journal,
    calculate_regime_and_guards,
    fetch_deribit_options_btc,
    fetch_cmc_global_metrics,
    fetch_blockworks_intel,
    audit_high_probability_setup,
)

load_dotenv()
logger = logging.getLogger("QuantUnifiedHub")
WIB = pytz.timezone("Asia/Jakarta")


class StandaloneLivePriceAdapter:
    """Adapter harga live otomatis jika QuantUnifiedHub dijalankan terpisah dari market_feeds.py."""

    def get_live_price_with_meta(self, symbol: str) -> Dict[str, Any]:
        sym = symbol.upper()
        if "BTC" in sym:
            for url, parser in [
                (
                    "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT",
                    lambda d: (float(d["data"][0]["last"]), "OKX Live Spot"),
                ),
                (
                    "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
                    lambda d: (float(d["price"]), "Binance Live Spot"),
                ),
            ]:
                try:
                    r = requests.get(url, timeout=5)
                    if r.status_code == 200:
                        px, src = parser(r.json())
                        if px > 0:
                            return {"price": px, "source": src}
                except Exception:
                    continue
        else:
            try:
                import yfinance as yf
                for tkr in ("GC=F", "XAUUSD=X"):
                    hist = yf.Ticker(tkr).history(period="2d", interval="1m")
                    if hist is not None and not hist.empty:
                        px = float(hist["Close"].iloc[-1])
                        if px > 0:
                            return {"price": round(px, 2), "source": f"Yahoo {tkr}"}
            except Exception:
                pass
        return {"price": 0.0, "source": "Offline"}


class QuantUnifiedHub:
    """
    Hub Terpadu 100% Additive (Zero-Deletion):
    Menggabungkan dan menyinkronkan seluruh modul Anda tanpa mengurangi aturan lama:
    1. InstitutionalSMCAlphaEngine (smc_alpha.py) -> M1-1MON SMC/ICT, SMT, CVD, VWAP, L2 Heatmap
    2. QuantStatDerivativesMMEngine (quant_derivatives_mm.py) -> Hurst, Cointegration, Deribit Max Pain, Stoikov & Avellaneda-Stoikov
    3. QuantIntelEngine (quant_intel.py) -> Daily OHLC NH/NL, GARCH(1,1), CVaR, Intermarket, CFTC COT, DeFiLlama
    4. QuantV28Core (quant_v28_core.py) -> ADX/RSI/Spike Guard, DVOL, CMC, Blockworks, Sidang Dewan Juri 4-Agen AI
    5. InstitutionalRiskEngine (risk_engine.py) -> News Circuit Breaker, Drawdown Kill Switch, Half-Kelly Lot & Auto-BEP
    6. Tambahan Data Realtime: Fear & Greed Index, OKX Top Trader Long/Short Ratio, & Sinkronisasi Sesi WIB
    """

    def __init__(self, cache_ttl_sec: int = 300):
        init_audit_db()
        self.smc_engine = InstitutionalSMCAlphaEngine()
        self.quant_mm_engine = QuantStatDerivativesMMEngine()
        self.macro_intel_engine = QuantIntelEngine()
        self.risk_engine = InstitutionalRiskEngine()
        self.cache_ttl_sec = cache_ttl_sec
        self._macro_cache: Dict[str, Any] = {}
        self._macro_cache_ts: float = 0.0

    # =========================================================================
    # 1. TAMBAHAN API DATA REALTIME & SENTIMEN (100% GRATIS)
    # =========================================================================
    def fetch_realtime_sentiment_and_positioning(self, symbol: str) -> Dict[str, Any]:
        """Mengambil Fear & Greed Index + Rasio Posisi Long/Short & Taker Realtime."""
        out: Dict[str, Any] = {}
        try:
            r_fng = requests.get("https://api.alternative.me/fng/?limit=1", timeout=6)
            if r_fng.status_code == 200:
                item = (r_fng.json().get("data") or [{}])[0]
                out["fear_greed_value"] = int(item.get("value", 50))
                out["fear_greed_label"] = item.get("value_classification", "Neutral")
        except Exception:
            pass

        if "BTC" in symbol.upper():
            try:
                r_ls = requests.get(
                    "https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?ccy=BTC&period=15m",
                    timeout=6,
                )
                if r_ls.status_code == 200:
                    rows = r_ls.json().get("data") or []
                    if rows:
                        out["okx_btc_long_short_ratio"] = round(float(rows[0][1]), 3)
            except Exception:
                pass

            try:
                r_tk = requests.get(
                    "https://www.okx.com/api/v5/rubik/stat/taker-volume?ccy=BTC&instType=CONTRACTS&period=15m",
                    timeout=6,
                )
                if r_tk.status_code == 200:
                    rows = r_tk.json().get("data") or []
                    if rows:
                        sell_vol = float(rows[0][1])
                        buy_vol = float(rows[0][2])
                        out["okx_taker_buy_sell_ratio"] = round(buy_vol / max(sell_vol, 1e-9), 3)
            except Exception:
                pass

        return out

    def get_cached_macro_and_intel(self, force_refresh: bool = False) -> Dict[str, Any]:
        """Cache 5 menit untuk data berat (YFinance, CFTC COT, DeFiLlama, CMC, Blockworks, Deribit)."""
        now = time.time()
        if not force_refresh and self._macro_cache and (now - self._macro_cache_ts) < self.cache_ttl_sec:
            return self._macro_cache

        quant_scan = self.macro_intel_engine.run_full_quant_scan()
        deribit_btc = fetch_deribit_options_btc()
        cmc_global = fetch_cmc_global_metrics()
        blockworks = fetch_blockworks_intel()

        self._macro_cache = {
            "quant_scan": quant_scan,
            "deribit_btc": deribit_btc,
            "cmc_global": cmc_global,
            "blockworks": blockworks,
        }
        self._macro_cache_ts = now
        return self._macro_cache

    # =========================================================================
    # 2. GUARD TAMBAHAN (TANPA MENGUBAH LOGIKA ASLI)
    # =========================================================================
    @staticmethod
    def evaluate_additive_market_guards(symbol: str, intraday_setup: Dict[str, Any]) -> Dict[str, Any]:
        """
        Memberikan label peringatan tambahan untuk:
        - Sinkronisasi jam WIB akurat (menutup celah jam 18:00-18:59 WIB)
        - Status libur akhir pekan XAUUSD
        - Deteksi setup counter-trend jika keselarasan MTF di bawah 4/6 TF
        """
        now_wib = datetime.datetime.now(WIB)
        hr = now_wib.hour
        wd = now_wib.weekday()

        if 7 <= hr < 14:
            wib_session = "ASIA_ACCUMULATION"
        elif 14 <= hr < 18:
            wib_session = "LONDON_KILLZONE"
        elif hr == 18:
            wib_session = "LONDON_NY_OVERLAP_KILLZONE"
        elif hr >= 19 or hr < 2:
            wib_session = "NY_KILLZONE_SILVER_BULLET"
        else:
            wib_session = "DEAD_ZONE_ROLLOVER"

        is_xau_weekend = ("XAU" in symbol.upper()) and (
            (wd == 5 and hr >= 4) or (wd == 6) or (wd == 0 and hr < 5)
        )

        aligned_str = str(intraday_setup.get("aligned_count", "0/6"))
        try:
            aligned_num = int(aligned_str.split("/")[0])
        except Exception:
            aligned_num = 0

        extra_warnings = []
        if is_xau_weekend:
            extra_warnings.append("XAUUSD_WEEKEND_MARKET_CLOSED")
        if wib_session == "DEAD_ZONE_ROLLOVER":
            extra_warnings.append("DEAD_ZONE_ROLLOVER_02_07_WIB")
        if aligned_num < 4:
            extra_warnings.append(f"LOW_MTF_ALIGNMENT_{aligned_str}")

        return {
            "wib_timestamp": now_wib.strftime("%Y-%m-%d %H:%M:%S WIB"),
            "wib_session": wib_session,
            "is_xau_weekend_closed": is_xau_weekend,
            "intraday_mtf_aligned": aligned_num,
            "extra_warnings": extra_warnings,
            "additive_pass": len(extra_warnings) == 0,
        }

    @staticmethod
    def select_active_playbook(regime_info: Dict[str, Any], quant_stats: Dict[str, Any], wib_session: str) -> str:
        """Memilih playbook optimal dari TRADING_SKILLS berdasarkan kondisi kuantitatif realtime."""
        adx_h1 = float(regime_info.get("adx_h1", 20.0))
        hurst = float(quant_stats.get("hurst_exponent", 0.5))

        if "KILLZONE" in wib_session and adx_h1 >= 22.0:
            return "SESSION_ICT_LIQUIDITY"
        if hurst < 0.45:
            return "MARKET_MAKER_ORDERFLOW"
        if adx_h1 >= 26.0 and hurst > 0.55:
            return "SWING_HEDGE_FUND"
        return "INTRADAY_EXECUTION"

    # =========================================================================
    # 3. EKSEKUSI ANALISIS TERPADU 360° (SEMUA ENGINE + KONFIRMASI + RISK)
    # =========================================================================
    def run_unified_analysis(
        self,
        symbol: str,
        feeds: Any = None,
        intel: Optional[Dict[str, Any]] = None,
        ai_router: Any = None,
        account_balance: Optional[float] = None,
        consume_quota_on_approve: bool = False,
    ) -> Dict[str, Any]:
        sym_norm = "XAUUSD" if "XAU" in symbol.upper() else "BTCUSDT"
        feeds_obj = feeds if feeds is not None else StandaloneLivePriceAdapter()
        intel_obj = intel if intel is not None else {}
        balance = float(account_balance or os.getenv("ACCOUNT_BALANCE", "1000.0"))

        # A. Ambil Intel Makro, COT, GARCH, OHLC Pivot, Deribit, CMC, Blockworks
        cached = self.get_cached_macro_and_intel()
        quant_scan = cached["quant_scan"]
        asset_macro = quant_scan.get("xauusd" if sym_norm == "XAUUSD" else "btcusdt", {})
        intermarket = quant_scan.get("intermarket_correlation", {})

        # Tentukan Macro Bias otomatis jika belum disediakan
        if sym_norm == "XAUUSD":
            m_verdict = intermarket.get("xauusd_macro_verdict", "")
            cot_bias = asset_macro.get("cftc_cot", {}).get("cot_institutional_bias", "")
            macro_bias = "BEARISH" if ("BEARISH" in m_verdict and "BEARISH" in cot_bias) else "BULLISH"
        else:
            m_verdict = intermarket.get("btcusd_macro_verdict", "")
            liq_verdict = asset_macro.get("defillama_liquidity", {}).get("liquidity_regime", "")
            macro_bias = "BEARISH" if ("RISK_OFF" in m_verdict or "OUTFLOW" in liq_verdict) else "BULLISH"

        # B. Jalankan SMC/ICT Cascade Engine (smc_alpha.py)
        smc_bundle = self.smc_engine.build_cascade_setups(sym_norm, feeds_obj, macro_bias)
        if ai_router is not None:
            smc_bundle, _ = self.smc_engine.refine_setups_with_flagship_ai(sym_norm, smc_bundle, intel_obj, ai_router)

        mtf_ladder = smc_bundle.get("mtf_ladder", {})
        base_score = self.smc_engine.calculate_confluence_score(sym_norm, intel_obj, macro_bias, mtf_ladder)

        # C. Jalankan 3 Pilar Quant Statistik, Derivatif, & Market Making (quant_derivatives_mm.py)
        three_pillars = self.quant_mm_engine.run_all_three_pillars(sym_norm)

        # D. Jalankan Regime ADX/RSI/Spike Guard & Sentimen Realtime
        regime_info = calculate_regime_and_guards(sym_norm)
        realtime_sent = self.fetch_realtime_sentiment_and_positioning(sym_norm)

        # E. Cek Berita Tier-1 USD & Verifikasi Bawaan smc_alpha.py
        news_check = self.risk_engine.check_tier1_usd_news()
        blackout = bool(news_check.get("blocked", False))
        smc_ok, smc_errors = self.smc_engine.verify_setup_before_send(smc_bundle, blackout, base_score)

        # F. Jalankan Sidang Dewan Juri 4-Agen AI (quant_v28_core.py)
        setups = smc_bundle.get("setups", {})
        intraday = setups.get("INTRADAY (M1-M5-M15-M30-H1-H4)", {})
        direction_raw = str(intraday.get("direction", "BUY"))
        clean_dir = "BUY" if "BUY" in direction_raw.upper() else "SELL"

        jury_audit = audit_high_probability_setup(
            symbol=sym_norm,
            base_score=base_score,
            direction=clean_dir,
            regime_info=regime_info,
            deribit_info=cached["deribit_btc"] if sym_norm == "BTCUSDT" else {"status": "N/A_FOR_XAU"},
            cmc_info=cached["cmc_global"],
            blockworks_info=cached["blockworks"],
            blackout=blackout,
            ai_router=ai_router,
        )

        # G. Evaluasi Guard Tambahan (Sinkronisasi WIB, Weekend Gold, MTF >= 4/6)
        additive_guard = self.evaluate_additive_market_guards(sym_norm, intraday)

        # H. Validasi Chief Risk Officer & Sizing Lot (risk_engine.py)
        risk_verdict: Dict[str, Any] = {"approved": False, "reason": "Setup Intraday Kosong"}
        if intraday and intraday.get("entry") and intraday.get("sl") and intraday.get("tp3"):
            pip_size = 0.1 if sym_norm == "XAUUSD" else 1.0
            pip_val = 1.0 if sym_norm == "XAUUSD" else 0.01
            spread_est = 1.8 if sym_norm == "XAUUSD" else max(
                1.0, float(three_pillars.get("market_making_orderbook", {}).get("spread_bps", 5.0))
            )
            risk_verdict = self.risk_engine.evaluate_trade_signal(
                symbol=sym_norm,
                direction=clean_dir,
                entry_price=float(intraday["entry"]),
                stop_loss=float(intraday["sl"]),
                take_profit_final=float(intraday["tp3"]),
                account_balance=balance,
                pip_value_per_lot=pip_val,
                pip_size=pip_size,
                spread_pips=spread_est,
                layers=2,
                confluence_score=int(jury_audit.get("final_score", base_score)),
                market_regime=str(regime_info.get("regime", "TRENDING")),
                z_score_h1=float(regime_info.get("z_score_h1", 0.0)),
            )

        # I. Penentuan Playbook & Keputusan Final Gabungan
        playbook_name = self.select_active_playbook(
            regime_info,
            three_pillars.get("quant_statistics", {}),
            additive_guard["wib_session"],
        )
        playbook_detail = TRADING_SKILLS.get(playbook_name, {})

        all_checks_passed = (
            smc_ok
            and bool(jury_audit.get("approved_high_prob", False))
            and bool(risk_verdict.get("approved", False))
            and bool(additive_guard.get("additive_pass", False))
        )

        if all_checks_passed and consume_quota_on_approve:
            check_and_consume_session_quota(consume=True)
            record_trade_journal(
                symbol=sym_norm,
                playbook=playbook_name,
                direction=clean_dir,
                entry_price=float(intraday.get("entry", 0.0)),
                sl_price=float(intraday.get("sl", 0.0)),
                tp_price=float(intraday.get("tp3", 0.0)),
                score=int(jury_audit.get("final_score", base_score)),
                notes=f"Auto-approved Unified Hub | {additive_guard['wib_session']}",
            )

        return {
            "symbol": sym_norm,
            "live_price": smc_bundle.get("live_price", 0.0),
            "price_source": smc_bundle.get("price_source", "Live"),
            "macro_bias": macro_bias,
            "active_playbook": playbook_name,
            "playbook_detail": playbook_detail,
            "smc_bundle": smc_bundle,
            "smc_verified": smc_ok,
            "smc_errors": smc_errors,
            "three_pillars": three_pillars,
            "macro_quant_intel": asset_macro,
            "intermarket_correlation": intermarket,
            "deribit_options_global": cached["deribit_btc"],
            "cmc_global": cached["cmc_global"],
            "blockworks_intel": cached["blockworks"],
            "regime_and_guards": regime_info,
            "realtime_sentiment": realtime_sent,
            "additive_guard": additive_guard,
            "jury_audit": jury_audit,
            "risk_verdict": risk_verdict,
            "master_execution_approved": all_checks_passed,
        }

    # =========================================================================
    # 4. FORMATTER LAPORAN TELEGRAM LENGKAP (AMAN TANPA KARAKTER < ATAU >)
    # =========================================================================
    def format_institutional_brief(self, report: Dict[str, Any]) -> str:
        sym = report["symbol"]
        px = report["live_price"]
        src = report["price_source"]
        guard = report["additive_guard"]
        jury = report["jury_audit"]
        risk = report["risk_verdict"]
        pillars = report["three_pillars"]
        q_stat = pillars.get("quant_statistics", {})
        deriv = pillars.get("derivatives_microstructure", {})
        mm = pillars.get("market_making_orderbook", {})
        macro_q = report["macro_quant_intel"]
        ohlc = macro_q.get("ohlc_strategy", {})
        garch = macro_q.get("garch_cvar_risk", {})
        cot = macro_q.get("cftc_cot", {})
        reg = report["regime_and_guards"]
        sent = report["realtime_sentiment"]
        intraday = report["smc_bundle"].get("setups", {}).get("INTRADAY (M1-M5-M15-M30-H1-H4)", {})

        status_badge = (
            "EXECUTE HIGH-PROBABILITY SETUP"
            if report["master_execution_approved"]
            else "WAIT / FILTERED BY INSTITUTIONAL GUARD"
        )

        lines = [
            f"🏛️ QUANT UNIFIED HUB 360° — {sym}",
            f"• Waktu: {guard['wib_timestamp']} | Sesi: {guard['wib_session']}",
            f"• Harga Live: {px} ({src}) | Playbook: {report['active_playbook']}",
            f"• Status Master: {status_badge}",
            "",
            "1️⃣ SMC / ICT & STRUKTUR MTF:",
            f"• Arah Intraday: {intraday.get('direction', 'N/A')} ({intraday.get('aligned_count', '0/6')})",
            f"• Chain MTF: {intraday.get('tf_chain', '-')}",
            f"• Entry: {intraday.get('entry', '-')} | SL: {intraday.get('sl', '-')} | TP1: {intraday.get('tp1', '-')} | TP2: {intraday.get('tp2', '-')} | TP3: {intraday.get('tp3', '-')}",
            "",
            "2️⃣ QUANT STATISTIK, GARCH & OHLC PIVOT:",
            f"• Hurst Exponent: {q_stat.get('hurst_exponent', '-')} ({q_stat.get('stat_regime', '-')})",
            f"• Price Z-Score 20D: {q_stat.get('price_zscore_20d', '-')} | Pair Z-Score: {q_stat.get('pair_spread_zscore_30d', '-')}",
            f"• Daily Pivot NH / NL: NH {ohlc.get('new_high_NH', '-')} | EQ {ohlc.get('equilibrium_mid', '-')} | NL {ohlc.get('new_low_NL', '-')} ({ohlc.get('spatial_status', '-')})",
            f"• GARCH Next-Day Vol: {garch.get('next_day_volatility_pct', '-')}% | CVaR 99%: {garch.get('cvar_99_daily_pct', '-')}% ({garch.get('volatility_regime', '-')})",
            f"• ADX(14) H1/D1: {reg.get('adx_h1', '-')}/{reg.get('adx_d1', '-')} ({reg.get('regime', '-')}) | RSI H1: {reg.get('rsi_h1', '-')} | Spike Z: {reg.get('z_score_h1', '-')}",
            "",
            "3️⃣ MARKET MAKING, ORDERBOOK L2 & DERIVATIF:",
            f"• Stoikov Micro-Price: {mm.get('stoikov_micro_price', '-')} | Spread: {mm.get('spread_bps', '-')} bps | OBI: {mm.get('orderbook_imbalance_obi', '-')}",
            f"• Avellaneda Optimal Bid / Ask: {mm.get('avellaneda_optimal_bid_limit', '-')} / {mm.get('avellaneda_optimal_ask_limit', '-')} ({mm.get('toxicity_status', '-')})",
            f"• Funding APR: {deriv.get('funding_apr_pct', '-')}% | Basis: {deriv.get('spot_perp_basis_bps', '-')} bps | {deriv.get('squeeze_regime', '-')}",
            f"• CFTC COT Bias: {cot.get('cot_institutional_bias', '-')} (Net: {cot.get('noncomm_net', '-')}) | Fear & Greed: {sent.get('fear_greed_value', '-')} ({sent.get('fear_greed_label', '-')})",
            "",
            "4️⃣ SIDANG DEWAN JURI 4-AGEN & CHIEF RISK OFFICER:",
            f"• Skor Konfluensi: Awal {jury.get('base_score', '-')} -> Final {jury.get('final_score', '-')}/100 ({jury.get('status_label', '-')})",
            f"• Red Flags Jury: {', '.join(jury.get('red_flags', [])) or 'NIHIL'}",
            f"• Guard Tambahan: {', '.join(guard.get('extra_warnings', [])) or 'LOLOS SEMUA'}",
            f"• Keputusan Risk Engine: {risk.get('reason', '-')}",
        ]
        if risk.get("approved"):
            lines.append(
                f"• Eksekusi Lot: {risk.get('layers')} layer x {risk.get('lot_per_layer')} lot | Auto-BEP setelah TP1: {risk.get('breakeven_sl_after_tp1')}"
            )
        return "\n".join(lines)


if __name__ == "__main__":
    hub = QuantUnifiedHub()
    for asset in ("XAUUSD", "BTCUSDT"):
        res = hub.run_unified_analysis(asset)
        print(hub.format_institutional_brief(res))
        print("=" * 70)
