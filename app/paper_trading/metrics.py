from decimal import Decimal, ROUND_HALF_UP
from typing import List, Dict, Optional, Any

def calculate_max_drawdown(equity_curve: List[Decimal]) -> Dict[str, Decimal]:
    """
    Menghitung maksimum drawdown dari kurva ekuitas.
    Mempertimbangkan SEMUA 'local peak', menghitung penurunan ke trough berikutnya,
    bukan hanya dari titik awal ke akhir.
    """
    if not equity_curve:
        return {"amount": Decimal('0.0000'), "percentage": Decimal('0.0000')}
        
    peak = equity_curve[0]
    max_dd_amount = Decimal('0')
    max_dd_pct = Decimal('0')
    
    for equity in equity_curve:
        # Jika nilai ekuitas melewati peak sebelumnya, set peak baru (Local Peak)
        if equity > peak:
            peak = equity
            
        # Hitung drawdown dari peak terkini yang sedang aktif
        dd_amount = peak - equity
        if dd_amount > max_dd_amount:
            max_dd_amount = dd_amount
            
        # Hitung dalam persentase
        if peak > Decimal('0'):
            dd_pct = dd_amount / peak
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct
                
    return {
        "amount": max_dd_amount.quantize(Decimal('0.0000'), rounding=ROUND_HALF_UP),
        "percentage": max_dd_pct.quantize(Decimal('0.0000'), rounding=ROUND_HALF_UP)
    }

def calculate_performance_metrics(
    trades: List[Dict[str, Any]], 
    equity_curve: List[Decimal], 
    initial_balance: Decimal,
    current_market_prices: Optional[Dict[str, Decimal]] = None,
    strategy_version: Optional[str] = None
) -> Dict[str, Any]:
    """
    Menghitung Win Rate, ROI, Max Drawdown, Realized & Unrealized P/L.
    Fungsi pure/testable yang memproses raw list of dictionaries.
    """
    # 1. Filter by Strategy Version
    if strategy_version:
        trades = [t for t in trades if t.get('strategy_version') == strategy_version]

    total_closed = 0
    wins = 0
    losses = 0
    realized_pnl = Decimal('0')
    unrealized_pnl = Decimal('0')
    
    current_market_prices = current_market_prices or {}

    # 2. Iterate Trades untuk metrik dan P/L
    for trade in trades:
        status = trade.get('status')
        if status in ['WON', 'LOST']:
            total_closed += 1
            if status == 'WON':
                wins += 1
            elif status == 'LOST':
                losses += 1
            realized_pnl += Decimal(str(trade.get('net_pnl', '0')))
            
        elif status == 'OPEN':
            # Kalkulasi Unrealized PNL berdasarkan MTM (Mark-to-Market) harga terkini
            market_id = trade.get('market_id')
            current_price = current_market_prices.get(market_id)
            if current_price is not None:
                shares = Decimal(str(trade.get('shares', '0')))
                position_size = Decimal(str(trade.get('position_size', '0')))
                current_value = shares * current_price
                unrealized_pnl += (current_value - position_size)

    # 3. Win Rate
    win_rate = Decimal('0')
    if total_closed > 0:
        win_rate = Decimal(wins) / Decimal(total_closed)

    # 4. ROI (Return on Investment)
    current_balance = initial_balance + realized_pnl
    roi = Decimal('0')
    if initial_balance > Decimal('0'):
        roi = (current_balance - initial_balance) / initial_balance

    # 5. Drawdown
    drawdown_metrics = calculate_max_drawdown(equity_curve)

    return {
        "total_closed_trades": total_closed,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate.quantize(Decimal('0.0000'), rounding=ROUND_HALF_UP),
        "realized_pnl": realized_pnl.quantize(Decimal('0.0000'), rounding=ROUND_HALF_UP),
        "unrealized_pnl": unrealized_pnl.quantize(Decimal('0.0000'), rounding=ROUND_HALF_UP),
        "roi": roi.quantize(Decimal('0.0000'), rounding=ROUND_HALF_UP),
        "max_drawdown_amount": drawdown_metrics["amount"],
        "max_drawdown_percentage": drawdown_metrics["percentage"]
    }
