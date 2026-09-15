"""Bilingual (AR/EN) renderer for :mod:`marketobserver.smc`.

The engine produces numbers only; this module turns them into the fixed
multi-timeframe report. Nothing is invented here: every printed level, time and
ratio comes from the analysis object, and each sentence template exists in both
languages so the Arabic output is not a machine translation of English text.
"""

from __future__ import annotations

import re

from .smc import SmartMoneyReport, TimeframeSMC

LANGS = ("ar", "en", "both")

GATE_NAMES = {
    "htf_trend": ("اتجاه 4H", "4H trend"),
    "confirmation": ("تأكيد الكسر (BOS/CHoCH)", "break confirmation (BOS/CHoCH)"),
    "liquidity_sweep": ("صيد السيولة", "liquidity sweep"),
    "price_at_zone": ("السعر داخل المنطقة", "price inside the zone"),
    "not_early": ("عدم المطاردة", "no chasing"),
    "candle_confirm": ("إشارة الشموع", "candle trigger"),
    "volume": ("الحجم", "volume"),
    "risk_reward": ("العائد/المخاطرة", "reward:risk"),
}

TREND = {
    "bullish": ("صاعد (هيكل HH/HL مؤكد)", "bullish (confirmed HH/HL structure)"),
    "bearish": ("هابط (هيكل LL/LH مؤكد)", "bearish (confirmed LL/LH structure)"),
    "bullish_early": ("صاعد مبكر — CHoCH حديث ولم يُبنَ الهيكل بعد", "early bullish — fresh CHoCH, structure not rebuilt yet"),
    "bearish_early": ("هابط مبكر — CHoCH حديث ولم يُبنَ الهيكل بعد", "early bearish — fresh CHoCH, structure not rebuilt yet"),
    "sideways": ("جانبي بدون هيكل واضح", "ranging, no clean structure"),
    "unknown": ("غير محدد", "undefined"),
}
SIDE = {"long": ("شراء", "long"), "short": ("بيع", "short"), "none": ("لا اتجاه", "no side")}
DECISION = {
    "watch_long": ("مراقبة شراء — WATCH LONG", "WATCH LONG"),
    "watch_short": ("مراقبة بيع — WATCH SHORT", "WATCH SHORT"),
    "wait": ("انتظار — WAIT", "WAIT"),
}
CONFIDENCE = {"high": ("مرتفعة", "high"), "medium": ("متوسطة", "medium"), "low": ("منخفضة", "low")}
LABELS = {
    "HH": ("قمة أعلى HH", "higher high (HH)"), "HL": ("قاع أعلى HL", "higher low (HL)"),
    "LH": ("قمة أقل LH", "lower high (LH)"), "LL": ("قاع أقل LL", "lower low (LL)"),
    "EQH": ("قمم متساوية EQH", "equal highs (EQH)"), "EQL": ("قيعان متساوية EQL", "equal lows (EQL)"),
    "H": ("قاع/قمة غير مصنفة", "uncategorised high"), "L": ("قاع/قمة غير مصنفة", "uncategorised low"),
    "": ("نقطة بلا وسم", "unlabelled pivot"),
}
LEVELS = {
    "support": ("دعم", "support"), "resistance": ("مقاومة", "resistance"),
    "equal_highs": ("سيولة فوق قمم متساوية", "liquidity above equal highs"),
    "equal_lows": ("سيولة تحت قيعان متساوية", "liquidity below equal lows"),
    "prev_day_high": ("قمة اليوم السابق", "previous day high"), "prev_day_low": ("قاع اليوم السابق", "previous day low"),
    "week_high": ("قمة الأسبوع", "week high"), "week_low": ("قاع الأسبوع", "week low"),
}
ZONES = {
    "order_block": ("أوردر بلوك", "order block"), "fair_value_gap": ("فجوة سعرية FVG", "fair value gap (FVG)"),
    "level": ("منطقة عرض/طلب", "clustered level"), "none": ("لا منطقة صالحة", "no valid zone"),
}
PATTERNS = {
    "hammer": ("مطرقة", "hammer", "ذيل سفلي طويل يعني أن العرض جُرّب ثم امتصّ داخل نفس الشمعة"),
    "inverted_hammer": ("مطرقة مقلوبة", "inverted hammer", "ذيل علوي طويل بعد الهبوط يدل على امتصاص العرض"),
    "shooting_star": ("نجمة ساقطة", "shooting star", "رفض علوي: المشترون لم ينجحوا في الإغلاق فوق القمة"),
    "doji": ("دوجي", "doji", "توازن تام بين العرض والطلب، لا اتجاه داخل الشمعة"),
    "dragonfly_doji": ("دوجي بذيل سفلي", "dragonfly doji"), "gravestone_doji": ("دوجي بذيل علوي", "gravestone doji"),
    "engulfing_bullish": ("ابتلاعي شرائي", "bullish engulfing", "جسم شمعة أغلق فوق جسم الشمعة الحمراء السابقة: تحويل للسيولة لصالح المشترين"),
    "engulfing_bearish": ("ابتلاعي بيعي", "bearish engulfing", "جسم شمعة أغلق تحت جسم الشمعة الخضراء السابقة: تحويل للسيولة لصالح البائعين"),
    "morning_star": ("نجمة الصباح", "morning star", "ثلاث شموع: بيع ثم تردد ثم استرداد فوق منتصف البيع"),
    "evening_star": ("نجمة المساء", "evening star", "ثلاث شموع: شراء ثم تردد ثم فشل تحت منتصف الشراء"),
    "piercing_line": ("خط الاختراق", "piercing line", "إغلاق فوق منتصف الشمعة البيعية مع بقاء العجز تحت قمة السابق"),
    "dark_cloud_cover": ("ستارة السواد", "dark cloud cover", "إغلاق تحت منتصف الشمعة الشرائية بعد فتح فوق قمتها"),
    "inside_bar": ("بار داخلي", "inside bar", "انضغاط: سيولة قائمة على الطرفين وانفجار قادم في أحد الاتجاهين"),
    "outside_bar": ("بار خارجي", "outside bar", "صيد سيولة في الاتجاهين، والإغلاق التالي يحدد الجهة"),
}
CONTEXT = {
    "at_demand": ("عند منطقة طلب", "at a demand area"), "at_supply": ("عند منطقة عرض", "at a supply area"),
    "at_discount": ("منطقة خصم داخل المدى", "in the discount half of the range"),
    "at_premium": ("منطقة علاوة داخل المدى", "in the premium half of the range"),
    "mid_range": ("منتصف المدى — أضعف موقع", "mid-range — weakest location"),
}
VOLUME_TREND = {"expanding": ("توسّع", "expanding"), "contracting": ("انكماش", "contracting"), "flat": ("مستقر", "flat")}


def _pair(mapping: dict[str, tuple[str, str]], key: str, lang: str) -> str:
    ar, en = mapping.get(key, (key, key))
    return ar if lang == "ar" else en


def _fmt(value: float | None, decimals: int) -> str:
    if value is None:
        return "—"
    return f"{value:.{decimals}f}"


def _pattern_label(name: str, lang: str) -> str:
    entry = PATTERNS.get(name)
    if not entry:
        return name
    return entry[0] if lang == "ar" else entry[1]


def _pattern_meaning(name: str, lang: str) -> str:
    entry = PATTERNS.get(name)
    if not entry:
        return ""
    if lang == "ar":
        return entry[2] if len(entry) > 2 else ""
    return MEANING_EN.get(name, "")


MEANING_EN = {
    "hammer": "long lower wick: supply was tested and absorbed inside the same candle",
    "inverted_hammer": "long upper wick after a decline: supply was absorbed by buyers",
    "shooting_star": "upper rejection: buyers could not hold the high",
    "doji": "balance between offer and demand inside the candle",
    "dragonfly_doji": "indecision that defended the low",
    "gravestone_doji": "indecision that rejected the high",
    "engulfing_bullish": "body closed over the previous bearish body: liquidity transferred to buyers",
    "engulfing_bearish": "body closed under the previous bullish body: liquidity transferred to sellers",
    "morning_star": "three candles: sell-off, stall, reclaim above the midpoint",
    "evening_star": "three candles: rally, stall, failure below the midpoint",
    "piercing_line": "closes above the midpoint of the bearish candle",
    "dark_cloud_cover": "closes below the midpoint of the bullish candle",
    "inside_bar": "compression with resting liquidity on both sides",
    "outside_bar": "two-way stop run, the next close decides",
}


def _pct(part: float, whole: float, decimals: int = 2) -> str:
    if not whole:
        return "—"
    return f"{abs(part) / abs(whole) * 100:.{decimals}f}%"


def gate_line(gate, lang: str) -> str:  # noqa: C901 - flat template switch on purpose
    params = gate.params
    mark = "✅" if gate.passed else "❌"
    if gate.key == "htf_trend":
        text = f"4H: {_pair(TREND, params.get('trend', 'unknown'), lang)}"
        sequence = params.get("sequence") or []
        if sequence:
            labels = " → ".join(_pair(LABELS, label, lang) if label else _fmt(price, 0) for label, price in sequence)
            text += f" | {labels}"
    elif gate.key == "confirmation":
        kind = params.get("kind", "none")
        if kind == "none":
            text = f"لا كسر هيكل على {params.get('timeframe', '')}" if lang == "ar" else f"no BOS/CHoCH close on {params.get('timeframe', '')}"
        else:
            name = "BOS" if kind == "bos" else "CHoCH"
            direction = _pair(SIDE, "long" if params.get("direction") == "bullish" else "short", lang)
            if lang == "ar":
                text = f"{name} {direction} على {params.get('timeframe')} عند مستوى {_fmt(params.get('level'), 0)} — {params.get('time')}"
            else:
                text = f"{name} {direction} on {params.get('timeframe')} at level {_fmt(params.get('level'), 0)} — {params.get('time')}"
            if params.get("stale"):
                text += " | تجاوزها كسر معاكس لاحقًا" if lang == "ar" else " | invalidated by a later opposing break"
    elif gate.key == "liquidity_sweep":
        text = (f"صيد سيولة عند {_fmt(params.get('level'), 0)} بذيل فقط — {params.get('time')} ({params.get('timeframe')})") if lang == "ar" else \
               f"liquidity sweep at {_fmt(params.get('level'), 0)} (wick only) — {params.get('time')} ({params.get('timeframe')})"
    elif gate.key == "price_at_zone":
        zone = _pair(ZONES, params.get("kind", "none"), lang)
        if lang == "ar":
            text = f"المسافة إلى {zone}: {_fmt(params.get('distance'), 0)} (الحد الأقصى {_fmt(params.get('tolerance'), 0)})"
        else:
            text = f"distance to {zone}: {_fmt(params.get('distance'), 0)} (tolerance {_fmt(params.get('tolerance'), 0)})"
    elif gate.key == "not_early":
        text = ("السعر لم يتجاوز المنطقة بعد" if gate.passed else "السعر غادر المنطقة — الدخول الآن مطاردة") if lang == "ar" else \
               ("price has not left the zone yet" if gate.passed else "price already left the zone — entering now is chasing")
    elif gate.key == "candle_confirm":
        name = params.get("name") or ""
        label = _pattern_label(name, lang) if name else ""
        if lang == "ar":
            text = f"{params.get('count', 0)} إشارة تأكيد على {params.get('timeframe')}" + (f": {label} عند {params.get('time')}" if name else " — لا شيء")
        else:
            text = f"{params.get('count', 0)} confirmation print(s) on {params.get('timeframe')}" + (f": {label} at {params.get('time')}" if name else " — none")
    elif gate.key == "volume":
        buy = params.get("buy_pct")
        break_label = "شمعة الكسر" if lang == "ar" else "break candle"
        volume_label = "الحجم" if lang == "ar" else "volume"
        parts = [f"RVOL {_fmt(params.get('relative'), 2)}x",
                 f"{break_label} {_fmt(params.get('break'), 2)}x",
                 f"{volume_label} {_pair(VOLUME_TREND, params.get('trend', 'flat'), lang)} ({params.get('change', 0):+.0f}%)"]
        if buy is not None:
            parts.append(f"شراء {(buy):.0f}%" if lang == "ar" else f"taker buys {buy:.0f}%")
        text = " | ".join(str(part) for part in parts)
    elif gate.key == "risk_reward":
        text = (f"العائد/المخاطرة {_fmt(params.get('ratio'), 2)}:1 (الحد الأدنى {_fmt(params.get('minimum'), 2)}:1)") if lang == "ar" else \
               f"reward:risk {_fmt(params.get('ratio'), 2)}:1 (minimum {_fmt(params.get('minimum'), 2)}:1)"
    else:
        text = gate.detail
    return f"{mark} {text}"


def _structure_view(view: TimeframeSMC, lang: str) -> str:
    """Pivot ladder: the confirmed swings with their HH/HL/LH/LL/EQ tags."""
    pivots = sorted(view.swings, key=lambda swing: swing.index)[-6:]
    if not pivots:
        return "—"
    return " → ".join(f"{_fmt(swing.price, 0)} {_pair(LABELS, swing.label, lang)}" for swing in pivots)


def _zone_detail_ar(detail: str) -> str:
    """Arabic rendering of the engine's zone fingerprint.

    The engine writes one machine string per zone ("kind a-b @ time | filled x% |
    volume yx"). Splitting it here keeps the detector free of presentation code
    while still giving Arabic output that is not English text pasted into Arabic."""
    kinds = {
        "support": "دعم", "resistance": "مقاومة", "equal_lows": "قيعان متساوية", "equal_highs": "قمم متساوية",
        "prev_day_low": "قاع اليوم السابق", "prev_day_high": "قمة اليوم السابق",
        "week_low": "قاع الأسبوع", "week_high": "قمة الأسبوع",
    }
    out: list[str] = []
    for chunk in [item.strip() for item in (detail or "").split("|")]:
        lowered = chunk.lower()
        if lowered.startswith("order block"):
            out.append("أوردر بلوك " + lowered[len("order block "):])
        elif lowered.startswith("fair value gap"):
            out.append("فجوة سعرية " + lowered[len("fair value gap "):])
        elif " cluster " in lowered:
            kind, numbers = lowered.split(" cluster ", 1)
            out.append(f"{kinds.get(kind, kind)} {numbers}")
        elif re.match(r"^\d+ touch\(es\)$", lowered):
            out.append(f"{lowered.split(' ')[0]} لمسة")
        elif lowered.startswith("filled"):
            out.append("عُمل منها " + lowered[len("filled "):])
        elif lowered.startswith("displacement"):
            out.append("إزاحة " + lowered[len("displacement "):])
        elif lowered.startswith("volume"):
            out.append("حجم " + lowered[len("volume "):])
        elif lowered == "fresh":
            out.append("لم تُمسّ بعد")
        elif lowered == "mitigated":
            out.append("خضعت لاختبار")
        elif lowered.startswith("after "):
            out.append("تلت " + {"bos": "كسر هيكل", "choch": "تغير شخصية"}.get(lowered[6:], lowered[6:]))
        elif "after a liquidity sweep" in lowered:
            out.append("تلت صيد سيولة")
        elif "@" in chunk:
            out.append(chunk.replace(" @ ", " عند "))
        elif chunk:
            out.append(chunk)
    return " | ".join(out)


def _plain(note: str) -> str:
    """Arabic version of the invalidation sentence built by the engine."""
    text = note or ""
    for english, arabic in (
        ("invalidated on a", "يُلغى بإغلاق"),
        ("close below", "ناقص تحت"),
        ("close above", "ناقص فوق"),
    ):
        text = text.replace(english, arabic)
    return text


def _pattern_block(views: tuple[TimeframeSMC, ...], lang: str, side: str) -> str:
    """Pattern + where it printed + whether it agrees with the trade side.

    A pattern alone is never a signal, so each line carries its location, and
    prints that agree with the 4H direction are listed first (→ agrees, ⚠ fights
    it, • neutral/compression).
    """
    wanted = "bullish" if side == "long" else "bearish" if side == "short" else ""
    context_map = {key: (value[0] if lang == "ar" else value[1]) for key, value in CONTEXT.items()}
    ordered_views = sorted(views, key=lambda item: {"15m": 0, "30m": 0, "1h": 1, "4h": 2, "1d": 3}.get(item.timeframe, 4))
    collected: list[tuple[int, TimeframeSMC, object]] = []
    for view in ordered_views:
        taken = 0
        for print_ in reversed(view.patterns):
            if print_.name not in PATTERNS or taken >= 3:
                continue
            taken += 1
            rank = 0 if (wanted and print_.direction == wanted) else 2 if (wanted and print_.direction != wanted) else 1
            collected.append((rank, view, print_))
    collected.sort(key=lambda item: (item[0], -item[1].candles if False else 0, -item[2].index))
    lines: list[str] = []
    for rank, view, print_ in collected[:6]:
        tag = {0: "→", 1: "•", 2: "⚠"}[rank]
        lines.append(f"   {tag} {view.timeframe}: {_pattern_label(print_.name, lang)} — {context_map.get(print_.context, print_.context)} — {_pattern_meaning(print_.name, lang)}")
    return "\n".join(lines) if lines else ("   • لا نمط شموع مؤثر داخل النطاق الأخير" if lang == "ar" else "   • no material candle pattern inside the recent window")


def render(report: SmartMoneyReport, lang: str = "both", include_checklist: bool = True) -> str:
    """Full report in the requested language, or Arabic followed by English."""
    if lang == "both":
        separator = "\n" + "─" * 26 + "\n"
        return render(report, "ar", include_checklist) + separator + render(report, "en", include_checklist)
    decimals = report.decimals
    highest = report.highest
    plan = report.plan
    name = report.asset_name_ar if lang == "ar" else report.asset_name_en
    label = lambda ar, en: ar if lang == "ar" else en  # noqa: E731 - tiny inline picker
    body: list[str] = []
    body.append(f"🧠 {label('تحليل سيولة ذكية + حركة سعر', 'Smart Money + Price Action')} | {name} ({report.asset_key})")
    body.append(f"⏱️ {label('آخر شمعة مغلقة', 'last closed candle')}: {report.as_of} | {label('المصدر', 'source')}: {report.source}")
    if report.live_price:
        body.append(f"📡 {label('السعر الآن (شمعة جارية غير محسوبة)', 'live price (forming bar, excluded from the reads)')}: {_fmt(report.live_price, decimals)} {report.quote}")
    else:
        body.append(f"💵 {label('السعر', 'price')}: {_fmt(report.price, decimals)} {report.quote}")
    body.append("─" * 22)

    body.append(f"• {label('الاتجاه (4H)', 'Trend (4H)')}: {_pair(TREND, report.trend, lang)}")
    body.append(f"• {label('الهيكل', 'Market Structure')}: {_structure_view(highest, lang)}")
    for view in report.views:
        if view.events:
            last = view.events[-1]
            direction = label("شراء", "bullish") if last.direction == "bullish" else label("بيع", "bearish")
            body.append(f"   • {view.timeframe}: {direction} {last.label} @ {last.time} ({label('مستوى', 'level')} {_fmt(last.level, decimals)}{', ' + label('بذيل فقط', 'wick only') if last.wick_only else ''})")

    body.append(f"• {label('المستويات المفتاحية (4H)', 'Key Levels (4H)')}")
    for level in sorted(highest.levels, key=lambda item: item.strength, reverse=True)[:6]:
        distance = level.price - report.price
        body.append(f"   • {_fmt(level.price, decimals)} | {_pair(LEVELS, level.kind, lang)} | {label('لمسات', 'touches')} {level.touches} | "
                    f"{label('فوق', 'above') if distance > 0 else label('تحت', 'below')} {_fmt(abs(distance), decimals)} ({_pct(distance, report.price)})")

    body.append(f"• {label('موقع السعر', 'Current Price Position')}: {report.position_pct}% {label('من مدى 4H', 'of the 4H range')} | "
                f"{label('علاوة', 'premium') if report.premium_discount == 'premium' else label('خصم', 'discount') if report.premium_discount == 'discount' else label('توازن', 'equilibrium')}")
    body.append(f"   {label('المدى', 'range')}: {_fmt(highest.range_low, decimals)} – {_fmt(highest.range_high, decimals)} | ATR: {_fmt(highest.atr, decimals)}")

    stats_view = report.views[-1] if len(report.views) > 2 else (next((view for view in report.views if view.timeframe == "1h"), highest))
    stats = stats_view.volume
    body.append(f"• {label('الحجم', 'Volume Insight')} ({stats_view.timeframe}{', ' + label('تأكيد الدخول', 'entry trigger') if len(report.views) > 2 else ''})")
    body.append(f"   RVOL {stats.relative:.2f}x | {label('الحجم', 'volume')} {_pair(VOLUME_TREND, stats.trend, lang)} ({stats.trend_change_pct:+.0f}%) | OBV {stats.obv_slope:+.1f}%")
    if stats.buy_pct is not None:
        if lang == "ar":
            bias = "أغلبية شراء عدواني" if stats.buy_pct >= 53 else "أغلبية بيع عدواني" if stats.buy_pct <= 47 else "تدفق متوازن"
            body.append(f"   {label('نسبة الشراء العدواني', 'aggressive buy share')} {stats.buy_pct:.1f}% — {bias}")
        else:
            bias = "aggressive buying dominates" if stats.buy_pct >= 53 else "aggressive selling dominates" if stats.buy_pct <= 47 else "taker flow balanced"
            body.append(f"   aggressive buys {stats.buy_pct:.1f}% of taker volume — {bias}")
    else:
        body.append(f"   {label('مزود البيانات لم يرجع حجم الشراء العدواني — لا تُخمن ديناميكية الأوامر', 'provider returned no taker buy volume — order flow is not guessed')}")
    for _, when, ratio in stats.spikes:
        body.append(f"   {label('ارتفاع حجم', 'volume spike')}: {when} — {ratio:.2f}x")

    body.append(f"• {label('إشارات الشموع', 'Candlestick Signals')}")
    body.append(_pattern_block(report.views, lang, plan.side))

    confirmation = next((gate for gate in plan.gates if gate.key == "confirmation"), None)
    confirmed_text = gate_line(confirmation, lang)[2:].strip() if confirmation else "—"
    if confirmation and confirmation.passed:
        confirmed_text = f"{label('مؤكد:', 'confirmed:')} {confirmed_text}"
    elif confirmation:
        confirmed_text = f"{label('لا تأكيد بعد:', 'no confirmation yet:')} {confirmed_text}"
    body.append(f"• {label('التأكيد', 'Confirmation')} (BOS / CHoCH / {label('لا شيء', 'none')}): {confirmed_text}")

    if plan.zone:
        body.append(f"• {label('منطقة الدخول', 'Entry Zone')}: {_pair(ZONES, plan.zone.kind, lang)} {_fmt(plan.entry_low, decimals)} – {_fmt(plan.entry_high, decimals)}")
        detail = plan.zone.detail if lang == "en" else _zone_detail_ar(plan.zone.detail)
        body.append(f"   {detail}")
        body.append(f"   {label('الدخول بعد التأكيد فقط — لا دخول مبكر', 'entry only after confirmation — no early fill')}")
    else:
        body.append(f"• {label('منطقة الدخول', 'Entry Zone')}: {label('لا منطقة صالحة الآن — الانتظار إلزامي', 'no valid zone — waiting is mandatory')}")

    if plan.stop:
        body.append(f"• {label('وقف الخسارة', 'Stop Loss')}: {_fmt(plan.stop, decimals)} ({label('المسافة', 'distance')} {_fmt(plan.risk, decimals)} = {_pct(plan.risk, report.price)})")
        rule = label("تحت", "below") if plan.stop_rule == "below" else label("فوق", "above")
        body.append(f"   {label('خارج الهيكل مباشرة:', 'just outside structure:')} "
                    + (f"invalid on a {plan.stop_timeframe} close {rule} {_fmt(plan.stop, decimals)}" if lang == "en"
                       else f"يُلغى بإغلاق {plan.stop_timeframe} {rule} {_fmt(plan.stop, decimals)}"))
        body.append(f"• {label('الهدف', 'Take Profit')}: TP1 {_fmt(plan.target_one, decimals)} ({_pct(plan.reward_one, report.price)}) | TP2 {_fmt(plan.target_two, decimals)}")
        body.append(f"• {label('العائد/المخاطرة', 'Risk/Reward')}: 1:{plan.risk_reward:.2f}")
    else:
        body.append(f"• {label('وقف الخسارة', 'Stop Loss')}: {label('يُحدد مع ظهور منطقة صالحة', 'set once a valid zone appears')}")
        body.append(f"• {label('الهدف', 'Take Profit')}: —")
        body.append(f"• {label('العائد/المخاطرة', 'Risk/Reward')}: — {label('(الحد الأدنى 1:2)', '(minimum 1:2)')}")

    if report.watch_condition and plan.decision not in {"watch_long", "watch_short"}:
        condition = report.watch_condition
        decimals_hint = report.decimals
        kind_ar, kind_en = ZONES.get(condition.get("zone_kind", "none"), ("منطقة", "zone"))
        zone_text = f"{(kind_ar if lang == 'ar' else kind_en)} {_fmt(condition.get('zone_low'), decimals_hint)}–{_fmt(condition.get('zone_high'), decimals_hint)}"
        if condition.get("side") == "long":
            trigger_text = (f"إغلاق {condition.get('timeframe')} فوق {_fmt(condition.get('reclaim_level'), decimals_hint)} مع بقاء {zone_text} دعمًا"
                            if lang == "ar" else
                            f"a {condition.get('timeframe')} close above {_fmt(condition.get('reclaim_level'), decimals_hint)} while {zone_text} holds as demand")
        else:
            trigger_text = (f"إغلاق {condition.get('timeframe')} تحت {_fmt(condition.get('reclaim_level'), decimals_hint)} مع بقاء {zone_text} عرضًا"
                            if lang == "ar" else
                            f"a {condition.get('timeframe')} close under {_fmt(condition.get('reclaim_level'), decimals_hint)} while {zone_text} holds as supply")
        body.append(f"👀 {label('شرط التفعيل', 'activation condition')}: {trigger_text}")
        body.append(f"   {label('إلغاء الفكرة', 'invalidation')}: "
                    + (f"إغلاق {_fmt(condition.get('invalidation'), decimals_hint)}" if lang == "ar" else f"close {_fmt(condition.get('invalidation'), decimals_hint)}"))
        missing = condition.get("missing") or ()
        if missing:
            names = ", ".join(GATE_NAMES.get(key, (key, key))[0 if lang == "ar" else 1] for key in missing)
            body.append(f"   {label('ينقص القرار', 'still missing')}: {names}")

    decision = _pair(DECISION, plan.decision, lang)
    confidence = _pair(CONFIDENCE, plan.confidence, lang)
    body.append("─" * 22)
    body.append(f"🎯 {label('القرار النهائي', 'Final Decision')}: {decision} | {label('الثقة', 'confidence')}: {confidence} | {label('الجانب', 'side')}: {_pair(SIDE, plan.side, lang)}")
    if include_checklist:
        body.append("")
        body.append(f"{'قائمة التحقق (سبب القرار)' if lang == 'ar' else 'Gate list (why this decision)'}:")
        for gate in plan.gates:
            body.append(f"   {gate_line(gate, lang)}")
    draw_lines = []
    if report.liquidity_above:
        kind, value, pct = report.liquidity_above
        draw_lines.append(label("جذب سيولة لأعلى", "liquidity draw above") + f": {_pair(LEVELS, kind, lang)} {_fmt(value, decimals)} ({pct}%)")
    if report.liquidity_below:
        kind, value, pct = report.liquidity_below
        draw_lines.append(label("جذب سيولة لأسفل", "liquidity draw below") + f": {_pair(LEVELS, kind, lang)} {_fmt(value, decimals)} ({pct}%)")
    flag_lines = {
        "no_entry_timeframe": ("إطار الدخول غير متاح من المزود: التأكيد سقط على 1H والثقة أقل",
                              "entry timeframe unavailable from the provider: confirmation fell back to 1H, so confidence is lower"),
        "no_zone_in_front": ("لا أوردر بلوك ولا فجوة سعرية ولا منطقة غير مختبرة أمام السعر على 1H",
                             "no unmitigated order block, fair value gap or level sits in front of price on the 1H"),
    }
    for flag in report.flags:
        ar_text, en_text = flag_lines.get(flag, (flag, flag))
        draw_lines.append(ar_text if lang == "ar" else en_text)
    for timeframe, when, ratio in report.extra_spikes:
        draw_lines.append(f"{label('ارتفاع حجم', 'volume spike')} {timeframe}: {when} — {ratio:.2f}x")
    if draw_lines:
        body.append("")
        for line in draw_lines:
            body.append(f"ℹ️ {line}")
    body.append("")
    body.append(("هذا سيناريو بحثي مشروط مبني على بيانات حقيقية، وليس توصية شخصية ولا ضمانًا للربح. بدون اجتياز كل الشروط = WAIT."
                 if lang == "ar" else
                 "Conditional research scenario built on real candles only. Not personalized advice and no profit guarantee. Every gate must pass, otherwise WAIT."))
    return "\n".join(body)


def render_brief(report: SmartMoneyReport, lang: str = "ar") -> str:
    """Compact view used for photo captions and the brief button.

    It always states the decision, the 4H bias, the zone (or its absence) and the
    single most important failing gate, so the short form never hides the reason.
    """
    if lang == "both":
        return render_brief(report, "ar") + "\n" + "─" * 18 + "\n" + render_brief(report, "en")
    plan = report.plan
    decimals = report.decimals
    ar = lang == "ar"
    name = report.asset_name_ar if ar else report.asset_name_en
    lines = [f"🧠 {name} ({report.asset_key}) — {_pair(DECISION, plan.decision, 'ar' if ar else 'en')} | {_pair(CONFIDENCE, plan.confidence, 'ar' if ar else 'en')}",
             f"{'اتجاه 4H' if ar else '4H trend'}: {_pair(TREND, report.trend, 'ar' if ar else 'en')} | {'السعر' if ar else 'price'}: {_fmt(report.price, decimals)} {report.quote} | {report.position_pct}%",
             f"{'مدى 4H' if ar else '4H range'}: {_fmt(report.highest.range_low, decimals)} – {_fmt(report.highest.range_high, decimals)} | ATR {_fmt(report.highest.atr, decimals)}"]
    if plan.zone:
        zone = _pair(ZONES, plan.zone.kind, 'ar' if ar else 'en')
        lines.append(f"{'المنطقة' if ar else 'zone'}: {zone} {_fmt(plan.entry_low, decimals)}–{_fmt(plan.entry_high, decimals)}")
    else:
        lines.append(f"{'لا منطقة صالحة الآن — لا دخول' if ar else 'no valid zone right now — no entry'}")
    if plan.stop:
        lines.append(f"SL {_fmt(plan.stop, decimals)} | TP {_fmt(plan.target_one, decimals)} | RR 1:{plan.risk_reward:.2f}")
    blocked = [gate for gate in plan.gates if not gate.passed]
    if blocked:
        lines.append(("⛔ " if ar else "⛔ ") + gate_line(blocked[0], 'ar' if ar else 'en')[2:].strip())
    lines.append(f"{report.as_of} | {report.source}")
    return "\n".join(lines)


def highest_range(report: SmartMoneyReport) -> float:
    return report.highest.range_high


def to_dict(report: SmartMoneyReport) -> dict:
    """Structured, provider-stamped payload of one analysis.

    Exposed over HTTP and handed to the optional LLM explainer so prose can only
    ever restate these numbers; nothing in here is generated by a model.
    """
    plan = report.plan
    return {
        "asset": report.asset_key,
        "name_ar": report.asset_name_ar,
        "name_en": report.asset_name_en,
        "price": report.price,
        "live_price": report.live_price or None,
        "as_of": report.as_of,
        "source": report.source,
        "trend_4h": report.trend,
        "structure": report.structure_text,
        "position_pct": report.position_pct,
        "premium_discount": report.premium_discount,
        "decision": plan.decision,
        "side": plan.side,
        "confidence": plan.confidence,
        "zone": None if not plan.zone else {
            "type": plan.zone.kind, "low": plan.entry_low, "high": plan.entry_high,
            "time": plan.zone.time, "fresh": plan.zone.fresh, "detail": plan.zone.detail,
        },
        "stop": plan.stop or None,
        "stop_rule": plan.stop_rule or None,
        "stop_timeframe": plan.stop_timeframe or None,
        "target_one": plan.target_one or None,
        "target_two": plan.target_two or None,
        "risk": plan.risk or None,
        "risk_reward": plan.risk_reward or None,
        "gates": [{"key": gate.key, "passed": gate.passed, "detail": gate.detail} for gate in plan.gates],
        "watch_condition": report.watch_condition or None,
        "flags": list(report.flags),
        "liquidity_above": list(report.liquidity_above) or None,
        "liquidity_below": list(report.liquidity_below) or None,
        "volume_spikes": [{"timeframe": frame, "time": when, "ratio": ratio} for frame, when, ratio in report.extra_spikes],
        "timeframes": [
            {
                "timeframe": view.timeframe,
                "candles": view.candles,
                "close": view.price,
                "atr": view.atr,
                "bias": view.bias,
                "structure": view.structure,
                "range": [view.range_low, view.range_high],
                "events": [
                    {"kind": event.kind, "label": event.label, "direction": event.direction, "level": event.level,
                     "time": event.time, "wick_only": event.wick_only, "volume_ratio": event.volume_ratio}
                    for event in view.events
                ],
                "levels": [
                    {"kind": level.kind, "price": level.price, "touches": level.touches, "strength": level.strength}
                    for level in view.levels
                ],
                "order_blocks": [
                    {"kind": block.kind, "bottom": block.bottom, "top": block.top, "fresh": block.fresh,
                     "time": block.time, "displacement": block.displacement, "volume_ratio": block.volume_ratio,
                     "caused_by": block.caused_by}
                    for block in view.blocks
                ],
                "fair_value_gaps": [
                    {"kind": gap.kind, "bottom": gap.bottom, "top": gap.top, "filled_pct": gap.filled_pct,
                     "mitigated": gap.mitigated, "time": gap.time}
                    for gap in view.gaps
                ],
                "patterns": [
                    {"name": print_.name, "direction": print_.direction, "context": print_.context,
                     "time": print_.time, "candle": print_.index}
                    for print_ in view.patterns
                ],
                "volume": {
                    "rvol": view.volume.relative, "trend": view.volume.trend,
                    "trend_change_pct": view.volume.trend_change_pct, "buy_pct": view.volume.buy_pct,
                    "obv_slope": view.volume.obv_slope,
                    "spikes": [{"time": when, "ratio": ratio} for _, when, ratio in view.volume.spikes],
                },
            }
            for view in report.views
        ],
    }
