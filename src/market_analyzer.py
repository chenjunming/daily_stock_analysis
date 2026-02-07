# -*- coding: utf-8 -*-
"""
===================================
大盘复盘分析模块
===================================

职责：
1. 获取大盘指数数据（上证、深证、创业板）
2. 搜索市场新闻形成复盘情报
3. 使用大模型生成每日大盘复盘报告
"""

import logging
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from src.time_utils import utc8_now
from typing import Optional, Dict, Any, List, Tuple

import pandas as pd

from src.config import get_config
from src.search_service import SearchService
from src.storage import get_db
from data_provider.base import DataFetcherManager

logger = logging.getLogger(__name__)


@dataclass
class MarketIndex:
    """大盘指数数据"""
    code: str                    # 指数代码
    name: str                    # 指数名称
    current: float = 0.0         # 当前点位
    change: float = 0.0          # 涨跌点数
    change_pct: float = 0.0      # 涨跌幅(%)
    open: float = 0.0            # 开盘点位
    high: float = 0.0            # 最高点位
    low: float = 0.0             # 最低点位
    prev_close: float = 0.0      # 昨收点位
    volume: float = 0.0          # 成交量（手）
    amount: float = 0.0          # 成交额（元）
    amplitude: float = 0.0       # 振幅(%)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'code': self.code,
            'name': self.name,
            'current': self.current,
            'change': self.change,
            'change_pct': self.change_pct,
            'open': self.open,
            'high': self.high,
            'low': self.low,
            'volume': self.volume,
            'amount': self.amount,
            'amplitude': self.amplitude,
        }


@dataclass
class MarketOverview:
    """市场概览数据"""
    date: str                           # 日期
    indices: List[MarketIndex] = field(default_factory=list)  # 主要指数
    up_count: int = 0                   # 上涨家数
    down_count: int = 0                 # 下跌家数
    flat_count: int = 0                 # 平盘家数
    limit_up_count: int = 0             # 涨停家数
    limit_down_count: int = 0           # 跌停家数
    total_amount: float = 0.0           # 两市成交额（亿元）
    # north_flow: float = 0.0           # 北向资金净流入（亿元）- 已废弃，接口不可用
    
    # 板块涨幅榜
    top_sectors: List[Dict] = field(default_factory=list)     # 涨幅前5板块
    bottom_sectors: List[Dict] = field(default_factory=list)  # 跌幅前5板块


class MarketAnalyzer:
    """
    大盘复盘分析器
    
    功能：
    1. 获取大盘指数实时行情
    2. 获取市场涨跌统计
    3. 获取板块涨跌榜
    4. 搜索市场新闻
    5. 生成大盘复盘报告
    """
    
    def __init__(self, search_service: Optional[SearchService] = None, analyzer=None):
        """
        初始化大盘分析器

        Args:
            search_service: 搜索服务实例
            analyzer: AI分析器实例（用于调用LLM）
        """
        self.config = get_config()
        self.search_service = search_service
        self.analyzer = analyzer
        self.data_manager = DataFetcherManager()
        self.db = get_db()

    @staticmethod
    def _infer_scope(query_text: str, market_hint: Optional[str] = None) -> List[str]:
        """
        根据用户问题推断市场范围。
        默认：未明确时使用 A股+港股；若明确提及美股则优先 US。
        """
        if market_hint in {"CN", "HK", "US"}:
            return [market_hint]

        text = (query_text or "").upper()
        has_cn = any(k in text for k in ["A股".upper(), "沪深".upper(), "上证".upper(), "深证".upper(), "CN"])
        has_hk = any(k in text for k in ["港股".upper(), "恒生".upper(), "H股".upper(), "HK"])
        has_us = any(k in text for k in ["美股".upper(), "纳指".upper(), "标普".upper(), "道指".upper(), "US", "今晚".upper(), "今夜".upper()])

        scope = []
        if has_cn:
            scope.append("CN")
        if has_hk:
            scope.append("HK")
        if has_us:
            scope.append("US")
        if scope:
            return scope
        return ["CN", "HK"]

    @staticmethod
    def _fmt_pct(value: Optional[float], digits: int = 1) -> str:
        if value is None:
            return "N/A"
        return f"{value:+.{digits}f}%"

    @staticmethod
    def _fmt_num(value: Optional[float], digits: int = 2) -> str:
        if value is None:
            return "N/A"
        return f"{value:.{digits}f}"

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        try:
            if value is None:
                return None
            return float(value)
        except Exception:
            return None

    @staticmethod
    def _infer_market_from_code(code: str) -> str:
        """按代码格式推断市场（CN/HK/US）。"""
        c = (code or "").strip().upper()
        if not c:
            return ""
        if c.startswith("HK") and c[2:].isdigit():
            return "HK"
        if c.isdigit() and len(c) == 5:
            return "HK"
        if c.isdigit() and len(c) == 6:
            return "CN"
        if re.match(r'^[A-Z]{1,5}(\.[A-Z])?$', c):
            return "US"
        return ""

    def _calc_rsi(self, close_series: pd.Series, period: int = 12) -> Optional[float]:
        try:
            if close_series is None or len(close_series) < period + 2:
                return None
            delta = close_series.diff()
            gain = delta.where(delta > 0, 0.0)
            loss = -delta.where(delta < 0, 0.0)
            avg_gain = gain.rolling(window=period).mean()
            avg_loss = loss.rolling(window=period).mean()
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))
            val = rsi.iloc[-1]
            return float(val) if pd.notna(val) else None
        except Exception:
            return None

    def _load_stock_signal_snapshot(self, code: str) -> Dict[str, Any]:
        """
        获取单票信号快照：价格相对20/50均线、量能、RSI。
        """
        out = {
            "code": code,
            "name": code,
            "current": None,
            "ma20": None,
            "ma50": None,
            "pct_vs_ma20": None,
            "pct_vs_ma50": None,
            "volume_ratio": None,
            "rsi12": None,
        }
        try:
            df, _ = self.data_manager.get_daily_data(code, days=100)
            if df is None or df.empty:
                return out
            latest = df.iloc[-1]
            close = self._to_float(latest.get("close"))
            ma20 = self._to_float(latest.get("ma20"))
            ma50 = self._to_float(df["close"].rolling(window=50, min_periods=20).mean().iloc[-1])
            volume_ratio = self._to_float(latest.get("volume_ratio"))
            rsi12 = self._calc_rsi(df["close"], period=12)
            out.update(
                {
                    "current": close,
                    "ma20": ma20,
                    "ma50": ma50,
                    "volume_ratio": volume_ratio,
                    "rsi12": rsi12,
                }
            )
            if close and ma20:
                out["pct_vs_ma20"] = (close - ma20) / ma20 * 100
            if close and ma50:
                out["pct_vs_ma50"] = (close - ma50) / ma50 * 100

            stock_name = self.data_manager.get_stock_name(code)
            if stock_name:
                out["name"] = stock_name
        except Exception as e:
            logger.debug(f"[大盘] 获取 {code} 信号快照失败: {e}")
        return out

    def _get_recent_72h_points(self, code: str, limit: int = 2) -> str:
        """
        从本地新闻情报提取近72小时要点。
        """
        try:
            rows = self.db.get_recent_news(code=code, days=3, limit=limit)
            points = []
            for row in rows:
                title = self._normalize_event_text(getattr(row, "title", "") or "")
                if title and not self._looks_like_noise_event(title):
                    points.append(title)
            # 去重并截断
            dedup = []
            for p in points:
                if p not in dedup:
                    dedup.append(p)
            points = dedup[:limit]
            return "；".join(points) if points else "暂无明显事件"
        except Exception:
            return "暂无明显事件"

    @staticmethod
    def _normalize_event_text(text: str, max_len: int = 72) -> str:
        """清洗事件文本，尽量输出可读的一句话。"""
        s = (text or "").strip()
        if not s:
            return ""
        s = s.replace("\n", " ").replace("\r", " ")
        s = re.sub(r"https?://\S+", "", s)
        s = re.sub(r"\s+", " ", s)
        for junk in [
            "股票价格_行情_走势图_东方财富",
            "_东方财富",
            "_腾讯新闻",
            "_网易订阅",
            "_新浪财经",
            "_同花顺财经",
            "手机东方财富网",
            "股票行情",
            "行情中心",
        ]:
            s = s.replace(junk, "")
        # 去除末尾来源尾巴，如 "_XXX新闻"/"- XXX网"
        s = re.sub(r"[_\-]\s*[^_\-]{0,16}(新闻|网|订阅|财经)\s*$", "", s)
        # 去除多余来源分隔尾巴
        s = re.sub(r"[|｜]\s*[^|｜]{0,20}$", "", s)
        s = s.strip(" -|_；;，,")
        if len(s) > max_len:
            s = s[:max_len].rstrip() + "..."
        return s

    @staticmethod
    def _looks_like_noise_event(text: str) -> bool:
        s = (text or "").strip()
        if not s or len(s) < 6:
            return True
        noisy = ["东方财富", "行情", "走势图", "个股吧", "点击", "下载", "APP"]
        if any(k in s for k in noisy):
            return True
        if re.fullmatch(r"[\d\.\-\+\%\s:/]+", s):
            return True
        return False

    def _extract_news_event_points(self, news: List, limit: int = 3) -> List[str]:
        points: List[str] = []
        seen = set()
        for n in (news or [])[:20]:
            title = (getattr(n, "title", "") if hasattr(n, "title") else n.get("title", "")) or ""
            cleaned = self._normalize_event_text(title)
            if not cleaned or self._looks_like_noise_event(cleaned):
                continue
            key = re.sub(r"[\W_]+", "", cleaned).lower()
            if key in seen:
                continue
            seen.add(key)
            points.append(cleaned)
            if len(points) >= limit:
                break
        return points

    @staticmethod
    def _is_reliable_source(item: Any) -> bool:
        """
        判断是否来自相对可靠的信息源（用于市场快照）。
        """
        source = ""
        url = ""
        if hasattr(item, "source"):
            source = str(getattr(item, "source", "") or "").lower()
            url = str(getattr(item, "url", "") or "").lower()
        elif isinstance(item, dict):
            source = str(item.get("source", "") or "").lower()
            url = str(item.get("url", "") or "").lower()

        text = f"{source} {url}"
        unreliable_keywords = [
            "eastmoney", "10jqka", "jrj.com", "finance.sina", "qq.com",
            "netease", "sohu", "stockstar", "hexun", "xueqiu", "guba",
            "东方财富", "同花顺", "证券之星", "金融界", "网易", "腾讯", "股吧",
        ]
        if any(k in text for k in unreliable_keywords):
            return False

        reliable_keywords = [
            "reuters", "bloomberg", "wsj", "cnbc", "marketwatch", "investing.com",
            "federalreserve", "fed", "bls.gov", "bea.gov", "treasury.gov",
            "sec.gov", "nasdaq.com", "nyse.com", "ft.com", "yahoo.com",
            "财联社", "新华", "证券时报", "上交所", "深交所", "中证网",
        ]
        return any(k in text for k in reliable_keywords)

    def _select_reliable_news(self, news: List, scope: List[str], limit: int = 8) -> List[Any]:
        """
        优先挑选可信来源新闻，若不足再补充普通来源。
        """
        reliable = []
        normal = []
        seen = set()
        for n in news or []:
            title = (getattr(n, "title", "") if hasattr(n, "title") else n.get("title", "")) or ""
            cleaned = self._normalize_event_text(title)
            key = re.sub(r"[\W_]+", "", cleaned).lower()
            if not cleaned or self._looks_like_noise_event(cleaned) or not key or key in seen:
                continue
            seen.add(key)
            if self._is_reliable_source(n):
                reliable.append(n)
            else:
                normal.append(n)
        # US 单市场下只保留高可信来源，避免门户“行情站”标题污染
        if scope == ["US"]:
            return reliable[:limit]
        out = reliable[:limit]
        if len(out) < limit:
            out.extend(normal[: limit - len(out)])
        return out

    @staticmethod
    def _extract_json_text(raw: str) -> str:
        s = str(raw or "").strip()
        start = s.find("{")
        end = s.rfind("}")
        if start >= 0 and end > start:
            return s[start:end + 1]
        return s

    def _summarize_events_with_ai(self, scope: List[str], points: List[str]) -> Optional[str]:
        """
        使用 AI 对关键事件做结构化归纳，输出固定格式单行文本。
        """
        if not points or not self.analyzer or not hasattr(self.analyzer, "_call_api_with_retry"):
            return None
        try:
            market = "/".join(scope) if scope else "GLOBAL"
            prompt = (
                "你是市场情报编辑。请根据输入标题输出 JSON，禁止输出其它文本。\n"
                "要求：\n"
                "1) 仅使用输入信息，不得编造；\n"
                "2) 每类最多18个中文词；\n"
                "3) 若某类缺失写“暂无高置信信号”；\n"
                "4) 同义标题去重，优先保留更具体的表述。\n"
                "输出格式：{\"macro\":\"...\",\"earnings\":\"...\",\"risk\":\"...\"}\n\n"
                f"市场范围: {market}\n"
                "输入标题:\n- " + "\n- ".join(points[:8])
            )
            out = self.analyzer._call_api_with_retry(
                prompt,
                generation_config={"temperature": 0.0, "max_output_tokens": 220},
            )
            payload = json.loads(self._extract_json_text(str(out or "")))
            macro = self._normalize_event_text(str(payload.get("macro", "") or "暂无高置信信号"), max_len=72)
            earnings = self._normalize_event_text(str(payload.get("earnings", "") or "暂无高置信信号"), max_len=72)
            risk = self._normalize_event_text(str(payload.get("risk", "") or "暂无高置信信号"), max_len=72)
            text = f"[宏观] {macro}；[财报] {earnings}；[风险] {risk}"
            if text:
                return text
            return None
        except Exception as e:
            logger.debug(f"[大盘] AI 归纳关键市场信息失败: {e}")
            return None

    def _collect_owner_symbols(self, owner_key: Optional[str], scope: List[str], limit: int = 12) -> List[str]:
        """
        收集用户当前持仓代码（按市场范围过滤）。
        """
        if not owner_key:
            return []
        rows = self.db.list_holdings(owner_key)
        allowed = {m.upper() for m in (scope or [])}
        out: List[str] = []
        for row in rows:
            code = str(getattr(row, "code", "") or "").strip().upper()
            if not code:
                continue
            market = str(getattr(row, "market", "") or "").strip().upper()
            if not market:
                market = self._infer_market_from_code(code)
            if allowed and market not in allowed:
                continue
            if code not in out:
                out.append(code)
            if len(out) >= limit:
                break
        return out

    def _build_stock_72h_points_with_ai(
        self,
        scope: List[str],
        symbols: List[str],
        owner_key: Optional[str] = None,
    ) -> Tuple[Dict[str, str], str, str, str]:
        """
        一次性调用 AI 生成：
        1) 多个标的的“近72h要点”
        2) 宏观/财报/风险摘要
        """
        sym_list = []
        for s in symbols:
            code = str(s or "").strip().upper()
            if code and code not in sym_list:
                sym_list.append(code)

        if not sym_list:
            return {}, "近72h宏观/财报信息暂不可用", "规则", ""

        if not self.analyzer or not hasattr(self.analyzer, "_call_api_with_retry"):
            mapping = {s: "暂无近72h高置信要点（AI未启用）" for s in sym_list}
            return mapping, "近72h宏观/财报信息暂不可用（AI未启用）", "规则", ""

        try:
            market = "/".join(scope) if scope else "GLOBAL"
            holding_symbols = self._collect_owner_symbols(owner_key, scope=scope, limit=20)
            holding_text = ", ".join(holding_symbols) if holding_symbols else "N/A"
            prompt = (
                "请按给定标的，输出“宏观/财报/风险摘要 + 每个标的近72h要点 + 事件明细”。严格输出 JSON，不要输出其它文字。\n"
                "要求：\n"
                "1) 覆盖所有 symbols，每个 symbol 仅一条；\n"
                "2) 单条 12~28 字，强调“财报/指引/订单/监管/风险”之一；\n"
                "3) 无高置信信息写“暂无高置信事件”；\n"
                "4) 中文输出，日期用美东时间并尽量给出绝对日期（如 2026-02-06 ET）；\n"
                "5) macro/risk 给一句话，若无高置信信息则写“暂无高置信信号”；\n"
                "6) detail_md 参考格式：\n"
                "   - 近72h宏观/政策（2-3条）\n"
                "   - 关键日程变更提醒（1-2条）\n"
                "   - 持仓相关财报节点（覆盖 symbols）\n"
                "7) 不要编造具体数值，拿不准就写“需二次核验”；\n"
                "输出格式：{\"macro\":\"...\",\"risk\":\"...\",\"earnings\":[{\"symbol\":\"TSM\",\"event\":\"...\"}],\"items\":[{\"symbol\":\"TSM\",\"point\":\"...\"}],\"detail_md\":\"...\"}\n\n"
                f"市场范围: {market}\n"
                f"用户持仓symbols(参考): {holding_text}\n"
                f"待输出symbols(必须全覆盖): {', '.join(sym_list)}"
            )
            out = self.analyzer._call_api_with_retry(
                prompt,
                generation_config={"temperature": 0.0, "max_output_tokens": 900},
            )
            payload = json.loads(self._extract_json_text(str(out or "")))
            items = payload.get("items", [])
            earnings_rows = payload.get("earnings", [])
            mapping: Dict[str, str] = {}
            if isinstance(items, list):
                for row in items:
                    if not isinstance(row, dict):
                        continue
                    sym = str(row.get("symbol", "") or "").strip().upper()
                    point = self._normalize_event_text(str(row.get("point", "") or ""), max_len=52)
                    if sym and point:
                        mapping[sym] = point

            for s in sym_list:
                if s not in mapping:
                    mapping[s] = "暂无高置信事件"

            macro = self._normalize_event_text(str(payload.get("macro", "") or "暂无高置信信号"), max_len=72)
            risk = self._normalize_event_text(str(payload.get("risk", "") or "暂无高置信信号"), max_len=72)
            earning_parts: List[str] = []
            covered = set()
            if isinstance(earnings_rows, list):
                for row in earnings_rows[:20]:
                    if not isinstance(row, dict):
                        continue
                    sym = str(row.get("symbol", "") or "").strip().upper()
                    evt = self._normalize_event_text(str(row.get("event", "") or ""), max_len=44)
                    if not sym or not evt or sym in covered:
                        continue
                    covered.add(sym)
                    earning_parts.append(f"{sym}: {evt}")
            for sym in holding_symbols:
                if sym not in covered:
                    earning_parts.append(f"{sym}: 暂无关键财报催化")
            if not earning_parts:
                earning_parts = ["暂无高置信财报信号"]
            earnings_text = " | ".join(earning_parts[:8])
            event_summary = f"[宏观] {macro}；[财报] {earnings_text}；[风险] {risk}"
            detail_md = str(payload.get("detail_md", "") or "").strip()
            detail_md = detail_md[:3000] if detail_md else ""
            return mapping, event_summary, "AI", detail_md
        except Exception as e:
            logger.warning(f"[大盘] 批量生成个股72h要点失败，降级占位文案: {e}")
            mapping = {s: "暂无高置信事件" for s in sym_list}
            return mapping, "近72h宏观/财报信息暂不可用，请稍后重试", "规则", ""

    def _build_event_72h_summary(
        self,
        scope: List[str],
        owner_key: Optional[str],
        monitor_rows: Dict[str, List[Dict[str, Any]]],
    ) -> Tuple[str, str]:
        """
        按用户要求：直接让 AI 生成“当日/近72h宏观与财报事件”。
        不注入新闻来源明细，仅给市场范围与关注标的。
        """
        if not self.analyzer or not hasattr(self.analyzer, "_call_api_with_retry"):
            return "近72h宏观/财报信息暂不可用（AI未启用）", "规则"

        try:
            market = "/".join(scope) if scope else "GLOBAL"
            watch_symbols: List[str] = []
            for m in (scope or []):
                for r in monitor_rows.get(m, []):
                    c = str(r.get("code", "") or "").strip().upper()
                    if c and c not in watch_symbols:
                        watch_symbols.append(c)

            holding_symbols = self._collect_owner_symbols(owner_key, scope=scope, limit=12)
            if not holding_symbols:
                holding_symbols = watch_symbols[:6]

            symbols_text = ", ".join(holding_symbols) if holding_symbols else "N/A"
            focus_text = ", ".join(watch_symbols[:8]) if watch_symbols else "N/A"

            prompt = (
                "请直接给出“当日/近72h宏观与财报事件”摘要，严格输出 JSON，不要输出其它文字。\n"
                "要求：\n"
                "1) 关注市场范围内最重要的宏观与风险变化；\n"
                "2) 财报部分必须覆盖用户持仓个股（下面给出 symbols），每个symbol都要有一句；\n"
                "3) 若某symbol暂无关键财报，写“暂无关键财报催化”；\n"
                "4) 中文输出，简洁但可执行；\n"
                "5) 不要包含来源网址或媒体名。\n"
                "输出格式：\n"
                "{\"macro\":\"...\",\"risk\":\"...\",\"earnings\":[{\"symbol\":\"TSM\",\"event\":\"...\"}]}\n\n"
                f"市场范围: {market}\n"
                f"用户持仓symbols(必须覆盖): {symbols_text}\n"
                f"重点观察symbols: {focus_text}\n"
                "时间窗: 当日与近72小时"
            )

            out = self.analyzer._call_api_with_retry(
                prompt,
                generation_config={"temperature": 0.0, "max_output_tokens": 320},
            )
            payload = json.loads(self._extract_json_text(str(out or "")))
            macro = self._normalize_event_text(str(payload.get("macro", "") or "暂无高置信宏观信号"), max_len=72)
            risk = self._normalize_event_text(str(payload.get("risk", "") or "暂无高置信风险信号"), max_len=72)
            earnings_rows = payload.get("earnings", [])
            earning_parts: List[str] = []

            covered = set()
            if isinstance(earnings_rows, list):
                for row in earnings_rows[:20]:
                    if not isinstance(row, dict):
                        continue
                    sym = str(row.get("symbol", "") or "").strip().upper()
                    evt = self._normalize_event_text(str(row.get("event", "") or ""), max_len=44)
                    if not sym or not evt:
                        continue
                    if sym in covered:
                        continue
                    covered.add(sym)
                    earning_parts.append(f"{sym}: {evt}")

            for sym in holding_symbols:
                if sym not in covered:
                    earning_parts.append(f"{sym}: 暂无关键财报催化")

            if not earning_parts:
                earning_parts = ["暂无高置信财报信号"]

            earnings_text = " | ".join(earning_parts[:8])
            return f"[宏观] {macro}；[财报] {earnings_text}；[风险] {risk}", "AI"
        except Exception as e:
            logger.warning(f"[大盘] AI 生成宏观/财报事件失败，降级规则文案: {e}")
            return "近72h宏观/财报信息暂不可用，请稍后重试", "规则"

    def _get_vix_value(self) -> Optional[float]:
        """优先使用 yfinance 获取 VIX。"""
        try:
            import yfinance as yf
            hist = yf.Ticker("^VIX").history(period="5d")
            if hist is not None and not hist.empty and "Close" in hist.columns:
                val = self._to_float(hist["Close"].iloc[-1])
                if val is not None:
                    return val
        except Exception as e:
            logger.debug(f"[大盘] yfinance 获取 VIX 失败: {e}")
        return None

    def _build_priority_a_targets(self, scope: List[str]) -> Dict[str, List[Dict[str, str]]]:
        """
        构建优先级A监控池。
        - 美股默认 VRT/ANET/MRVL/DELL
        - A/H 默认从自选股按顺序取前4只（不足则补空）
        """
        out: Dict[str, List[Dict[str, str]]] = {}
        for market in scope:
            if market == "US":
                out[market] = [
                    {"code": "VRT", "name": "Vertiv"},
                    {"code": "ANET", "name": "Arista"},
                    {"code": "MRVL", "name": "Marvell"},
                    {"code": "DELL", "name": "Dell"},
                ]
                continue

            rows = self.db.get_watchlist(market=market)
            picks = []
            for row in rows[:4]:
                picks.append({"code": row.code, "name": row.name or row.code})
            out[market] = picks
        return out

    def _conclusion_and_action(self, snap: Dict[str, Any]) -> Tuple[str, str]:
        """
        根据量价+均线+RSI给出结论与动作。
        """
        ma20 = snap.get("pct_vs_ma20")
        ma50 = snap.get("pct_vs_ma50")
        vol = snap.get("volume_ratio")
        rsi = snap.get("rsi12")

        if ma20 is None or ma50 is None:
            return "数据不足", "观察"
        if ma20 < 0 and ma50 < 0 and (rsi is not None and rsi < 45):
            return "弱势下行", "减仓/回避新仓"
        if ma20 > 0 and ma50 > 0 and (rsi is None or 45 <= rsi <= 65) and (vol is None or vol >= 0.9):
            return "趋势健康", "回踩20日线分批买"
        if ma20 > 0 and ma50 > 0 and rsi is not None and rsi > 70:
            return "高位偏热", "只持有不追高"
        return "震荡分歧", "等待触发器"

    def _build_holding_rows(
        self,
        owner_key: Optional[str],
        allowed_markets: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        生成当前持仓建议表数据。
        """
        if not owner_key:
            return []
        rows = self.db.list_holdings(owner_key)
        allowed_set = {m.upper() for m in (allowed_markets or []) if (m or "").strip()}
        result: List[Dict[str, Any]] = []
        for row in rows:
            row_market = (getattr(row, "market", "") or "").strip().upper()
            if not row_market:
                row_market = self._infer_market_from_code(getattr(row, "code", ""))
            if allowed_set and row_market and row_market not in allowed_set:
                continue
            if allowed_set and not row_market:
                # 在指定市场视图下，无法识别市场的持仓不展示，避免串市场
                continue

            snap = self._load_stock_signal_snapshot(row.code)
            current = snap.get("current")
            avg_cost = self._to_float(getattr(row, "avg_cost", None))
            pnl = None
            if current is not None and avg_cost and avg_cost > 0:
                pnl = (current - avg_cost) / avg_cost * 100

            ma20 = snap.get("pct_vs_ma20")
            ma50 = snap.get("pct_vs_ma50")
            vol = snap.get("volume_ratio")
            rsi = snap.get("rsi12")

            if ma20 is not None and ma50 is not None and ma20 > 0 and ma50 > 0 and (rsi is None or 45 <= rsi <= 62):
                action = "加/持"
                trigger = f"回踩20DMA附近({self._fmt_num(snap.get('ma20'))})止跌且量比>=1.0"
            elif ma20 is not None and ma50 is not None and ma20 < 0 and ma50 < 0 and (rsi is not None and rsi < 45):
                action = "减"
                trigger = f"跌破50DMA({self._fmt_num(snap.get('ma50'))})且量比>=1.2继续减仓"
            else:
                action = "持"
                trigger = f"区间震荡，等待放量突破或回踩确认（RSI={self._fmt_num(rsi, 1)}）"

            result.append(
                {
                    "code": row.code,
                    "name": getattr(row, "name", "") or row.code,
                    "market": getattr(row, "market", "") or "",
                    "avg_cost": avg_cost,
                    "weight_pct": self._to_float(getattr(row, "weight_pct", None)),
                    "current": current,
                    "pnl_pct": pnl,
                    "action": action,
                    "trigger": trigger,
                    "volume_ratio": vol,
                    "rsi12": rsi,
                }
            )
        return result

    def _index_above_ma50(self, code: str) -> str:
        snap = self._load_stock_signal_snapshot(code)
        ma50 = snap.get("pct_vs_ma50")
        if ma50 is None:
            return "N/A"
        return "是" if ma50 >= 0 else "否"

    def _render_market_quicklook(
        self,
        scope: List[str],
        overview: MarketOverview,
        news: List,
        monitor_rows: Dict[str, List[Dict[str, Any]]],
        event_72h: str,
        event_src: str,
    ) -> List[str]:
        lines = ["## ① 市场四键速览", ""]

        event_rows = self._parse_event_summary(event_72h)

        if "US" in scope and len(scope) == 1:
            spy_up = self._index_above_ma50("SPY")
            qqq_up = self._index_above_ma50("QQQ")
            soxx_up = self._index_above_ma50("SOXX")
            vix_value = self._get_vix_value()
            us_rows = monitor_rows.get("US", [])
            breadth_count = sum(1 for r in us_rows if (r.get("pct_vs_ma20") or -999) >= 0)
            breadth = f"{breadth_count}/{max(1, len(us_rows))} 在20DMA上方"

            lines.extend(
                [
                    f"- 标普/纳指/半导体在50DMA上方: SPY={spy_up} | QQQ={qqq_up} | SOXX={soxx_up}",
                    f"- VIX: {self._fmt_num(vix_value, 2)}",
                    f"- 市场宽度: {breadth}",
                    f"- 当日/近72h宏观与财报事件({event_src}):",
                    "| 类别 | 内容 |",
                    "|---|---|",
                    f"| 宏观 | {event_rows.get('macro', '暂无高置信信号')} |",
                    f"| 财报 | {event_rows.get('earnings', '暂无高置信信号')} |",
                    f"| 风险 | {event_rows.get('risk', '暂无高置信信号')} |",
                    "",
                ]
            )
            return lines

        # A/H 默认
        sh_up = self._index_above_ma50("000001")
        hsi_up = self._index_above_ma50("HSI")
        breadth = (
            f"上涨 {overview.up_count} / 下跌 {overview.down_count} / 涨停 {overview.limit_up_count} / 跌停 {overview.limit_down_count}"
            if (overview.up_count or overview.down_count or overview.limit_up_count or overview.limit_down_count)
            else "宽度数据不足"
        )
        lines.extend(
            [
                f"- 指数相对50DMA: 上证={sh_up} | 恒生={hsi_up}",
                f"- 成交额: {overview.total_amount:.0f}亿",
                f"- 市场宽度: {breadth}",
                f"- 近72h要点({event_src}):",
                "| 类别 | 内容 |",
                "|---|---|",
                f"| 宏观 | {event_rows.get('macro', '暂无高置信信号')} |",
                f"| 财报 | {event_rows.get('earnings', '暂无高置信信号')} |",
                f"| 风险 | {event_rows.get('risk', '暂无高置信信号')} |",
                "",
            ]
        )
        return lines

    @staticmethod
    def _parse_event_summary(summary: str) -> Dict[str, str]:
        """
        解析形如：
        [宏观] ...；[财报] ...；[风险] ...
        的摘要文本，回填到表格结构。
        """
        text = str(summary or "").strip()
        out = {"macro": "", "earnings": "", "risk": ""}
        if not text:
            return out

        macro = re.search(r"\[宏观\]\s*(.*?)(?=；\s*\[财报\]|$)", text)
        earnings = re.search(r"\[财报\]\s*(.*?)(?=；\s*\[风险\]|$)", text)
        risk = re.search(r"\[风险\]\s*(.*)$", text)

        if macro:
            out["macro"] = macro.group(1).strip()
        if earnings:
            out["earnings"] = earnings.group(1).strip()
        if risk:
            out["risk"] = risk.group(1).strip()

        # 兜底：若不是标准格式，把全文塞进宏观，避免空表
        if not any(out.values()):
            out["macro"] = text
        return out

    def _generate_operation_template(
        self,
        overview: MarketOverview,
        news: List,
        query_text: str = "",
        market_hint: Optional[str] = None,
        owner_key: Optional[str] = None,
    ) -> str:
        scope = self._infer_scope(query_text=query_text, market_hint=market_hint)
        scope_names = {"CN": "A股", "HK": "港股", "US": "美股"}
        scope_text = "/".join(scope_names.get(m, m) for m in scope)

        targets = self._build_priority_a_targets(scope)
        monitor_symbols: List[str] = []
        for market in scope:
            for item in targets.get(market, []):
                c = str(item.get("code", "") or "").strip().upper()
                if c and c not in monitor_symbols:
                    monitor_symbols.append(c)
        stock_72h_map, event_72h, event_src, event_detail_md = self._build_stock_72h_points_with_ai(
            scope=scope,
            symbols=monitor_symbols,
            owner_key=owner_key,
        )

        monitor_rows: Dict[str, List[Dict[str, Any]]] = {}
        for market in scope:
            rows: List[Dict[str, Any]] = []
            for item in targets.get(market, []):
                snap = self._load_stock_signal_snapshot(item["code"])
                snap["code"] = item["code"]
                snap["name"] = item.get("name") or snap.get("name") or item["code"]
                snap["news_72h"] = stock_72h_map.get(str(item["code"]).upper(), "暂无高置信事件")
                conclusion, action = self._conclusion_and_action(snap)
                snap["conclusion"] = conclusion
                snap["action"] = action
                rows.append(snap)
            monitor_rows[market] = rows

        lines = [
            f"## 📌 今日操作模板（{overview.date} | {scope_text}）",
            "",
        ]

        lines.extend(self._render_market_quicklook(scope, overview, news, monitor_rows, event_72h=event_72h, event_src=event_src))

        if event_detail_md:
            lines.append("## ①.1 当日/近72h宏观与财报事件明细")
            lines.append("")
            lines.append(event_detail_md)
            lines.append("")

        # ② 优先级A监控表
        lines.append("## ② 优先级A监控表")
        lines.append("")
        for market in scope:
            market_label = scope_names.get(market, market)
            lines.append(f"### {market_label} 优先级A")
            lines.append("| 标的 | 价格相对20/50日线 | 量能/RSI | 近72h要点 | 结论 | 具体动作 |")
            lines.append("|---|---|---|---|---|---|")
            rows = monitor_rows.get(market, [])
            if not rows:
                lines.append("| - | - | - | - | 暂无监控标的 | 先补充自选股 |")
                lines.append("")
                continue
            for r in rows:
                rel = f"{self._fmt_pct(r.get('pct_vs_ma20'))} / {self._fmt_pct(r.get('pct_vs_ma50'))}"
                vol_rsi = f"量比 {self._fmt_num(r.get('volume_ratio'), 2)} / RSI {self._fmt_num(r.get('rsi12'), 1)}"
                lines.append(
                    f"| {r.get('name', r.get('code'))}({r.get('code')}) | {rel} | {vol_rsi} | "
                    f"{r.get('news_72h', '暂无明显事件')} | {r.get('conclusion')} | {r.get('action')} |"
                )
            lines.append("")

        # ③ 今日执行清单
        lines.append("## ③ 今日执行清单（触发条件与下单价位）")
        lines.append("")
        checklist_count = 0
        for market in scope:
            for r in monitor_rows.get(market, []):
                if checklist_count >= 8:
                    break
                code = r.get("code")
                name = r.get("name", code)
                action = r.get("action", "观察")
                ma20 = r.get("ma20")
                trigger_price = ma20 if ma20 is not None else r.get("current")
                lines.append(
                    f"- [{scope_names.get(market, market)}] {name}({code})："
                    f"触发=`站上/回踩确认 {self._fmt_num(trigger_price)}`；"
                    f"量能/RSI=`量比≥1.0 且 RSI 45-65`；动作=`{action}`"
                )
                checklist_count += 1
        if checklist_count == 0:
            lines.append("- 暂无可执行标的，先等待触发器出现（价格回到20DMA附近并放量确认）")
        lines.append("")

        # ④ R1 仓位配比与风控
        lines.append("## ④ R1 仓位配比与风控")
        lines.append("")
        if overview.up_count > overview.down_count and overview.total_amount >= 9000:
            regime = "偏风险偏好（Risk-On）"
            allocation = "股票仓位 60%-70%，现金/对冲 30%-40%"
        elif overview.up_count < overview.down_count:
            regime = "偏风险回避（Risk-Off）"
            allocation = "股票仓位 20%-35%，现金/对冲 65%-80%"
        else:
            regime = "中性震荡（Neutral）"
            allocation = "股票仓位 40%-55%，现金 45%-60%"
        lines.append(f"- 市场状态: {regime}")
        lines.append(f"- R1 仓位建议: {allocation}")
        lines.append("- 单票风险: 单笔亏损不超过总资产 0.5%-1.0%；单票仓位上限 15%-20%")
        lines.append("- 组合风控: 连续两笔止损后当日停止加仓；跌破50DMA且放量时优先降杠杆")
        lines.append("")

        # ⑤ 当前持仓建议表
        lines.append("## ⑤ 当前持仓建议表（含成本与触发器）")
        lines.append("")
        hold_rows = self._build_holding_rows(owner_key, allowed_markets=scope)
        lines.append("| 标的 | 成本/现价/盈亏 | 当前仓位 | 建议(加/减/持) | 价位/量能/RSI触发器 |")
        lines.append("|---|---|---:|---|---|")
        if not hold_rows:
            lines.append("| - | - | - | 无持仓数据 | 使用 `/position set 代码 成本 仓位%` 后可显示个性化建议 |")
        else:
            for r in hold_rows[:20]:
                price_info = (
                    f"{self._fmt_num(r.get('avg_cost'))} / {self._fmt_num(r.get('current'))} / {self._fmt_pct(r.get('pnl_pct'))}"
                )
                lines.append(
                    f"| {r.get('name')}({r.get('code')}) | {price_info} | {self._fmt_num(r.get('weight_pct'), 1)}% | "
                    f"{r.get('action')} | {r.get('trigger')} |"
                )
        lines.append("")
        lines.append(f"_生成时间: {utc8_now().strftime('%H:%M:%S')} (UTC+8)_")
        return "\n".join(lines)

    def get_market_overview(self, scope: Optional[List[str]] = None) -> MarketOverview:
        """
        获取市场概览数据
        
        Returns:
            MarketOverview: 市场概览数据对象
        """
        today = datetime.now().strftime('%Y-%m-%d')
        overview = MarketOverview(date=today)

        scope = scope or ["CN", "HK"]

        # 美股视图：只获取美股关键指数，不再调用 A 股统计/板块接口
        if scope == ["US"]:
            overview.indices = self._get_us_main_indices()
            return overview

        # 港股单市场：避免错误混入 A 股宽度/板块数据
        if scope == ["HK"]:
            overview.indices = self._get_hk_main_indices()
            return overview

        # A股/混合市场默认逻辑（沿用现有数据源）
        # 1. 获取主要指数行情
        overview.indices = self._get_main_indices()

        # 2. 获取涨跌统计（A股口径）
        self._get_market_statistics(overview)

        # 3. 获取板块涨跌榜（A股口径）
        self._get_sector_rankings(overview)

        # 4. 获取北向资金（可选）
        # self._get_north_flow(overview)
        
        return overview

    def _get_us_main_indices(self) -> List[MarketIndex]:
        """获取美股关键指数（SPY/QQQ/SOXX）快照。"""
        idx_map = {
            "SPY": "标普500ETF(SPY)",
            "QQQ": "纳指100ETF(QQQ)",
            "SOXX": "半导体ETF(SOXX)",
        }
        out: List[MarketIndex] = []
        for code, name in idx_map.items():
            snap = self._load_stock_signal_snapshot(code)
            cur = self._to_float(snap.get("current"))
            if cur is None:
                continue
            # 用日线快照近似指数现状：缺少昨收时不强行计算涨跌
            out.append(
                MarketIndex(
                    code=code,
                    name=name,
                    current=cur,
                    change=0.0,
                    change_pct=0.0,
                    open=0.0,
                    high=0.0,
                    low=0.0,
                    prev_close=0.0,
                    volume=0.0,
                    amount=0.0,
                    amplitude=0.0,
                )
            )
        return out

    def _get_hk_main_indices(self) -> List[MarketIndex]:
        """获取港股关键指数（恒生/恒生科技）快照。"""
        idx_map = {
            "HSI": "恒生指数(HSI)",
            "HSTECH": "恒生科技指数(HSTECH)",
        }
        out: List[MarketIndex] = []
        for code, name in idx_map.items():
            snap = self._load_stock_signal_snapshot(code)
            cur = self._to_float(snap.get("current"))
            if cur is None:
                continue
            out.append(
                MarketIndex(
                    code=code,
                    name=name,
                    current=cur,
                    change=0.0,
                    change_pct=0.0,
                    open=0.0,
                    high=0.0,
                    low=0.0,
                    prev_close=0.0,
                    volume=0.0,
                    amount=0.0,
                    amplitude=0.0,
                )
            )
        return out

    
    def _get_main_indices(self) -> List[MarketIndex]:
        """获取主要指数实时行情"""
        indices = []

        try:
            logger.info("[大盘] 获取主要指数实时行情...")

            # 使用 DataFetcherManager 获取指数行情
            # Manager 会自动尝试：Akshare -> Tushare -> Yfinance
            data_list = self.data_manager.get_main_indices()

            if data_list:
                for item in data_list:
                    index = MarketIndex(
                        code=item['code'],
                        name=item['name'],
                        current=item['current'],
                        change=item['change'],
                        change_pct=item['change_pct'],
                        open=item['open'],
                        high=item['high'],
                        low=item['low'],
                        prev_close=item['prev_close'],
                        volume=item['volume'],
                        amount=item['amount'],
                        amplitude=item['amplitude']
                    )
                    indices.append(index)

            if not indices:
                logger.warning("[大盘] 所有行情数据源失败，将依赖新闻搜索进行分析")
            else:
                logger.info(f"[大盘] 获取到 {len(indices)} 个指数行情")

        except Exception as e:
            logger.error(f"[大盘] 获取指数行情失败: {e}")

        return indices

    def _get_market_statistics(self, overview: MarketOverview):
        """获取市场涨跌统计"""
        try:
            logger.info("[大盘] 获取市场涨跌统计...")

            stats = self.data_manager.get_market_stats()

            if stats:
                overview.up_count = stats.get('up_count', 0)
                overview.down_count = stats.get('down_count', 0)
                overview.flat_count = stats.get('flat_count', 0)
                overview.limit_up_count = stats.get('limit_up_count', 0)
                overview.limit_down_count = stats.get('limit_down_count', 0)
                overview.total_amount = stats.get('total_amount', 0.0)

                logger.info(f"[大盘] 涨:{overview.up_count} 跌:{overview.down_count} 平:{overview.flat_count} "
                          f"涨停:{overview.limit_up_count} 跌停:{overview.limit_down_count} "
                          f"成交额:{overview.total_amount:.0f}亿")

        except Exception as e:
            logger.error(f"[大盘] 获取涨跌统计失败: {e}")

    def _get_sector_rankings(self, overview: MarketOverview):
        """获取板块涨跌榜"""
        try:
            logger.info("[大盘] 获取板块涨跌榜...")

            top_sectors, bottom_sectors = self.data_manager.get_sector_rankings(5)

            if top_sectors or bottom_sectors:
                overview.top_sectors = top_sectors
                overview.bottom_sectors = bottom_sectors

                logger.info(f"[大盘] 领涨板块: {[s['name'] for s in overview.top_sectors]}")
                logger.info(f"[大盘] 领跌板块: {[s['name'] for s in overview.bottom_sectors]}")

        except Exception as e:
            logger.error(f"[大盘] 获取板块涨跌榜失败: {e}")
    
    # def _get_north_flow(self, overview: MarketOverview):
    #     """获取北向资金流入"""
    #     try:
    #         logger.info("[大盘] 获取北向资金...")
            
    #         # 获取北向资金数据
    #         df = ak.stock_hsgt_north_net_flow_in_em(symbol="北上")
            
    #         if df is not None and not df.empty:
    #             # 取最新一条数据
    #             latest = df.iloc[-1]
    #             if '当日净流入' in df.columns:
    #                 overview.north_flow = float(latest['当日净流入']) / 1e8  # 转为亿元
    #             elif '净流入' in df.columns:
    #                 overview.north_flow = float(latest['净流入']) / 1e8
                    
    #             logger.info(f"[大盘] 北向资金净流入: {overview.north_flow:.2f}亿")
                
    #     except Exception as e:
    #         logger.warning(f"[大盘] 获取北向资金失败: {e}")
    
    def search_market_news(
        self,
        query_text: str = "",
        market_hint: Optional[str] = None,
    ) -> List[Dict]:
        """
        市场新闻搜索已下线：
        /market 路径统一改为直接由 AI 生成宏观/财报摘要。
        """
        _ = (query_text, market_hint)  # 保留参数签名，兼容旧调用
        logger.info("[大盘] 已关闭市场新闻搜索，改为 AI 直出宏观/财报事件")
        return []
    
    def generate_market_review(
        self,
        overview: MarketOverview,
        news: List,
        query_text: str = "",
        market_hint: Optional[str] = None,
        owner_key: Optional[str] = None,
    ) -> str:
        """
        生成“今日怎么操作”固定模板报告。

        Args:
            overview: 市场概览数据
            news: 市场新闻列表 (SearchResult 对象列表)
            query_text: 用户原始问题
            market_hint: 显式市场提示（CN/HK/US）
            owner_key: 用户隔离键（用于持仓建议）
            
        Returns:
            模板化市场操作报告
        """
        return self._generate_operation_template(
            overview=overview,
            news=news,
            query_text=query_text,
            market_hint=market_hint,
            owner_key=owner_key,
        )
    
    def _build_review_prompt(self, overview: MarketOverview, news: List) -> str:
        """构建复盘报告 Prompt"""
        # 指数行情信息（简洁格式，不用emoji）
        indices_text = ""
        for idx in overview.indices:
            direction = "↑" if idx.change_pct > 0 else "↓" if idx.change_pct < 0 else "-"
            indices_text += f"- {idx.name}: {idx.current:.2f} ({direction}{abs(idx.change_pct):.2f}%)\n"
        
        # 板块信息
        top_sectors_text = ", ".join([f"{s['name']}({s['change_pct']:+.2f}%)" for s in overview.top_sectors[:3]])
        bottom_sectors_text = ", ".join([f"{s['name']}({s['change_pct']:+.2f}%)" for s in overview.bottom_sectors[:3]])
        
        # 新闻信息 - 支持 SearchResult 对象或字典
        news_text = ""
        for i, n in enumerate(news[:6], 1):
            # 兼容 SearchResult 对象和字典
            if hasattr(n, 'title'):
                title = n.title[:50] if n.title else ''
                snippet = n.snippet[:100] if n.snippet else ''
            else:
                title = n.get('title', '')[:50]
                snippet = n.get('snippet', '')[:100]
            news_text += f"{i}. {title}\n   {snippet}\n"
        
        prompt = f"""你是一位专业的A/H/美股市场分析师，请根据以下数据生成一份简洁的大盘复盘报告。

【重要】输出要求：
- 必须输出纯 Markdown 文本格式
- 禁止输出 JSON 格式
- 禁止输出代码块
- emoji 仅在标题处少量使用（每个标题最多1个）

---

# 今日市场数据

## 日期
{overview.date}

## 主要指数
{indices_text if indices_text else "暂无指数数据（接口异常）"}

## 市场概况
- 上涨: {overview.up_count} 家 | 下跌: {overview.down_count} 家 | 平盘: {overview.flat_count} 家
- 涨停: {overview.limit_up_count} 家 | 跌停: {overview.limit_down_count} 家
- 两市成交额: {overview.total_amount:.0f} 亿元

## 板块表现
领涨: {top_sectors_text if top_sectors_text else "暂无数据"}
领跌: {bottom_sectors_text if bottom_sectors_text else "暂无数据"}

## 市场新闻
{news_text if news_text else "暂无相关新闻"}

{"注意：由于行情数据获取失败，请主要根据【市场新闻】进行定性分析和总结，不要编造具体的指数点位。" if not indices_text else ""}

---

# 输出格式模板（请严格按此格式输出）

## 📊 {overview.date} 大盘复盘

### 一、市场总结
（2-3句话概括今日市场整体表现，包括指数涨跌、成交量变化）

### 二、指数点评
（分析上证、深证、创业板等各指数走势特点）

### 三、资金动向
（解读成交额流向的含义）

### 四、热点解读
（分析领涨领跌板块背后的逻辑和驱动因素）

### 五、后市展望
（结合当前走势和新闻，给出明日市场预判）

### 六、风险提示
（需要关注的风险点）

---

请直接输出复盘报告内容，不要输出其他说明文字。
"""
        return prompt
    
    def _generate_template_review(self, overview: MarketOverview, news: List) -> str:
        """使用模板生成复盘报告（无大模型时的备选方案）"""
        
        # 判断市场走势
        sh_index = next((idx for idx in overview.indices if idx.code == '000001'), None)
        if sh_index:
            if sh_index.change_pct > 1:
                market_mood = "强势上涨"
            elif sh_index.change_pct > 0:
                market_mood = "小幅上涨"
            elif sh_index.change_pct > -1:
                market_mood = "小幅下跌"
            else:
                market_mood = "明显下跌"
        else:
            market_mood = "震荡整理"
        
        # 指数行情（简洁格式）
        indices_text = ""
        for idx in overview.indices[:4]:
            direction = "↑" if idx.change_pct > 0 else "↓" if idx.change_pct < 0 else "-"
            indices_text += f"- **{idx.name}**: {idx.current:.2f} ({direction}{abs(idx.change_pct):.2f}%)\n"
        
        # 板块信息
        top_text = "、".join([s['name'] for s in overview.top_sectors[:3]])
        bottom_text = "、".join([s['name'] for s in overview.bottom_sectors[:3]])
        
        report = f"""## 📊 {overview.date} 大盘复盘

### 一、市场总结
今日A股市场整体呈现**{market_mood}**态势。

### 二、主要指数
{indices_text}

### 三、涨跌统计
| 指标 | 数值 |
|------|------|
| 上涨家数 | {overview.up_count} |
| 下跌家数 | {overview.down_count} |
| 涨停 | {overview.limit_up_count} |
| 跌停 | {overview.limit_down_count} |
| 两市成交额 | {overview.total_amount:.0f}亿 |

### 四、板块表现
- **领涨**: {top_text}
- **领跌**: {bottom_text}

### 五、风险提示
市场有风险，投资需谨慎。以上数据仅供参考，不构成投资建议。

---
*复盘时间: {datetime.now().strftime('%H:%M')}*
"""
        return report
    
    def run_daily_review(
        self,
        query_text: str = "",
        market_hint: Optional[str] = None,
        owner_key: Optional[str] = None,
    ) -> str:
        """
        执行每日大盘复盘流程
        
        Returns:
            复盘报告文本
        """
        logger.info("========== 开始大盘复盘分析 ==========")

        scope = self._infer_scope(query_text=query_text, market_hint=market_hint)

        # 1. 获取对应市场概览（避免串市场）
        overview = self.get_market_overview(scope=scope)
        
        # 2. 不再执行市场新闻搜索（按需求改为直接问 AI）
        news = []
        
        # 3. 生成复盘报告
        report = self.generate_market_review(
            overview,
            news,
            query_text=query_text,
            market_hint=market_hint,
            owner_key=owner_key,
        )
        
        logger.info("========== 大盘复盘分析完成 ==========")
        
        return report


# 测试入口
if __name__ == "__main__":
    import sys
    sys.path.insert(0, '.')
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s',
    )
    
    analyzer = MarketAnalyzer()
    
    # 测试获取市场概览
    overview = analyzer.get_market_overview()
    print(f"\n=== 市场概览 ===")
    print(f"日期: {overview.date}")
    print(f"指数数量: {len(overview.indices)}")
    for idx in overview.indices:
        print(f"  {idx.name}: {idx.current:.2f} ({idx.change_pct:+.2f}%)")
    print(f"上涨: {overview.up_count} | 下跌: {overview.down_count}")
    print(f"成交额: {overview.total_amount:.0f}亿")
    
    # 测试生成模板报告
    report = analyzer._generate_template_review(overview, [])
    print(f"\n=== 复盘报告 ===")
    print(report)
