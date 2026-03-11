"""
SQLAlchemy models for validator auction tracking.

Stores auction events and tracks wins for reward calculation.
"""

import logging
from typing import Callable

from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    BigInteger,
    Boolean,
    Index,
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker, Session


class Base(DeclarativeBase):
    pass


SessionFactory = Callable[[], Session]


class AuctionEventModel(Base):
    """
    Store auction events from blockchain.

    Records all auction-related events for historical tracking
    and debugging purposes.
    """

    __tablename__ = "auction_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_type = Column(String, nullable=False)  # CREATED, BID_PLACED, FINALIZED
    auction_id = Column(BigInteger, nullable=False)
    block_number = Column(BigInteger, nullable=False)
    vault_owner = Column(String, nullable=True)
    vault_id = Column(Integer, nullable=True)
    bidder = Column(String, nullable=True)
    bid_id = Column(BigInteger, nullable=True)
    bid_amount = Column(BigInteger, nullable=True)
    winner = Column(String, nullable=True)
    winning_bid = Column(BigInteger, nullable=True)
    processed = Column(Boolean, default=False)

    # Indexes for efficient querying
    __table_args__ = (
        Index("ix_auction_events_auction_id", "auction_id"),
        Index("ix_auction_events_block_number", "block_number"),
        Index("ix_auction_events_event_type", "event_type"),
    )


class AuctionWin(Base):
    """
    Track auction wins by miner hotkey.

    Used for reward calculation at end of each tempo.
    """

    __tablename__ = "auction_wins"

    id = Column(Integer, primary_key=True, autoincrement=True)
    auction_id = Column(BigInteger, nullable=False, unique=True)
    winner_hotkey = Column(String, nullable=False)
    winning_bid = Column(BigInteger, nullable=False)
    debt_balance = Column(BigInteger, nullable=True)
    block_number = Column(BigInteger, nullable=False)
    # Which tempo this win was rewarded in (null if not yet processed)
    tempo_block = Column(BigInteger, nullable=True)

    # Indexes for efficient querying
    __table_args__ = (
        Index("ix_auction_wins_winner_hotkey", "winner_hotkey"),
        Index("ix_auction_wins_block_number", "block_number"),
        Index("ix_auction_wins_tempo_block", "tempo_block"),
    )


def init_db(db_path: str = "tensorusd.db") -> SessionFactory:
    """
    Initialize the SQLite database.

    Creates all tables if they don't exist and returns a session factory.

    Args:
        db_path: Path to SQLite database file

    Returns:
        Session factory for creating database sessions
    """
    # Suppress SQLAlchemy ORM mapper logs
    logging.getLogger("sqlalchemy").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.orm.mapper.Mapper").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.orm.engine.Engine").setLevel(logging.WARNING)

    engine = create_engine(
        f"sqlite:///{db_path}",
        echo=False,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)
