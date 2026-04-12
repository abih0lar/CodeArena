from datetime import datetime, timezone
from typing import Optional
import numpy as np

from config import BOT_CONFIG
from data_engine import DataEngine
from database import Database
from models import Bias, MonologueEntry, ScoutSignal, SignalStrength, WickRejection

class Scout:
    def __init__(self, data_engine: DataEngine, db: Database):
        self.data = data_engine; self.db = db

    def scan(self) -> ScoutSignal:
        dyn = BOT_CONFIG.dynamic; log = []
        df_h1 = self.data.fetch_ohlcv(BOT_CONFIG.timeframes.primary, 200); df_h1 = self.data.compute_indicators(df_h1)
        snapshot = self.data.build_snapshot(); price = snapshot.price

        trend_bull, price_above_ma = snapshot.ma20 > snapshot.ma50, price > snapshot.ma20
        log.extend([f"BTC ${price:,.2f}", f"H1 trend: {'bullish' if trend_bull else 'bearish'} (MA20 {'>' if trend_bull else '<'} MA50)"])

        sr_levels = self.data.find_support_resistance(df_h1, window=dyn.pivot_lookback)
        supports = [s for s in sr_levels if s.level_type == "support" and s.level < price]
        resistances = [s for s in sr_levels if s.level_type == "resistance" and s.level > price]

        nearest_sup = max((s.level for s in supports), default=price * 0.97)
        nearest_res = min((s.level for s in resistances), default=price * 1.03)
        dist_sup, dist_res = (price - nearest_sup) / price * 100, (nearest_res - price) / price * 100
        log.append(f"Support ${nearest_sup:,.0f} ({dist_sup:.1f}% away) | Resistance ${nearest_res:,.0f} ({dist_res:.1f}% away)")

        rejections = self.data.detect_wick_rejections(df_h1, sr_levels, lookback=3)
        best_rejection: Optional[WickRejection] = rejections[-1] if rejections else None

        if best_rejection: log.append(f"WICK REJECTION: {best_rejection.direction} at ${best_rejection.rejected_level:,.0f} ({best_rejection.level_type}) | Wick ratio: {best_rejection.wick_ratio:.1f}x body | Wick = {best_rejection.wick_pct_of_range:.0%} of range")
        else: log.append("No wick rejection detected in last 3 H1 candles")

        bb_touch, bb_pct = self.data.detect_bb_touch(df_h1), snapshot.bb_pct_b
        if bb_touch == "lower": log.append(f"BB lower touch — %B: {bb_pct:.3f} — potential bounce zone")
        elif bb_touch == "upper": log.append(f"BB upper touch — %B: {bb_pct:.3f} — potential rejection zone")
        else: log.append(f"BB %B: {bb_pct:.2f} — within bands")

        macd_read, rsi_read = self.data.interpret_macd(df_h1), self.data.interpret_rsi(df_h1)
        log.extend([f"MACD: {macd_read['detail']}", f"RSI: {rsi_read['detail']}"])

        conf = {}
        if best_rejection:
            wick_base = min(0.95, 0.50 + best_rejection.wick_ratio * 0.08)
            if best_rejection.level_type in ("bb_lower", "bb_upper"): wick_base = min(0.95, wick_base + 0.10)
            conf["wick"] = round(wick_base, 2)
        else: conf["wick"] = 0.10

        if bb_touch == "lower" and (trend_bull or snapshot.rsi < 40): conf["bollinger"] = 0.80
        elif bb_touch == "upper" and ((not trend_bull) or snapshot.rsi > 60): conf["bollinger"] = 0.80
        elif bb_pct < 0.2 or bb_pct > 0.8: conf["bollinger"] = 0.55
        else: conf["bollinger"] = 0.30

        conf["macd"], conf["rsi"] = round(macd_read["strength"], 2), round(rsi_read["strength"], 2)
        conf["trend"] = 0.80 if (trend_bull and price_above_ma) else 0.60 if trend_bull else 0.70 if (not trend_bull and not price_above_ma) else 0.40
        conf["sr_proximity_long"] = 0.85 if dist_sup < 0.8 else 0.65 if dist_sup < 1.5 else 0.35
        conf["sr_proximity_short"] = 0.85 if dist_res < 0.8 else 0.65 if dist_res < 1.5 else 0.35
        conf["volume"] = round(min(0.90, 0.30 + snapshot.volume_ratio * 0.20), 2)

        bull_signals, bear_signals = 0, 0
        if best_rejection: bull_signals += 3 if best_rejection.direction == "bullish" else 0; bear_signals += 3 if best_rejection.direction == "bearish" else 0
        if macd_read["signal"] == "bullish": bull_signals += 2
        elif macd_read["signal"] == "bearish": bear_signals += 2
        if rsi_read["signal"] in ("bullish", "bullish_lean"): bull_signals += 1
        elif rsi_read["signal"] in ("bearish", "bearish_lean"): bear_signals += 1
        if rsi_read.get("divergence") == "bullish": bull_signals += 2
        elif rsi_read.get("divergence") == "bearish": bear_signals += 2
        if bb_touch == "lower": bull_signals += 1
        elif bb_touch == "upper": bear_signals += 1
        bull_signals += 1 if trend_bull else 0; bear_signals += 1 if not trend_bull else 0

        total_directional = bull_signals + bear_signals
        directional_agreement = abs(bull_signals - bear_signals) / total_directional if total_directional > 0 else 0
        bias = Bias.LONG if bull_signals > bear_signals + 1 else Bias.SHORT if bear_signals > bull_signals + 1 else Bias.NEUTRAL

        conf["sr_proximity"] = conf["sr_proximity_long"] if bias == Bias.LONG else conf["sr_proximity_short"] if bias == Bias.SHORT else max(conf["sr_proximity_long"], conf["sr_proximity_short"])
        conf.pop("sr_proximity_long", None); conf.pop("sr_proximity_short", None)

        weights = {"wick": 0.30, "bollinger": 0.15, "macd": 0.15, "rsi": 0.12, "trend": 0.10, "sr_proximity": 0.10, "volume": 0.08}
        raw_confidence = sum(conf[k] * weights[k] for k in weights)

        if bias == Bias.NEUTRAL: total = round(np.clip(raw_confidence * 0.5, 0, 0.40), 3)
        else: total = round(np.clip(raw_confidence * (0.6 + (directional_agreement * 0.4)), 0, 1), 3)
        conf["agreement"] = round(directional_agreement, 2)
        log.append(f"Direction: {bull_signals} bull vs {bear_signals} bear (agreement: {directional_agreement:.0%})")

        strength = SignalStrength.STRONG if total >= 0.70 else SignalStrength.MODERATE if total >= dyn.min_confidence else SignalStrength.WEAK if total >= 0.35 else SignalStrength.NONE

        atr = snapshot.atr if snapshot.atr > 0 else price * 0.01
        suggested_sl, suggested_tp = price, price

        wick_matches_bias = best_rejection is not None and ((bias == Bias.LONG and best_rejection.direction == "bullish") or (bias == Bias.SHORT and best_rejection.direction == "bearish"))

        if wick_matches_bias:
            buffer = price * (dyn.sl_wick_buffer_pct / 100)
            if bias == Bias.LONG:
                suggested_sl = best_rejection.candle_low - buffer
                suggested_tp = price + (price - suggested_sl) * dyn.tp_rr_ratio
            else:
                suggested_sl = best_rejection.candle_high + buffer
                suggested_tp = price - (suggested_sl - price) * dyn.tp_rr_ratio

            sl_pct = abs(price - suggested_sl) / price * 100
            if sl_pct > dyn.sl_max_pct:
                suggested_sl = price * (1 - dyn.sl_max_pct / 100) if bias == Bias.LONG else price * (1 + dyn.sl_max_pct / 100)
                suggested_tp = price + abs(price - suggested_sl) * dyn.tp_rr_ratio * (1 if bias == Bias.LONG else -1)
            log.append(f"SL: ${suggested_sl:,.0f} ({abs(price - suggested_sl) / price * 100:.2f}% from entry) | TP: ${suggested_tp:,.0f} ({dyn.tp_rr_ratio}:1 R:R)")
        else:
            if bias == Bias.LONG: suggested_sl, suggested_tp = price - atr * dyn.sl_atr_multiplier, price + atr * dyn.sl_atr_multiplier * dyn.tp_rr_ratio
            elif bias == Bias.SHORT: suggested_sl, suggested_tp = price + atr * dyn.sl_atr_multiplier, price - atr * dyn.sl_atr_multiplier * dyn.tp_rr_ratio
            else: suggested_sl, suggested_tp = price - atr, price + atr * 2

        patterns = []
        if best_rejection: patterns.append(f"{best_rejection.direction}_wick_rejection")
        if bb_touch: patterns.append(f"bb_{bb_touch}_touch")
        if rsi_read.get("divergence"): patterns.append(f"{rsi_read['divergence']}_rsi_divergence")
        if macd_read["signal"] in ("bullish", "bearish"): patterns.append(f"macd_{macd_read['signal']}")

        log.append(f"Bias: {bias.value} | Strength: {strength.value} | Confidence: {total:.0%}")
        for msg in log: self.db.add_monologue(MonologueEntry(datetime.now(timezone.utc), "Scout", msg, "info"))

        return ScoutSignal(
            datetime.now(timezone.utc), bias, strength, total, price, snapshot, nearest_sup, nearest_res,
            best_rejection, patterns, "\n".join(log), conf, suggested_sl, suggested_tp
        )