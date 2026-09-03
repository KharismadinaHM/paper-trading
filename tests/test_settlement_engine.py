import unittest
from decimal import Decimal
from app.paper_trading.settlement_engine import (
    calculate_shares, 
    calculate_shares_with_slippage,
    calculate_settlement, 
    evaluate_risk_and_rules,
    apply_slippage_and_spread
)

class TestSettlementEngine(unittest.TestCase):
    
    def test_share_calculation_standard(self):
        position = Decimal('1.0')
        entry = Decimal('0.75')
        shares = calculate_shares(position, entry)
        self.assertEqual(shares, Decimal('1.3333'))

    def test_share_calculation_trace_2(self):
        position = Decimal('1.0')
        entry = Decimal('0.74')
        shares = calculate_shares(position, entry)
        self.assertEqual(shares, Decimal('1.3514'))
        
        # Test settlement gross profit matching the requirement trace
        settlement = calculate_settlement(
            shares=shares, 
            position_size=position, 
            is_winner=True, 
            is_resolved=True, 
            fee_rate_bps=0
        )
        self.assertEqual(settlement['gross_profit'], Decimal('0.3514'))

    def test_share_calculation_extreme_odds(self):
        # Entry price very close to 0
        shares = calculate_shares(Decimal('10.0'), Decimal('0.0001'))
        self.assertEqual(shares, Decimal('100000.0000'))
        
        # Entry price very close to 1
        shares = calculate_shares(Decimal('10.0'), Decimal('0.9999'))
        self.assertEqual(shares, Decimal('10.0010'))

    def test_risk_control_position_size_exceeds_balance(self):
        is_approved, reason = evaluate_risk_and_rules(
            position_size=Decimal('50.0'),
            available_balance=Decimal('20.0'),
            max_position_size=Decimal('100.0'),
            historical_price_available=True
        )
        self.assertFalse(is_approved)
        self.assertIn("Insufficient balance", reason)

    def test_risk_control_position_size_exceeds_max(self):
        is_approved, reason = evaluate_risk_and_rules(
            position_size=Decimal('150.0'),
            available_balance=Decimal('200.0'),
            max_position_size=Decimal('100.0'),
            historical_price_available=True
        )
        self.assertFalse(is_approved)
        self.assertIn("exceeds max", reason)

    def test_anti_cheating_historical_price_unavailable(self):
        is_approved, reason = evaluate_risk_and_rules(
            position_size=Decimal('10.0'),
            available_balance=Decimal('100.0'),
            max_position_size=Decimal('100.0'),
            historical_price_available=False
        )
        self.assertFalse(is_approved)
        self.assertIn("Historical market price not available", reason)
        self.assertIn("No Lookahead Bias", reason)

    def test_settlement_unresolved_market_error(self):
        with self.assertRaisesRegex(ValueError, "Cannot settle an unresolved market"):
            calculate_settlement(Decimal('10'), Decimal('5'), is_winner=True, is_resolved=False)

    def test_settlement_losing_trade(self):
        settlement = calculate_settlement(
            shares=Decimal('1.3333'), 
            position_size=Decimal('1.0'), 
            is_winner=False, 
            is_resolved=True
        )
        self.assertEqual(settlement['payout'], Decimal('0.0000'))
        self.assertEqual(settlement['gross_profit'], Decimal('-1.0000'))
        self.assertEqual(settlement['net_profit'], Decimal('-1.0000'))

    def test_calculate_shares_negative_position_size(self):
        with self.assertRaisesRegex(ValueError, "Position size must be strictly positive"):
            calculate_shares(Decimal('-10.0'), Decimal('0.5'))
            
    def test_risk_control_negative_position_size(self):
        is_approved, reason = evaluate_risk_and_rules(
            position_size=Decimal('-5.0'),
            available_balance=Decimal('20.0'),
            max_position_size=Decimal('100.0'),
            historical_price_available=True
        )
        self.assertFalse(is_approved)
        self.assertIn("Position size must be strictly positive", reason)
        
    def test_calculate_settlement_negative_fee(self):
        with self.assertRaisesRegex(ValueError, "fee_rate_bps cannot be negative"):
            calculate_settlement(
                shares=Decimal('10.0'), 
                position_size=Decimal('5.0'), 
                is_winner=True, 
                is_resolved=True,
                fee_rate_bps=-100
            )

    def test_slippage_and_spread(self):
        # Base price 0.5, spread 20 bps, slippage 30 bps -> Total impact 50 bps = 0.5%
        # Buy impact: 0.5 * (1 + 0.0050) = 0.5025
        exec_price = apply_slippage_and_spread(Decimal('0.5'), spread_bps=20, slippage_bps=30, is_buy=True)
        self.assertEqual(exec_price, Decimal('0.5025'))

    def test_calculate_shares_with_slippage(self):
        # mid=0.5, spread=20, slippage=30 -> exec_price=0.5025
        # position=1.0 -> shares = 1.0 / 0.5025 = 1.99004975... -> 1.9900
        shares, exec_price = calculate_shares_with_slippage(
            position_size=Decimal('1.0'),
            historical_mid_price=Decimal('0.5'),
            spread_bps=20,
            slippage_bps=30,
            is_buy=True
        )
        self.assertEqual(exec_price, Decimal('0.5025'))
        self.assertEqual(shares, Decimal('1.9900'))

    def test_settlement_winning_trade_with_fees(self):
        # Trace manual:
        # shares = 1.3514, position_size = 1.0, is_winner = True
        # gross_payout = 1.3514 * 1.0 = 1.3514
        # gross_profit = 1.3514 - 1.0 = 0.3514
        # fee_rate_bps = 200 -> fee_rate = 0.02
        # fees = gross_profit * 0.02 = 0.3514 * 0.02 = 0.007028 -> quantized: 0.0070
        # net_profit = gross_profit - fees = 0.3514 - 0.007028 = 0.344372 -> quantized: 0.3444
        settlement = calculate_settlement(
            shares=Decimal('1.3514'),
            position_size=Decimal('1.0'),
            is_winner=True,
            is_resolved=True,
            fee_rate_bps=200
        )
        self.assertEqual(settlement['gross_profit'], Decimal('0.3514'))
        self.assertEqual(settlement['fees'], Decimal('0.0070'))
        self.assertEqual(settlement['net_profit'], Decimal('0.3444'))
        self.assertEqual(settlement['payout'], Decimal('1.3514'))

    def test_settlement_zero_or_negative_shares_and_position(self):
        # Zero shares
        with self.assertRaisesRegex(ValueError, "Shares and position size must be strictly positive"):
            calculate_settlement(Decimal('0'), Decimal('1.0'), is_winner=True, is_resolved=True)
        # Negative shares
        with self.assertRaisesRegex(ValueError, "Shares and position size must be strictly positive"):
            calculate_settlement(Decimal('-1.0'), Decimal('1.0'), is_winner=True, is_resolved=True)
        # Zero position_size
        with self.assertRaisesRegex(ValueError, "Shares and position size must be strictly positive"):
            calculate_settlement(Decimal('1.0'), Decimal('0'), is_winner=True, is_resolved=True)
        # Negative position_size
        with self.assertRaisesRegex(ValueError, "Shares and position size must be strictly positive"):
            calculate_settlement(Decimal('1.0'), Decimal('-1.0'), is_winner=True, is_resolved=True)

if __name__ == '__main__':
    unittest.main()
