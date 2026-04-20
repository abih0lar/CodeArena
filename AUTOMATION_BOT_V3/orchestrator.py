import logging
import threading
import time
import uuid
import json
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from config import BOT_CONFIG
from data_engine import DataEngine
from database import Database, CustomEncoder
from models import Bias, MonologueEntry, PivotEvent, Trade, TradeStatus
from notifier import Notifier
from risk_manager import RiskManager
from scholar import Scholar
from scout import Scout

log = logging.getLogger("__main__")

class PivotEngine:
    def __init__(self, data_engine: DataEngine, db: Database):
        self.data = data_engine
        self.db = db
        self.current_bias = Bias.NEUTRAL
        self.key_levels =[]
        self.last_pivot_time = None

    def update_levels(self, supports, resistances):
        self.key_levels = sorted(set(supports + resistances))

    def check_for_pivot(self, price, volume_ratio):
        if self.last_pivot_time and (datetime.now(timezone.utc) - self.last_pivot_time).total_seconds() < 3600:
            return None
            
        threshold = BOT_CONFIG.dynamic.volume_breakout_multiplier
        
        for level in self.key_levels:
            dist = abs(price - level) / level * 100
            if dist < 0.3 and volume_ratio > threshold:
                if price > level and self.current_bias != Bias.LONG:
                    event = PivotEvent(
                        timestamp=datetime.now(timezone.utc), 
                        old_bias=self.current_bias, 
                        new_bias=Bias.LONG, 
                        trigger_price=price, 
                        broken_level=level, 
                        volume_ratio=volume_ratio, 
                        reasoning=f"PIVOT: ${price:,.0f} broke above ${level:,.0f} with {volume_ratio:.1f}x vol — LONG"
                    )
                    self.current_bias = Bias.LONG
                    self.last_pivot_time = datetime.now(timezone.utc)
                    self._log(event)
                    return event
                    
                if price < level and self.current_bias != Bias.SHORT:
                    event = PivotEvent(
                        timestamp=datetime.now(timezone.utc), 
                        old_bias=self.current_bias, 
                        new_bias=Bias.SHORT, 
                        trigger_price=price, 
                        broken_level=level, 
                        volume_ratio=volume_ratio, 
                        reasoning=f"PIVOT: ${price:,.0f} broke below ${level:,.0f} with {volume_ratio:.1f}x vol — SHORT"
                    )
                    self.current_bias = Bias.SHORT
                    self.last_pivot_time = datetime.now(timezone.utc)
                    self._log(event)
                    return event
        return None

    def _log(self, event: PivotEvent):
        self.db.add_pivot_event({
            "timestamp": event.timestamp.isoformat(), 
            "old_bias": event.old_bias.value, 
            "new_bias": event.new_bias.value, 
            "trigger_price": event.trigger_price, 
            "broken_level": event.broken_level, 
            "volume_ratio": event.volume_ratio, 
            "reasoning": event.reasoning
        })
        self.db.add_monologue(MonologueEntry(
            timestamp=event.timestamp, 
            agent="Pivot Engine", 
            message=event.reasoning, 
            severity="action"
        ))

class TrailingStopManager:
    def update(self, bias: str, entry: float, current_price: float, current_sl: float, current_extreme: float, trail_pct: float, activation_pct: float = 0.3) -> Tuple[Optional[float], float]:
        if bias == "LONG":
            updated_extreme = max(current_extreme, current_price)
            profit_pct = (current_price - entry) / entry * 100
            
            if profit_pct < activation_pct:
                return None, updated_extreme
                
            new_sl = updated_extreme * (1 - trail_pct / 100)
            if new_sl > current_sl:
                return round(new_sl, 2), updated_extreme
            else:
                return None, updated_extreme
                
        elif bias == "SHORT":
            updated_extreme = min(current_extreme, current_price)
            profit_pct = (entry - current_price) / entry * 100
            
            if profit_pct < activation_pct:
                return None, updated_extreme
                
            new_sl = updated_extreme * (1 + trail_pct / 100)
            if new_sl < current_sl:
                return round(new_sl, 2), updated_extreme
            else:
                return None, updated_extreme
                
        return None, current_extreme

class Orchestrator:
    SCAN_INTERVAL_SECONDS = 30

    def __init__(self):
        self.db = Database(BOT_CONFIG.db_path)
        self._restore_dynamic_config()
        self.data_engine = DataEngine()
        self.scout = Scout(self.data_engine, self.db)
        self.risk_mgr = RiskManager(self.db)
        self.scholar = Scholar(self.db)
        self.pivot_engine = PivotEngine(self.data_engine, self.db)
        self.trailing = TrailingStopManager()
        self.notifier = Notifier()

        self.running = False
        self._thread: Optional[threading.Thread] = None
        self.last_signal = None
        self.last_verdict = None
        self.last_scholar_review = None
        self.cycle_count = 0
        
        # ── V3.1 STATEFUL ALERT MEMORY ──
        self.last_alert_hour = None
        self.last_alert_bias = None
        
        self._scan_lock = threading.Lock()
        self._initial_scan_done = threading.Event()
        
        self._init_thread = threading.Thread(target=self._safe_initial_scan, daemon=True)
        self._init_thread.start()

    def _restore_dynamic_config(self):
        try:
            snapshot = self.db.get_latest_config_snapshot()
            if snapshot:
                for k, v in snapshot.items():
                    if hasattr(BOT_CONFIG.dynamic, k):
                        setattr(BOT_CONFIG.dynamic, k, v)
        except Exception:
            pass

    def _safe_initial_scan(self):
        try:
            self._execute_scan()
        except Exception:
            pass
        finally:
            self._initial_scan_done.set()

    def start(self):
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False
        if self._thread:
            self._thread.join(timeout=10)

    def _run_loop(self):
        if not self._initial_scan_done.is_set():
            self._initial_scan_done.wait(timeout=10)
            
        while self.running:
            loop_start = time.monotonic()
            try:
                self.cycle_count += 1
                cycle_time = datetime.now(timezone.utc).strftime("%H:%M:%S")
                log.info(f"--- Cycle {self.cycle_count} | {cycle_time} UTC ---")
                
                self._manage_positions()
                self._execute_scan()
                
            except Exception as e:
                log.error("Cycle error: %s", e)
                
            elapsed = time.monotonic() - loop_start
            time.sleep(max(0, self.SCAN_INTERVAL_SECONDS - elapsed))

    def _execute_scan(self):
        acquired = self._scan_lock.acquire(blocking=False)
        if not acquired:
            return

        try:
            signal = self.scout.scan()
            self.last_signal = signal
            self.db.set_memory_state("last_signal", signal) 
            
            log.info(f"Scout Signal: {signal.bias.value} | Conf: {int(signal.confidence*100)}%")

            sr = self.data_engine.find_support_resistance(
                self.data_engine.fetch_ohlcv(BOT_CONFIG.timeframes.primary, 100), 
                window=BOT_CONFIG.dynamic.pivot_lookback
            )
            supports =[s.level for s in sr if s.level_type == "support"]
            resistances =[s.level for s in sr if s.level_type == "resistance"]
            self.pivot_engine.update_levels(supports, resistances)

            pivot = self.pivot_engine.check_for_pivot(signal.indicators.price, signal.indicators.volume_ratio)
            if pivot:
                signal.bias = pivot.new_bias

            try:
                bal = self.data_engine.get_account_balance()
                if bal > 0:
                    self.risk_mgr.set_portfolio_value(bal)
            except Exception:
                pass

            verdict = self.risk_mgr.evaluate(signal)
            self.last_verdict = verdict
            self.db.set_memory_state("last_verdict", verdict) 
            
            log.info(f"Risk Manager: {'APPROVED' if verdict.approved else 'DENIED'}")

            if verdict.approved:
                active_open = self.db.get_active_trades()
                if len(active_open) >= BOT_CONFIG.dynamic.max_open_positions:
                    pass
                else:
                    is_ready = False
                    if signal.bias == Bias.LONG and signal.indicators.price <= signal.sniper_entry_price:
                        is_ready = True
                    if signal.bias == Bias.SHORT and signal.indicators.price >= signal.sniper_entry_price:
                        is_ready = True
                        
                    initial_status = TradeStatus.OPEN if is_ready else TradeStatus.PENDING

                    trade = Trade(
                        id=str(uuid.uuid4())[:8], 
                        timestamp_open=datetime.now(timezone.utc), 
                        timestamp_close=None,
                        symbol=BOT_CONFIG.exchange.symbol, 
                        bias=signal.bias, 
                        entry_price=signal.sniper_entry_price, 
                        exit_price=None,
                        position_size_usd=verdict.position_size_usd, 
                        stop_loss=verdict.stop_loss, 
                        take_profit=verdict.take_profit,
                        status=initial_status, 
                        pnl_usd=None, 
                        pnl_pct=None, 
                        scout_reasoning=signal.reasoning,
                        risk_reasoning=verdict.reasoning, 
                        trailing_extreme_price=0.0,
                    )
                    self.db.insert_trade(trade)
                    
                    if is_ready:
                        self.db.add_monologue(MonologueEntry(
                            timestamp=datetime.now(timezone.utc), 
                            agent="Orchestrator", 
                            message=f"TRADE OPENED (Market): {trade.bias.value} @ ${trade.entry_price:,.0f} | SL ${trade.stop_loss:,.0f} | TP ${trade.take_profit:,.0f}", 
                            severity="action"
                        ))
                        try:
                            self.notifier.trade_opened(
                                bias=trade.bias.value, 
                                entry=trade.entry_price, 
                                sl=verdict.stop_loss, 
                                tp=verdict.take_profit, 
                                rr=verdict.risk_reward_ratio, 
                                size=verdict.position_size_usd, 
                                trade_id=trade.id, 
                                sl_pct=verdict.sl_distance_pct
                            )
                        except Exception:
                            pass
                    else:
                        self.db.add_monologue(MonologueEntry(
                            timestamp=datetime.now(timezone.utc), 
                            agent="Orchestrator", 
                            message=f"ORDER PENDING (Limit): {trade.bias.value} waiting for ${trade.entry_price:,.0f}", 
                            severity="info"
                        ))

            # ── V3.1 ANTI-SPAM NOTIFICATION LOGIC ──
            if signal.bias != Bias.NEUTRAL and signal.confidence >= BOT_CONFIG.dynamic.min_confidence:
                # Isolate the current H1 candle time
                current_hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
                
                # Only alert if it's a NEW candle, OR if the market flipped directions in the same candle
                if self.last_alert_hour != current_hour or self.last_alert_bias != signal.bias:
                    try:
                        self.notifier.signal_detected(
                            bias=signal.bias.value, 
                            confidence=signal.confidence, 
                            price=signal.entry_price, 
                            patterns=len(signal.patterns_detected)
                        )
                        # Lock the memory state so it doesn't spam again
                        self.last_alert_hour = current_hour
                        self.last_alert_bias = signal.bias
                    except Exception:
                        pass

            if len([t for t in self.db.get_trades(1) if t["status"] == "CLOSED"]) > 0 and self.scholar.should_review():
                threading.Thread(target=self._run_scholar, daemon=True).start()
                
        finally:
            self._scan_lock.release()

    def _manage_positions(self) -> int:
        try:
            price = self.data_engine.get_current_price()
        except Exception:
            return 0
            
        closed_count = 0

        # --- 1. PROCESS PENDING ORDERS (V3.1 Sniper Module) ---
        pending_trades = self.db.get_pending_trades()
        for pt in pending_trades:
            tid = pt["id"]
            bias = pt["bias"]
            entry = pt["entry_price"]
            tp = pt["take_profit"]
            
            # Price ran away to Target? Cancel.
            if (bias == "LONG" and price >= tp) or (bias == "SHORT" and price <= tp):
                self.db.update_trade(tid, status="CANCELLED")
                self.db.add_monologue(MonologueEntry(
                    timestamp=datetime.now(timezone.utc), 
                    agent="Orchestrator", 
                    message=f"PENDING CANCELLED: {bias} {tid} missed. Price ran away.", 
                    severity="info"
                ))
                continue
            
            # Price dipped into Sniper Limit? Execute.
            activated = False
            if bias == "LONG" and price <= entry:
                activated = True
            elif bias == "SHORT" and price >= entry:
                activated = True
            
            if activated:
                self.db.update_trade(tid, status="OPEN", timestamp_open=datetime.now(timezone.utc).isoformat())
                self.db.add_monologue(MonologueEntry(
                    timestamp=datetime.now(timezone.utc), 
                    agent="Orchestrator", 
                    message=f"TRADE OPENED (Sniper Limit): {bias} {tid} @ ${entry:,.0f}", 
                    severity="action"
                ))
                try:
                    self.notifier.trade_opened(
                        bias=bias, 
                        entry=entry, 
                        sl=pt["stop_loss"], 
                        tp=pt["take_profit"], 
                        rr=0, 
                        size=pt["position_size_usd"], 
                        trade_id=tid, 
                        sl_pct=0
                    )
                except Exception:
                    pass

        # --- 2. MANAGE OPEN ORDERS ---
        opens = self.db.get_open_trades()
        for t in opens:
            sl = t["stop_loss"]
            tp = t["take_profit"]
            bias = t["bias"]
            entry = t["entry_price"]
            tid = t["id"]
            
            current_extreme = t.get("trailing_extreme_price") or entry
            if current_extreme == 0.0:
                current_extreme = entry

            new_sl, updated_extreme = self.trailing.update(
                bias=bias, 
                entry=entry, 
                current_price=price, 
                current_sl=sl, 
                current_extreme=current_extreme, 
                trail_pct=BOT_CONFIG.dynamic.trailing_stop_pct, 
                activation_pct=0.3
            )

            if new_sl is not None:
                self.db.update_trade(tid, stop_loss=new_sl, trailing_extreme_price=updated_extreme)
                try:
                    self.notifier.trailing_stop_moved(
                        bias=bias, 
                        trade_id=tid, 
                        old_sl=sl, 
                        new_sl=new_sl, 
                        price=price
                    )
                except Exception:
                    pass
            elif updated_extreme != current_extreme:
                self.db.update_trade(tid, trailing_extreme_price=updated_extreme)

            hit = False
            exit_p = price
            
            if bias == "LONG":
                if price <= sl:
                    hit, exit_p = True, sl
                elif price >= tp:
                    hit, exit_p = True, tp
            elif bias == "SHORT":
                if price >= sl:
                    hit, exit_p = True, sl
                elif price <= tp:
                    hit, exit_p = True, tp

            manual = self.db.get_system_flag("manual_override")
            if manual in ["CLOSE_ALL"]:
                hit = True
            
            if hit:
                reason = "trailing stop hit" if new_sl is not None and exit_p == sl else "SL/TP hit"
                if manual == "CLOSE_ALL":
                    reason = "Manual Close"
                self._close_trade(t, exit_p, reason=reason)
                closed_count += 1
                
        return closed_count

    def _close_trade(self, t: dict, exit_price: float, reason: str = ""):
        entry = t["entry_price"]
        bias = t["bias"]
        tid = t["id"]
        
        pnl_pct = ((exit_price - entry) / entry) * 100 * (-1 if bias == "SHORT" else 1)
        pnl_usd = t["position_size_usd"] * (pnl_pct / 100)

        self.db.update_trade(
            tid, 
            status="CLOSED", 
            exit_price=exit_price, 
            pnl_usd=round(pnl_usd, 2), 
            pnl_pct=round(pnl_pct, 4), 
            timestamp_close=datetime.now(timezone.utc).isoformat()
        )
        
        self.db.add_monologue(MonologueEntry(
            timestamp=datetime.now(timezone.utc), 
            agent="Orchestrator", 
            message=f"TRADE CLOSED ({reason or 'SL/TP hit'}): {bias} {tid} | ${entry:,.0f} → ${exit_price:,.0f} | P&L: ${pnl_usd:,.2f} ({pnl_pct:+.2f}%)", 
            severity="action"
        ))
        
        try:
            self.notifier.trade_closed(
                bias=bias, 
                entry=entry, 
                exit_price=exit_price, 
                pnl_usd=round(pnl_usd, 2), 
                pnl_pct=round(pnl_pct, 2), 
                reason=reason or 'SL/TP hit', 
                trade_id=tid
            )
        except Exception:
            pass

    def _run_scholar(self):
        try:
            df = self.data_engine.fetch_ohlcv(BOT_CONFIG.timeframes.primary, 50)
            df = self.data_engine.compute_indicators(df)
            
            price = df["close"].iloc[-1]
            atr = df["atr"].iloc[-1] if "atr" in df else 0
            vol = atr / price if price > 0 else 0.01
            
            ma20 = df["ma20"].iloc[-1] if "ma20" in df else price
            ma50 = df["ma50"].iloc[-1] if "ma50" in df else price
            
            self.last_scholar_review = self.scholar.meta_strategy_update(
                vol, 
                min(1.0, abs(ma20 - ma50) / (price * 0.005))
            )
            self.db.set_memory_state("last_scholar_review", self.last_scholar_review) 
        except Exception as e:
            log.error("Scholar review failed: %s", e)

    def close_trade_manually(self, trade_id: str) -> bool:
        trade = next((t for t in self.db.get_active_trades() if t["id"] == trade_id), None)
        if not trade:
            return False
        
        if trade["status"] == "PENDING":
            self.db.update_trade(trade_id, status="CANCELLED")
            self.db.add_monologue(MonologueEntry(
                datetime.now(timezone.utc), 
                "Orchestrator", 
                f"PENDING CANCELLED: {trade['bias']} {trade_id} manually.", 
                "action"
            ))
            return True
            
        try:
            price = self.data_engine.get_current_price()
        except Exception:
            return False
            
        self._close_trade(trade, price, reason="manual close")
        return True

    def close_all_trades(self) -> int:
        self.db.set_system_flag("manual_override", "CLOSE_ALL")
        return 1

    def get_current_state(self):
        def _serialize(obj):
            if not obj:
                return None
            if isinstance(obj, dict):
                return obj
            return json.loads(json.dumps(asdict(obj), cls=CustomEncoder))

        return {
            "running": True, 
            "cycle_count": len(self.db.get_monologue(5000)),
            "last_signal": self.db.get_memory_state("last_signal") or _serialize(self.last_signal),
            "last_verdict": self.db.get_memory_state("last_verdict") or _serialize(self.last_verdict),
            "last_scholar_review": self.db.get_memory_state("last_scholar_review") or _serialize(self.last_scholar_review),
            "kill_switch": self.db.get_system_flag("kill_switch") == "1",
            "current_bias": self.pivot_engine.current_bias.value,
            "monologue": self.db.get_monologue(50), 
            "active_trades": self.db.get_active_trades(), 
            "trade_stats_7d": self.db.get_trade_stats(7), 
            "trade_stats_30d": self.db.get_trade_stats(30),
            "scholar_reviews": self.db.get_scholar_reviews(5), 
            "dynamic_config": vars(BOT_CONFIG.dynamic),
        }

    def force_scan(self):
        self.db.set_system_flag("manual_override", "FORCE_SCAN")
        return self.get_current_state()

    def force_scholar_review(self):
        threading.Thread(target=self._run_scholar, daemon=True).start()

    def activate_kill_switch(self):
        self.db.set_system_flag("kill_switch", "1")

    def deactivate_kill_switch(self):
        self.db.set_system_flag("kill_switch", "0")

    def reset_portfolio_tracking(self, full_reset: bool = False):
        if full_reset:
            self.db.reset_all_data()
        else:
            self.db.reset_trades_only()

    def manual_override(self, action):
        self.db.set_system_flag("manual_override", action)
