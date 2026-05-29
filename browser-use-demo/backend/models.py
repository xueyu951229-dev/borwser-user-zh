from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()


class Session(Base):
    __tablename__ = "sessions"
    id = Column(String(36), primary_key=True)
    title = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    is_active = Column(Boolean, default=True)
    # Per-session Docker container tracking
    container_id = Column(String(64), nullable=True)
    container_name = Column(String(128), nullable=True)
    novnc_port = Column(Integer, nullable=True)
    cdp_url = Column(String(256), nullable=True)


class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(36), nullable=False, index=True)
    role = Column(String(50))  # user, assistant, tool
    content = Column(Text)  # JSON serialized content blocks
    created_at = Column(DateTime, default=datetime.utcnow)