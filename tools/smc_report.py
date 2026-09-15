"""Command-line runner for the Smart Money engine.

Live mode uses the same provider the Telegram bot uses (Binance first for
crypto, Kraken and Yahoo as fallbacks) so nothing here can invent prices.
`--csv-prefix` reads closed candles from CSV dumps (columns:
ts,open,high,low,close,volume[,buy_volume,trades]) which is handy for offline
review and for tests.

    python tools/smc_report.py BTC
    python tools/smc_report.py BTC --lang both --csv-prefix data/btc
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from marketobserver.assets import resolve_asset  # noqa: E402
from marketobserver.market_data import Candle  # noqa: E402
from marketobserver.smc import build_report  # noqa: E402
from marketobserver.smc_text import LANGS, render  # noqa: E402

TIMEFRAMES = ("4h", "1h", "15m")


def load_csv(prefix: str, timeframe: str) -> list[Candle]:
    path = Path(f"{prefix}_{timeframe}.csv")
    if not path.exists():
        raise SystemExit(f"missing candle file: {path}")
    candles: list[Candle] = []
    with path.open() as handle:
        header = handle.readline().strip().split(",")
        for line in handle:
            values = line.strip().split(",")
            if len(values) < len(header) or not values[0].isdigit():
                continue
            row = dict(zip(header, values))
            buy = (row.get("buy_volume") or "").strip()
            candles.append(Candle(
                timestamp=datetime.fromtimestamp(int(row["ts"]) / 1000, tz=timezone.utc),
                open=float(row["open"]), high=float(row["high"]), low=float(row["low"]), close=float(row["close"]),
                volume=float(row["volume"]),
                buy_volume=float(buy) if buy else None,
            ))
    return candles


def main() -> int:
    parser = argparse.ArgumentParser(description="Multi-timeframe Smart Money analysis")
    parser.add_argument("asset", nargs="?", default="BTC")
    parser.add_argument("--lang", choices=LANGS, default="both")
    parser.add_argument("--csv-prefix", default=None, help="read candles from <prefix>_<tf>.csv instead of the network")
    parser.add_argument("--no-checklist", action="store_true")
    parser.add_argument("--now", default=None, help="reference clock (ISO) used to drop bars still forming at capture time")
    args = parser.parse_args()

    asset = resolve_asset(args.asset)
    if asset is None:
        raise SystemExit(f"unknown asset: {args.asset}")

    if args.csv_prefix:
        candles_by_timeframe = {timeframe: load_csv(args.csv_prefix, timeframe) for timeframe in TIMEFRAMES}
        source = f"csv:{args.csv_prefix}"
    else:
        from marketobserver.market_data import MarketDataProvider

        provider = MarketDataProvider()
        candles_by_timeframe = {}
        for timeframe in TIMEFRAMES:
            try:
                candles_by_timeframe[timeframe] = provider.get_candles(asset, timeframe, 200)
            except Exception as exc:  # surfaced as an explicit failure, never faked
                print(f"{timeframe}: data unavailable ({exc})", file=sys.stderr)
        source = provider.last_source(asset.key) or "unknown"

    if not candles_by_timeframe:
        raise SystemExit("no market data available; refusing to analyse invented candles")

    now = None
    if args.now:
        from datetime import datetime as _dt
        now = _dt.fromisoformat(args.now)
    report = build_report(asset.key, asset.name_ar, asset.name_en, asset.quote, asset.price_decimals, candles_by_timeframe, source, now=now)
    print(render(report, args.lang, include_checklist=not args.no_checklist))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
