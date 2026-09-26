"""
Settlement Worker.
Dijalankan setiap siklus Market Collector:
1. Memperbarui harga & status resolusi market yang masih punya posisi terbuka (Gamma API).
2. Menutup posisi pada market yang sudah resolve (WON / LOST / CANCELLED) dan mengkredit saldo.
"""
from typing import Any, Dict

from app.core.logging import get_logger
from app.market_collector.collector import sync_open_position_markets
from app.paper_service import settle_resolved_positions

logger = get_logger("settlement_worker")


def run_settlement_cycle() -> Dict[str, Any]:
    """Satu siklus sinkronisasi resolusi + settlement. Tidak pernah melempar exception."""
    summary: Dict[str, Any] = {"synced": {"snapshots": 0, "resolutions": 0}, "settled": 0}
    try:
        summary["synced"] = sync_open_position_markets()
    except Exception as err:
        logger.error("Gagal sinkronisasi market posisi terbuka: %s", err, exc_info=True)
    try:
        summary["settled"] = len(settle_resolved_positions())
    except Exception as err:
        logger.error("Gagal menjalankan settlement: %s", err, exc_info=True)
    logger.info("Siklus settlement selesai: %s", summary)
    return summary
