"""Command-line runner for the strict BUY / SELL / WAIT decision engine.

    python tools/decision.py BTC
    python tools/decision.py XAUUSD --lang ar --verbose
    python tools/decision.py BTC --csv-prefix data/btc --now 2026-09-16T09:00:00+00:00
    python tools/decision.py BTC --json

Live mode uses the same provider as the Telegram bot (Binance first for crypto,
Kraken and Yahoo as fallbacks) so a decision can never be based on invented
prices. ``--csv-prefix`` replays a stored dump instead, which is how a past
verdict can be reproduced candle for candle.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from marketobserver.assets import resolve_asset  # noqa: E402
from marketobserver.decision import decide, render, to_dict  # noqa: E402
from marketobserver.market_data import load_candle_csv  # noqa: E402

TIMEFRAMES = ("4h", "1h", "15m")
LANGS = ("en", "ar", "both")


def main() -> int:
    parser = argparse.ArgumentParser(description="Strict Smart Money / Price Action decision (BUY / SELL / WAIT)")
    parser.add_argument("asset", nargs="?", default="BTC")
    parser.add_argument("--lang", choices=LANGS, default="en")
    parser.add_argument("--verbose", action="store_true", help="print the full gate chain behind the verdict")
    parser.add_argument("--json", action="store_true", help="machine-readable output with every gate")
    parser.add_argument("--csv-prefix", default=None, help="read candles from <prefix>_<tf>.csv instead of the network")
    parser.add_argument("--now", default=None, help="reference clock (ISO) used to drop bars still forming at capture time")
    args = parser.parse_args()

    asset = resolve_asset(args.asset)
    if asset is None:
        raise SystemExit(f"unknown asset: {args.asset}")

    if args.csv_prefix:
        candles_by_timeframe = {timeframe: load_candle_csv(f"{args.csv_prefix}_{timeframe}.csv") for timeframe in TIMEFRAMES}
        source = f"csv:{args.csv_prefix}"
    else:
        from marketobserver.market_data import MarketDataProvider

        provider = MarketDataProvider()
        candles_by_timeframe = {}
        for timeframe in TIMEFRAMES:
            try:
                candles_by_timeframe[timeframe] = provider.get_candles(asset, timeframe, 200)
            except Exception as exc:  # explicit failure, never a fabricated candle
                print(f"{timeframe}: data unavailable ({exc})", file=sys.stderr)
        source = provider.last_source(asset.key) or "unknown"

    if not candles_by_timeframe:
        raise SystemExit("no market data available; refusing to decide on invented candles")

    now = datetime.fromisoformat(args.now) if args.now else None
    decision = decide(asset.key, asset.name_ar, asset.name_en, asset.quote, asset.price_decimals,
                      candles_by_timeframe, source, now=now)
    if args.json:
        print(json.dumps(to_dict(decision), indent=2, ensure_ascii=False))
    else:
        print(render(decision, args.lang, verbose=args.verbose))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
