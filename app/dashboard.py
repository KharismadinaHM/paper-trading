"""
FastAPI Dashboard untuk Polymarket Paper Trading.
Dijalankan via: uvicorn app.dashboard:app --reload atau python -m app.dashboard
"""
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

# Template directory
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"

app = FastAPI(title="Polymarket Paper Trading Dashboard")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Import service functions dari paper_service.py dengan fallback fleksibel
try:
    from app.paper_service import (
        get_account_status,
        get_equity_snapshots,
        get_open_positions,
        get_performance,
        get_trade_history,
    )
except ImportError:
    try:
        from app.paper_trading.paper_service import (
            get_account_status,
            get_equity_snapshots,
            get_open_positions,
            get_performance,
            get_trade_history,
        )
    except ImportError:
        from .paper_service import (
            get_account_status,
            get_equity_snapshots,
            get_open_positions,
            get_performance,
            get_trade_history,
        )


@app.get("/", response_class=HTMLResponse)
def get_dashboard(request: Request, strategy: Optional[str] = None):
    """
    Halaman utama dashboard paper trading.
    Menerima optional query parameter `?strategy=weather_v1` untuk memfilter data.
    """
    account = get_account_status()
    performance = get_performance(strategy_version=strategy)
    positions = get_open_positions()
    trades = get_trade_history(limit=50, strategy_version=strategy)
    snapshots = get_equity_snapshots()

    # Siapkan data untuk Chart.js
    chart_labels = [str(s.get("timestamp", "")) for s in snapshots]
    chart_balances = [float(s.get("balance", 0)) for s in snapshots]
    chart_equities = [float(s.get("equity", s.get("balance", 0))) for s in snapshots]

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "account": account,
            "performance": performance,
            "positions": positions,
            "trades": trades,
            "chart_labels": chart_labels,
            "chart_balances": chart_balances,
            "chart_equities": chart_equities,
            "current_strategy": strategy,
        },
    )


@app.get("/api/summary")
def get_summary_api(strategy: Optional[str] = None):
    """
    JSON API endpoint untuk status ringkasan & performa.
    """
    return {
        "account": get_account_status(),
        "performance": get_performance(strategy_version=strategy),
        "positions_count": len(get_open_positions()),
        "equity_snapshots": get_equity_snapshots(),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.dashboard:app", host="127.0.0.1", port=8000, reload=True)
