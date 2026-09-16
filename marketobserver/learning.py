from __future__ import annotations

"""Learning loop: record verdicts, measure outcomes, calibrate confidence.

This is the honest version of "a bot that learns": every tracked verdict is
stored with its reference price, the price is re-checked after the horizon,
and the win/loss is written to an append-only journal. Accuracy statistics
come only from those rows, and the calibration check below lowers confidence
when a specific asset+direction has recently been losing.

What it does NOT do: rewrite the strategy, overfit on a handful of trades,
or promise future profit. Calibration needs at least MIN_TRADES resolved
outcomes before it says anything, and it only ever downgrades confidence —
it never invents a signal.
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MIN_TRADES = 15          # silence until this many resolved outcomes exist
LOW_ACCURACY = 0.45      # below this win rate -> downgrade confidence
FLAT_TOLERANCE = 0.0002  # |move| under 0.02% counts as flat, not win/loss

LONG_VERDICTS = {"CALL", "BUY"}
SHORT_VERDICTS = {"PUT", "SELL"}


@dataclass(frozen=True)
class Calibration:
    downgrade: bool
    note_ar: str
    note_en: str
    trades: int
    wins: int


def classify(ref_price: float, exit_price: float | None, verdict: str) -> str:
    """Win/loss/flat for a directional call measured after its horizon."""
    if exit_price is None or ref_price <= 0:
        return "flat"
    move = (exit_price - ref_price) / ref_price
    if abs(move) < FLAT_TOLERANCE:
        return "flat"
    if verdict in LONG_VERDICTS:
        return "win" if move > 0 else "loss"
    if verdict in SHORT_VERDICTS:
        return "win" if move < 0 else "loss"
    return "flat"


def resolve_due(db, price_fn, limit: int = 50) -> dict[str, int]:
    """Resolve every journal row whose horizon passed. Returns counts."""
    resolved = {"win": 0, "loss": 0, "flat": 0, "skipped": 0}
    for entry in db.journal_due(limit=limit):
        try:
            exit_price = price_fn(entry.asset_key)
        except Exception as exc:
            logger.info("journal resolve skipped for %s: %s", entry.asset_key, exc)
            resolved["skipped"] += 1
            continue
        outcome = classify(entry.ref_price, exit_price, entry.verdict)
        if db.journal_resolve(entry.id, outcome, exit_price):
            resolved[outcome] += 1
    return resolved


def accuracy(db, days: int = 30, kind: str | None = None,
             asset_key: str | None = None, verdict: str | None = None) -> tuple[int, int, float]:
    """(trades, wins, win_rate) over resolved, non-flat outcomes."""
    stats = db.journal_stats(days=days, kind=kind, asset_key=asset_key, verdict=verdict)
    decided = stats["wins"] + stats["losses"]
    rate = (stats["wins"] / decided) if decided else 0.0
    return stats["trades"], stats["wins"], round(rate, 3)


def calibration_for(db, asset_key: str, verdict: str, kind: str = "binary",
                    days: int = 30) -> Calibration:
    """Downgrade confidence when this asset+direction has been losing."""
    trades, wins, rate = accuracy(db, days=days, kind=kind, asset_key=asset_key, verdict=verdict)
    if trades < MIN_TRADES or rate >= LOW_ACCURACY:
        return Calibration(False, "", "", trades, wins)
    note_ar = (f"⚠️ المعايرة: دقة {verdict} على {asset_key} آخر {days} يومًا "
               f"منخفضة ({wins}/{trades} = {rate:.0%}) — تم خفض الثقة. هذا تذكير بالمخاطرة لا إشارة.")
    note_en = (f"⚠️ Calibration: recent {verdict} accuracy on {asset_key} is low "
               f"({wins}/{trades} = {rate:.0%}) — confidence lowered. A risk reminder, not a signal.")
    return Calibration(True, note_ar, note_en, trades, wins)


def stats_report(db, lang: str = "ar", days: int = 30) -> str:
    """Human-readable accuracy report from the journal only."""
    trades, wins, rate = accuracy(db, days=days)
    if lang == "ar":
        if not trades:
            return ("📊 سجل الأداء: لا توجد قرارات مُقيّمة بعد.\n"
                    "كل قرار ثنائي (CALL/PUT) يُسجَّل تلقائيًا وتُفحص نتيجته بعد 15 دقيقة — عد لاحقًا.")
        lines = [f"📊 دقة البوت — آخر {days} يومًا",
                 f"القرارات المُقيّمة: {trades} | ناجحة: {wins} | الدقة: {rate:.0%}"]
        for asset, verdict, count, win_count in db.journal_breakdown(days=days):
            asset_rate = win_count / count if count else 0.0
            lines.append(f"• {asset} {verdict}: {win_count}/{count} ({asset_rate:.0%})")
        lines.append("تُحسب الدقة من أسعار حقيقية بعد انتهاء الأفق الزمني، ولا تُحذف أي نتيجة.")
        return "\n".join(lines)
    if not trades:
        return ("📊 Performance journal: no resolved decisions yet.\n"
                "Every binary verdict (CALL/PUT) is journaled and checked after 15 minutes — check back later.")
    lines = [f"📊 Bot accuracy — last {days} days",
             f"Resolved: {trades} | wins: {wins} | win rate: {rate:.0%}"]
    for asset, verdict, count, win_count in db.journal_breakdown(days=days):
        asset_rate = win_count / count if count else 0.0
        lines.append(f"• {asset} {verdict}: {win_count}/{count} ({asset_rate:.0%})")
    lines.append("Measured from live prices after each horizon; no outcome is ever deleted.")
    return "\n".join(lines)
