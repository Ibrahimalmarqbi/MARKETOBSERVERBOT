from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, create_engine, select, update, delete, Index, inspect, text, or_
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def coerce_aware(value: datetime | None) -> datetime | None:
    """Return an aware UTC datetime for cooldown comparisons.

    SQLite returns naive datetimes for DateTime(timezone=True) columns while
    PostgreSQL returns aware ones; comparing a naive value against
    ``datetime.now(timezone.utc)`` raises TypeError and used to kill the whole
    news scanner after the first cooldown was ever stored. All stored times
    are UTC, so attaching UTC to a naive value is exact, not a guess.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def cooldown_active(until: datetime | None, now: datetime) -> bool:
    moment = coerce_aware(until)
    return bool(moment and moment > now)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    language: Mapped[str] = mapped_column(String(5), default="ar")
    last_asset: Mapped[str] = mapped_column(String(30), default="BTC")
    role: Mapped[str] = mapped_column(String(20), default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    tz_name: Mapped[str] = mapped_column(String(64), default="Asia/Riyadh")
    signals_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    signal_cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    news_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    news_assets: Mapped[str] = mapped_column(String(500), default="ALL")
    news_preference_set: Mapped[bool] = mapped_column(Boolean, default=False)
    news_cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    calendar_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    capital: Mapped[float] = mapped_column(Float, default=1000.0)
    risk_percent: Mapped[float] = mapped_column(Float, default=1.0)
    binary_mode: Mapped[str | None] = mapped_column(String(10), nullable=True)  # strict | scored; None = global default
    lang_explicit: Mapped[bool] = mapped_column(Boolean, default=False)  # language chosen in settings; detection must not overwrite it
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(Integer, index=True)
    asset_key: Mapped[str] = mapped_column(String(30), index=True)
    target_price: Mapped[float] = mapped_column(Float)
    condition: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NewsSeen(Base):
    __tablename__ = "news_seen"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NewsSeenUser(Base):
    """Per-user delivery log. The legacy global table suppressed a headline
    for *everyone* once a single user received it, so idle users silently
    missed news. New deliveries are recorded here per chat."""
    __tablename__ = "news_seen_user"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(Integer, index=True)
    fingerprint: Mapped[str] = mapped_column(String(128), index=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CalendarState(Base):
    """Exactly-once tracking for economic-event notifications.

    One row per event; the three stages (pre-brief, release, follow-up) flip
    independently so a restart can never resend a stage or skip the next one.
    ``ref_prices`` is a JSON object {asset_key: price} snapshotted at release
    so the follow-up measures the market's real reaction.
    """
    __tablename__ = "calendar_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255))
    country: Mapped[str] = mapped_column(String(10))
    impact: Mapped[str] = mapped_column(String(10))
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    forecast: Mapped[str] = mapped_column(String(64), default="")
    previous: Mapped[str] = mapped_column(String(64), default="")
    pre_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    release_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    followup_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    ref_prices: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Journal(Base):
    """Decision journal: every auto-tracked verdict with its measured outcome.

    The learning loop is deliberately boring and honest — record the call and
    the reference price, re-check the price after the horizon, store win/loss.
    Statistics and confidence calibration are computed from these rows only;
    nothing is ever edited or deleted, so accuracy cannot be gamed.
    """
    __tablename__ = "journal"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), index=True)  # binary | event | news
    chat_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    asset_key: Mapped[str] = mapped_column(String(30), index=True)
    verdict: Mapped[str] = mapped_column(String(10))  # CALL | PUT | BUY | SELL
    ref_price: Mapped[float] = mapped_column(Float)
    horizon_minutes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolve_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome: Mapped[str] = mapped_column(String(10), default="pending", index=True)  # pending | win | loss | flat
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")


class PaperAccount(Base):
    __tablename__ = "paper_accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    balance: Mapped[float] = mapped_column(Float, default=10000.0)
    currency: Mapped[str] = mapped_column(String(10), default="USD")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Trade(Base):
    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(Integer, index=True)
    asset_key: Mapped[str] = mapped_column(String(30), index=True)
    side: Mapped[str] = mapped_column(String(5))
    quantity: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    mode: Mapped[str] = mapped_column(String(10), default="paper")
    broker_order_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


Index("ix_alerts_active_asset", Alert.status, Alert.asset_key)
Index("ix_news_seen_user_chat_fp", NewsSeenUser.chat_id, NewsSeenUser.fingerprint, unique=True)


class Database:
    def __init__(self, url: str):
        if url.startswith("sqlite:///"):
            connect_args = {"check_same_thread": False}
        else:
            connect_args = {}
        self.engine = create_engine(url, future=True, pool_pre_ping=True, connect_args=connect_args)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)
        # create_all does not add new columns to an existing table; migrate the
        # small user preference explicitly for existing Render databases.
        columns = {column["name"] for column in inspect(self.engine).get_columns("users")}
        missing = []
        if "tz_name" not in columns:
            missing.append("ALTER TABLE users ADD COLUMN tz_name VARCHAR(64) DEFAULT 'Asia/Riyadh'")
        if "signals_enabled" not in columns:
            missing.append("ALTER TABLE users ADD COLUMN signals_enabled BOOLEAN DEFAULT FALSE")
        if "signal_cooldown_until" not in columns:
            missing.append("ALTER TABLE users ADD COLUMN signal_cooldown_until TIMESTAMP NULL")
        if "news_enabled" not in columns:
            missing.append("ALTER TABLE users ADD COLUMN news_enabled BOOLEAN DEFAULT FALSE")
        if "news_cooldown_until" not in columns:
            missing.append("ALTER TABLE users ADD COLUMN news_cooldown_until TIMESTAMP NULL")
        if "news_assets" not in columns:
            missing.append("ALTER TABLE users ADD COLUMN news_assets VARCHAR(500) DEFAULT 'ALL'")
        if "news_preference_set" not in columns:
            missing.append("ALTER TABLE users ADD COLUMN news_preference_set BOOLEAN DEFAULT FALSE")
        if "calendar_enabled" not in columns:
            missing.append("ALTER TABLE users ADD COLUMN calendar_enabled BOOLEAN DEFAULT TRUE")
        if "capital" not in columns:
            missing.append("ALTER TABLE users ADD COLUMN capital DOUBLE PRECISION DEFAULT 1000.0")
        if "risk_percent" not in columns:
            missing.append("ALTER TABLE users ADD COLUMN risk_percent DOUBLE PRECISION DEFAULT 1.0")
        if "binary_mode" not in columns:
            missing.append("ALTER TABLE users ADD COLUMN binary_mode VARCHAR(10) NULL")
        if "lang_explicit" not in columns:
            missing.append("ALTER TABLE users ADD COLUMN lang_explicit BOOLEAN DEFAULT FALSE")
        if missing:
            with self.engine.begin() as connection:
                for statement in missing:
                    connection.execute(text(statement))
        # Legacy rows may contain NULLs after an ALTER TABLE migration. They
        # must receive the automatic default unless the user explicitly opted out.
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE users SET news_preference_set = FALSE WHERE news_preference_set IS NULL"))
            connection.execute(text("UPDATE users SET news_enabled = TRUE WHERE news_preference_set = FALSE AND news_enabled IS NULL"))
            connection.execute(text("UPDATE users SET news_assets = 'ALL' WHERE news_preference_set = FALSE AND (news_assets IS NULL OR news_assets = '')"))
            connection.execute(text("UPDATE users SET calendar_enabled = TRUE WHERE calendar_enabled IS NULL"))
            connection.execute(text("UPDATE users SET capital = 1000.0 WHERE capital IS NULL OR capital <= 0"))
            connection.execute(text("UPDATE users SET risk_percent = 1.0 WHERE risk_percent IS NULL OR risk_percent <= 0"))
            connection.execute(text("UPDATE users SET lang_explicit = FALSE WHERE lang_explicit IS NULL"))

    @contextmanager
    def session(self):
        session = self.Session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def upsert_user(self, chat_id: int, username: str | None, language: str, last_asset: str | None = None) -> User:
        with self.session() as s:
            user = s.scalar(select(User).where(User.chat_id == chat_id))
            if user is None:
                user = User(chat_id=chat_id, username=username, language=language, last_asset=last_asset or "BTC")
                s.add(user)
            else:
                user.username = username
                # A language chosen in ⚙️ settings beats per-message detection.
                if user.lang_explicit is not True:
                    user.language = language
                if last_asset:
                    user.last_asset = last_asset
                # Re-enroll legacy users automatically, but never override an
                # explicit /newsalerts off choice.
                if user.news_preference_set is not True:
                    user.news_enabled = True
                    user.news_assets = user.news_assets or "ALL"
                    user.news_preference_set = False
                user.updated_at = utcnow()
            s.flush()
            return user

    def get_user(self, chat_id: int) -> User | None:
        with self.session() as s:
            return s.scalar(select(User).where(User.chat_id == chat_id))

    def set_last_asset(self, chat_id: int, asset_key: str) -> None:
        with self.session() as s:
            s.execute(update(User).where(User.chat_id == chat_id).values(last_asset=asset_key, updated_at=utcnow()))

    def set_timezone(self, chat_id: int, tz_name: str) -> None:
        with self.session() as s:
            s.execute(update(User).where(User.chat_id == chat_id).values(tz_name=tz_name, updated_at=utcnow()))

    def set_capital(self, chat_id: int, capital: float) -> None:
        with self.session() as s:
            s.execute(update(User).where(User.chat_id == chat_id).values(capital=capital, updated_at=utcnow()))

    def set_risk_percent(self, chat_id: int, risk_percent: float) -> None:
        with self.session() as s:
            s.execute(update(User).where(User.chat_id == chat_id).values(risk_percent=risk_percent, updated_at=utcnow()))

    def set_binary_mode(self, chat_id: int, mode: str | None) -> None:
        """Persist the user's binary entry mode. None = follow the global default."""
        with self.session() as s:
            s.execute(update(User).where(User.chat_id == chat_id).values(binary_mode=mode, updated_at=utcnow()))

    def set_language(self, chat_id: int, lang: str) -> None:
        """Persist an explicit language choice from the settings panel."""
        if lang not in ("ar", "en"):
            return
        with self.session() as s:
            s.execute(update(User).where(User.chat_id == chat_id)
                      .values(language=lang, lang_explicit=True, updated_at=utcnow()))

    def set_calendar_enabled(self, chat_id: int, enabled: bool) -> None:
        with self.session() as s:
            s.execute(update(User).where(User.chat_id == chat_id).values(calendar_enabled=enabled, updated_at=utcnow()))

    def set_signals_enabled(self, chat_id: int, enabled: bool) -> None:
        with self.session() as s:
            s.execute(update(User).where(User.chat_id == chat_id).values(signals_enabled=enabled, updated_at=utcnow()))

    def signal_users(self) -> list[User]:
        with self.session() as s:
            return list(s.scalars(select(User).where(User.is_active.is_(True), User.signals_enabled.is_(True))).all())

    def set_signal_cooldown(self, chat_id: int, until: datetime) -> None:
        with self.session() as s:
            s.execute(update(User).where(User.chat_id == chat_id).values(signal_cooldown_until=until, updated_at=utcnow()))

    def set_news_enabled(self, chat_id: int, enabled: bool, assets: str | None = None) -> None:
        with self.session() as s:
            values = {"news_enabled": enabled, "news_preference_set": True, "updated_at": utcnow()}
            if assets:
                values["news_assets"] = assets
            s.execute(update(User).where(User.chat_id == chat_id).values(**values))

    def migrate_news_subscriptions(self) -> int:
        """Enroll active legacy users unless they explicitly opted out."""
        migrated = 0
        with self.session() as s:
            users = list(s.scalars(select(User).where(User.is_active.is_(True), or_(User.news_preference_set.is_(False), User.news_preference_set.is_(None)))).all())
            for user in users:
                changed = user.news_enabled is not True or not user.news_assets or user.news_preference_set is not False
                user.news_enabled = True
                user.news_assets = user.news_assets or "ALL"
                user.news_preference_set = False
                if changed:
                    user.updated_at = utcnow()
                    migrated += 1
        return migrated

    def news_users(self) -> list[User]:
        with self.session() as s:
            return list(s.scalars(select(User).where(User.is_active.is_(True), or_(User.news_preference_set.is_(False), User.news_preference_set.is_(None), User.news_enabled.is_(True)))).all())

    def calendar_users(self) -> list[User]:
        with self.session() as s:
            return list(s.scalars(select(User).where(User.is_active.is_(True), or_(User.calendar_enabled.is_(True), User.calendar_enabled.is_(None)))).all())

    def set_news_cooldown(self, chat_id: int, until: datetime) -> None:
        with self.session() as s:
            s.execute(update(User).where(User.chat_id == chat_id).values(news_cooldown_until=until, updated_at=utcnow()))

    def news_was_seen(self, fingerprint: str, chat_id: int | None = None) -> bool:
        """Legacy global check, plus the per-user log when a chat is given."""
        with self.session() as s:
            if chat_id is not None:
                row = s.scalar(select(NewsSeenUser.id).where(
                    NewsSeenUser.chat_id == chat_id, NewsSeenUser.fingerprint == fingerprint))
                if row is not None:
                    return True
            return s.scalar(select(NewsSeen.id).where(NewsSeen.fingerprint == fingerprint)) is not None

    def mark_news_seen(self, fingerprint: str, chat_id: int | None = None) -> None:
        with self.session() as s:
            if chat_id is not None:
                exists = s.scalar(select(NewsSeenUser.id).where(
                    NewsSeenUser.chat_id == chat_id, NewsSeenUser.fingerprint == fingerprint))
                if exists is None:
                    s.add(NewsSeenUser(chat_id=chat_id, fingerprint=fingerprint))
                return
            if s.scalar(select(NewsSeen.id).where(NewsSeen.fingerprint == fingerprint)) is None:
                s.add(NewsSeen(fingerprint=fingerprint))

    # ---------------- economic calendar state ----------------

    def get_calendar_state(self, event_key: str) -> CalendarState | None:
        with self.session() as s:
            return s.scalar(select(CalendarState).where(CalendarState.event_key == event_key))

    def ensure_calendar_state(self, event_key: str, title: str, country: str, impact: str,
                              event_time: datetime, forecast: str = "", previous: str = "") -> CalendarState:
        with self.session() as s:
            state = s.scalar(select(CalendarState).where(CalendarState.event_key == event_key))
            if state is None:
                state = CalendarState(event_key=event_key, title=title, country=country,
                                      impact=impact, event_time=event_time,
                                      forecast=forecast or "", previous=previous or "")
                s.add(state)
                s.flush()
            return state

    def mark_calendar_stage(self, event_key: str, stage: str, ref_prices: str | None = None) -> None:
        if stage not in {"pre_sent", "release_sent", "followup_sent"}:
            raise ValueError(f"Unknown calendar stage: {stage}")
        with self.session() as s:
            values: dict = {stage: True, "updated_at": utcnow()}
            if ref_prices is not None:
                values["ref_prices"] = ref_prices
            s.execute(update(CalendarState).where(CalendarState.event_key == event_key).values(**values))

    def prune_calendar_state(self, older_than_days: int = 14) -> int:
        cutoff = utcnow() - timedelta(days=older_than_days)
        with self.session() as s:
            result = s.execute(delete(CalendarState).where(CalendarState.event_time < cutoff))
            return result.rowcount or 0

    # ---------------- decision journal ----------------

    def journal_add(self, kind: str, asset_key: str, verdict: str, ref_price: float,
                    horizon_minutes: int, chat_id: int | None = None, note: str = "") -> Journal:
        now = utcnow()
        with self.session() as s:
            entry = Journal(kind=kind, chat_id=chat_id, asset_key=asset_key, verdict=verdict,
                            ref_price=ref_price, horizon_minutes=horizon_minutes,
                            created_at=now, resolve_at=now + timedelta(minutes=horizon_minutes),
                            outcome="pending", note=note or "")
            s.add(entry)
            s.flush()
            return entry

    def journal_due(self, limit: int = 50) -> list[Journal]:
        with self.session() as s:
            return list(s.scalars(select(Journal).where(
                Journal.outcome == "pending", Journal.resolve_at <= utcnow(),
            ).order_by(Journal.resolve_at.asc()).limit(limit)).all())

    def journal_resolve(self, entry_id: int, outcome: str, exit_price: float | None) -> bool:
        if outcome not in {"win", "loss", "flat"}:
            raise ValueError(f"Unknown outcome: {outcome}")
        with self.session() as s:
            result = s.execute(update(Journal).where(
                Journal.id == entry_id, Journal.outcome == "pending",
            ).values(outcome=outcome, exit_price=exit_price, resolved_at=utcnow()))
            return result.rowcount == 1

    def journal_stats(self, days: int = 30, kind: str | None = None,
                      asset_key: str | None = None, verdict: str | None = None) -> dict[str, int]:
        cutoff = utcnow() - timedelta(days=days)
        with self.session() as s:
            query = select(Journal.outcome).where(
                Journal.resolved_at.is_not(None), Journal.resolved_at >= cutoff,
                Journal.outcome.in_(["win", "loss", "flat"]),
            )
            if kind:
                query = query.where(Journal.kind == kind)
            if asset_key:
                query = query.where(Journal.asset_key == asset_key)
            if verdict:
                query = query.where(Journal.verdict == verdict)
            rows = list(s.scalars(query).all())
        wins = sum(1 for outcome in rows if outcome == "win")
        losses = sum(1 for outcome in rows if outcome == "loss")
        flats = sum(1 for outcome in rows if outcome == "flat")
        return {"trades": len(rows), "wins": wins, "losses": losses, "flats": flats}

    def journal_breakdown(self, days: int = 30, kind: str | None = None) -> list[tuple[str, str, int, int]]:
        """(asset_key, verdict, trades, wins) for the stats report."""
        from collections import Counter
        cutoff = utcnow() - timedelta(days=days)
        with self.session() as s:
            query = select(Journal.asset_key, Journal.verdict, Journal.outcome).where(
                Journal.resolved_at.is_not(None), Journal.resolved_at >= cutoff,
                Journal.outcome.in_(["win", "loss", "flat"]),
            )
            if kind:
                query = query.where(Journal.kind == kind)
            rows = list(s.execute(query).all())
        trades: Counter[tuple[str, str]] = Counter()
        wins: Counter[tuple[str, str]] = Counter()
        for asset_key, verdict, outcome in rows:
            trades[(asset_key, verdict)] += 1
            if outcome == "win":
                wins[(asset_key, verdict)] += 1
        return [(asset, verdict, count, wins[(asset, verdict)]) for (asset, verdict), count in trades.most_common(12)]

    def add_alert(self, chat_id: int, asset_key: str, target_price: float, condition: str) -> Alert:
        with self.session() as s:
            alert = Alert(chat_id=chat_id, asset_key=asset_key, target_price=target_price, condition=condition)
            s.add(alert)
            s.flush()
            return alert

    def active_alerts(self) -> list[Alert]:
        with self.session() as s:
            return list(s.scalars(select(Alert).where(Alert.status == "active")).all())

    def trigger_alert(self, alert_id: int) -> None:
        with self.session() as s:
            s.execute(update(Alert).where(Alert.id == alert_id, Alert.status == "active").values(status="triggered", triggered_at=utcnow()))

    def list_alerts(self, chat_id: int) -> list[Alert]:
        with self.session() as s:
            return list(s.scalars(select(Alert).where(Alert.chat_id == chat_id).order_by(Alert.id.desc())).all())

    def cancel_alert(self, chat_id: int, alert_id: int) -> bool:
        with self.session() as s:
            result = s.execute(update(Alert).where(Alert.id == alert_id, Alert.chat_id == chat_id, Alert.status == "active").values(status="cancelled"))
            return result.rowcount == 1

    def ensure_paper_account(self, chat_id: int) -> PaperAccount:
        with self.session() as s:
            account = s.scalar(select(PaperAccount).where(PaperAccount.chat_id == chat_id))
            if account is None:
                account = PaperAccount(chat_id=chat_id)
                s.add(account)
                s.flush()
            return account

    def add_trade(self, **kwargs) -> Trade:
        with self.session() as s:
            trade = Trade(**kwargs)
            s.add(trade)
            s.flush()
            return trade

    def set_user_role(self, chat_id: int, role: str) -> bool:
        if role not in {"user", "analyst", "admin"}:
            raise ValueError("Unsupported role")
        with self.session() as s:
            result = s.execute(update(User).where(User.chat_id == chat_id).values(role=role, updated_at=utcnow()))
            return result.rowcount == 1

    def set_user_active(self, chat_id: int, active: bool) -> bool:
        with self.session() as s:
            result = s.execute(update(User).where(User.chat_id == chat_id).values(is_active=active, updated_at=utcnow()))
            return result.rowcount == 1

    def list_users(self, limit: int = 100) -> list[User]:
        with self.session() as s:
            return list(s.scalars(select(User).order_by(User.created_at.desc()).limit(limit)).all())

    def broadcast_users(self) -> list[User]:
        """Every user still allowed to receive messages, oldest first."""
        with self.session() as s:
            return list(s.scalars(select(User).where(User.is_active.is_(True)).order_by(User.id.asc())).all())

    def count_active_users(self) -> int:
        with self.session() as s:
            return len(s.scalars(select(User.id).where(User.is_active.is_(True))).all())

    def stats(self) -> dict[str, int]:
        with self.session() as s:
            return {
                "users": len(s.scalars(select(User.id)).all()),
                "active_users": len(s.scalars(select(User.id).where(User.is_active.is_(True))).all()),
                "active_alerts": len(s.scalars(select(Alert.id).where(Alert.status == "active")).all()),
                "trades": len(s.scalars(select(Trade.id)).all()),
                "journal_pending": len(s.scalars(select(Journal.id).where(Journal.outcome == "pending")).all()),
                "journal_resolved": len(s.scalars(select(Journal.id).where(Journal.outcome != "pending")).all()),
            }
