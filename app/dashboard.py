"""
FastAPI Dashboard untuk Polymarket Paper Trading.
Dijalankan via: uvicorn app.dashboard:app --reload atau python -m app.dashboard
"""
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import func, text

from app.core.config import settings, validate_production_settings
from app.core.logging import get_logger
from app.paper_service import (
    create_paper_order,
    deposit_paper_funds,
    ensure_default_account,
    get_account_status,
    get_equity_snapshots,
    get_market_suggestions,
    get_open_positions,
    get_performance,
    get_recommendation_schedule,
    get_trade_history,
    reset_paper_account,
    search_market_snapshots,
    sell_paper_position,
)

logger = get_logger("dashboard")

# Template directory
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"

_basic_auth = HTTPBasic(auto_error=False)


def require_auth(credentials: Optional[HTTPBasicCredentials] = Depends(_basic_auth)) -> None:
    """
    HTTP Basic Auth untuk seluruh dashboard & API. Aktif jika DASHBOARD_PASSWORD diisi.
    """
    password = settings.DASHBOARD_PASSWORD
    if not password:
        return
    valid = credentials is not None and secrets.compare_digest(
        credentials.username.encode(), settings.DASHBOARD_USERNAME.encode()
    ) and secrets.compare_digest(credentials.password.encode(), password.encode())
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Autentikasi diperlukan.",
            headers={"WWW-Authenticate": "Basic"},
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Inisialisasi tabel database otomatis, akun paper default, dan data awal snapshot pasar.
    """
    validate_production_settings(settings)
    if not settings.DASHBOARD_PASSWORD:
        logger.warning(
            "DASHBOARD_PASSWORD belum diatur: dashboard & API dapat diakses TANPA autentikasi. "
            "Wajib diisi sebelum dashboard dibuka ke publik."
        )

    try:
        from app.core.database import init_db
        init_db()
        ensure_default_account()
    except Exception as err:
        logger.error("Database init gagal saat startup: %s", err, exc_info=True)

    try:
        from app.market_collector.collector import ensure_initial_market_snapshots
        ensure_initial_market_snapshots()
    except Exception as err:
        logger.warning("Market snapshots bootstrap warning: %s", err)
    yield


app = FastAPI(title="Polymarket Paper Trading Dashboard", lifespan=lifespan)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@app.get("/healthz", include_in_schema=False)
def healthz():
    """
    Healthcheck tanpa autentikasi: koneksi database dan umur snapshot pasar terbaru.
    Mengembalikan 503 jika database tidak dapat diakses.
    """
    from app.core.database import get_db_session
    from app.paper_trading.models import MarketLatest

    db = get_db_session()
    try:
        db.execute(text("SELECT 1"))
        latest = db.query(func.max(MarketLatest.timestamp)).scalar()
    except Exception as err:
        return JSONResponse(status_code=503, content={"status": "error", "database": str(err)})
    finally:
        db.close()

    age_seconds = None
    if latest is not None:
        latest = latest if latest.tzinfo else latest.replace(tzinfo=timezone.utc)
        age_seconds = int((datetime.now(timezone.utc) - latest).total_seconds())
    stale_after = settings.COLLECTOR_INTERVAL_SECONDS * 3
    return {
        "status": "ok",
        "database": "ok",
        "latest_snapshot_age_seconds": age_seconds,
        "collector_stale": age_seconds is None or age_seconds > stale_after,
    }


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
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
            "default_top_cities": settings.TELEGRAM_RECOMMENDATION_TOP_CITIES,
            "chart_labels": chart_labels,
            "chart_balances": chart_balances,
            "chart_equities": chart_equities,
            "current_strategy": strategy,
        },
    )


@app.get("/api/summary", dependencies=[Depends(require_auth)])
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


@app.get("/api/positions", dependencies=[Depends(require_auth)])
def get_positions_api():
    """
    JSON API endpoint untuk daftar open positions terkini.
    """
    return get_open_positions()


@app.get("/api/trades", dependencies=[Depends(require_auth)])
def get_trades_api(limit: int = 50, strategy: Optional[str] = None):
    """
    JSON API endpoint untuk trade history.
    """
    return get_trade_history(limit=limit, strategy_version=strategy)


@app.get("/api/markets/suggestions", dependencies=[Depends(require_auth)])
def get_market_suggestions_api(
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
):
    """
    Rekomendasi market suhu berbasis jam puncak lokal tiap kota, dikelompokkan per event
    (kota + highest/lowest + tanggal) dengan semua bracket-nya:
    - Jam puncak = solar noon / matahari terbit + lag hasil riset data historis kota tersebut.
    - Muncul pada jendela menjelang puncak (default 2–1 jam sebelum awal puncak), hanya untuk
      market bertanggal hari itu di kota tersebut.
    - Tanpa filter harga; min_price / max_price opsional.
    """
    return get_market_suggestions(min_price=min_price, max_price=max_price)


@app.get("/api/markets/suggestions/schedule", dependencies=[Depends(require_auth)])
def get_recommendation_schedule_api(limit: int = 10):
    """Jendela rekomendasi berikutnya per kota & jenis (highest/lowest)."""
    return get_recommendation_schedule(limit=max(1, min(limit, 100)))


@app.get("/api/weather/current", dependencies=[Depends(require_auth)])
def get_current_weather_api(limit: int = 7, city: Optional[str] = None):
    """
    Cuaca terkini di stasiun resolusi market suhu (NOAA METAR / HKO): kota top volume (`limit`, maks 20)
    atau satu kota (`city`), dengan kondisi, tren °/jam, max/min hari ini, perkiraan & kesimpulan.
    """
    from app.paper_service import get_current_weather
    return get_current_weather(limit=max(1, min(limit, 20)), city=city or None)


@app.get("/api/autotrade", dependencies=[Depends(require_auth)])
def autotrade_status_api():
    """Status auto paper trader: aktif/berhenti, pemakaian hari ini, aturan, hasil per strategi, keputusan terbaru."""
    from app.paper_trading.autotrader import status_summary
    return status_summary()


@app.get("/api/autotrade/research", dependencies=[Depends(require_auth)])
def autotrade_research_api(days: Optional[int] = None):
    """Riset auto trader: kalibrasi, ROI per rentang edge/menit/kota, fill rate maker, saran ambang."""
    from app.paper_trading.autotrade_research import research_report
    return research_report(days=days if days and days > 0 else None)


@app.get("/api/autotrade/signals.csv", dependencies=[Depends(require_auth)])
def autotrade_signals_csv_api(days: Optional[int] = None):
    """Semua sampel sinyal (ditrade & dilewati) + konteks + hasil, untuk dianalisis di spreadsheet."""
    from fastapi.responses import Response
    from app.paper_trading.autotrade_research import signals_csv
    return Response(content=signals_csv(days=days if days and days > 0 else None), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=autotrade_signals.csv"})


class AutotradeConfigRequest(BaseModel):
    values: dict = Field(default_factory=dict, description="Nama aturan → nilai baru (null = kembali ke default .env)")


@app.put("/api/autotrade/config", dependencies=[Depends(require_auth)])
def autotrade_config_api(req: AutotradeConfigRequest):
    """Ubah aturan auto trader (batas dana, filter, strategi aktif); berlaku langsung tanpa restart."""
    from app.paper_trading.autotrader import set_config, status_summary
    try:
        set_config(req.values)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    return status_summary()


@app.delete("/api/autotrade/config", dependencies=[Depends(require_auth)])
def autotrade_config_reset_api():
    """Kembalikan semua aturan auto trader ke default dari .env."""
    from app.paper_trading.autotrader import reset_config, status_summary
    reset_config()
    return status_summary()


@app.post("/api/autotrade/{action}", dependencies=[Depends(require_auth)])
def autotrade_toggle_api(action: str):
    from app.paper_trading.autotrader import set_enabled, status_summary
    if action not in ("start", "stop"):
        raise HTTPException(status_code=404, detail="Aksi tidak dikenal")
    set_enabled(action == "start")
    return status_summary()


@app.get("/api/my-wallet", dependencies=[Depends(require_auth)])
def my_wallet_api(refresh: bool = False):
    """Portfolio Polymarket sendiri (read-only). configured=False jika alamat wallet belum diatur."""
    from app.paper_trading import my_wallet
    try:
        summary = my_wallet.get_summary(refresh=refresh)
    except Exception as err:
        logger.error("Portfolio API gagal: %s", err, exc_info=True)
        raise HTTPException(status_code=502, detail="Gagal mengambil data dari Polymarket")
    return {"configured": summary is not None, "summary": summary}


# --- Wallet tracker -------------------------------------------------------------------------

class TrackWalletRequest(BaseModel):
    address: str = Field(..., min_length=42, max_length=200, description="Alamat 0x… atau URL profil Polymarket")
    follow: bool = False


def _wallet_call(fn, *args, **kwargs):
    from app.paper_trading.wallets import WalletError
    try:
        return fn(*args, **kwargs)
    except WalletError as err:
        raise HTTPException(status_code=400, detail=str(err))
    except HTTPException:
        raise
    except Exception as err:
        logger.error("Wallet API gagal: %s", err, exc_info=True)
        raise HTTPException(status_code=502, detail="Gagal mengambil data dari Polymarket")


@app.get("/api/wallets", dependencies=[Depends(require_auth)])
def list_wallets_api():
    """Wallet yang dilacak + kandidat rekomendasi (dari cache database)."""
    from app.paper_trading import wallets
    return {"tracked": wallets.list_tracked(), "candidates": wallets.list_candidates()[:10],
            "candidates_age_minutes": (lambda a: round(a.total_seconds() / 60) if a else None)(wallets.candidates_age())}


@app.post("/api/wallets/discover", dependencies=[Depends(require_auth)])
def discover_wallets_api():
    """Hitung ulang rekomendasi wallet menarik (leaderboard + statistik)."""
    from app.paper_trading import wallets
    return {"candidates": _wallet_call(wallets.discover_wallets)[:10]}


@app.post("/api/wallets", dependencies=[Depends(require_auth)])
def track_wallet_api(req: TrackWalletRequest):
    from app.paper_trading import wallets
    return _wallet_call(wallets.track_wallet, req.address, follow=req.follow)


@app.get("/api/wallets/{address}", dependencies=[Depends(require_auth)])
def wallet_detail_api(address: str):
    """Statistik (win rate, PnL) + riwayat aktivitas terbaru sebuah wallet."""
    from app.paper_trading import wallets
    addr = _wallet_call(wallets.normalize_address, address)
    return {"stats": _wallet_call(wallets.get_stats, addr),
            "activity": _wallet_call(wallets.recent_activity, addr, limit=15),
            "tracked": next((w for w in wallets.list_tracked(include_skipped=True) if w["address"] == addr), None),
            "profile_url": wallets.profile_url(addr)}


@app.post("/api/wallets/{address}/follow", dependencies=[Depends(require_auth)])
def follow_wallet_api(address: str):
    from app.paper_trading import wallets
    return _wallet_call(wallets.set_follow, address, True)


@app.post("/api/wallets/{address}/unfollow", dependencies=[Depends(require_auth)])
def unfollow_wallet_api(address: str):
    from app.paper_trading import wallets
    return _wallet_call(wallets.set_follow, address, False)


@app.post("/api/wallets/{address}/skip", dependencies=[Depends(require_auth)])
def skip_wallet_api(address: str):
    from app.paper_trading import wallets
    _wallet_call(wallets.skip_wallet, address)
    return {"skipped": True}


@app.delete("/api/wallets/{address}", dependencies=[Depends(require_auth)])
def untrack_wallet_api(address: str):
    from app.paper_trading import wallets
    return {"removed": _wallet_call(wallets.untrack_wallet, address)}


@app.get("/api/recommendations/stats", dependencies=[Depends(require_auth)])
def get_recommendation_stats_api(days: Optional[int] = None):
    """Win rate & ROI saran beli bot (notifikasi rekomendasi Telegram), opsional N hari terakhir."""
    from app.paper_trading.recommendation_results import get_recommendation_stats
    return get_recommendation_stats(days=days if days and days > 0 else None)


@app.get("/api/markets/search", dependencies=[Depends(require_auth)])
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


@app.get("/api/markets/categories", dependencies=[Depends(require_auth)])
def get_market_categories_api():
    """Daftar kategori market yang aktif (ENABLED_MARKET_CATEGORIES)."""
    from app.market_collector.categories import enabled_categories

    return [{"key": c.key, "label": c.label} for c in enabled_categories()]


@app.get("/api/markets/events", dependencies=[Depends(require_auth)])
def get_category_events_api(category: str = "weather", date_filter: Optional[str] = None):
    """
    Event berkelompok satu kategori dari Polymarket Gamma API (mis. suhu per kota + tanggal,
    atau jumlah tweet Elon Musk per periode) beserta sub-market bracket-nya.

    Args:
        category: Kunci kategori ('weather', 'elon_tweets', ...).
        date_filter: Opsional, format "YYYY-MM-DD" untuk filter event berdasarkan tanggal berakhir.
    """
    from app.market_collector.categories import enabled_categories
    from app.market_collector.collector import fetch_category_events

    if category not in {c.key for c in enabled_categories()}:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Kategori '{category}' tidak aktif.")
    try:
        return fetch_category_events(category, date_filter=date_filter)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Gagal mengambil event kategori '{category}': {str(e)}"
        )


@app.get("/api/markets/weather-events", dependencies=[Depends(require_auth)])
def get_weather_events_api(date_filter: Optional[str] = None):
    """Alias kompatibilitas untuk /api/markets/events?category=weather."""
    return get_category_events_api(category="weather", date_filter=date_filter)


class SellPositionRequest(BaseModel):
    market_id: str = Field(..., description="ID unik pasar yang akan dijual")
    side: str = Field(..., description="Sisi transaksi (YES atau NO)")
    shares: Optional[float] = Field(None, gt=0, description="Jumlah shares yang dijual (opsional, jika tidak diset maka jual semua)")


@app.post("/api/positions/sell", dependencies=[Depends(require_auth)])
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
        err_msg = str(e)
        if "tidak ditemukan" in err_msg.lower():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=err_msg)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=err_msg)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Terjadi kesalahan saat menjual posisi: {str(e)}"
        )


class DepositFundsRequest(BaseModel):
    amount: float = Field(..., gt=0, description="Jumlah deposit USD")


@app.post("/api/account/deposit", dependencies=[Depends(require_auth)])
def deposit_funds_api(payload: DepositFundsRequest):
    """
    Endpoint untuk menambah saldo paper account.
    """
    try:
        amt = Decimal(str(payload.amount))
        return deposit_paper_funds(amt)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@app.post("/api/account/reset", dependencies=[Depends(require_auth)])
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


@app.post(
    "/api/orders",
    response_model=CreateOrderResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_auth)],
)
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
