# Paper Trading Feature

## 1. Objective

Paper Trading memungkinkan user menjalankan strategi Polymarket Weather menggunakan **market data real-time tanpa menggunakan uang sungguhan**.

Tujuan:

- Menguji strategy sebelum deposit.
- Menguji entry timing.
- Mengukur win rate.
- Mengukur profitability.
- Mengukur drawdown.
- Menguji position sizing.
- Memvalidasi seluruh pipeline aplikasi.

Paper trading harus menggunakan **data market sebenarnya**, bukan data simulasi.

---

# 2. Virtual Account

Default account:

```text
Starting Balance: $20
Position Size: $1
Currency: USD
Mode: PAPER
```

User dapat melakukan reset account kapan saja.

Contoh:

```text
Balance
$20.00

Available
$20.00

Invested
$0.00

Realized P/L
$0.00

Unrealized P/L
$0.00
```

---

# 3. Paper Trade Lifecycle

```text
Market Discovery
      ↓
Strategy Filter
      ↓
BUY Signal
      ↓
Paper Order
      ↓
Virtual Position
      ↓
Market Monitoring
      ↓
Market Resolution
      ↓
Settlement Simulation
      ↓
P/L Calculation
      ↓
Account Balance
```

---

# 4. Paper Order

Ketika strategy menghasilkan BUY:

```text
Market:
Weather Market

Side:
YES

Price:
$0.74

Position:
$1.00
```

System membuat paper order.

Data yang disimpan:

```text
paper_order_id
market_id
timestamp
side
entry_price
position_size
shares
status
strategy_version
```

---

# 5. Share Calculation

Paper trading harus mengikuti mekanisme market.

Formula:

```text
shares = position_size / entry_price
```

Contoh:

```text
position = $1
entry = $0.75

shares = 1 / 0.75
       = 1.3333
```

---

# 6. Settlement

Ketika market resolved:

### Winning Position

Setiap winning share memiliki payout sesuai settlement rules.

System menghitung:

```text
gross_payout
gross_profit
fees
net_profit
```

### Losing Position

Jika outcome tidak sesuai:

```text
payout = $0
loss = position_size
```

---

# 7. Paper Portfolio

Portfolio page/dashboard menampilkan:

```text
Initial Balance
Current Balance
Total Deposited
Total Invested
Realized P/L
Unrealized P/L
Total Fees
ROI
Win Rate
```

Tambahkan:

```text
Open Positions
Closed Positions
Winning Trades
Losing Trades
```

---

# 8. Trade History

Setiap paper trade dapat dilihat kembali.

Fields:

```text
Timestamp
Market
Side
Entry Price
Position Size
Shares
Settlement
Gross P/L
Fees
Net P/L
Holding Time
Strategy Version
```

Filter:

```text
Date
City
Market
Win/Loss
Price Range
Entry Window
```

---

# 9. Equity Curve

System menyimpan balance setelah setiap settled trade.

Contoh:

```text
$20.00
  ↓
$20.33
  ↓
$19.33
  ↓
$19.66
  ↓
$20.00
```

Kemudian visualisasikan sebagai equity curve.

Metric:

- Starting balance
- Ending balance
- Maximum drawdown
- Peak balance
- Lowest balance

---

# 10. Strategy Comparison

Paper trading harus memungkinkan membandingkan beberapa configuration.

Contoh:

```text
Strategy A
Price: 70–80¢
Entry: 30–90 minutes

Strategy B
Price: 70–80¢
Entry: 0–30 minutes

Strategy C
Price: 65–75¢
Entry: 30–90 minutes
```

Masing-masing strategy memiliki virtual performance sendiri.

---

# 11. Paper Trading Modes

## Manual Mode

User memilih market sendiri.

System hanya melakukan:

```text
Market Data
↓
User clicks BUY
↓
Paper Order
```

Cocok untuk memahami mekanisme Polymarket.

---

## Strategy Mode

Strategy engine mencari market.

```text
Market Data
↓
Weather Data
↓
Strategy
↓
Signal
↓
Paper BUY
```

Ini mode utama untuk eksperimen.

---

## Signal-Only Mode

System tidak membuat paper order.

System hanya mengirim:

```text
BUY SIGNAL

Market: ...
Price: ...
Position: $1
Reason: ...
```

User kemudian mencatat apakah signal tersebut akan menang/kalah.

Mode ini berguna untuk membandingkan keputusan system dengan keputusan manual.

---

# 12. Telegram Integration

Paper trading dapat mengirim notification.

Contoh:

```text
🟢 PAPER BUY

Market:
Temperature in Tokyo

Side:
YES

Entry:
$0.74

Position:
$1.00

Shares:
1.3514

Expected Peak:
14:00

Reason:
Weather conditions match strategy criteria.
```

Ketika market selesai:

```text
✅ PAPER TRADE SETTLED

Entry:
$0.74

Result:
WIN

Gross P/L:
+$0.3514

Fees:
-$X

Net P/L:
+$X

Balance:
$20 → $20.XX
```

---

# 13. Risk Controls

Paper trading tetap harus menggunakan risk management yang sama dengan real trading.

Default:

```text
Initial Balance = $20
Max Position = $1
Max Open Exposure = configurable
```

System menolak paper order jika:

```text
position_size > max_position_size
```

atau:

```text
available_balance < position_size
```

Tujuannya agar paper trading merepresentasikan kondisi real.

---

# 14. No Artificial Advantages

Paper trading tidak boleh memberikan keuntungan yang tidak mungkin didapat saat real trading.

System harus memperhitungkan:

- Entry price
- Market liquidity
- Spread
- Fees
- Market resolution
- Order timing

Jika historical market price tidak tersedia pada timestamp tertentu, system tidak boleh menggunakan harga masa depan.

---

# 15. Slippage Simulation

Optional pada MVP, tetapi architecture harus mendukungnya.

Configuration:

```text
SLIPPAGE_BPS=0
```

Kemudian dapat diuji:

```text
SLIPPAGE_BPS=10
SLIPPAGE_BPS=25
SLIPPAGE_BPS=50
```

Tujuannya mengetahui apakah strategy tetap profitable ketika execution tidak sempurna.

---

# 16. Paper Trading Database

Tambahkan tabel:

```text
paper_accounts
paper_orders
paper_positions
paper_trades
paper_balance_snapshots
```

Relationship:

```text
paper_accounts
      │
      ├── paper_orders
      │
      ├── paper_positions
      │
      └── paper_balance_snapshots
```

---

# 17. Paper Account Schema

Minimum:

```text
id
name
initial_balance
current_balance
realized_pnl
unrealized_pnl
total_fees
created_at
updated_at
```

---

# 18. Paper Trade Schema

Minimum:

```text
id
account_id
market_id
side
entry_price
position_size
shares
exit_price
gross_pnl
fees
net_pnl
status
opened_at
closed_at
strategy_version
```

Status:

```text
OPEN
WON
LOST
CANCELLED
```

---

# 19. Strategy Versioning

Setiap paper trade harus menyimpan strategy version.

Contoh:

```text
weather_v1
weather_v2
weather_v3
```

Dengan demikian ketika strategy berubah, historical results tidak tercampur.

Contoh:

```text
weather_v1
100 trades
Win Rate: 78%
ROI: +X%

weather_v2
100 trades
Win Rate: 83%
ROI: +Y%
```

---

# 20. Dashboard

MVP dashboard dapat dibuat sederhana.

### Account

```text
Balance       $20.00
P/L           +$0.00
ROI           0%
```

### Performance

```text
Trades        0
Wins          0
Losses        0
Win Rate      0%
Max DD        0%
```

### Open Positions

```text
Market | Side | Entry | Size | Current
```

### Trade History

```text
Date | Market | Entry | Result | P/L
```

### Equity Curve

Chart balance terhadap waktu.

---

# 21. Local Architecture

Pada Mac:

```text
┌─────────────────────────────┐
│             Mac             │
│                             │
│  Python Application         │
│      │                      │
│      ├── Market Collector   │
│      ├── Weather Collector  │
│      ├── Strategy Engine    │
│      ├── Paper Trading      │
│      ├── Backtest Engine    │
│      └── Telegram           │
│              │              │
│              ▼              │
│       PostgreSQL Docker     │
│                             │
└─────────────────────────────┘
```

---

# 22. Recommended MVP UI

Tidak perlu langsung membuat frontend kompleks.

Tahap pertama dapat menggunakan:

```text
Python
+
CLI
+
Jupyter
+
Telegram
```

Kemudian jika workflow sudah stabil:

```text
FastAPI
+
Simple Web Dashboard
```

Frontend dapat ditambahkan setelah core trading engine stabil.

---

# 23. CLI

MVP dapat menyediakan commands:

```bash
python -m app.paper start
python -m app.paper status
python -m app.paper positions
python -m app.paper trades
python -m app.paper performance
python -m app.paper reset
```

Contoh:

```bash
python -m app.paper status
```

Output:

```text
PAPER ACCOUNT

Balance       $20.00
Invested       $3.00
Realized P/L  +$0.42
Win Rate       80%
Open Trades       3
```

---

# 24. Development Roadmap Update

## Phase 1

Foundation

- Python
- Docker
- PostgreSQL
- Configuration
- Logging

## Phase 2

Data ingestion

- Polymarket
- Weather API
- Market snapshots
- Forecast snapshots

## Phase 3

Strategy engine

- Weather filters
- Price filters
- Entry timing
- Signal generation

## Phase 4

Paper Trading

- Virtual account
- Virtual orders
- Positions
- Settlement
- P/L
- Fees
- Portfolio
- Trade history

## Phase 5

Backtesting

- Historical data
- Point-in-time simulation
- Strategy comparison
- Performance report

## Phase 6

Paper Trading Validation

Target:

**100–200 trades**

Capital:

**$20 virtual**

Position:

**$1**

## Phase 7

Real Trading

Only after paper trading demonstrates acceptable performance.

Initial real position:

**$1**

## Phase 8

VM Deployment

- Docker
- PostgreSQL
- Scheduler
- Telegram
- Monitoring
- Backup

---

# 25. MVP Definition of Done

Paper Trading MVP dianggap selesai apabila:

- [ ] Virtual account dapat dibuat.
- [ ] Starting balance dapat dikonfigurasi.
- [ ] Market real-time dapat dipilih.
- [ ] Paper BUY dapat dibuat.
- [ ] Position size dihitung.
- [ ] Open position dapat dimonitor.
- [ ] Market resolution dapat dideteksi.
- [ ] Winning/losing trade dapat dihitung.
- [ ] Fees dapat dihitung.
- [ ] Balance diperbarui otomatis.
- [ ] Trade history tersimpan.
- [ ] Win rate dihitung.
- [ ] ROI dihitung.
- [ ] Maximum drawdown dihitung.
- [ ] Equity curve tersedia.
- [ ] Telegram notification tersedia.
- [ ] Strategy version tersimpan.
- [ ] Paper account dapat di-reset.
- [ ] Semua dapat dijalankan secara lokal menggunakan Docker.

---

# 26. Recommended Development Sequence

Jangan langsung membuat dashboard.

Urutan development:

```text
1. PostgreSQL
       ↓
2. Polymarket Collector
       ↓
3. Weather Collector
       ↓
4. Market Snapshot
       ↓
5. Strategy Engine
       ↓
6. Paper Trading Engine
       ↓
7. Settlement Engine
       ↓
8. Performance Metrics
       ↓
9. Telegram
       ↓
10. Backtesting
       ↓
11. Dashboard
       ↓
12. VM Deployment
```

## Final Goal

Sebelum user melakukan deposit ke Polymarket, system harus mampu menjawab:

> **"Kalau saya menjalankan strategy ini dengan $20 dan $1 per trade berdasarkan data real-time, bagaimana performanya?"**

Paper trading menjadi **jembatan antara backtest dan real-money trading**, sehingga tidak perlu langsung mempertaruhkan uang untuk mengetahui apakah pipeline dan strategy bekerja.