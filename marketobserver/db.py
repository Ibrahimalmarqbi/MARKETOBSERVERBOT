from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, create_engine, select, update, delete, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
                user.language = language
                if last_asset:
                    user.last_asset = last_asset
                user.updated_at = utcnow()
            s.flush()
            return user

    def get_user(self, chat_id: int) -> User | None:
        with self.session() as s:
            return s.scalar(select(User).where(User.chat_id == chat_id))

    def set_last_asset(self, chat_id: int, asset_key: str) -> None:
        with self.session() as s:
            s.execute(update(User).where(User.chat_id == chat_id).values(last_asset=asset_key, updated_at=utcnow()))

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

    def stats(self) -> dict[str, int]:
        with self.session() as s:
            return {
                "users": len(s.scalars(select(User.id)).all()),
                "active_users": len(s.scalars(select(User.id).where(User.is_active.is_(True))).all()),
                "active_alerts": len(s.scalars(select(Alert.id).where(Alert.status == "active")).all()),
                "trades": len(s.scalars(select(Trade.id)).all()),
            }