import logging
import signal
import sys
import time
from datetime import datetime, timezone
from orchestrator import Orchestrator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("bot.log", encoding="utf-8")],
)
log = logging.getLogger("__main__")

_orchestrator: Orchestrator | None = None

def _shutdown(signum, frame):
    log.info("Shutdown signal received — stopping orchestrator...")
    if _orchestrator is not None: _orchestrator.stop()
    log.info("Bot stopped cleanly. Goodbye.")
    sys.exit(0)

signal.signal(signal.SIGINT, _shutdown)
signal.signal(signal.SIGTERM, _shutdown)

def main():
    global _orchestrator
    log.info("=" * 60)
    log.info(" H1 Wick-Rejection Agentic Trading Bot")
    log.info(f" Started at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    log.info("=" * 60)
    
    log.info("Initialising Orchestrator...")
    _orchestrator = Orchestrator()
    
    log.info("Starting background trading loop...")
    _orchestrator.start()
    
    log.info("Bot is live. Press Ctrl+C to stop.")
    log.info("Dashboard: run `streamlit run dashboard.py` in a separate terminal.")
    log.info("-" * 60)
    
    while True:
        try: time.sleep(1)
        except Exception as e: log.error("Heartbeat error: %s", e)

if __name__ == "__main__":
    main()