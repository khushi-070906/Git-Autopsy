from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import Column, DateTime, String, Text, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = "sqlite:///./autopsy.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class Analysis(Base):
    __tablename__ = "analyses"

    id = Column(String, primary_key=True)
    repo_url = Column(String, nullable=False)
    status = Column(String, nullable=False, default="queued")
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # JSON blobs — SQLite has no native JSON type, so store as text.
    result_json = Column(Text, nullable=True)

    def result(self) -> dict | None:
        return json.loads(self.result_json) if self.result_json else None

    def set_result(self, data: dict) -> None:
        self.result_json = json.dumps(data)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


def get_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
