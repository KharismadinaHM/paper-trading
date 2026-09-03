import unittest
from decimal import Decimal
from app.paper_trading.metrics import calculate_performance_metrics, calculate_max_drawdown

class TestMetricsEngine(unittest.TestCase):
    
    def test_max_drawdown_trace_1(self):
        # Trace manual dari dokumen: $20.00 -> $20.33 -> $19.33 -> $20.66 -> $20.00
        # Peak 1 = 20.33, Trough 1 = 19.33 -> Drawdown = 1.00
        # Peak 2 = 20.66, Trough 2 = 20.00 -> Drawdown = 0.66
        # Max Drawdown harus = 1.00 dari Peak 20.33 ke 19.33
        curve = [
            Decimal('20.00'),
            Decimal('20.33'),
            Decimal('19.33'),
            Decimal('20.66'),
            Decimal('20.00')
        ]
        result = calculate_max_drawdown(curve)
        self.assertEqual(result['amount'], Decimal('1.0000'))
        
    def test_zero_trades(self):
        metrics = calculate_performance_metrics(
            trades=[],
            equity_curve=[Decimal('20.00')],
            initial_balance=Decimal('20.00')
        )
        self.assertEqual(metrics['total_closed_trades'], 0)
        self.assertEqual(metrics['win_rate'], Decimal('0.0000'))
        self.assertEqual(metrics['roi'], Decimal('0.0000'))
        
    def test_all_trades_win(self):
        trades = [
            {"status": "WON", "net_pnl": "5.00"},
            {"status": "WON", "net_pnl": "3.00"}
        ]
        metrics = calculate_performance_metrics(
            trades=trades,
            equity_curve=[Decimal('20.00'), Decimal('25.00'), Decimal('28.00')],
            initial_balance=Decimal('20.00')
        )
        self.assertEqual(metrics['win_rate'], Decimal('1.0000'))
        self.assertEqual(metrics['roi'], Decimal('0.4000')) # (28-20)/20 = 8/20 = 0.4
        
    def test_all_trades_lose(self):
        trades = [
            {"status": "LOST", "net_pnl": "-5.00"},
            {"status": "LOST", "net_pnl": "-3.00"}
        ]
        metrics = calculate_performance_metrics(
            trades=trades,
            equity_curve=[Decimal('20.00'), Decimal('15.00'), Decimal('12.00')],
            initial_balance=Decimal('20.00')
        )
        self.assertEqual(metrics['win_rate'], Decimal('0.0000'))
        self.assertEqual(metrics['roi'], Decimal('-0.4000')) # -8/20 = -0.4
        
    def test_strategy_filter_and_unrealized_pnl(self):
        trades = [
            {"status": "WON", "net_pnl": "2.00", "strategy_version": "v1"},
            {"status": "LOST", "net_pnl": "-1.00", "strategy_version": "v2"},
            {"status": "OPEN", "shares": "10", "position_size": "5.00", "market_id": "M1", "strategy_version": "v1"}
        ]
        # Current price = 0.8
        # Posisi OPEN: Shares 10 * 0.8 = 8.00 current value.
        # Unrealized PNL = 8.00 - 5.00(position size) = 3.00.
        market_prices = {"M1": Decimal('0.8')}
        
        # Test Filter v1
        metrics_v1 = calculate_performance_metrics(
            trades=trades,
            equity_curve=[],
            initial_balance=Decimal('20.00'),
            current_market_prices=market_prices,
            strategy_version="v1"
        )
        
        self.assertEqual(metrics_v1['total_closed_trades'], 1)
        self.assertEqual(metrics_v1['win_rate'], Decimal('1.0000'))
        self.assertEqual(metrics_v1['realized_pnl'], Decimal('2.0000'))
        self.assertEqual(metrics_v1['unrealized_pnl'], Decimal('3.0000'))

    def test_drawdown_in_middle_of_sequence(self):
        # Drawdown terjadi di tengah, lalu recovery dan mencetak new high
        curve = [
            Decimal('100.00'),
            Decimal('120.00'), # Peak 1
            Decimal('90.00'),  # Trough 1 -> Drawdown = 30
            Decimal('110.00'),
            Decimal('105.00'),
            Decimal('150.00'), # Peak 2
            Decimal('140.00')  # Trough 2 -> Drawdown = 10
        ]
        metrics = calculate_max_drawdown(curve)
        self.assertEqual(metrics['amount'], Decimal('30.0000'))

if __name__ == '__main__':
    unittest.main()
