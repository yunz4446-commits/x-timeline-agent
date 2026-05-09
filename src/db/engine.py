"""数据库连接管理"""

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session

from .models import Base

_engine = None
_session_factory = None


def _migrate_schema(db_url: str, engine) -> None:
    """追加缺失列（兼容旧库 schema 升级）"""
    if "sqlite" not in db_url:
        return

    # bookmarks 表
    with engine.connect() as conn:
        existing = {row[1] for row in conn.exec_driver_sql(
            "PRAGMA table_info('bookmarks')")}
    bm_missing = [
        ("author_username", "VARCHAR(256) DEFAULT ''"),
        ("text", "TEXT DEFAULT ''"),
        ("link", "TEXT DEFAULT ''"),
        ("tweet_created_at", "DATETIME"),
        ("score", "FLOAT DEFAULT 0.0"),
    ]
    bm_added = 0
    with engine.connect() as conn:
        for col_name, col_def in bm_missing:
            if col_name not in existing:
                conn.exec_driver_sql(
                    f"ALTER TABLE bookmarks ADD COLUMN {col_name} {col_def}")
                bm_added += 1
        conn.commit()

    # tweets 表 embedding 列
    with engine.connect() as conn:
        tweet_cols = {row[1] for row in conn.exec_driver_sql(
            "PRAGMA table_info('tweets')")}
    if "embedding" not in tweet_cols:
        with engine.connect() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE tweets ADD COLUMN embedding TEXT DEFAULT ''")
            conn.commit()
        bm_added += 1

    if bm_added:
        import logging
        logging.getLogger(__name__).info(
            "schema migrated: %d columns added", bm_added)




def init_db(db_url: str) -> None:
    global _engine, _session_factory

    # 确保数据目录存在
    if not db_url.startswith("sqlite"):
        db_url = "sqlite:///" + db_url
    db_path = db_url.replace("sqlite:///", "")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    _engine = create_engine(
        db_url,
        echo=False,
        connect_args={"check_same_thread": False} if "sqlite" in db_url else {},
    )

    # SQLite WAL 模式 + 外键
    if "sqlite" in db_url:
        @event.listens_for(_engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    Base.metadata.create_all(_engine)
    _migrate_schema(db_url, _engine)
    _session_factory = sessionmaker(bind=_engine)


def get_session() -> Session:
    return _session_factory()


def get_engine():
    return _engine
