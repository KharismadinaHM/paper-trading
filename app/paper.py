"""
CLI untuk Polymarket Weather Paper Trading.
Dijalankan via: python -m app.paper <command>
"""
from decimal import Decimal
from typing import Optional

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# Import service functions dari paper_service.py dengan fallback fleksibel
try:
    from app.paper_service import (
        get_account_status,
        get_open_positions,
        get_performance,
        get_trade_history,
        reset_paper_account,
        start_paper_trading,
    )
except ImportError:
    try:
        from app.paper_trading.paper_service import (
            get_account_status,
            get_open_positions,
            get_performance,
            get_trade_history,
            reset_paper_account,
            start_paper_trading,
        )
    except ImportError:
        from .paper_service import (
            get_account_status,
            get_open_positions,
            get_performance,
            get_trade_history,
            reset_paper_account,
            start_paper_trading,
        )

app = typer.Typer(
    help="Polymarket Weather Paper Trading CLI",
    add_completion=False,
    no_args_is_help=True
)
console = Console()


def format_currency(value: Decimal, show_plus: bool = True) -> str:
    """Format angka ke dalam format mata uang USD ($X.XX atau -$X.XX)."""
    if value > 0:
        return f"+${value:,.2f}" if show_plus else f"${value:,.2f}"
    elif value < 0:
        return f"-${abs(value):,.2f}"
    return f"${value:,.2f}"


@app.command()
def start(
    strategy: Optional[str] = typer.Option(
        None, "--strategy", "-s", help="Strategy version yang ingin dijalankan (contoh: weather_v1)"
    ),
):
    """
    Memulai Paper Trading Engine.
    """
    console.print("[bold cyan]Memulai Paper Trading Engine...[/bold cyan]")
    result = start_paper_trading(strategy=strategy)
    msg = result.get("message", "Paper trading engine started.")
    console.print(f"[bold green]✓ {msg}[/bold green]\n")


@app.command()
def status():
    """
    Tampilkan ringkasan status akun Paper Trading (Saldo, P/L, Win Rate).
    """
    data = get_account_status()
    balance = Decimal(str(data.get("balance", "0")))
    invested = Decimal(str(data.get("invested", "0")))
    realized_pnl = Decimal(str(data.get("realized_pnl", "0")))
    raw_win_rate = data.get("win_rate", "0")
    open_trades = data.get("open_trades", 0)

    # Format Realized P/L
    pnl_str = format_currency(realized_pnl, show_plus=True)
    pnl_color = "green" if realized_pnl > 0 else "red" if realized_pnl < 0 else "white"

    # Format Win Rate
    if isinstance(raw_win_rate, (int, float, Decimal)) and Decimal(str(raw_win_rate)) <= 1:
        win_rate_str = f"{Decimal(str(raw_win_rate)) * 100:.0f}%"
    else:
        win_rate_str = f"{raw_win_rate}%" if "%" not in str(raw_win_rate) else str(raw_win_rate)

    # Grid output persis seperti format spesifikasi dokumen
    table = Table.grid(padding=(0, 4))
    table.add_column(style="bold white", justify="left")
    table.add_column(justify="right")

    table.add_row("Balance", f"${balance:,.2f}")
    table.add_row("Invested", f"${invested:,.2f}")
    table.add_row("Realized P/L", f"[{pnl_color}]{pnl_str}[/{pnl_color}]")
    table.add_row("Win Rate", win_rate_str)
    table.add_row("Open Trades", str(open_trades))

    console.print("\n[bold cyan]PAPER ACCOUNT[/bold cyan]\n")
    console.print(table)
    console.print()


@app.command()
def positions():
    """
    Tampilkan seluruh posisi yang sedang aktif (Open Positions).
    """
    positions_list = get_open_positions()
    if not positions_list:
        console.print("[yellow]Tidak ada open positions saat ini.[/yellow]")
        return

    table = Table(
        title="[bold cyan]Open Positions[/bold cyan]",
        box=box.ROUNDED,
        header_style="bold magenta",
        show_header=True
    )
    table.add_column("Market", style="cyan", no_wrap=False)
    table.add_column("Side", justify="center")
    table.add_column("Entry", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("Shares", justify="right")
    table.add_column("Current", justify="right")
    table.add_column("Unrealized P/L", justify="right")
    table.add_column("Strategy", style="dim", justify="center")

    total_unrealized = Decimal("0")
    total_size = Decimal("0")

    for pos in positions_list:
        side = str(pos.get("side", "BUY")).upper()
        side_color = "green" if side == "BUY" else "red"

        entry = Decimal(str(pos.get("entry_price", "0")))
        size = Decimal(str(pos.get("size", "0")))
        shares = Decimal(str(pos.get("shares", "0")))
        curr = Decimal(str(pos.get("current_price", "0")))
        unrealized = Decimal(str(pos.get("unrealized_pnl", "0")))
        strategy = str(pos.get("strategy_version", "-"))

        total_unrealized += unrealized
        total_size += size

        u_str = format_currency(unrealized, show_plus=True)
        u_color = "green" if unrealized > 0 else "red" if unrealized < 0 else "white"

        table.add_row(
            str(pos.get("market", "-")),
            f"[{side_color}]{side}[/{side_color}]",
            f"${entry:.2f}",
            f"${size:,.2f}",
            f"{shares:.4f}",
            f"${curr:.2f}",
            f"[{u_color}]{u_str}[/{u_color}]",
            strategy,
        )

    console.print()
    console.print(table)
    u_tot_str = format_currency(total_unrealized, show_plus=True)
    u_tot_color = "green" if total_unrealized > 0 else "red" if total_unrealized < 0 else "white"
    console.print(
        f"Total Size: [bold]${total_size:,.2f}[/bold] | "
        f"Total Unrealized P/L: [{u_tot_color}][bold]{u_tot_str}[/bold][/{u_tot_color}]\n"
    )


@app.command()
def trades(
    limit: int = typer.Option(20, "--limit", "-n", help="Jumlah trade terakhir yang ditampilkan"),
    strategy: Optional[str] = typer.Option(None, "--strategy", "-s", help="Filter berdasarkan strategy version"),
):
    """
    Tampilkan riwayat closed trades (Trade History).
    """
    trades_list = get_trade_history(limit=limit, strategy_version=strategy)
    if not trades_list:
        console.print("[yellow]Tidak ada data trade history.[/yellow]")
        return

    title_text = "[bold cyan]Trade History[/bold cyan]"
    if strategy:
        title_text += f" [dim]({strategy})[/dim]"

    table = Table(
        title=title_text,
        box=box.ROUNDED,
        header_style="bold magenta",
        show_header=True
    )
    table.add_column("Date", style="dim", no_wrap=True)
    table.add_column("Market", style="cyan", no_wrap=False)
    table.add_column("Side", justify="center")
    table.add_column("Entry", justify="right")
    table.add_column("Exit", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("Status", justify="center")
    table.add_column("Net P/L", justify="right")
    table.add_column("Strategy", style="dim", justify="center")

    for t in trades_list:
        status = str(t.get("status", "-")).upper()
        if status == "WON":
            status_style = "[bold green]WON[/bold green]"
        elif status == "LOST":
            status_style = "[bold red]LOST[/bold red]"
        elif status == "CANCELLED":
            status_style = "[yellow]CANCELLED[/yellow]"
        else:
            status_style = f"[cyan]{status}[/cyan]"

        side = str(t.get("side", "BUY")).upper()
        side_color = "green" if side == "BUY" else "red"

        entry = Decimal(str(t.get("entry_price", "0")))
        exit_p = t.get("exit_price")
        exit_str = f"${Decimal(str(exit_p)):.2f}" if exit_p is not None else "-"
        size = Decimal(str(t.get("size", "0")))
        pnl = Decimal(str(t.get("net_pnl", "0")))
        pnl_str = format_currency(pnl, show_plus=True)
        pnl_color = "green" if pnl > 0 else "red" if pnl < 0 else "white"

        table.add_row(
            str(t.get("date", "-")),
            str(t.get("market", "-")),
            f"[{side_color}]{side}[/{side_color}]",
            f"${entry:.2f}",
            exit_str,
            f"${size:,.2f}",
            status_style,
            f"[{pnl_color}]{pnl_str}[/{pnl_color}]",
            str(t.get("strategy_version", "-")),
        )

    console.print()
    console.print(table)
    console.print(f"[dim]Menampilkan {len(trades_list)} trade terbaru.[/dim]\n")


@app.command()
def performance(
    strategy: Optional[str] = typer.Option(
        None, "--strategy", "-s", help="Filter performa berdasarkan strategy version (contoh: weather_v1)"
    ),
):
    """
    Tampilkan ringkasan metrik performa trading (Win Rate, ROI, Drawdown).
    """
    perf = get_performance(strategy_version=strategy)

    trades_count = perf.get("trades", 0)
    wins = perf.get("wins", 0)
    losses = perf.get("losses", 0)
    win_rate = Decimal(str(perf.get("win_rate", "0")))
    roi = Decimal(str(perf.get("roi", "0")))
    max_dd = Decimal(str(perf.get("max_drawdown", "0")))
    realized_pnl = Decimal(str(perf.get("realized_pnl", "0")))
    unrealized_pnl = Decimal(str(perf.get("unrealized_pnl", "0")))

    # Format percentages
    win_rate_str = f"{win_rate * 100:.1f}%" if win_rate <= 1 else f"{win_rate:.1f}%"
    roi_prefix = "+" if roi > 0 else ""
    roi_color = "green" if roi > 0 else "red" if roi < 0 else "white"
    roi_str = f"[{roi_color}]{roi_prefix}{roi * 100:.2f}%[/{roi_color}]"

    max_dd_str = f"[red]{max_dd * 100:.2f}%[/red]" if max_dd > 0 else "0.00%"

    r_pnl_str = format_currency(realized_pnl, show_plus=True)
    r_color = "green" if realized_pnl > 0 else "red" if realized_pnl < 0 else "white"
    r_pnl_formatted = f"[{r_color}]{r_pnl_str}[/{r_color}]"

    u_pnl_str = format_currency(unrealized_pnl, show_plus=True)
    u_color = "green" if unrealized_pnl > 0 else "red" if unrealized_pnl < 0 else "white"
    u_pnl_formatted = f"[{u_color}]{u_pnl_str}[/{u_color}]"

    grid = Table.grid(padding=(0, 5))
    grid.add_column(style="bold cyan", justify="left")
    grid.add_column(justify="right")

    grid.add_row("Total Trades", str(trades_count))
    grid.add_row("Wins", f"[green]{wins}[/green]")
    grid.add_row("Losses", f"[red]{losses}[/red]")
    grid.add_row("Win Rate", win_rate_str)
    grid.add_row("ROI", roi_str)
    grid.add_row("Realized P/L", r_pnl_formatted)
    grid.add_row("Unrealized P/L", u_pnl_formatted)
    grid.add_row("Max Drawdown", max_dd_str)

    title_text = "Performance Metrics"
    if strategy:
        title_text += f" ({strategy})"

    panel = Panel(
        grid,
        title=f"[bold green]{title_text}[/bold green]",
        border_style="cyan",
        box=box.ROUNDED,
        expand=False,
    )
    console.print()
    console.print(panel)
    console.print()


@app.command()
def reset(
    force: bool = typer.Option(False, "--force", "-y", help="Lewati dialog konfirmasi reset"),
):
    """
    Reset paper trading account ke kondisi saldo awal (hapus semua posisi & trade history).
    """
    if not force:
        confirmed = typer.confirm(
            "⚠️  Apakah Anda yakin ingin me-reset paper trading account? Tindakan ini akan menghapus semua posisi & trade history.",
            default=False
        )
        if not confirmed:
            console.print("[yellow]Reset dibatalkan.[/yellow]\n")
            raise typer.Abort()

    result = reset_paper_account()
    msg = result.get("message", "Paper account reset successfully.")
    console.print(f"[bold green]✓ {msg}[/bold green]\n")


@app.command("test-telegram")
def test_telegram():
    """
    Kirim pesan uji coba ke bot Telegram untuk memverifikasi konfigurasi .env.
    """
    from app.paper_trading.telegram import send_telegram_message
    console.print("[cyan]Mengirim pesan uji coba ke Telegram...[/cyan]")
    res = send_telegram_message("🔔 [TEST] Paper Trading System berhasil terhubung ke bot Telegram Anda!")
    if res.get("success"):
        console.print("[bold green]✓ Berhasil! Notifikasi uji coba telah terkirim ke Telegram.[/bold green]\n")
    else:
        console.print(f"[bold red]✗ Gagal mengirim pesan: {res.get('error')}[/bold red]")
        console.print("[yellow]Pastikan TELEGRAM_BOT_TOKEN dan TELEGRAM_CHAT_ID sudah diisi dengan benar di .env dan Anda sudah menekan /start di bot.[/yellow]\n")


@app.command("bot")
def run_telegram_bot():
    """
    Jalankan listener interaktif Bot Telegram (merespon perintah /start, /status, /positions, /trades, dll).
    """
    from app.paper_trading.telegram_bot import start_bot_polling
    console.print("[bold green]🤖 Memulai interactive Telegram bot listener...[/bold green]")
    try:
        start_bot_polling()
    except Exception as e:
        console.print(f"[bold red]✗ Gagal menjalankan bot Telegram: {e}[/bold red]")


if __name__ == "__main__":
    app()
