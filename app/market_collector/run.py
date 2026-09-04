"""
Runner Daemon for Market Collector.
Dapat dijalankan secara mandiri:
  python -m app.market_collector.run
  python -m app.market_collector.run --once
  python -m app.market_collector.run --interval 300
"""
import argparse
import os
import signal
import sys
import time
from datetime import datetime, timezone

from app.core.config import settings
from app.core.logging import get_logger
from app.market_collector.collector import run_collection_cycle

logger = get_logger("market_collector_runner")

_running = True


def _handle_signal(signum, frame):
    global _running
    sig_name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
    logger.info("Menerima sinyal %s, menyiapkan shutdown anggun (graceful shutdown)...", sig_name)
    _running = False


def main():
    parser = argparse.ArgumentParser(description="Polymarket Weather Market Collector Daemon")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Jalankan satu siklus pengumpulan data saja lalu keluar",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Interval perulangan dalam detik (default dari COLLECTOR_INTERVAL_SECONDS)",
    )
    args = parser.parse_args()

    # Registrasi signal handler
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # Menentukan interval: CLI argument > ENV VAR > Settings default
    if args.interval is not None:
        interval = args.interval
    else:
        interval_env = os.getenv("COLLECTOR_INTERVAL_SECONDS")
        interval = int(interval_env) if interval_env else settings.COLLECTOR_INTERVAL_SECONDS

    logger.info(
        "Market Collector Service dimulai (Interval: %d detik, Run Mode: %s)",
        interval,
        "ONCE" if args.once else "DAEMON",
    )

    if args.once:
        count = run_collection_cycle()
        logger.info("Mode --once selesai. Total snapshot tersimpan: %d", count)
        sys.exit(0)

    # Daemon loop
    while _running:
        start_time = datetime.now(timezone.utc)
        try:
            count = run_collection_cycle()
            logger.info("Siklus berhasil, %d market tersimpan pada %s", count, start_time.isoformat())
        except Exception as loop_err:
            logger.error("Error tak tertangani pada runner loop: %s", str(loop_err), exc_info=True)

        # Sleep bertahap 1 detik agar responsif terhadap sinyal shutdown
        elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
        sleep_duration = max(0.0, float(interval) - elapsed)
        logger.info("Menunggu siklus berikutnya dalam %.1f detik...", sleep_duration)

        slept = 0.0
        while _running and slept < sleep_duration:
            step = min(1.0, sleep_duration - slept)
            time.sleep(step)
            slept += step

    logger.info("Market Collector Service dihentikan dengan aman.")


if __name__ == "__main__":
    main()
