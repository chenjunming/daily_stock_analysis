# -*- coding: utf-8 -*-
"""
===================================
A股自选股智能分析系统 - 存储层
===================================

职责：
1. 管理 SQLite 数据库连接（单例模式）
2. 定义 ORM 数据模型
3. 提供数据存取接口
4. 实现智能更新逻辑（断点续传）
"""

import atexit
import hashlib
import json
import logging
import re
import threading
from datetime import datetime, date, timedelta
from typing import Optional, List, Dict, Any, TYPE_CHECKING, Tuple
from pathlib import Path

import pandas as pd
from sqlalchemy import (
    create_engine,
    Column,
    String,
    Float,
    Date,
    DateTime,
    Integer,
    Index,
    UniqueConstraint,
    Text,
    select,
    and_,
    desc,
    text,
)
from sqlalchemy.orm import (
    declarative_base,
    sessionmaker,
    Session,
)
from sqlalchemy.exc import IntegrityError

from src.config import get_config

logger = logging.getLogger(__name__)

# SQLAlchemy ORM 基类
Base = declarative_base()

if TYPE_CHECKING:
    from src.search_service import SearchResponse


# === 数据模型定义 ===

class StockDaily(Base):
    """
    股票日线数据模型
    
    存储每日行情数据和计算的技术指标
    支持多股票、多日期的唯一约束
    """
    __tablename__ = 'stock_daily'
    
    # 主键
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # 股票代码（如 600519, 000001）
    code = Column(String(10), nullable=False, index=True)
    
    # 交易日期
    date = Column(Date, nullable=False, index=True)
    
    # OHLC 数据
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    
    # 成交数据
    volume = Column(Float)  # 成交量（股）
    amount = Column(Float)  # 成交额（元）
    pct_chg = Column(Float)  # 涨跌幅（%）
    
    # 技术指标
    ma5 = Column(Float)
    ma10 = Column(Float)
    ma20 = Column(Float)
    volume_ratio = Column(Float)  # 量比
    
    # 数据来源
    data_source = Column(String(50))  # 记录数据来源（如 AkshareFetcher）
    
    # 更新时间
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    
    # 唯一约束：同一股票同一日期只能有一条数据
    __table_args__ = (
        UniqueConstraint('code', 'date', name='uix_code_date'),
        Index('ix_code_date', 'code', 'date'),
    )
    
    def __repr__(self):
        return f"<StockDaily(code={self.code}, date={self.date}, close={self.close})>"
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'code': self.code,
            'date': self.date,
            'open': self.open,
            'high': self.high,
            'low': self.low,
            'close': self.close,
            'volume': self.volume,
            'amount': self.amount,
            'pct_chg': self.pct_chg,
            'ma5': self.ma5,
            'ma10': self.ma10,
            'ma20': self.ma20,
            'volume_ratio': self.volume_ratio,
            'data_source': self.data_source,
        }


class NewsIntel(Base):
    """
    新闻情报数据模型

    存储搜索到的新闻情报条目，用于后续分析与查询
    """
    __tablename__ = 'news_intel'

    id = Column(Integer, primary_key=True, autoincrement=True)

    # 关联用户查询操作
    query_id = Column(String(64), index=True)

    # 股票信息
    code = Column(String(10), nullable=False, index=True)
    name = Column(String(50))

    # 搜索上下文
    dimension = Column(String(32), index=True)  # latest_news / risk_check / earnings / market_analysis / industry
    query = Column(String(255))
    provider = Column(String(32), index=True)

    # 新闻内容
    title = Column(String(300), nullable=False)
    snippet = Column(Text)
    url = Column(String(1000), nullable=False)
    source = Column(String(100))
    published_date = Column(DateTime, index=True)

    # 入库时间
    fetched_at = Column(DateTime, default=datetime.now, index=True)
    query_source = Column(String(32), index=True)  # bot/web/cli/system
    requester_platform = Column(String(20))
    requester_user_id = Column(String(64))
    requester_user_name = Column(String(64))
    requester_chat_id = Column(String(64))
    requester_message_id = Column(String(64))
    requester_query = Column(String(255))

    __table_args__ = (
        UniqueConstraint('url', name='uix_news_url'),
        Index('ix_news_code_pub', 'code', 'published_date'),
    )

    def __repr__(self) -> str:
        return f"<NewsIntel(code={self.code}, title={self.title[:20]}...)>"


class Watchlist(Base):
    """
    自选股列表模型
    
    支持按市场分组（A股、港股、美股）
    """
    __tablename__ = 'watchlist'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # 股票代码
    code = Column(String(10), nullable=False, unique=True, index=True)
    
    # 股票名称
    name = Column(String(50))
    
    # 市场类型: CN（A股）、HK（港股）、US（美股）
    market = Column(String(10), nullable=False, index=True)
    
    # 排序权重（小的优先显示）
    order_key = Column(Integer, default=0)
    
    # 备注
    remarks = Column(Text)
    
    # 时间戳
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'id': self.id,
            'code': self.code,
            'name': self.name,
            'market': self.market,
            'order_key': self.order_key,
            'remarks': self.remarks,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class AnalysisHistory(Base):
    """
    分析结果历史记录模型

    保存每次分析结果，支持按 query_id/股票代码检索
    """
    __tablename__ = 'analysis_history'

    id = Column(Integer, primary_key=True, autoincrement=True)

    # 关联查询链路
    query_id = Column(String(64), index=True)

    # 股票信息
    code = Column(String(10), nullable=False, index=True)
    name = Column(String(50))
    report_type = Column(String(16), index=True)

    # 核心结论
    sentiment_score = Column(Integer)
    operation_advice = Column(String(20))
    trend_prediction = Column(String(50))
    analysis_summary = Column(Text)

    # 详细数据
    raw_result = Column(Text)
    news_content = Column(Text)
    context_snapshot = Column(Text)

    # 狙击点位（用于回测）
    ideal_buy = Column(Float)
    secondary_buy = Column(Float)
    stop_loss = Column(Float)
    take_profit = Column(Float)

    created_at = Column(DateTime, default=datetime.now, index=True)

    __table_args__ = (
        Index('ix_analysis_code_time', 'code', 'created_at'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'id': self.id,
            'query_id': self.query_id,
            'code': self.code,
            'name': self.name,
            'report_type': self.report_type,
            'sentiment_score': self.sentiment_score,
            'operation_advice': self.operation_advice,
            'trend_prediction': self.trend_prediction,
            'analysis_summary': self.analysis_summary,
            'raw_result': self.raw_result,
            'news_content': self.news_content,
            'context_snapshot': self.context_snapshot,
            'ideal_buy': self.ideal_buy,
            'secondary_buy': self.secondary_buy,
            'stop_loss': self.stop_loss,
            'take_profit': self.take_profit,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class PortfolioHoldingCurrent(Base):
    """
    用户当前持仓数据（按 owner_key 隔离）。
    """
    __tablename__ = 'portfolio_holding_current'

    id = Column(Integer, primary_key=True, autoincrement=True)
    owner_key = Column(String(128), nullable=False, index=True)
    code = Column(String(10), nullable=False, index=True)
    name = Column(String(50), nullable=False)
    market = Column(String(10), nullable=False, index=True)  # CN/HK/US
    avg_cost = Column(Float, nullable=False)
    shares = Column(Float, nullable=True)      # 持仓股数（可选，支持小数）
    weight_pct = Column(Float, nullable=False)  # 占总资产比例 [0, 100]
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)

    __table_args__ = (
        UniqueConstraint('owner_key', 'code', name='uix_owner_code'),
        Index('ix_owner_market_order', 'owner_key', 'market', 'updated_at'),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "owner_key": self.owner_key,
            "code": self.code,
            "name": self.name,
            "market": self.market,
            "avg_cost": self.avg_cost,
            "shares": self.shares,
            "weight_pct": self.weight_pct,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class PortfolioHoldingHistory(Base):
    """
    用户持仓变更快照历史。
    """
    __tablename__ = 'portfolio_holding_history'

    id = Column(Integer, primary_key=True, autoincrement=True)
    owner_key = Column(String(128), nullable=False, index=True)
    code = Column(String(10), nullable=False, index=True)
    name = Column(String(50))
    market = Column(String(10), nullable=False, index=True)
    action = Column(String(16), nullable=False, index=True)  # add/update/remove
    old_avg_cost = Column(Float)
    new_avg_cost = Column(Float)
    old_shares = Column(Float)
    new_shares = Column(Float)
    old_weight_pct = Column(Float)
    new_weight_pct = Column(Float)
    trade_price = Column(Float)    # 本次成交价格（买/卖）
    trade_shares = Column(Float)   # 本次成交股数
    realized_pnl = Column(Float)   # 本次已实现盈亏（卖出时）
    operator = Column(String(128))
    changed_at = Column(DateTime, default=datetime.now, nullable=False, index=True)

    __table_args__ = (
        Index('ix_holding_history_owner_time', 'owner_key', 'changed_at'),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "owner_key": self.owner_key,
            "code": self.code,
            "name": self.name,
            "market": self.market,
            "action": self.action,
            "old_avg_cost": self.old_avg_cost,
            "new_avg_cost": self.new_avg_cost,
            "old_shares": self.old_shares,
            "new_shares": self.new_shares,
            "old_weight_pct": self.old_weight_pct,
            "new_weight_pct": self.new_weight_pct,
            "trade_price": self.trade_price,
            "trade_shares": self.trade_shares,
            "realized_pnl": self.realized_pnl,
            "operator": self.operator,
            "changed_at": self.changed_at.isoformat() if self.changed_at else None,
        }


class PortfolioUserConfig(Base):
    """
    用户组合配置（总资产、汇率参数）。
    """
    __tablename__ = 'portfolio_user_config'

    id = Column(Integer, primary_key=True, autoincrement=True)
    owner_key = Column(String(128), nullable=False, unique=True, index=True)
    total_asset_cny = Column(Float, nullable=True)     # 组合总资产（人民币）
    usd_cny = Column(Float, nullable=False, default=6.94)
    hkd_cny = Column(Float, nullable=False, default=0.888)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "owner_key": self.owner_key,
            "total_asset_cny": self.total_asset_cny,
            "usd_cny": self.usd_cny,
            "hkd_cny": self.hkd_cny,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class DatabaseManager:
    """
    数据库管理器 - 单例模式
    
    职责：
    1. 管理数据库连接池
    2. 提供 Session 上下文管理
    3. 封装数据存取操作
    """
    
    _instance: Optional['DatabaseManager'] = None
    
    def __new__(cls, *args, **kwargs):
        """单例模式实现"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self, db_url: Optional[str] = None):
        """
        初始化数据库管理器
        
        Args:
            db_url: 数据库连接 URL（可选，默认从配置读取）
        """
        if self._initialized:
            return
        
        if db_url is None:
            config = get_config()
            db_url = config.get_db_url()
        
        # 创建数据库引擎
        self._engine = create_engine(
            db_url,
            echo=False,  # 设为 True 可查看 SQL 语句
            pool_pre_ping=True,  # 连接健康检查
        )
        
        # 创建 Session 工厂
        self._SessionLocal = sessionmaker(
            bind=self._engine,
            autocommit=False,
            autoflush=False,
        )
        
        # 创建所有表
        Base.metadata.create_all(self._engine)
        self._ensure_portfolio_schema()

        self._initialized = True
        logger.info(f"数据库初始化完成: {db_url}")

        # 注册退出钩子，确保程序退出时关闭数据库连接
        atexit.register(DatabaseManager._cleanup_engine, self._engine)
        self._name_fetcher_manager = None
        self._name_cache_lock = threading.Lock()
        self._ak_name_cache: Dict[str, Dict[str, str]] = {"CN": {}, "HK": {}, "US": {}}

    def _ensure_portfolio_schema(self) -> None:
        """
        对已存在库做轻量列迁移（SQLite 兼容）。
        """
        try:
            with self._engine.begin() as conn:
                existing_cols = {}
                for table_name in ["portfolio_holding_current", "portfolio_holding_history"]:
                    rows = conn.execute(text(f"PRAGMA table_info({table_name})")).mappings().all()
                    existing_cols[table_name] = {r.get("name") for r in rows}

                current_additions = {
                    "shares": "ALTER TABLE portfolio_holding_current ADD COLUMN shares FLOAT",
                }
                for col, ddl in current_additions.items():
                    if col not in existing_cols.get("portfolio_holding_current", set()):
                        conn.execute(text(ddl))
                        logger.info(f"数据库迁移: portfolio_holding_current 新增列 {col}")

                history_additions = {
                    "old_shares": "ALTER TABLE portfolio_holding_history ADD COLUMN old_shares FLOAT",
                    "new_shares": "ALTER TABLE portfolio_holding_history ADD COLUMN new_shares FLOAT",
                    "trade_price": "ALTER TABLE portfolio_holding_history ADD COLUMN trade_price FLOAT",
                    "trade_shares": "ALTER TABLE portfolio_holding_history ADD COLUMN trade_shares FLOAT",
                    "realized_pnl": "ALTER TABLE portfolio_holding_history ADD COLUMN realized_pnl FLOAT",
                }
                for col, ddl in history_additions.items():
                    if col not in existing_cols.get("portfolio_holding_history", set()):
                        conn.execute(text(ddl))
                        logger.info(f"数据库迁移: portfolio_holding_history 新增列 {col}")
        except Exception as e:
            logger.warning(f"数据库轻量迁移失败（可忽略新功能）: {e}")
    
    @classmethod
    def get_instance(cls) -> 'DatabaseManager':
        """获取单例实例"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    @classmethod
    def reset_instance(cls) -> None:
        """重置单例（用于测试）"""
        if cls._instance is not None:
            cls._instance._engine.dispose()
            cls._instance = None

    @classmethod
    def _cleanup_engine(cls, engine) -> None:
        """
        清理数据库引擎（atexit 钩子）

        确保程序退出时关闭所有数据库连接，避免 ResourceWarning

        Args:
            engine: SQLAlchemy 引擎对象
        """
        try:
            if engine is not None:
                engine.dispose()
                logger.debug("数据库引擎已清理")
        except Exception as e:
            logger.warning(f"清理数据库引擎时出错: {e}")
    
    def get_session(self) -> Session:
        """
        获取数据库 Session
        
        使用示例:
            with db.get_session() as session:
                # 执行查询
                session.commit()  # 如果需要
        """
        session = self._SessionLocal()
        try:
            return session
        except Exception:
            session.close()
            raise
    
    def has_today_data(self, code: str, target_date: Optional[date] = None) -> bool:
        """
        检查是否已有指定日期的数据
        
        用于断点续传逻辑：如果已有数据则跳过网络请求
        
        Args:
            code: 股票代码
            target_date: 目标日期（默认今天）
            
        Returns:
            是否存在数据
        """
        if target_date is None:
            target_date = date.today()
        
        with self.get_session() as session:
            result = session.execute(
                select(StockDaily).where(
                    and_(
                        StockDaily.code == code,
                        StockDaily.date == target_date
                    )
                )
            ).scalar_one_or_none()
            
            return result is not None
    
    def get_latest_data(
        self, 
        code: str, 
        days: int = 2
    ) -> List[StockDaily]:
        """
        获取最近 N 天的数据
        
        用于计算"相比昨日"的变化
        
        Args:
            code: 股票代码
            days: 获取天数
            
        Returns:
            StockDaily 对象列表（按日期降序）
        """
        with self.get_session() as session:
            results = session.execute(
                select(StockDaily)
                .where(StockDaily.code == code)
                .order_by(desc(StockDaily.date))
                .limit(days)
            ).scalars().all()
            
            return list(results)

    def save_news_intel(
        self,
        code: str,
        name: str,
        dimension: str,
        query: str,
        response: 'SearchResponse',
        query_context: Optional[Dict[str, str]] = None
    ) -> int:
        """
        保存新闻情报到数据库

        去重策略：
        - 优先按 URL 去重（唯一约束）
        - URL 缺失时按 title + source + published_date 进行软去重

        关联策略：
        - query_context 记录用户查询信息（平台、用户、会话、原始指令等）
        """
        if not response or not response.results:
            return 0

        saved_count = 0

        with self.get_session() as session:
            try:
                for item in response.results:
                    title = (item.title or '').strip()
                    url = (item.url or '').strip()
                    source = (item.source or '').strip()
                    snippet = (item.snippet or '').strip()
                    published_date = self._parse_published_date(item.published_date)

                    if not title and not url:
                        continue

                    url_key = url or self._build_fallback_url_key(
                        code=code,
                        title=title,
                        source=source,
                        published_date=published_date
                    )

                    # 优先按 URL 或兜底键去重
                    existing = session.execute(
                        select(NewsIntel).where(NewsIntel.url == url_key)
                    ).scalar_one_or_none()

                    if existing:
                        existing.name = name or existing.name
                        existing.dimension = dimension or existing.dimension
                        existing.query = query or existing.query
                        existing.provider = response.provider or existing.provider
                        existing.snippet = snippet or existing.snippet
                        existing.source = source or existing.source
                        existing.published_date = published_date or existing.published_date
                        existing.fetched_at = datetime.now()

                        if query_context:
                            existing.query_id = query_context.get("query_id") or existing.query_id
                            existing.query_source = query_context.get("query_source") or existing.query_source
                            existing.requester_platform = query_context.get("requester_platform") or existing.requester_platform
                            existing.requester_user_id = query_context.get("requester_user_id") or existing.requester_user_id
                            existing.requester_user_name = query_context.get("requester_user_name") or existing.requester_user_name
                            existing.requester_chat_id = query_context.get("requester_chat_id") or existing.requester_chat_id
                            existing.requester_message_id = query_context.get("requester_message_id") or existing.requester_message_id
                            existing.requester_query = query_context.get("requester_query") or existing.requester_query
                    else:
                        try:
                            with session.begin_nested():
                                record = NewsIntel(
                                    code=code,
                                    name=name,
                                    dimension=dimension,
                                    query=query,
                                    provider=response.provider,
                                    title=title,
                                    snippet=snippet,
                                    url=url_key,
                                    source=source,
                                    published_date=published_date,
                                    fetched_at=datetime.now(),
                                    query_id=(query_context or {}).get("query_id"),
                                    query_source=(query_context or {}).get("query_source"),
                                    requester_platform=(query_context or {}).get("requester_platform"),
                                    requester_user_id=(query_context or {}).get("requester_user_id"),
                                    requester_user_name=(query_context or {}).get("requester_user_name"),
                                    requester_chat_id=(query_context or {}).get("requester_chat_id"),
                                    requester_message_id=(query_context or {}).get("requester_message_id"),
                                    requester_query=(query_context or {}).get("requester_query"),
                                )
                                session.add(record)
                                session.flush()
                            saved_count += 1
                        except IntegrityError:
                            # 单条 URL 唯一约束冲突（如并发插入），仅跳过本条，保留本批其余成功项
                            logger.debug("新闻情报重复（已跳过）: %s %s", code, url_key)

                session.commit()
                logger.info(f"保存新闻情报成功: {code}, 新增 {saved_count} 条")

            except Exception as e:
                session.rollback()
                logger.error(f"保存新闻情报失败: {e}")
                raise

        return saved_count

    def get_recent_news(self, code: str, days: int = 7, limit: int = 20) -> List[NewsIntel]:
        """
        获取指定股票最近 N 天的新闻情报
        """
        cutoff_date = datetime.now() - timedelta(days=days)

        with self.get_session() as session:
            results = session.execute(
                select(NewsIntel)
                .where(
                    and_(
                        NewsIntel.code == code,
                        NewsIntel.fetched_at >= cutoff_date
                    )
                )
                .order_by(desc(NewsIntel.fetched_at))
                .limit(limit)
            ).scalars().all()

            return list(results)

    def save_analysis_history(
        self,
        result: Any,
        query_id: str,
        report_type: str,
        news_content: Optional[str],
        context_snapshot: Optional[Dict[str, Any]] = None,
        save_snapshot: bool = True
    ) -> int:
        """
        保存分析结果历史记录
        """
        if result is None:
            return 0

        sniper_points = self._extract_sniper_points(result)
        raw_result = self._build_raw_result(result)
        context_text = None
        if save_snapshot and context_snapshot is not None:
            context_text = self._safe_json_dumps(context_snapshot)

        record = AnalysisHistory(
            query_id=query_id,
            code=result.code,
            name=result.name,
            report_type=report_type,
            sentiment_score=result.sentiment_score,
            operation_advice=result.operation_advice,
            trend_prediction=result.trend_prediction,
            analysis_summary=result.analysis_summary,
            raw_result=self._safe_json_dumps(raw_result),
            news_content=news_content,
            context_snapshot=context_text,
            ideal_buy=sniper_points.get("ideal_buy"),
            secondary_buy=sniper_points.get("secondary_buy"),
            stop_loss=sniper_points.get("stop_loss"),
            take_profit=sniper_points.get("take_profit"),
            created_at=datetime.now(),
        )

        with self.get_session() as session:
            try:
                session.add(record)
                session.commit()
                return 1
            except Exception as e:
                session.rollback()
                logger.error(f"保存分析历史失败: {e}")
                return 0

    def get_analysis_history(
        self,
        code: Optional[str] = None,
        query_id: Optional[str] = None,
        days: int = 30,
        limit: int = 50
    ) -> List[AnalysisHistory]:
        """
        查询分析历史记录
        """
        cutoff_date = datetime.now() - timedelta(days=days)

        with self.get_session() as session:
            conditions = [AnalysisHistory.created_at >= cutoff_date]
            if code:
                conditions.append(AnalysisHistory.code == code)
            if query_id:
                conditions.append(AnalysisHistory.query_id == query_id)

            results = session.execute(
                select(AnalysisHistory)
                .where(and_(*conditions))
                .order_by(desc(AnalysisHistory.created_at))
                .limit(limit)
            ).scalars().all()

            return list(results)
    
    def get_data_range(
        self, 
        code: str, 
        start_date: date, 
        end_date: date
    ) -> List[StockDaily]:
        """
        获取指定日期范围的数据
        
        Args:
            code: 股票代码
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            StockDaily 对象列表
        """
        with self.get_session() as session:
            results = session.execute(
                select(StockDaily)
                .where(
                    and_(
                        StockDaily.code == code,
                        StockDaily.date >= start_date,
                        StockDaily.date <= end_date
                    )
                )
                .order_by(StockDaily.date)
            ).scalars().all()
            
            return list(results)
    
    # ==================== Watchlist 操作 ====================

    @staticmethod
    def _normalize_watchlist_code(code: str, market: str) -> str:
        """规范化自选股代码格式。"""
        code = (code or "").strip().upper()
        market = (market or "CN").strip().upper()

        if market == "HK":
            if re.match(r'^\d{5}$', code):
                return f"HK{code}"
            if re.match(r'^HK\d{5}$', code):
                return code
        return code

    def _resolve_watchlist_name(self, code: str, market: str) -> Optional[str]:
        """
        通过数据源反查股票名称。
        失败时返回 None。
        """
        query_candidates = [code]
        if market == "HK" and code.startswith("HK") and len(code) == 7:
            query_candidates.append(code[2:])  # 某些数据源使用 5 位港股代码

        # 1) 静态映射兜底（快速且稳定）
        try:
            from src.analyzer import STOCK_NAME_MAP
            for q in query_candidates:
                if q in STOCK_NAME_MAP:
                    return STOCK_NAME_MAP[q]
        except Exception:
            pass

        # 2) AkShare 全市场列表兜底（覆盖大量 A股/ETF/港股）
        ak_name = self._resolve_name_from_akshare_cache(code, market)
        if ak_name:
            return ak_name

        # 3) 搜索服务兜底（可复用内置/历史/在线候选池）
        search_name = self._resolve_name_from_search_service(code, market)
        if search_name:
            return search_name

        # 4) 美股实时行情兜底（US 优先走 yfinance，返回公司简称）
        realtime_name = self._resolve_name_from_realtime_quote(code, market)
        if realtime_name:
            return realtime_name

        # 5) AI 识别兜底（最后手段，可能有误差）
        ai_name = self._resolve_name_with_ai(code, market)
        if ai_name:
            return ai_name

        return None

    def _resolve_name_from_search_service(self, code: str, market: str) -> Optional[str]:
        try:
            from web.services import get_stock_search_service
            service = get_stock_search_service()
            resp = service.search(keyword=code, market=market, limit=5)
            rows = resp.get("data", []) if isinstance(resp, dict) else []
            code_upper = (code or "").strip().upper()
            for row in rows:
                row_code = str(row.get("code", "")).strip().upper()
                row_name = str(row.get("name", "")).strip()
                if not row_code or not row_name:
                    continue
                if row_code == code_upper and row_name.upper() != code_upper:
                    return row_name
            if rows:
                first = rows[0]
                first_name = str(first.get("name", "")).strip()
                first_code = str(first.get("code", "")).strip().upper()
                if first_name and first_name.upper() != first_code:
                    return first_name
        except Exception as e:
            logger.debug(f"search_service 兜底查名失败 code={code}, market={market}: {e}")
        return None

    def _resolve_name_from_realtime_quote(self, code: str, market: str) -> Optional[str]:
        if (market or "").upper() != "US":
            return None
        try:
            from data_provider import DataFetcherManager
            manager = DataFetcherManager()
            quote = manager.get_realtime_quote(code)
            if quote and getattr(quote, "name", None):
                name = str(quote.name).strip()
                if name and name.upper() != code.upper():
                    return name
        except Exception as e:
            logger.debug(f"realtime_quote 兜底查名失败 code={code}: {e}")
        return None

    def _resolve_name_with_ai(self, code: str, market: str) -> Optional[str]:
        """
        AI 兜底识别股票名称。
        注意：仅作为最后手段，要求返回简短名称；返回 UNKNOWN 则视为失败。
        """
        try:
            from src.config import get_config
            cfg = get_config()
            if not (cfg.gemini_api_key or cfg.openai_api_key):
                return None
            from src.analyzer import GeminiAnalyzer
            analyzer = GeminiAnalyzer()
            if not analyzer.is_available():
                return None

            prompt = (
                "你是股票代码识别器。请根据股票代码返回该股票常用中文名或英文公司简称。"
                "如果无法确定，返回 UNKNOWN。只输出名称，不要解释。\n"
                f"市场: {market}\n代码: {code}"
            )
            out = analyzer._call_api_with_retry(
                prompt,
                generation_config={"temperature": 0.0, "max_output_tokens": 64}
            )
            name = (out or "").strip().replace("`", "").replace('"', "")
            if not name:
                return None
            bad = {"UNKNOWN", "N/A", "NONE", "无法确定", "不确定"}
            if name.upper() in bad:
                return None
            if len(name) > 40:
                name = name[:40].strip()
            if name.upper() == code.upper():
                return None
            return name
        except Exception as e:
            logger.debug(f"AI 兜底查名失败 code={code}, market={market}: {e}")
            return None

    def _resolve_name_from_akshare_cache(self, code: str, market: str) -> Optional[str]:
        """
        从 akshare 全市场列表缓存中反查名称。
        注意：首次调用会拉全量列表，后续命中本地缓存。
        """
        market = (market or "CN").upper()
        with self._name_cache_lock:
            cache = self._ak_name_cache.setdefault(market, {})
            if cache:
                return cache.get(code)

            try:
                import akshare as ak
            except Exception as e:
                logger.debug(f"akshare 导入失败，无法兜底查名: {e}")
                return None

            try:
                if market == "CN":
                    # 包含大量 A股/ETF 标的
                    df = ak.stock_zh_a_spot_em()
                    if df is not None and not df.empty and '代码' in df.columns and '名称' in df.columns:
                        for _, row in df.iterrows():
                            c = str(row.get('代码', '')).strip().upper()
                            n = str(row.get('名称', '')).strip()
                            if c and n:
                                cache[c] = n
                elif market == "HK":
                    df = ak.stock_hk_spot_em()
                    if df is not None and not df.empty and '代码' in df.columns and '名称' in df.columns:
                        for _, row in df.iterrows():
                            c5 = str(row.get('代码', '')).strip().zfill(5)
                            n = str(row.get('名称', '')).strip()
                            if c5 and n:
                                cache[f"HK{c5}"] = n
                else:
                    if hasattr(ak, "stock_us_spot_em"):
                        df = ak.stock_us_spot_em()
                        if df is not None and not df.empty and '代码' in df.columns and '名称' in df.columns:
                            for _, row in df.iterrows():
                                c = str(row.get('代码', '')).strip().upper()
                                n = str(row.get('名称', '')).strip()
                                if c and n:
                                    cache[c] = n
            except Exception as e:
                logger.debug(f"akshare 全市场查名失败 market={market}: {e}")
                return None

            return cache.get(code)

    def backfill_watchlist_names(self, limit: int = 200) -> int:
        """
        对数据库中“名称缺失或等于代码”的自选股执行名称补全并落库。

        Returns:
            本次成功补全数量
        """
        updated_count = 0
        with self.get_session() as session:
            try:
                rows = session.execute(
                    select(Watchlist).order_by(Watchlist.order_key).limit(limit)
                ).scalars().all()

                for row in rows:
                    code = (row.code or "").strip().upper()
                    market = (row.market or "CN").strip().upper()
                    name = (row.name or "").strip()

                    if not code:
                        continue
                    if name and name.upper() != code:
                        continue

                    resolved = self._resolve_watchlist_name(code, market)
                    if resolved and resolved.upper() != code:
                        row.name = resolved
                        updated_count += 1

                if updated_count > 0:
                    session.commit()
                    logger.info(f"自选股名称补全完成，更新 {updated_count} 条")
                else:
                    session.rollback()
            except Exception as e:
                session.rollback()
                logger.error(f"自选股名称补全失败: {e}")

        return updated_count
    
    def add_watchlist(
        self,
        code: str,
        name: Optional[str] = None,
        market: str = "CN",
        verified: bool = False
    ) -> Optional[Dict[str, Any]]:
        """
        添加自选股
        
        Args:
            code: 股票代码
            name: 股票名称（可选）
            market: 市场类型（CN/HK/US）
            verified: 名称是否已在上游完成验证（如名称搜索命中）
            
        Returns:
            Watchlist 字典，或 None（如果已存在）
        """
        market = (market or "CN").strip().upper()
        code = self._normalize_watchlist_code(code, market)
        input_name = (name or "").strip()
        if input_name and input_name.upper() == code:
            input_name = ""
        
        # 优先尝试从数据源反查标准名称
        resolved_from_source = self._resolve_watchlist_name(code, market)
        if resolved_from_source and resolved_from_source.upper() == code:
            resolved_from_source = None

        if verified and input_name:
            # 调用方已完成名称匹配验证（例如通过名称搜索命中）
            resolved_name = input_name
        else:
            resolved_name = resolved_from_source or input_name or code

        # 允许降级使用代码入库，避免外部名称源短时不可用导致无法添加港股/美股
        if resolved_name.upper() == code:
            logger.warning(f"添加自选股降级为代码名: code={code}, market={market}")

        with self.get_session() as session:
            try:
                # 检查是否已存在
                existing = session.execute(
                    select(Watchlist).where(Watchlist.code == code)
                ).scalar_one_or_none()
                
                if existing:
                    return None
                
                # 获取最大的 order_key
                max_order = session.execute(
                    select(Watchlist).order_by(desc(Watchlist.order_key)).limit(1)
                ).scalar_one_or_none()
                
                next_order = (max_order.order_key + 1) if max_order else 0
                
                # 创建新的自选股
                watchlist = Watchlist(
                    code=code,
                    name=resolved_name,
                    market=market,
                    order_key=next_order
                )
                session.add(watchlist)
                session.commit()
                
                # 返回字典而不是对象
                return watchlist.to_dict()
            except IntegrityError:
                session.rollback()
                return None
            except Exception as e:
                session.rollback()
                logger.error(f"添加自选股失败: {e}")
                return None
    
    def remove_watchlist(self, code: str) -> bool:
        """
        删除自选股
        
        Args:
            code: 股票代码
            
        Returns:
            是否删除成功
        """
        with self.get_session() as session:
            try:
                watchlist = session.execute(
                    select(Watchlist).where(Watchlist.code == code)
                ).scalar_one_or_none()
                
                if watchlist:
                    session.delete(watchlist)
                    session.commit()
                    return True
                return False
            except Exception as e:
                session.rollback()
                logger.error(f"删除自选股失败: {e}")
                return False

    def update_watchlist_name(self, code: str, name: str) -> bool:
        """
        更新自选股名称。

        Args:
            code: 股票代码（如 600519 / HK00700）
            name: 股票名称

        Returns:
            是否更新成功
        """
        code = (code or "").strip().upper()
        name = (name or "").strip()
        if not code or not name:
            return False

        with self.get_session() as session:
            try:
                row = session.execute(
                    select(Watchlist).where(Watchlist.code == code)
                ).scalar_one_or_none()

                if row is None:
                    return False

                if row.name == name:
                    return True

                row.name = name
                session.commit()
                return True
            except Exception as e:
                session.rollback()
                logger.error(f"更新自选股名称失败: code={code}, error={e}")
                return False
    
    def get_watchlist(self, market: Optional[str] = None) -> List[Watchlist]:
        """
        获取自选股列表
        
        Args:
            market: 市场筛选（CN/HK/US，为 None 时返回全部）
            
        Returns:
            Watchlist 对象列表
        """
        with self.get_session() as session:
            query = select(Watchlist)
            
            if market:
                query = query.where(Watchlist.market == market)
            
            results = session.execute(
                query.order_by(Watchlist.order_key, Watchlist.created_at)
            ).scalars().all()
            
            return list(results)
    
    def get_watchlist_grouped(self) -> Dict[str, List[Dict[str, Any]]]:
        """
        按市场分组获取自选股列表
        
        Returns:
            {'CN': [...], 'HK': [...], 'US': [...]}
        """
        watchlist = self.get_watchlist()
        grouped: Dict[str, List[Dict[str, Any]]] = {'CN': [], 'HK': [], 'US': []}
        
        for item in watchlist:
            market = item.market or 'CN'
            if market not in grouped:
                grouped[market] = []
            # 如果已经是字典，直接使用；否则调用 to_dict()
            if isinstance(item, dict):
                grouped[market].append(item)
            else:
                grouped[market].append(item.to_dict())
        
        return grouped

    # ==================== Portfolio Holding 操作 ====================

    @staticmethod
    def _validate_holding_values(avg_cost: float, weight_pct: float) -> Optional[str]:
        if avg_cost is None:
            return "持仓成本不能为空"
        if weight_pct is None or weight_pct <= 0 or weight_pct > 100:
            return "持仓比例必须在 (0, 100] 范围内"
        return None

    def save_holding_history(
        self,
        owner_key: str,
        code: str,
        name: str,
        market: str,
        action: str,
        old_avg_cost: Optional[float],
        new_avg_cost: Optional[float],
        old_shares: Optional[float],
        new_shares: Optional[float],
        old_weight_pct: Optional[float],
        new_weight_pct: Optional[float],
        trade_price: Optional[float] = None,
        trade_shares: Optional[float] = None,
        realized_pnl: Optional[float] = None,
        operator: Optional[str] = None,
    ) -> bool:
        """
        保存持仓变更历史快照。
        """
        with self.get_session() as session:
            try:
                record = PortfolioHoldingHistory(
                    owner_key=owner_key,
                    code=code,
                    name=name,
                    market=market,
                    action=action,
                    old_avg_cost=old_avg_cost,
                    new_avg_cost=new_avg_cost,
                    old_shares=old_shares,
                    new_shares=new_shares,
                    old_weight_pct=old_weight_pct,
                    new_weight_pct=new_weight_pct,
                    trade_price=trade_price,
                    trade_shares=trade_shares,
                    realized_pnl=realized_pnl,
                    operator=operator or owner_key,
                    changed_at=datetime.now(),
                )
                session.add(record)
                session.commit()
                return True
            except Exception as e:
                session.rollback()
                logger.error(f"保存持仓历史失败: owner={owner_key}, code={code}, error={e}")
                return False

    def upsert_holding(
        self,
        owner_key: str,
        code: str,
        name: Optional[str],
        market: str,
        avg_cost: float,
        shares: Optional[float],
        weight_pct: float,
        operator: Optional[str] = None,
        verified: bool = False,
    ) -> Dict[str, Any]:
        """
        新增或更新用户持仓（写入 current，并记录 history）。
        """
        owner_key = (owner_key or "").strip().lower()
        if not owner_key:
            return {"success": False, "error": "owner_key 不能为空"}

        market = (market or "CN").strip().upper()
        code = self._normalize_watchlist_code(code, market)
        err = self._validate_holding_values(avg_cost, weight_pct)
        if err:
            return {"success": False, "error": err}

        resolved_name = (name or "").strip()
        if not resolved_name or resolved_name.upper() == code:
            resolved_name = self._resolve_watchlist_name(code, market) or ""
        if verified and name and name.strip():
            resolved_name = name.strip()
        if not resolved_name or resolved_name.upper() == code:
            return {"success": False, "error": f"无法确认股票名称: {code}"}

        with self.get_session() as session:
            try:
                existing = session.execute(
                    select(PortfolioHoldingCurrent).where(
                        and_(
                            PortfolioHoldingCurrent.owner_key == owner_key,
                            PortfolioHoldingCurrent.code == code
                        )
                    )
                ).scalar_one_or_none()

                action = "add"
                old_avg_cost = None
                old_weight = None
                old_shares = None
                if existing:
                    action = "update"
                    old_avg_cost = existing.avg_cost
                    old_weight = existing.weight_pct
                    old_shares = existing.shares
                    existing.name = resolved_name
                    existing.market = market
                    existing.avg_cost = float(avg_cost)
                    existing.shares = float(shares) if shares is not None else existing.shares
                    existing.weight_pct = float(weight_pct)
                    existing.updated_at = datetime.now()
                    row = existing
                else:
                    row = PortfolioHoldingCurrent(
                        owner_key=owner_key,
                        code=code,
                        name=resolved_name,
                        market=market,
                        avg_cost=float(avg_cost),
                        shares=float(shares) if shares is not None else None,
                        weight_pct=float(weight_pct),
                        created_at=datetime.now(),
                        updated_at=datetime.now(),
                    )
                    session.add(row)
                    session.flush()

                history = PortfolioHoldingHistory(
                    owner_key=owner_key,
                    code=code,
                    name=resolved_name,
                    market=market,
                    action=action,
                    old_avg_cost=old_avg_cost,
                    new_avg_cost=float(avg_cost),
                    old_shares=old_shares,
                    new_shares=float(shares) if shares is not None else row.shares,
                    old_weight_pct=old_weight,
                    new_weight_pct=float(weight_pct),
                    operator=operator or owner_key,
                    changed_at=datetime.now(),
                )
                session.add(history)
                session.commit()

                return {"success": True, "action": action, "data": row.to_dict()}
            except Exception as e:
                session.rollback()
                logger.error(f"upsert_holding 失败: owner={owner_key}, code={code}, error={e}")
                return {"success": False, "error": str(e)}

    def remove_holding(self, owner_key: str, code: str, operator: Optional[str] = None) -> Dict[str, Any]:
        """
        删除用户持仓，并写入历史快照。
        """
        owner_key = (owner_key or "").strip().lower()
        code = (code or "").strip().upper()
        if not owner_key or not code:
            return {"success": False, "error": "owner_key/code 不能为空"}

        with self.get_session() as session:
            try:
                row = session.execute(
                    select(PortfolioHoldingCurrent).where(
                        and_(
                            PortfolioHoldingCurrent.owner_key == owner_key,
                            PortfolioHoldingCurrent.code == code
                        )
                    )
                ).scalar_one_or_none()
                if row is None:
                    return {"success": False, "error": "持仓不存在"}

                data = row.to_dict()
                session.delete(row)
                session.flush()

                history = PortfolioHoldingHistory(
                    owner_key=owner_key,
                    code=data["code"],
                    name=data.get("name"),
                    market=data["market"],
                    action="remove",
                    old_avg_cost=data.get("avg_cost"),
                    new_avg_cost=None,
                    old_shares=data.get("shares"),
                    new_shares=None,
                    old_weight_pct=data.get("weight_pct"),
                    new_weight_pct=None,
                    operator=operator or owner_key,
                    changed_at=datetime.now(),
                )
                session.add(history)
                session.commit()
                return {"success": True, "data": data}
            except Exception as e:
                session.rollback()
                logger.error(f"remove_holding 失败: owner={owner_key}, code={code}, error={e}")
                return {"success": False, "error": str(e)}

    def sell_holding(
        self,
        owner_key: str,
        code: str,
        sell_price: float,
        sell_shares: float,
        operator: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        卖出持仓：自动扣减股数，按比例更新仓位；部分卖出不改变持仓成本。
        """
        owner_key = (owner_key or "").strip().lower()
        code = (code or "").strip().upper()
        if not owner_key or not code:
            return {"success": False, "error": "owner_key/code 不能为空"}
        if sell_price is None or sell_price <= 0:
            return {"success": False, "error": "卖出价格必须大于 0"}
        if sell_shares is None or sell_shares <= 0:
            return {"success": False, "error": "卖出股数必须大于 0"}

        with self.get_session() as session:
            try:
                row = session.execute(
                    select(PortfolioHoldingCurrent).where(
                        and_(
                            PortfolioHoldingCurrent.owner_key == owner_key,
                            PortfolioHoldingCurrent.code == code
                        )
                    )
                ).scalar_one_or_none()
                if row is None:
                    return {"success": False, "error": "持仓不存在"}

                old_shares = float(row.shares or 0.0)
                if old_shares <= 0:
                    return {"success": False, "error": "当前持仓未记录股数，请先用 /position set 补充股数后再卖出"}
                if sell_shares - old_shares > 1e-8:
                    return {"success": False, "error": f"卖出股数超过持仓股数（持仓: {old_shares:g}）"}

                old_avg_cost = float(row.avg_cost or 0.0)
                old_weight = float(row.weight_pct or 0.0)
                remain_shares = old_shares - float(sell_shares)
                realized_pnl = (float(sell_price) - old_avg_cost) * float(sell_shares)

                if remain_shares <= 1e-8:
                    data = row.to_dict()
                    session.delete(row)
                    session.flush()
                    history = PortfolioHoldingHistory(
                        owner_key=owner_key,
                        code=data["code"],
                        name=data.get("name"),
                        market=data["market"],
                        action="sell_all",
                        old_avg_cost=old_avg_cost,
                        new_avg_cost=None,
                        old_shares=old_shares,
                        new_shares=0.0,
                        old_weight_pct=old_weight,
                        new_weight_pct=0.0,
                        trade_price=float(sell_price),
                        trade_shares=float(sell_shares),
                        realized_pnl=float(realized_pnl),
                        operator=operator or owner_key,
                        changed_at=datetime.now(),
                    )
                    session.add(history)
                    session.commit()
                    return {
                        "success": True,
                        "action": "sell_all",
                        "data": data,
                        "realized_pnl": float(realized_pnl),
                        "remain_shares": 0.0,
                        "remain_weight_pct": 0.0,
                        "avg_cost": old_avg_cost,
                    }

                remain_ratio = remain_shares / old_shares
                new_weight = max(0.0, old_weight * remain_ratio)

                row.shares = float(remain_shares)
                row.weight_pct = float(new_weight)
                row.updated_at = datetime.now()

                history = PortfolioHoldingHistory(
                    owner_key=owner_key,
                    code=row.code,
                    name=row.name,
                    market=row.market,
                    action="sell",
                    old_avg_cost=old_avg_cost,
                    new_avg_cost=old_avg_cost,
                    old_shares=old_shares,
                    new_shares=float(remain_shares),
                    old_weight_pct=old_weight,
                    new_weight_pct=float(new_weight),
                    trade_price=float(sell_price),
                    trade_shares=float(sell_shares),
                    realized_pnl=float(realized_pnl),
                    operator=operator or owner_key,
                    changed_at=datetime.now(),
                )
                session.add(history)
                session.commit()
                return {
                    "success": True,
                    "action": "sell",
                    "data": row.to_dict(),
                    "realized_pnl": float(realized_pnl),
                    "remain_shares": float(remain_shares),
                    "remain_weight_pct": float(new_weight),
                    "avg_cost": old_avg_cost,
                }
            except Exception as e:
                session.rollback()
                logger.error(f"sell_holding 失败: owner={owner_key}, code={code}, error={e}")
                return {"success": False, "error": str(e)}

    def list_holdings(self, owner_key: str, market: Optional[str] = None) -> List[PortfolioHoldingCurrent]:
        """
        列出用户当前持仓。
        """
        owner_key = (owner_key or "").strip().lower()
        if not owner_key:
            return []

        with self.get_session() as session:
            query = select(PortfolioHoldingCurrent).where(PortfolioHoldingCurrent.owner_key == owner_key)
            if market:
                query = query.where(PortfolioHoldingCurrent.market == market.upper())
            rows = session.execute(
                query.order_by(desc(PortfolioHoldingCurrent.weight_pct), PortfolioHoldingCurrent.code)
            ).scalars().all()
            return list(rows)

    def get_holding(self, owner_key: str, code: str) -> Optional[PortfolioHoldingCurrent]:
        """
        获取用户单只持仓。
        """
        owner_key = (owner_key or "").strip().lower()
        code = (code or "").strip().upper()
        if not owner_key or not code:
            return None

        with self.get_session() as session:
            return session.execute(
                select(PortfolioHoldingCurrent).where(
                    and_(
                        PortfolioHoldingCurrent.owner_key == owner_key,
                        PortfolioHoldingCurrent.code == code
                    )
                )
            ).scalar_one_or_none()

    def get_portfolio_profile(
        self,
        owner_key: str,
        latest_prices: Optional[Dict[str, float]] = None,
        top_n: int = 5,
    ) -> Dict[str, Any]:
        """
        生成用户组合画像：总仓位、市场分布、集中度、成本偏离等。
        """
        latest_prices = latest_prices or {}
        holdings = self.list_holdings(owner_key)
        if not holdings:
            return {
                "owner_key": (owner_key or "").strip().lower(),
                "holding_count": 0,
                "total_weight_pct": 0.0,
                "market_distribution": {"CN": 0.0, "HK": 0.0, "US": 0.0},
                "max_single_weight_pct": 0.0,
                "concentration_top3_pct": 0.0,
                "top_holdings": [],
                "cost_deviation_stats": {"profit_count": 0, "loss_count": 0, "flat_count": 0},
                "overall_pnl_weighted_pct": None,
            }

        total_weight = 0.0
        by_market = {"CN": 0.0, "HK": 0.0, "US": 0.0}
        top_holdings: List[Dict[str, Any]] = []
        weighted_pnl_sum = 0.0
        pnl_weight_sum = 0.0
        profit_count = 0
        loss_count = 0
        flat_count = 0

        for row in holdings:
            weight = float(row.weight_pct or 0.0)
            total_weight += weight
            by_market[row.market] = by_market.get(row.market, 0.0) + weight

            current_price = latest_prices.get(row.code)
            pnl_pct = None
            if current_price and row.avg_cost and row.avg_cost > 0:
                pnl_pct = (float(current_price) - float(row.avg_cost)) / float(row.avg_cost) * 100.0
                weighted_pnl_sum += pnl_pct * weight
                pnl_weight_sum += weight
                if pnl_pct > 1e-8:
                    profit_count += 1
                elif pnl_pct < -1e-8:
                    loss_count += 1
                else:
                    flat_count += 1
            else:
                flat_count += 1

            top_holdings.append({
                "code": row.code,
                "name": row.name,
                "market": row.market,
                "avg_cost": float(row.avg_cost),
                "weight_pct": weight,
                "current_price": float(current_price) if current_price is not None else None,
                "pnl_pct": pnl_pct,
                "weight_contribution_pct": (pnl_pct * weight / 100.0) if pnl_pct is not None else None,
            })

        top_holdings.sort(key=lambda x: (-x["weight_pct"], x["code"]))
        top_n_rows = top_holdings[:max(1, top_n)]
        top3_weight = sum(x["weight_pct"] for x in top_holdings[:3])
        max_single = max((x["weight_pct"] for x in top_holdings), default=0.0)

        return {
            "owner_key": (owner_key or "").strip().lower(),
            "holding_count": len(holdings),
            "total_weight_pct": round(total_weight, 4),
            "market_distribution": {k: round(v, 4) for k, v in by_market.items()},
            "max_single_weight_pct": round(max_single, 4),
            "concentration_top3_pct": round(top3_weight, 4),
            "top_holdings": top_n_rows,
            "cost_deviation_stats": {
                "profit_count": profit_count,
                "loss_count": loss_count,
                "flat_count": flat_count,
            },
            "overall_pnl_weighted_pct": round(weighted_pnl_sum / pnl_weight_sum, 4) if pnl_weight_sum > 0 else None,
        }

    def get_portfolio_user_config(self, owner_key: str) -> Dict[str, Any]:
        owner_key = (owner_key or "").strip().lower()
        if not owner_key:
            return {"owner_key": "", "total_asset_cny": None, "usd_cny": 6.94, "hkd_cny": 0.888}

        with self.get_session() as session:
            row = session.execute(
                select(PortfolioUserConfig).where(PortfolioUserConfig.owner_key == owner_key)
            ).scalar_one_or_none()
            if row is None:
                return {"owner_key": owner_key, "total_asset_cny": None, "usd_cny": 6.94, "hkd_cny": 0.888}
            data = row.to_dict()
            data["usd_cny"] = float(data.get("usd_cny") or 6.94)
            data["hkd_cny"] = float(data.get("hkd_cny") or 0.888)
            return data

    def upsert_portfolio_user_config(
        self,
        owner_key: str,
        total_asset_cny: Optional[float] = None,
        usd_cny: Optional[float] = None,
        hkd_cny: Optional[float] = None,
    ) -> Dict[str, Any]:
        owner_key = (owner_key or "").strip().lower()
        if not owner_key:
            return {"success": False, "error": "owner_key 不能为空"}

        with self.get_session() as session:
            try:
                row = session.execute(
                    select(PortfolioUserConfig).where(PortfolioUserConfig.owner_key == owner_key)
                ).scalar_one_or_none()
                if row is None:
                    row = PortfolioUserConfig(
                        owner_key=owner_key,
                        total_asset_cny=float(total_asset_cny) if total_asset_cny is not None else None,
                        usd_cny=float(usd_cny) if usd_cny is not None else 6.94,
                        hkd_cny=float(hkd_cny) if hkd_cny is not None else 0.888,
                        updated_at=datetime.now(),
                    )
                    session.add(row)
                else:
                    if total_asset_cny is not None:
                        row.total_asset_cny = float(total_asset_cny)
                    if usd_cny is not None:
                        row.usd_cny = float(usd_cny)
                    if hkd_cny is not None:
                        row.hkd_cny = float(hkd_cny)
                    row.updated_at = datetime.now()

                session.commit()
                return {"success": True, "data": row.to_dict()}
            except Exception as e:
                session.rollback()
                logger.error(f"upsert_portfolio_user_config 失败: owner={owner_key}, error={e}")
                return {"success": False, "error": str(e)}
    
    def update_watchlist_order(self, codes: List[str]) -> bool:
        """
        批量更新自选股排序
        
        Args:
            codes: 按顺序的股票代码列表
            
        Returns:
            是否更新成功
        """
        with self.get_session() as session:
            try:
                for idx, code in enumerate(codes):
                    watchlist = session.execute(
                        select(Watchlist).where(Watchlist.code == code)
                    ).scalar_one_or_none()
                    
                    if watchlist:
                        watchlist.order_key = idx
                
                session.commit()
                return True
            except Exception as e:
                session.rollback()
                logger.error(f"更新自选股排序失败: {e}")
                return False
    
    def save_daily_data(
        self, 
        df: pd.DataFrame, 
        code: str,
        data_source: str = "Unknown"
    ) -> int:
        """
        保存日线数据到数据库
        
        策略：
        - 使用 UPSERT 逻辑（存在则更新，不存在则插入）
        - 跳过已存在的数据，避免重复
        
        Args:
            df: 包含日线数据的 DataFrame
            code: 股票代码
            data_source: 数据来源名称
            
        Returns:
            新增/更新的记录数
        """
        if df is None or df.empty:
            logger.warning(f"保存数据为空，跳过 {code}")
            return 0
        
        saved_count = 0
        
        with self.get_session() as session:
            try:
                for _, row in df.iterrows():
                    # 解析日期
                    row_date = row.get('date')
                    if isinstance(row_date, str):
                        row_date = datetime.strptime(row_date, '%Y-%m-%d').date()
                    elif isinstance(row_date, datetime):
                        row_date = row_date.date()
                    elif isinstance(row_date, pd.Timestamp):
                        row_date = row_date.date()
                    
                    # 检查是否已存在
                    existing = session.execute(
                        select(StockDaily).where(
                            and_(
                                StockDaily.code == code,
                                StockDaily.date == row_date
                            )
                        )
                    ).scalar_one_or_none()
                    
                    if existing:
                        # 更新现有记录
                        existing.open = row.get('open')
                        existing.high = row.get('high')
                        existing.low = row.get('low')
                        existing.close = row.get('close')
                        existing.volume = row.get('volume')
                        existing.amount = row.get('amount')
                        existing.pct_chg = row.get('pct_chg')
                        existing.ma5 = row.get('ma5')
                        existing.ma10 = row.get('ma10')
                        existing.ma20 = row.get('ma20')
                        existing.volume_ratio = row.get('volume_ratio')
                        existing.data_source = data_source
                        existing.updated_at = datetime.now()
                    else:
                        # 创建新记录
                        record = StockDaily(
                            code=code,
                            date=row_date,
                            open=row.get('open'),
                            high=row.get('high'),
                            low=row.get('low'),
                            close=row.get('close'),
                            volume=row.get('volume'),
                            amount=row.get('amount'),
                            pct_chg=row.get('pct_chg'),
                            ma5=row.get('ma5'),
                            ma10=row.get('ma10'),
                            ma20=row.get('ma20'),
                            volume_ratio=row.get('volume_ratio'),
                            data_source=data_source,
                        )
                        session.add(record)
                        saved_count += 1
                
                session.commit()
                logger.info(f"保存 {code} 数据成功，新增 {saved_count} 条")
                
            except Exception as e:
                session.rollback()
                logger.error(f"保存 {code} 数据失败: {e}")
                raise
        
        return saved_count
    
    def get_analysis_context(
        self, 
        code: str,
        target_date: Optional[date] = None
    ) -> Optional[Dict[str, Any]]:
        """
        获取分析所需的上下文数据
        
        返回今日数据 + 昨日数据的对比信息
        
        Args:
            code: 股票代码
            target_date: 目标日期（默认今天）
            
        Returns:
            包含今日数据、昨日对比等信息的字典
        """
        if target_date is None:
            target_date = date.today()
        
        # 获取最近2天数据
        recent_data = self.get_latest_data(code, days=2)
        
        if not recent_data:
            logger.warning(f"未找到 {code} 的数据")
            return None
        
        today_data = recent_data[0]
        yesterday_data = recent_data[1] if len(recent_data) > 1 else None
        
        context = {
            'code': code,
            'date': today_data.date.isoformat(),
            'today': today_data.to_dict(),
        }
        
        if yesterday_data:
            context['yesterday'] = yesterday_data.to_dict()
            
            # 计算相比昨日的变化
            if yesterday_data.volume and yesterday_data.volume > 0:
                context['volume_change_ratio'] = round(
                    today_data.volume / yesterday_data.volume, 2
                )
            
            if yesterday_data.close and yesterday_data.close > 0:
                context['price_change_ratio'] = round(
                    (today_data.close - yesterday_data.close) / yesterday_data.close * 100, 2
                )
            
            # 均线形态判断
            context['ma_status'] = self._analyze_ma_status(today_data)
        
        return context
    
    def _analyze_ma_status(self, data: StockDaily) -> str:
        """
        分析均线形态
        
        判断条件：
        - 多头排列：close > ma5 > ma10 > ma20
        - 空头排列：close < ma5 < ma10 < ma20
        - 震荡整理：其他情况
        """
        close = data.close or 0
        ma5 = data.ma5 or 0
        ma10 = data.ma10 or 0
        ma20 = data.ma20 or 0
        
        if close > ma5 > ma10 > ma20 > 0:
            return "多头排列 📈"
        elif close < ma5 < ma10 < ma20 and ma20 > 0:
            return "空头排列 📉"
        elif close > ma5 and ma5 > ma10:
            return "短期向好 🔼"
        elif close < ma5 and ma5 < ma10:
            return "短期走弱 🔽"
        else:
            return "震荡整理 ↔️"

    @staticmethod
    def _parse_published_date(value: Optional[str]) -> Optional[datetime]:
        """
        解析发布时间字符串（失败返回 None）
        """
        if not value:
            return None

        if isinstance(value, datetime):
            return value

        text = str(value).strip()
        if not text:
            return None

        # 优先尝试 ISO 格式
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            pass

        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d %H:%M",
            "%Y/%m/%d",
        ):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue

        return None

    @staticmethod
    def _safe_json_dumps(data: Any) -> str:
        """
        安全序列化为 JSON 字符串
        """
        try:
            return json.dumps(data, ensure_ascii=False, default=str)
        except Exception:
            return json.dumps(str(data), ensure_ascii=False)

    @staticmethod
    def _build_raw_result(result: Any) -> Dict[str, Any]:
        """
        生成完整分析结果字典
        """
        data = result.to_dict() if hasattr(result, "to_dict") else {}
        data.update({
            'data_sources': getattr(result, 'data_sources', ''),
            'raw_response': getattr(result, 'raw_response', None),
        })
        return data

    @staticmethod
    def _parse_sniper_value(value: Any) -> Optional[float]:
        """
        解析狙击点位数值
        """
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)

        text = str(value).replace(',', '').strip()
        if not text:
            return None

        match = re.search(r"-?\d+(?:\.\d+)?", text)
        if not match:
            return None
        try:
            return float(match.group())
        except ValueError:
            return None

    def _extract_sniper_points(self, result: Any) -> Dict[str, Optional[float]]:
        """
        抽取狙击点位数据
        """
        raw_points = {}
        if hasattr(result, "get_sniper_points"):
            raw_points = result.get_sniper_points() or {}

        return {
            "ideal_buy": self._parse_sniper_value(raw_points.get("ideal_buy")),
            "secondary_buy": self._parse_sniper_value(raw_points.get("secondary_buy")),
            "stop_loss": self._parse_sniper_value(raw_points.get("stop_loss")),
            "take_profit": self._parse_sniper_value(raw_points.get("take_profit")),
        }

    @staticmethod
    def _build_fallback_url_key(
        code: str,
        title: str,
        source: str,
        published_date: Optional[datetime]
    ) -> str:
        """
        生成无 URL 时的去重键（确保稳定且较短）
        """
        date_str = published_date.isoformat() if published_date else ""
        raw_key = f"{code}|{title}|{source}|{date_str}"
        digest = hashlib.md5(raw_key.encode("utf-8")).hexdigest()
        return f"no-url:{code}:{digest}"


# 便捷函数
def get_db() -> DatabaseManager:
    """获取数据库管理器实例的快捷方式"""
    return DatabaseManager.get_instance()


if __name__ == "__main__":
    # 测试代码
    logging.basicConfig(level=logging.DEBUG)
    
    db = get_db()
    
    print("=== 数据库测试 ===")
    print(f"数据库初始化成功")
    
    # 测试检查今日数据
    has_data = db.has_today_data('600519')
    print(f"茅台今日是否有数据: {has_data}")
    
    # 测试保存数据
    test_df = pd.DataFrame({
        'date': [date.today()],
        'open': [1800.0],
        'high': [1850.0],
        'low': [1780.0],
        'close': [1820.0],
        'volume': [10000000],
        'amount': [18200000000],
        'pct_chg': [1.5],
        'ma5': [1810.0],
        'ma10': [1800.0],
        'ma20': [1790.0],
        'volume_ratio': [1.2],
    })
    
    saved = db.save_daily_data(test_df, '600519', 'TestSource')
    print(f"保存测试数据: {saved} 条")
    
    # 测试获取上下文
    context = db.get_analysis_context('600519')
    print(f"分析上下文: {context}")
