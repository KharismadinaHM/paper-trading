"""
FastAPI Dashboard untuk Polymarket Paper Trading.
Dijalankan via: uvicorn app.dashboard:app --reload atau python -m app.dashboard
"""
from decimal import Decimal
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

# Template directory
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"

app = FastAPI(title="Polymarket Paper Trading Dashboard")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Import service functions dari paper_service.py dengan fallback fleksibel
try:
    from app.paper_service import (
        create_paper_order,
        deposit_paper_funds,
        get_account_status,
        get_equity_snapshots,
        get_market_suggestions,
        get_open_positions,
        get_performance,
        get_trade_history,
        reset_paper_account,
        search_market_snapshots,
        sell_paper_position,
    )
except ImportError:
    try:
        from app.paper_trading.paper_service import (
            create_paper_order,
            deposit_paper_funds,
            get_account_status,
            get_equity_snapshots,
            get_market_suggestions,
            get_open_positions,
            get_performance,
            get_trade_history,
            reset_paper_account,
            search_market_snapshots,
            sell_paper_position,
        )
    except ImportError:
        from .paper_service import (
            create_paper_order,
            deposit_paper_funds,
            get_account_status,
            get_equity_snapshots,
            get_market_suggestions,
            get_open_positions,
            get_performance,
            get_trade_history,
            reset_paper_account,
            search_market_snapshots,
            sell_paper_position,
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
    suggested_markets = get_market_suggestions()

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
            "suggested_markets": suggested_markets,
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


@app.get("/api/positions")
def get_positions_api():
    """
    JSON API endpoint untuk daftar open positions terkini.
    """
    return get_open_positions()


@app.get("/api/trades")
def get_trades_api(limit: int = 50, strategy: Optional[str] = None):
    """
    JSON API endpoint untuk trade history.
    """
    return get_trade_history(limit=limit, strategy_version=strategy)


@app.get("/api/markets/suggestions")
def get_market_suggestions_api(
    max_hours_to_resolution: float = 6.0,
    min_price: float = 0.70,
    max_price: float = 0.75,
):
    """
    Endpoint query saran pasar (market suggestions) dari data Market Collector:
    - Status market belum resolved (open/active).
    - Waktu resolution mendekati sekarang (<= max_hours_to_resolution, default 6 jam).
    - Harga YES atau NO berada di rentang [min_price, max_price] (default 0.70-0.75).
    """
    return get_market_suggestions(
        max_hours_to_resolution=max_hours_to_resolution,
        min_price=min_price,
        max_price=max_price,
    )


@app.get("/api/markets/search")
def search_markets_api(
    q: str = "",
    category: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    time_filter: Optional[str] = None,
    sort_by: Optional[str] = None,
):
    """
    Endpoint pencarian market data dari Market Collector:
    - q: Kata kunci pencarian nama atau kategori.
    - category: Filter opsional berdasarkan kategori spesifik.
    - min_price / max_price: Filter opsional rentang harga.
    - time_filter: Filter sisa waktu (e.g. '6h', '24h', '3d', '7d', '30d').
    - sort_by: Pengurutan ('ending_soonest', 'ending_latest', 'highest_price', 'lowest_price', 'name').
    """
    return search_market_snapshots(
        query=q,
        category=category,
        min_price=min_price,
        max_price=max_price,
        time_filter=time_filter,
        sort_by=sort_by,
    )


class SellPositionRequest(BaseModel):
    market_id: str = Field(..., description="ID unik pasar yang akan dijual")
    side: str = Field(..., description="Sisi transaksi (YES atau NO)")
    shares: Optional[float] = Field(None, gt=0, description="Jumlah shares yang dijual (opsional, jika tidak diset maka jual semua)")


@app.post("/api/positions/sell")
def sell_position_api(payload: SellPositionRequest):
    """
    Endpoint untuk menjual posisi paper trading yang sedang terbuka (Paper Sell).
    """
    try:
        sh_dec = Decimal(str(payload.shares)) if payload.shares is not None else None
        res = sell_paper_position(
            market_id=payload.market_id,
            side=payload.side,
            shares_to_sell=sh_dec,
        )
        return res
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Terjadi kesalahan saat menjual posisi: {str(e)}"
        )


class DepositFundsRequest(BaseModel):
    amount: float = Field(..., gt=0, description="Jumlah deposit USD")


@app.post("/api/account/deposit")
def deposit_funds_api(payload: DepositFundsRequest):
    """
    Endpoint untuk menambah saldo paper account.
    """
    try:
        amt = Decimal(str(payload.amount))
        return deposit_paper_funds(amt)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@app.post("/api/account/reset")
def reset_account_api():
    """
    Endpoint untuk mereset akun paper trading ke kondisi awal ($20.00).
    """
    return reset_paper_account()


# --- Request & Response Models untuk Paper Orders ---

class CreateOrderRequest(BaseModel):
    market_id: str = Field(..., description="ID unik pasar dari Market Collector")
    side: str = Field(..., description="Sisi transaksi (YES atau NO)")
    position_size: float = Field(..., gt=0, description="Ukuran posisi dalam USD (default $1.00)")
    entry_price: Optional[float] = Field(
        None,
        description="Harga yang dilihat user di UI saat klik buy. CATATAN: Field ini HANYA untuk audit trail/logging, TIDAK DIGUNAKAN untuk kalkulasi eksekusi/settlement."
    )


class CreateOrderResponse(BaseModel):
    order_id: str
    market_id: str
    market_name: str
    side: str
    status: str
    requested_price: Optional[float] = None
    actual_price: float
    execution_price: float
    entry_price: float
    position_size: float
    shares: float
    warning: Optional[str] = None
    message: str


@app.post("/api/orders", response_model=CreateOrderResponse, status_code=status.HTTP_201_CREATED)
def create_order_api(payload: CreateOrderRequest):
    """
    Endpoint pemesanan paper order manual (Paper Buy).
    Dipanggil dari tombol 'Paper Buy' pada Suggested Markets dan Search Market.
    
    Proteksi Anti Stale Price & Urutan Wajib:
    1. entry_price dari frontend TIDAK DIPERCAYA / TIDAK DIGUNAKAN untuk kalkulasi shares/settlement.
    2. Backend mem-fetch harga real-time terbaru langsung dari Market Collector.
    3. Jika selisih harga real-time vs harga yang dilihat user > 5%, sertakan warning di response.
    4. Eksekusi alur: apply_slippage_and_spread -> evaluate_risk_and_rules -> calculate_shares -> simpan DB (OPEN).
    """
    try:
        user_p = Decimal(str(payload.entry_price)) if payload.entry_price is not None else None
        order = create_paper_order(
            market_id=payload.market_id,
            side=payload.side,
            position_size=Decimal(str(payload.position_size)),
            user_viewed_price=user_p,
            strategy_version="manual",
        )

        msg = f"Paper buy order berhasil dibuat (Status: {order['status']})."
        if order.get("warning"):
            msg = f"{msg} {order['warning']}"

        return CreateOrderResponse(
            order_id=order["order_id"],
            market_id=order["market_id"],
            market_name=order.get("market_name", order["market_id"]),
            side=order["side"],
            status=order["status"],
            requested_price=float(order["requested_price"]) if order.get("requested_price") is not None else None,
            actual_price=float(order["actual_price"]),
            execution_price=float(order["execution_price"]),
            entry_price=float(order["entry_price"]),
            position_size=float(order["position_size"]),
            shares=float(order["shares"]),
            warning=order.get("warning"),
            message=msg,
        )
    except ValueError as e:
        err_msg = str(e)
        if "tidak ditemukan" in err_msg.lower():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=err_msg)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=err_msg)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Terjadi kesalahan saat memproses order: {str(e)}"
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.dashboard:app", host="127.0.0.1", port=8000, reload=True)
