"""持久化层：PostgreSQL（生产，docker-compose）/ SQLite（本地自测，由 DATABASE_URL 切换）。"""
import os

from sqlalchemy import (Column, DateTime, ForeignKey, Integer, String,
                        UniqueConstraint, create_engine, event, func)
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.types import JSON

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./archive.db")

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()

# PostgreSQL 上使用 JSONB，其余方言退化为通用 JSON
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
JsonDoc = JSON().with_variant(JSONB(), "postgresql")

if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_busy_timeout(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()


class DeviceModel(Base):
    """设备型号：每个型号登记两个枚举字段及若干离线归档包。"""
    __tablename__ = "device_models"
    id = Column(Integer, primary_key=True)
    name = Column(String(64), unique=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class EnumField(Base):
    """枚举字段（如质控码 qc、工况 mode）。每型号固定登记两个。

    values 为离散枚举值清单（字符串/整数）；缺省 missing 与显式 null 是两个
    独立的隐式状态，不放进 values，计数时分开统计。
    """
    __tablename__ = "enum_fields"
    id = Column(Integer, primary_key=True)
    model_id = Column(Integer, ForeignKey("device_models.id"), nullable=False, index=True)
    field_key = Column(String(64), nullable=False)          # 报文中的字段名，如 qc
    label = Column(String(128), nullable=False)             # 展示名，如 质控码
    values = Column(JsonDoc, nullable=False)                # ["N", "Q", ...]
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("model_id", "field_key"),)


class ArchivePackage(Base):
    """离线归档包：整包在两个枚举字段上各状态的报文条数（摘要计数）。

    counts 结构：{field_key: {"missing": n0, "null": n1, "value:<v>": n, ...}}
    缺省与显式 null 分开计数；未出现的状态键按 0 处理。
    """
    __tablename__ = "archive_packages"
    id = Column(Integer, primary_key=True)
    model_id = Column(Integer, ForeignKey("device_models.id"), nullable=False, index=True)
    package_no = Column(String(64), nullable=False)
    counts = Column(JsonDoc, nullable=False)
    note = Column(String(256), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("model_id", "package_no"),)


class FirmwareSegment(Base):
    """包内固件段：某已发布固件版本的报文条数与该版本的固件契约（受限 schema）。"""
    __tablename__ = "firmware_segments"
    id = Column(Integer, primary_key=True)
    package_id = Column(Integer, ForeignKey("archive_packages.id"), nullable=False, index=True)
    segment = Column(String(64), nullable=False)            # 固件版本/段名，如 v1
    message_count = Column(Integer, nullable=False)         # 该段报文条数
    doc = Column("schema", JsonDoc, nullable=False)         # 固件契约 schema
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("package_id", "segment"),)
