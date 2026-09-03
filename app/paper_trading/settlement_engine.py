from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Tuple

# Configuration untuk presisi kalkulasi (Standardizing to 4 decimal places)
SHARE_PRECISION = Decimal('0.0001')
PRICE_PRECISION = Decimal('0.0001')
MONEY_PRECISION = Decimal('0.0001') 

def apply_slippage_and_spread(
    historical_mid_price: Decimal, 
    spread_bps: int = 0, 
    slippage_bps: int = 0,
    is_buy: bool = True
) -> Decimal:
    """
    Mengaplikasikan spread dan slippage pada harga historis pasar.
    1 bps = 0.0001 (0.01%).
    """
    total_impact_bps = Decimal(spread_bps + slippage_bps)
    impact_factor = total_impact_bps / Decimal('10000')
    
    if is_buy:
        # Buy order: harga menjadi lebih mahal (slippage positif ke atas)
        execution_price = historical_mid_price * (Decimal('1') + impact_factor)
    else:
        # Sell order: harga menjadi lebih murah (slippage negatif ke bawah)
        execution_price = historical_mid_price * (Decimal('1') - impact_factor)
        
    # Cap harga probabilitas Polymarket (0 <= price <= 1)
    execution_price = max(Decimal('0'), min(Decimal('1'), execution_price))
    
    return execution_price.quantize(PRICE_PRECISION, rounding=ROUND_HALF_UP)


def calculate_shares(position_size: Decimal, entry_price: Decimal) -> Decimal:
    """
    Kalkulasi jumlah saham: Shares = Position Size / Entry Price.
    
    PENTING: Parameter `entry_price` yang dimasukkan HARUS sudah memperhitungkan
    spread dan slippage pasar (yaitu hasil output dari `apply_slippage_and_spread`).
    Gunakan `calculate_shares_with_slippage` jika ingin menghitung slippage dan shares
    secara otomatis dalam satu langkah.
    """
    if position_size <= Decimal('0'):
        raise ValueError("Position size must be strictly positive")
    if entry_price <= Decimal('0') or entry_price >= Decimal('1'):
        raise ValueError("Entry price must be strictly between 0 and 1")
    
    shares = position_size / entry_price
    return shares.quantize(SHARE_PRECISION, rounding=ROUND_HALF_UP)


def calculate_shares_with_slippage(
    position_size: Decimal,
    historical_mid_price: Decimal,
    spread_bps: int = 0,
    slippage_bps: int = 0,
    is_buy: bool = True
) -> Tuple[Decimal, Decimal]:
    """
    Convenience wrapper: mengaplikasikan spread dan slippage pada harga pasar historis,
    lalu menghitung jumlah shares yang didapatkan secara otomatis.
    
    Returns:
        Tuple[Decimal, Decimal]: (shares, execution_price)
    """
    execution_price = apply_slippage_and_spread(
        historical_mid_price=historical_mid_price,
        spread_bps=spread_bps,
        slippage_bps=slippage_bps,
        is_buy=is_buy
    )
    shares = calculate_shares(position_size=position_size, entry_price=execution_price)
    return shares, execution_price


def evaluate_risk_and_rules(
    position_size: Decimal,
    available_balance: Decimal,
    max_position_size: Decimal,
    historical_price_available: bool
) -> Tuple[bool, str]:
    """
    Evaluasi aturan paper trading (Anti-cheating & Risk Management).
    Return format: (is_approved, rejection_reason)
    """
    if position_size <= Decimal('0'):
        return False, "REJECTED: Position size must be strictly positive"
        
    # Aturan Anti-Cheating: No Lookahead Bias
    if not historical_price_available:
        return False, "REJECTED: Historical market price not available at this timestamp (No Lookahead Bias)"
        
    # Risk Control: Max Position Size
    if position_size > max_position_size:
        return False, f"REJECTED: Position size {position_size} exceeds max allowed {max_position_size}"
        
    # Risk Control: Available Balance
    if position_size > available_balance:
        return False, f"REJECTED: Insufficient balance {available_balance} for position {position_size}"
        
    return True, "APPROVED"


def calculate_settlement(
    shares: Decimal, 
    position_size: Decimal, 
    is_winner: bool, 
    is_resolved: bool,
    fee_rate_bps: int = 0
) -> Dict[str, Decimal]:
    """
    Kalkulasi penyelesaian PnL saat market resolve.
    Polymarket standard: Share pemenang dibayar $1. Share kalah $0.
    Fee dihitung dari gross profit pemenang (fee rate bps).
    """
    if not is_resolved:
        raise ValueError("Cannot settle an unresolved market")

    if shares <= Decimal('0') or position_size <= Decimal('0'):
        raise ValueError("Shares and position size must be strictly positive")

    if fee_rate_bps < 0:
        raise ValueError("fee_rate_bps cannot be negative")

    fee_rate = Decimal(fee_rate_bps) / Decimal('10000')
    
    if is_winner:
        gross_payout = shares * Decimal('1.0')
        gross_profit = gross_payout - position_size
        fees = max(Decimal('0'), gross_profit * fee_rate)
        net_profit = gross_profit - fees
        payout = gross_payout
    else:
        payout = Decimal('0.0')
        gross_profit = -position_size
        fees = Decimal('0.0')  # Lose total = 0 payout = 0 fee payout
        net_profit = gross_profit
        
    return {
        "payout": payout.quantize(MONEY_PRECISION, rounding=ROUND_HALF_UP),
        "gross_profit": gross_profit.quantize(MONEY_PRECISION, rounding=ROUND_HALF_UP),
        "fees": fees.quantize(MONEY_PRECISION, rounding=ROUND_HALF_UP),
        "net_profit": net_profit.quantize(MONEY_PRECISION, rounding=ROUND_HALF_UP)
    }
