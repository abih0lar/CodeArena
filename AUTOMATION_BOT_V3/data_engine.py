import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

import ccxt
import numpy as np
import pandas as pd
import requests
import ta as ta_lib

from config import BOT_CONFIG
from models import IndicatorSnapshot, SupportResistance, WickRejection

log = logging.getLogger(__name__)

class DataEngine:
    def __init__(self):
        cfg = BOT_CONFIG.exchange
        exchange_class = getattr(ccxt, cfg.exchange_id)
        
        self.exchange = exchange_class({
            "apiKey": cfg.api_key, 
            "secret": cfg.api_secret, 
            "sandbox": cfg.sandbox, 
            "enableRateLimit": True, 
            "options": {"defaultType": "future"}
        })
        
        self.public_exchange = exchange_class({
            "enableRateLimit": True, 
            "options": {"defaultType": "future"}
        })
        
        if hasattr(self.public_exchange, "set_sandbox_mode"):
            try:
                self.public_exchange.set_sandbox_mode(False)
            except Exception as e:
                log.debug("set_sandbox_mode failed (non-fatal): %s", e)

        self._cache: Dict[str, pd.DataFrame] = {}
        self._last_fetch: Dict[str, datetime] = {}
        self._last_price: float = 0.0
        self._last_price_time: Optional[datetime] = None

    def get_live_price(self) -> float:
        symbol_noslash = BOT_CONFIG.exchange.symbol.replace("/", "")
        
        try:
            url = "https://fapi.binance.com/fapi/v1/ticker/price"
            resp = requests.get(url, params={"symbol": symbol_noslash}, timeout=5)
            resp.raise_for_status()
            price = float(resp.json()["price"])
            self._last_price = price
            self._last_price_time = datetime.now(timezone.utc)
            return price
        except Exception as e:
            log.warning("Binance REST price fetch failed: %s", e)

        try:
            ticker = self.public_exchange.fetch_ticker(BOT_CONFIG.exchange.symbol)
            price = float(ticker["last"])
            self._last_price = price
            self._last_price_time = datetime.now(timezone.utc)
            return price
        except Exception as e:
            log.warning("Public ccxt fetch failed: %s", e)

        try:
            ticker = self.exchange.fetch_ticker(BOT_CONFIG.exchange.symbol)
            price = float(ticker["last"])
            self._last_price = price
            self._last_price_time = datetime.now(timezone.utc)
            return price
        except Exception as e:
            log.warning("Auth ccxt fetch failed: %s", e)

        if self._last_price > 0:
            return self._last_price

        cache_key = f"{BOT_CONFIG.exchange.symbol}_{BOT_CONFIG.timeframes.primary}"
        if cache_key in self._cache and len(self._cache[cache_key]) > 0:
            return float(self._cache[cache_key]["close"].iloc[-1])

        return 0.0

    def get_current_price(self) -> float:
        return self.get_live_price()

    def get_account_balance(self) -> float:
        try:
            balance_data = self.exchange.fetch_balance()
            return float(balance_data.get("USDT", {}).get("free", 0) or 0)
        except Exception as e:
            log.warning("fetch_balance failed: %s", e)
            return 0.0

    def fetch_ohlcv(self, timeframe: str, limit: int = 200) -> pd.DataFrame:
        cache_key = f"{BOT_CONFIG.exchange.symbol}_{timeframe}"
        now = datetime.now(timezone.utc)
        
        if cache_key in self._last_fetch and cache_key in self._cache:
            if (now - self._last_fetch[cache_key]).total_seconds() < 10 and len(self._cache[cache_key]) >= limit:
                return self._cache[cache_key].iloc[-limit:].copy()

        raw = None
        try:
            raw = self.public_exchange.fetch_ohlcv(BOT_CONFIG.exchange.symbol, timeframe, limit=limit)
        except Exception as e:
            log.warning("Public OHLCV fetch failed: %s", e)

        if raw is None:
            try:
                raw = self.exchange.fetch_ohlcv(BOT_CONFIG.exchange.symbol, timeframe, limit=limit)
            except Exception as e:
                log.error("Auth OHLCV fetch failed: %s", e)
                raise

        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("timestamp", inplace=True)
        
        if cache_key not in self._cache or len(df) >= len(self._cache.get(cache_key, [])):
            self._cache[cache_key] = df
            self._last_fetch[cache_key] = now
            
        return df.copy()

    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        dyn = BOT_CONFIG.dynamic
        min_candles = max(dyn.ma_slow, dyn.macd_slow, dyn.bb_length, dyn.rsi_length, 14) + 5
        
        if len(df) < min_candles:
            raise ValueError(f"Not enough candles to compute indicators. Need {min_candles}.")

        df = df.copy()
        
        df["ma20"] = ta_lib.trend.SMAIndicator(close=df["close"], window=dyn.ma_fast).sma_indicator()
        df["ma50"] = ta_lib.trend.SMAIndicator(close=df["close"], window=dyn.ma_slow).sma_indicator()
        df["rsi"] = ta_lib.momentum.RSIIndicator(close=df["close"], window=dyn.rsi_length).rsi()

        macd = ta_lib.trend.MACD(
            close=df["close"], 
            window_fast=dyn.macd_fast, 
            window_slow=dyn.macd_slow, 
            window_sign=dyn.macd_signal
        )
        df["MACD"] = macd.macd()
        df["MACDs"] = macd.macd_signal()
        df["MACDh"] = macd.macd_diff()

        bb = ta_lib.volatility.BollingerBands(
            close=df["close"], 
            window=dyn.bb_length, 
            window_dev=dyn.bb_std
        )
        df["BBU"] = bb.bollinger_hband()
        df["BBM"] = bb.bollinger_mavg()
        df["BBL"] = bb.bollinger_lband()
        df["bb_pct_b"] = bb.bollinger_pband()

        df["atr"] = ta_lib.volatility.AverageTrueRange(
            high=df["high"], low=df["low"], close=df["close"], window=14
        ).average_true_range()
        
        df["vol_avg"] = df["volume"].rolling(20).mean()
        df["vol_ratio"] = df["volume"] / (df["vol_avg"] + 1e-10)
        
        return df

    def detect_wick_rejections(self, df: pd.DataFrame, sr_levels: List[SupportResistance], lookback: int = 3) -> List[WickRejection]:
        dyn = BOT_CONFIG.dynamic
        rejections = []
        levels =[{"price": sr.level, "type": sr.level_type} for sr in sr_levels]
        latest = df.iloc[-1]
        
        if "BBU" in df.columns and "BBL" in df.columns:
            levels.extend([
                {"price": float(latest.get("BBU", 0)), "type": "bb_upper"},
                {"price": float(latest.get("BBL", 0)), "type": "bb_lower"}
            ])

        scan_start = max(0, len(df) - lookback)
        for i in range(scan_start, len(df)):
            row = df.iloc[i]
            o, h, l, c = row["open"], row["high"], row["low"], row["close"]
            body = abs(c - o)
            full_range = h - l
            
            if full_range <= 0 or body <= 0:
                continue
                
            upper_wick = h - max(c, o)
            lower_wick = min(c, o) - l

            # Check Bullish Wicks
            if lower_wick / body >= dyn.min_wick_ratio and lower_wick / full_range >= dyn.min_wick_pct_of_range:
                for lvl in levels:
                    if lvl["type"] in ("support", "bb_lower") and lvl["price"] > 0:
                        dist_pct = abs(l - lvl["price"]) / lvl["price"] * 100
                        if dist_pct <= dyn.wick_proximity_pct:
                            rejections.append(WickRejection(
                                timestamp=df.index[i].to_pydatetime(),
                                direction="bullish",
                                price=float(c),
                                wick_length=float(lower_wick),
                                body_length=float(body),
                                wick_ratio=round(lower_wick / body, 2),
                                wick_pct_of_range=round(lower_wick / full_range, 2),
                                rejected_level=float(lvl["price"]),
                                level_type=lvl["type"],
                                candle_low=float(l),
                                candle_high=float(h)
                            ))
                            break

            # Check Bearish Wicks
            if upper_wick / body >= dyn.min_wick_ratio and upper_wick / full_range >= dyn.min_wick_pct_of_range:
                for lvl in levels:
                    if lvl["type"] in ("resistance", "bb_upper") and lvl["price"] > 0:
                        dist_pct = abs(h - lvl["price"]) / lvl["price"] * 100
                        if dist_pct <= dyn.wick_proximity_pct:
                            rejections.append(WickRejection(
                                timestamp=df.index[i].to_pydatetime(),
                                direction="bearish",
                                price=float(c),
                                wick_length=float(upper_wick),
                                body_length=float(body),
                                wick_ratio=round(upper_wick / body, 2),
                                wick_pct_of_range=round(upper_wick / full_range, 2),
                                rejected_level=float(lvl["price"]),
                                level_type=lvl["type"],
                                candle_low=float(l),
                                candle_high=float(h)
                            ))
                            break
        return rejections

    def detect_bb_touch(self, df: pd.DataFrame) -> Optional[str]:
        if "bb_pct_b" not in df.columns or len(df) < 1:
            return None
        pct_b = df["bb_pct_b"].iloc[-1]
        if pct_b <= BOT_CONFIG.dynamic.bb_touch_threshold:
            return "lower"
        if pct_b >= BOT_CONFIG.dynamic.bb_upper_threshold:
            return "upper"
        return None

    def interpret_macd(self, df: pd.DataFrame) -> Dict:
        if "MACD" not in df.columns or len(df) < 3:
            return {"signal": "neutral", "strength": 0.0, "detail": "insufficient data"}
            
        curr_hist = df["MACDh"].iloc[-1]
        prev_hist = df["MACDh"].iloc[-2]
        prev2_hist = df["MACDh"].iloc[-3]
        curr_macd = df["MACD"].iloc[-1]
        curr_signal = df["MACDs"].iloc[-1]
        
        hist_rising = curr_hist > prev_hist
        prev_diff = df["MACD"].iloc[-2] - df["MACDs"].iloc[-2]
        curr_diff = curr_macd - curr_signal
        
        if prev_diff <= 0 and curr_diff > 0:
            return {"signal": "bullish", "strength": 0.85, "detail": "MACD bullish crossover"}
        if prev_diff >= 0 and curr_diff < 0:
            return {"signal": "bearish", "strength": 0.85, "detail": "MACD bearish crossover"}
            
        if prev_hist < prev2_hist and curr_hist > prev_hist and curr_hist < 0:
            return {"signal": "bullish", "strength": 0.65, "detail": "MACD histogram turning bullish"}
        if prev_hist > prev2_hist and curr_hist < prev_hist and curr_hist > 0:
            return {"signal": "bearish", "strength": 0.65, "detail": "MACD histogram turning bearish"}
            
        if curr_hist > 0 and hist_rising:
            return {"signal": "bullish", "strength": 0.50, "detail": "MACD positive and rising"}
        if curr_hist < 0 and not hist_rising:
            return {"signal": "bearish", "strength": 0.50, "detail": "MACD negative and falling"}
            
        return {"signal": "neutral", "strength": 0.30, "detail": "MACD mixed signals"}

    def interpret_rsi(self, df: pd.DataFrame) -> Dict:
        dyn = BOT_CONFIG.dynamic
        if "rsi" not in df.columns or len(df) < 20:
            return {"signal": "neutral", "strength": 0.0, "detail": "insufficient data"}
            
        rsi = df["rsi"].iloc[-1]
        
        if rsi < dyn.rsi_oversold:
            base = {"signal": "bullish", "strength": 0.75, "detail": f"RSI oversold at {rsi:.1f}"}
        elif rsi > dyn.rsi_overbought:
            base = {"signal": "bearish", "strength": 0.75, "detail": f"RSI overbought at {rsi:.1f}"}
        elif rsi < 45:
            base = {"signal": "bearish_lean", "strength": 0.40, "detail": f"RSI at {rsi:.1f} — bearish lean"}
        elif rsi > 55:
            base = {"signal": "bullish_lean", "strength": 0.40, "detail": f"RSI at {rsi:.1f} — bullish lean"}
        else:
            base = {"signal": "neutral", "strength": 0.25, "detail": f"RSI neutral at {rsi:.1f}"}

        if len(df) >= 28:
            recent = df.iloc[-14:]
            prev_section = df.iloc[-28:-14]
            
            bull_div = recent["low"].min() < prev_section["low"].min() and recent["rsi"].min() > prev_section["rsi"].min()
            bear_div = recent["high"].max() > prev_section["high"].max() and recent["rsi"].max() < prev_section["rsi"].max()
            
            if bull_div and not bear_div:
                base.update({
                    "divergence": "bullish", 
                    "strength": min(0.90, base["strength"] + 0.20), 
                    "detail": base["detail"] + " + BULLISH DIVERGENCE"
                })
            elif bear_div and not bull_div:
                base.update({
                    "divergence": "bearish", 
                    "strength": min(0.90, base["strength"] + 0.20), 
                    "detail": base["detail"] + " + BEARISH DIVERGENCE"
                })
        return base

    def find_support_resistance(self, df: pd.DataFrame, window: int = 10, num_levels: int = 5) -> List[SupportResistance]:
        levels =[]
        for i in range(window, len(df) - window):
            h_slice = df["high"].iloc[i - window:i + window + 1]
            l_slice = df["low"].iloc[i - window:i + window + 1]
            
            if df["high"].iloc[i] == h_slice.max():
                levels.append({
                    "level": float(df["high"].iloc[i]), 
                    "type": "resistance", 
                    "time": df.index[i].to_pydatetime()
                })
            if df["low"].iloc[i] == l_slice.min():
                levels.append({
                    "level": float(df["low"].iloc[i]), 
                    "type": "support", 
                    "time": df.index[i].to_pydatetime()
                })

        if not levels:
            return[]
            
        clustered = self._cluster_levels(levels, BOT_CONFIG.dynamic.pivot_cluster_pct)
        clustered.sort(key=lambda x: x.strength, reverse=True)
        return clustered[:num_levels]

    def _cluster_levels(self, raw: List[dict], tol_pct: float) -> List[SupportResistance]:
        if not raw:
            return []
            
        ordered = sorted(raw, key=lambda x: x["level"])
        clusters =[]
        current = [ordered[0]]
        
        for item in ordered[1:]:
            avg = np.mean([c["level"] for c in current])
            if abs(item["level"] - avg) / avg * 100 < tol_pct:
                current.append(item)
            else:
                clusters.append(current)
                current = [item]
        clusters.append(current)

        result = []
        for cluster in clusters:
            avg = np.mean([c["level"] for c in cluster])
            level_type = max(set(c["type"] for c in cluster), key=lambda t: sum(1 for c in cluster if c["type"] == t))
            last_time = max(c["time"] for c in cluster)
            result.append(SupportResistance(round(float(avg), 2), len(cluster), level_type, last_time))
            
        return result

    def build_snapshot(self) -> IndicatorSnapshot:
        df = self.fetch_ohlcv(BOT_CONFIG.timeframes.primary, 200)
        df = self.compute_indicators(df)
        r = df.iloc[-1]
        
        current_price = self.get_live_price()
        if current_price <= 0:
            current_price = float(r["close"])
            
        return IndicatorSnapshot(
            timestamp=datetime.now(timezone.utc),
            price=current_price,
            rsi=float(r.get("rsi", 50)),
            macd_line=float(r.get("MACD", 0)),
            macd_signal=float(r.get("MACDs", 0)),
            macd_histogram=float(r.get("MACDh", 0)),
            bb_upper=float(r.get("BBU", 0)),
            bb_middle=float(r.get("BBM", 0)),
            bb_lower=float(r.get("BBL", 0)),
            bb_pct_b=float(r.get("bb_pct_b", 0.5)),
            ma20=float(r.get("ma20", 0)),
            ma50=float(r.get("ma50", 0)),
            atr=float(r.get("atr", 0)),
            volume_current=float(r["volume"]),
            volume_avg=float(r.get("vol_avg", 0)),
            volume_ratio=float(r.get("vol_ratio", 1))
        )
