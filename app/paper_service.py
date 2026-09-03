"""
Paper Service Interface and Stub Implementation.
Ganti implementasi fungsi di bawah ini dengan database query aktual saat wiring ke PostgreSQL/SQLAlchemy.
"""
from decimal import Decimal
from typing import Any, Dict, List, Optional


def start_paper_trading(strategy: Optional[str] = None) -> Dict[str, Any]:
    """
    Memulai engine paper trading (listener/collector/strategy loop).
    """
    return {
        "status": "running",
        "strategy": strategy or "all_active",
        "message": f"Paper trading engine started with strategy: {strategy or 'all'}"
    }


def get_account_status() -> Dict[str, Any]:
    """
    Mengambil ringkasan status paper account saat ini.
    """
    return {
        "balance": Decimal("20.00"),
        "invested": Decimal("3.00"),
        "realized_pnl": Decimal("0.42"),
        "win_rate": Decimal("0.80"),
        "open_trades": 3,
    }


def get_open_positions() -> List[Dict[str, Any]]:
    """
    Mengambil daftar posisi yang sedang terbuka (open positions).
    """
    return [
        {
            "market": "Will NYC exceed 85°F on Sep 5?",
            "side": "BUY",
            "entry_price": Decimal("0.65"),
            "size": Decimal("1.00"),
            "shares": Decimal("1.5385"),
            "current_price": Decimal("0.72"),
            "unrealized_pnl": Decimal("0.11"),
            "strategy_version": "weather_v1",
        },
        {
            "market": "Will Miami rain > 0.5 in on Sep 6?",
            "side": "BUY",
            "entry_price": Decimal("0.40"),
            "size": Decimal("1.00"),
            "shares": Decimal("2.5000"),
            "current_price": Decimal("0.38"),
            "unrealized_pnl": Decimal("-0.05"),
            "strategy_version": "weather_v1",
        },
        {
            "market": "Will Chicago wind > 25mph on Sep 6?",
            "side": "BUY",
            "entry_price": Decimal("0.55"),
            "size": Decimal("1.00"),
            "shares": Decimal("1.8182"),
            "current_price": Decimal("0.58"),
            "unrealized_pnl": Decimal("0.05"),
            "strategy_version": "weather_v2",
        },
    ]


def get_trade_history(
    limit: int = 20, 
    strategy_version: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Mengambil riwayat trade yang telah closed/selesai.
    """
    all_trades = [
        {
            "id": "tr-001",
            "date": "2026-09-01 10:15",
            "market": "Austin high > 95°F Sep 1",
            "side": "BUY",
            "entry_price": Decimal("0.70"),
            "exit_price": Decimal("1.00"),
            "size": Decimal("1.00"),
            "status": "WON",
            "net_pnl": Decimal("0.43"),
            "strategy_version": "weather_v1",
        },
        {
            "id": "tr-002",
            "date": "2026-09-01 14:30",
            "market": "Seattle rain > 0.1 in Sep 1",
            "side": "BUY",
            "entry_price": Decimal("0.50"),
            "exit_price": Decimal("0.00"),
            "size": Decimal("1.00"),
            "status": "LOST",
            "net_pnl": Decimal("-1.00"),
            "strategy_version": "weather_v1",
        },
        {
            "id": "tr-003",
            "date": "2026-09-02 09:00",
            "market": "Dallas temp > 90°F Sep 2",
            "side": "BUY",
            "entry_price": Decimal("0.60"),
            "exit_price": Decimal("1.00"),
            "size": Decimal("1.00"),
            "status": "WON",
            "net_pnl": Decimal("0.67"),
            "strategy_version": "weather_v1",
        },
        {
            "id": "tr-004",
            "date": "2026-09-02 11:20",
            "market": "Denver snow > 1 in Sep 2",
            "side": "BUY",
            "entry_price": Decimal("0.75"),
            "exit_price": Decimal("1.00"),
            "size": Decimal("1.00"),
            "status": "WON",
            "net_pnl": Decimal("0.33"),
            "strategy_version": "weather_v2",
        },
    ]
    if strategy_version:
        all_trades = [t for t in all_trades if t.get("strategy_version") == strategy_version]
    return all_trades[:limit]


def get_performance(strategy_version: Optional[str] = None) -> Dict[str, Any]:
    """
    Mengambil metrik performa (Win Rate, ROI, Drawdown, Realized/Unrealized P/L).
    """
    if strategy_version == "weather_v2":
        return {
            "strategy_version": "weather_v2",
            "trades": 1,
            "wins": 1,
            "losses": 0,
            "win_rate": Decimal("1.00"),
            "roi": Decimal("0.0165"),
            "max_drawdown": Decimal("0.00"),
            "realized_pnl": Decimal("0.33"),
            "unrealized_pnl": Decimal("0.05"),
        }
    return {
        "strategy_version": strategy_version or "all",
        "trades": 4,
        "wins": 3,
        "losses": 1,
        "win_rate": Decimal("0.75"),
        "roi": Decimal("0.0210"),
        "max_drawdown": Decimal("0.0487"),  # ~4.87%
        "realized_pnl": Decimal("0.42"),
        "unrealized_pnl": Decimal("0.11"),
    }


def reset_paper_account() -> Dict[str, Any]:
    """
    Mereset seluruh paper account ke kondisi awal (saldo awal, hapus trades & positions).
    """
    return {
        "success": True,
        "initial_balance": Decimal("20.00"),
        "message": "Paper account reset to $20.00 initial balance. All trades wiped."
    }


def get_equity_snapshots() -> List[Dict[str, Any]]:
    """
    Mengambil data riwayat balance & equity dari waktu ke waktu untuk grafik Equity Curve.
    """
    return [
        {"timestamp": "2026-09-01 10:00", "balance": Decimal("20.00"), "equity": Decimal("20.00")},
        {"timestamp": "2026-09-01 10:15", "balance": Decimal("20.43"), "equity": Decimal("20.43")},
        {"timestamp": "2026-09-01 14:30", "balance": Decimal("19.43"), "equity": Decimal("19.43")},
        {"timestamp": "2026-09-02 09:00", "balance": Decimal("20.10"), "equity": Decimal("20.10")},
        {"timestamp": "2026-09-02 11:20", "balance": Decimal("20.43"), "equity": Decimal("20.43")},
        {"timestamp": "2026-09-03 12:00", "balance": Decimal("20.42"), "equity": Decimal("20.53")},
    ]

