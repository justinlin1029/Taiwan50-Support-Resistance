# Taiwan 50 Support & Resistance Strategy

Python research framework for systematic trading in the constituent stocks of Taiwan's 0050 ETF. The ETF itself is used as a benchmark. Includes daily signal generation, event-driven backtesting, risk-based position sizing and a separate position-reconciliation workflow. It does not connect to a broker or place orders.

## Finding support and resistance

The indicator adapts Zeiierman's **Buyers & Sellers Profile + Dynamic S/R** from Pine Script to Python/NumPy; see [ATTRIBUTION.md](ATTRIBUTION.md) for the source, modifications and CC BY-NC-SA 4.0 attribution.

- Confirm swing highs and lows using five bars on each side of a candidate pivot.
- Build an estimated volume-by-price profile over 600 daily bars and 72 price bins. This is an OHLCV-based estimate, not trade-level order flow.
- Refine nearby pivot levels using volume concentration and candlestick evidence, score candidates, merge nearby levels and invalidate broken levels with an ATR buffer.
- Use only levels available at the previous close for each trading session; newly confirmed levels cannot be used retrospectively. Require at least 620 observations for warm-up.

## Trading and risk management

The default support-touch variant identifies a candidate when the daily low is within 0.5 ATR of support and a valid resistance level exists. Entry is considered on the next session, and is cancelled if the opening or executable quote is at or below support.

Initial stop: support minus 1 ATR. A close-confirmed stop is executed on the following session. A close above resistance plus 0.5 ATR switches to trend management, using a non-decreasing stop based on the highest close minus 2 ATR. Otherwise, resistance rejection triggers an exit. Stops take priority.

Sizing includes estimated transaction fees, slippage, sell-side tax, cash and liquidity constraints. The supplied research configuration uses 5% initial risk per trade, a 30% position cap and 15% total initial risk, with no sector cap or pyramiding. These are experimental parameters, not recommended allocations. Alternative configurations are included for comparison.

## Quick start

Python 3.10+ on macOS/Linux (or WSL). Inventory locking uses `fcntl`.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-tested.txt
cp config.example.json config.json
cp examples/account.example.json account.json
python main.py --help
python -m unittest discover -s tests -v
```

The account example is synthetic and starts without positions. Local account files, generated reports, price caches and private snapshots are excluded from version control.

Download data before running a backtest. For example:

```bash
python main.py download --end 2026-10-16
python main.py backtest --start 2026-07-17 --end 2026-10-16
```

Dates above illustrate a historical research run; change them deliberately. `data/universe.json` is a dated constituent snapshot with limited reconstructed membership history, not a complete point-in-time dataset. For a current scan, use `download --end YYYY-MM-DD --refresh-universe`. A current-only universe requires explicit acceptance of survivorship bias via `--allow-current-universe` before backtesting.

Price caches are not distributed. Data-provider availability, revisions and rate limits can affect reproduction. FinMind credentials, if needed, are read from `FINMIND_TOKEN`; never commit credentials.

For position planning, replace the synthetic account through the documented CLI snapshot workflow, then run `python main.py plan --as-of YYYY-MM-DD`. Examples carry `example_only` markers to prevent accidental import: populate real local inputs before removing those markers. Generated plans are conditional research outputs, not executed trades.

## Modules

| Module | Purpose |
| --- | --- |
| `indicators.py` | Volume profile, confirmed pivots, level management, ATR and RSI |
| `strategy.py` | Shared signals, exits, sizing and risk constraints |
| `backtest.py` | Daily portfolio simulation, costs, corporate actions and reports |
| `market_data.py` | Universe, OHLCV, corporate-action preparation and validation |
| `metrics.py` | Annualized Sharpe calculation from daily portfolio returns |
| `planner.py` | Daily plans and next-session quote checks |
| `inventory.py` | Reconciliation, idempotent fills, locking and backups |
| `diagnostics.py`, `compare.py` | Signal diagnostics and configuration comparisons |
| `tests/test_system.py` | Indicator timing, execution, risk and accounting regressions |

## Research limitations

The original exploratory evaluation covered only three months and four closed trades, with rules adjusted on the same period. It is not independent out-of-sample evidence. No performance claim is made for this public source snapshot.

Daily open-plus-slippage prices approximate execution; they do not model odd-lot order-book fills. Constituent history and corporate-action handling are incomplete. Python output has not been exhaustively reconciled against TradingView on identical data. Tests check implementation properties, not profitability. Raw market data and personal holdings, balances and transaction logs are intentionally absent.

## Attribution and reuse

The indicator adaptation carries **CC BY-NC-SA 4.0** terms. Preserve the original author's credit and applicable noncommercial/share-alike terms when reusing the adaptation. See [ATTRIBUTION.md](ATTRIBUTION.md) for details and source links. No blanket permissive license is asserted for the repository or its data sources.
