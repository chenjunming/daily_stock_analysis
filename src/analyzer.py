# -*- coding: utf-8 -*-
"""
===================================
A股自选股智能分析系统 - AI分析层
===================================

职责：
1. 封装 Gemini API 调用逻辑
2. 利用 Google Search Grounding 获取实时新闻
3. 结合技术面和消息面生成分析报告
"""

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Optional, Dict, Any, List
from json_repair import repair_json

from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

from src.config import get_config

logger = logging.getLogger(__name__)


# 股票名称映射（常见股票）
STOCK_NAME_MAP = {
    # === A股 ===
    '600519': '贵州茅台',
    '000001': '平安银行',
    '300750': '宁德时代',
    '002594': '比亚迪',
    '600036': '招商银行',
    '601318': '中国平安',
    '000858': '五粮液',
    '600276': '恒瑞医药',
    '601012': '隆基绿能',
    '002475': '立讯精密',
    '300059': '东方财富',
    '002415': '海康威视',
    '600900': '长江电力',
    '601166': '兴业银行',
    '600028': '中国石化',

    # === 美股 ===
    'AAPL': '苹果',
    'TSLA': '特斯拉',
    'MSFT': '微软',
    'GOOGL': '谷歌A',
    'GOOG': '谷歌C',
    'AMZN': '亚马逊',
    'NVDA': '英伟达',
    'META': 'Meta',
    'AMD': 'AMD',
    'INTC': '英特尔',
    'BABA': '阿里巴巴',
    'PDD': '拼多多',
    'JD': '京东',
    'BIDU': '百度',
    'NIO': '蔚来',
    'XPEV': '小鹏汽车',
    'LI': '理想汽车',
    'COIN': 'Coinbase',
    'MSTR': 'MicroStrategy',

    # === 港股 (5位数字) ===
    '00700': '腾讯控股',
    '03690': '美团',
    '01810': '小米集团',
    '09988': '阿里巴巴',
    '09618': '京东集团',
    '09888': '百度集团',
    '01024': '快手',
    '00981': '中芯国际',
    '02015': '理想汽车',
    '09868': '小鹏汽车',
    '00005': '汇丰控股',
    '01299': '友邦保险',
    '00941': '中国移动',
    '00883': '中国海洋石油',
}


def get_stock_name_multi_source(
    stock_code: str, 
    context: Optional[Dict] = None,
    data_manager = None
) -> str:
    """
    多来源获取股票中文名称
    
    获取策略（按优先级）：
    1. 从传入的 context 中获取（realtime 数据）
    2. 从静态映射表 STOCK_NAME_MAP 获取
    3. 从 DataFetcherManager 获取（各数据源）
    4. 返回默认名称（股票+代码）
    
    Args:
        stock_code: 股票代码
        context: 分析上下文（可选）
        data_manager: DataFetcherManager 实例（可选）
        
    Returns:
        股票中文名称
    """
    # 1. 从上下文获取（实时行情数据）
    if context:
        # 优先从 stock_name 字段获取
        if context.get('stock_name'):
            name = context['stock_name']
            if name and not name.startswith('股票'):
                return name
        
        # 其次从 realtime 数据获取
        if 'realtime' in context and context['realtime'].get('name'):
            return context['realtime']['name']
    
    # 2. 从静态映射表获取
    if stock_code in STOCK_NAME_MAP:
        return STOCK_NAME_MAP[stock_code]
    
    # 3. 从数据源获取
    if data_manager is None:
        try:
            from data_provider.base import DataFetcherManager
            data_manager = DataFetcherManager()
        except Exception as e:
            logger.debug(f"无法初始化 DataFetcherManager: {e}")
    
    if data_manager:
        try:
            name = data_manager.get_stock_name(stock_code)
            if name:
                # 更新缓存
                STOCK_NAME_MAP[stock_code] = name
                return name
        except Exception as e:
            logger.debug(f"从数据源获取股票名称失败: {e}")
    
    # 4. 返回默认名称
    return f'股票{stock_code}'


@dataclass
class AnalysisResult:
    """
    AI 分析结果数据类 - 决策仪表盘版
    
    封装 Gemini 返回的分析结果，包含决策仪表盘和详细分析
    """
    code: str
    name: str
    
    # ========== 核心指标 ==========
    sentiment_score: int  # 综合评分 0-100 (>70强烈看多, >60看多, 40-60震荡, <40看空)
    trend_prediction: str  # 趋势预测：强烈看多/看多/震荡/看空/强烈看空
    operation_advice: str  # 操作建议：买入/加仓/持有/减仓/卖出/观望
    decision_type: str = "hold"  # 决策类型：buy/hold/sell（用于统计）
    confidence_level: str = "中"  # 置信度：高/中/低
    
    # ========== 决策仪表盘 (新增) ==========
    dashboard: Optional[Dict[str, Any]] = None  # 完整的决策仪表盘数据
    
    # ========== 走势分析 ==========
    trend_analysis: str = ""  # 走势形态分析（支撑位、压力位、趋势线等）
    short_term_outlook: str = ""  # 短期展望（1-3日）
    medium_term_outlook: str = ""  # 中期展望（1-2周）
    
    # ========== 技术面分析 ==========
    technical_analysis: str = ""  # 技术指标综合分析
    ma_analysis: str = ""  # 均线分析（多头/空头排列，金叉/死叉等）
    volume_analysis: str = ""  # 量能分析（放量/缩量，主力动向等）
    pattern_analysis: str = ""  # K线形态分析
    
    # ========== 基本面分析 ==========
    fundamental_analysis: str = ""  # 基本面综合分析
    sector_position: str = ""  # 板块地位和行业趋势
    company_highlights: str = ""  # 公司亮点/风险点
    
    # ========== 情绪面/消息面分析 ==========
    news_summary: str = ""  # 近期重要新闻/公告摘要
    market_sentiment: str = ""  # 市场情绪分析
    hot_topics: str = ""  # 相关热点话题
    
    # ========== 综合分析 ==========
    analysis_summary: str = ""  # 综合分析摘要
    key_points: str = ""  # 核心看点（3-5个要点）
    risk_warning: str = ""  # 风险提示
    buy_reason: str = ""  # 买入/卖出理由
    
    # ========== 元数据 ==========
    raw_response: Optional[str] = None  # 原始响应（调试用）
    search_performed: bool = False  # 是否执行了联网搜索
    data_sources: str = ""  # 数据来源说明
    user_holding: Optional[Dict[str, Any]] = None  # 用户当前标的持仓视角
    portfolio_profile: Optional[Dict[str, Any]] = None  # 用户组合画像
    success: bool = True
    error_message: Optional[str] = None

    @staticmethod
    def _as_dict(value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, list):
            first = next((x for x in value if isinstance(x, dict)), None)
            if isinstance(first, dict):
                return first
        return {}
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'code': self.code,
            'name': self.name,
            'sentiment_score': self.sentiment_score,
            'trend_prediction': self.trend_prediction,
            'operation_advice': self.operation_advice,
            'decision_type': self.decision_type,
            'confidence_level': self.confidence_level,
            'dashboard': self.dashboard,  # 决策仪表盘数据
            'trend_analysis': self.trend_analysis,
            'short_term_outlook': self.short_term_outlook,
            'medium_term_outlook': self.medium_term_outlook,
            'technical_analysis': self.technical_analysis,
            'ma_analysis': self.ma_analysis,
            'volume_analysis': self.volume_analysis,
            'pattern_analysis': self.pattern_analysis,
            'fundamental_analysis': self.fundamental_analysis,
            'sector_position': self.sector_position,
            'company_highlights': self.company_highlights,
            'news_summary': self.news_summary,
            'market_sentiment': self.market_sentiment,
            'hot_topics': self.hot_topics,
            'analysis_summary': self.analysis_summary,
            'key_points': self.key_points,
            'risk_warning': self.risk_warning,
            'buy_reason': self.buy_reason,
            'search_performed': self.search_performed,
            'user_holding': self.user_holding,
            'portfolio_profile': self.portfolio_profile,
            'success': self.success,
            'error_message': self.error_message,
        }
    
    def get_core_conclusion(self) -> str:
        """获取核心结论（一句话）"""
        dashboard = self._as_dict(self.dashboard)
        core = self._as_dict(dashboard.get('core_conclusion'))
        if core:
            return str(core.get('one_sentence', self.analysis_summary) or self.analysis_summary)
        return self.analysis_summary
    
    def get_position_advice(self, has_position: bool = False) -> str:
        """获取持仓建议"""
        dashboard = self._as_dict(self.dashboard)
        core = self._as_dict(dashboard.get('core_conclusion'))
        if core:
            pos_advice = self._as_dict(core.get('position_advice', {}))
            if has_position:
                return str(pos_advice.get('has_position', self.operation_advice) or self.operation_advice)
            return str(pos_advice.get('no_position', self.operation_advice) or self.operation_advice)
        return self.operation_advice
    
    def get_sniper_points(self) -> Dict[str, str]:
        """获取狙击点位"""
        dashboard = self._as_dict(self.dashboard)
        battle = self._as_dict(dashboard.get('battle_plan'))
        sniper = self._as_dict(battle.get('sniper_points'))
        return sniper
    
    def get_checklist(self) -> List[str]:
        """获取检查清单"""
        dashboard = self._as_dict(self.dashboard)
        battle = self._as_dict(dashboard.get('battle_plan'))
        checklist = battle.get('action_checklist', [])
        if isinstance(checklist, list):
            return [str(x) for x in checklist if x is not None]
        return []
    
    def get_risk_alerts(self) -> List[str]:
        """获取风险警报"""
        dashboard = self._as_dict(self.dashboard)
        intel = self._as_dict(dashboard.get('intelligence'))
        alerts = intel.get('risk_alerts', [])
        if isinstance(alerts, list):
            return [str(x) for x in alerts if x is not None]
        return []
    
    def get_emoji(self) -> str:
        """根据操作建议返回对应 emoji"""
        emoji_map = {
            '买入': '🟢',
            '加仓': '🟢',
            '强烈买入': '💚',
            '持有': '🟡',
            '观望': '⚪',
            '减仓': '🟠',
            '卖出': '🔴',
            '强烈卖出': '❌',
        }
        return emoji_map.get(self.operation_advice, '🟡')
    
    def get_confidence_stars(self) -> str:
        """返回置信度星级"""
        star_map = {'高': '⭐⭐⭐', '中': '⭐⭐', '低': '⭐'}
        return star_map.get(self.confidence_level, '⭐⭐')


class GeminiAnalyzer:
    """
    Gemini AI 分析器
    
    职责：
    1. 调用 Google Gemini API 进行股票分析
    2. 结合预先搜索的新闻和技术面数据生成分析报告
    3. 解析 AI 返回的 JSON 格式结果
    
    使用方式：
        analyzer = GeminiAnalyzer()
        result = analyzer.analyze(context, news_context)
    """
    
    # ========================================
    # 系统提示词 - 决策仪表盘 v2.0
    # ========================================
    # 输出格式升级：从简单信号升级为决策仪表盘
    # 核心模块：核心结论 + 数据透视 + 舆情情报 + 作战计划
    # ========================================
    
    SYSTEM_PROMPT = """你是股票趋势交易分析师，输出严格 JSON（禁止 Markdown/解释性前后缀）。

硬约束：
1) 不追高：bias_ma5 > 5% 时，不得给“买入/加仓”。
2) 空头结构（MA5<MA10<MA20 且价在 MA20 下）不得给“买入/加仓”。
3) 数据缺失时必须写“无法判断”，不能编造。
4) 若给“减仓/卖出”，必须给价格触发位和减仓比例。
5) 有用户持仓时，必须给单股动作+组合风控动作。

交易理念（必须遵守）：
1) 先看市场流动性阈值，再做赛道判断；通过“现象级事件”与“自主可控/遥遥领先”双透镜定位高景气赛道。
2) 组合分配默认：短线交易仓30% + 趋势配置仓70%。
   - 短线交易仓（30%）：短期主升捕捉，跌破5日线优先考虑止盈/止损。
   - 趋势配置仓（70%）：长期确定性持有，容忍20-30%回撤，遇极端短涨可减仓。
3) 个股量化评分模型权重：
   - 行业景气 25%
   - 业务纯度 25%
   - 历史估值位置 25%
   - 细分龙头 10%
   - 市场辨识度 10%
4) 风险清单（解禁/减持/造假/事故等）必须扣分，严重时直接否决（不建议）。
5) 必须先确认“赛道高景气”再启用评分模型，避免熊市误判。
6) 最终结论必须结合用户既有规则（50DMA、RSI、量能、近72小时消息面），输出“通过/存疑/不建议”及替代执行方案。

输出主字段必须包含：
stock_name, sentiment_score, trend_prediction, operation_advice, decision_type, confidence_level,
dashboard, analysis_summary, key_points, risk_warning, buy_reason。

dashboard 至少包含：
core_conclusion, data_perspective, intelligence, battle_plan, user_position_advice,
portfolio_risk_advice, strategy_execution, discretionary_advice。

strategy_execution.final_gate.verdict 只能是：通过/存疑/不建议。
若 verdict 为“存疑/不建议”，必须给 alternative_plan。

intelligence 强制结构（必须完整输出，字段不能为空）：
- industry_boom: {level, cycle_phase, evidence}
- company_analysis: {positioning, growth_quality, core_risks}
- valuation_snapshot: {valuation_conclusion, pe_pb_ps_percentile, vs_industry_percentile}
- expectation_gap: {gap_verdict, market_expectation, company_guidance}

industry_boom 填写要求（必须遵守）：
- level: 明确写“高景气/中景气/低景气”其一，并可补一句原因。
- cycle_phase: 明确写“上行/筑底/高位震荡/下行”其一。
- evidence: 必须写 2-3 条可验证依据（用“1) ...；2) ...；3) ...”），优先结合近期新闻催化、供需/订单、政策或资本开支。
- 禁止写“行业分析数据缺失”“仅从技术面判断”等占位语；可在末尾补“置信度：高/中/低”。

company_analysis 填写要求（必须遵守）：
- positioning: 必须写 2-3 条（产品/份额/护城河/竞争格局）。
- growth_quality: 必须写 2-3 条（收入利润趋势、订单能见度、现金流或资本开支效率）。
- core_risks: 必须写 2-3 条（需求波动、价格压力、监管/地缘/执行风险）。
- 三个字段都禁止“数据缺失/仅从技术面判断”这类占位语，优先结合已给上下文推断。

discretionary_advice 强制结构（允许自由发挥，但必须给可执行建议）：
- free_judgement: 结合现价/MA5/MA20/RSI/量能后的自由判断（1-2句）
- action_suggestion: 下一步具体动作（触发条件 + 仓位建议 + 风险控制）

strategy_execution.execution_plan 强制字段（必须给出明确仓位比例）：
- buy_size_pct: 买入/加仓比例（如 "2%-4%"）
- sell_size_pct: 减仓/卖出比例（如 "30%-50%"）
- position_plan: 仓位执行说明（需体现短线交易仓/趋势配置仓）
- add_reduce_triggers: 加减仓触发条件
- invalidation: 失效条件

如果确实拿不到数据，不能写空字符串/N/A/null，统一写：
“无法判断（数据不足）”。
但仅在确实无法推断时使用；应优先结合已给技术面、新闻、公司信息做具体判断。
"""

    def __init__(self, api_key: Optional[str] = None):
        """
        初始化 AI 分析器
        
        优先级：Gemini > OpenAI 兼容 API
        
        Args:
            api_key: Gemini API Key（可选，默认从配置读取）
        """
        config = get_config()
        self._api_key = api_key or config.gemini_api_key
        self._model = None
        self._current_model_name = None  # 当前使用的模型名称
        self._using_fallback = False  # 是否正在使用备选模型
        self._use_openai = False  # 是否使用 OpenAI 兼容 API
        self._openai_client = None  # OpenAI 客户端
        
        # 检查 Gemini API Key 是否有效（过滤占位符）
        gemini_key_valid = self._api_key and not self._api_key.startswith('your_') and len(self._api_key) > 10
        
        # 优先尝试初始化 Gemini
        if gemini_key_valid:
            try:
                self._init_model()
            except Exception as e:
                logger.warning(f"Gemini 初始化失败: {e}，尝试 OpenAI 兼容 API")
                self._init_openai_fallback()
        else:
            # Gemini Key 未配置，尝试 OpenAI
            logger.info("Gemini API Key 未配置，尝试使用 OpenAI 兼容 API")
            self._init_openai_fallback()
        
        # 两者都未配置
        if not self._model and not self._openai_client:
            logger.warning("未配置任何 AI API Key，AI 分析功能将不可用")
    
    def _init_openai_fallback(self) -> None:
        """
        初始化 OpenAI 兼容 API 作为备选
        
        支持所有 OpenAI 格式的 API，包括：
        - OpenAI 官方
        - DeepSeek
        - 通义千问
        - Moonshot 等
        """
        config = get_config()
        
        # 检查 OpenAI API Key 是否有效（过滤占位符）
        openai_key_valid = (
            config.openai_api_key and 
            not config.openai_api_key.startswith('your_') and 
            len(config.openai_api_key) > 10
        )
        
        if not openai_key_valid:
            logger.debug("OpenAI 兼容 API 未配置或配置无效")
            return
        
        # 分离 import 和客户端创建，以便提供更准确的错误信息
        try:
            from openai import OpenAI
        except ImportError:
            logger.error("未安装 openai 库，请运行: pip install openai")
            return
        
        try:
            # base_url 可选，不填则使用 OpenAI 官方默认地址
            client_kwargs = {"api_key": config.openai_api_key}
            if config.openai_base_url and config.openai_base_url.startswith('http'):
                client_kwargs["base_url"] = config.openai_base_url
            
            self._openai_client = OpenAI(**client_kwargs)
            self._current_model_name = config.openai_model
            self._use_openai = True
            logger.info(f"OpenAI 兼容 API 初始化成功 (base_url: {config.openai_base_url}, model: {config.openai_model})")
        except ImportError as e:
            # 依赖缺失（如 socksio）
            if 'socksio' in str(e).lower() or 'socks' in str(e).lower():
                logger.error(f"OpenAI 客户端需要 SOCKS 代理支持，请运行: pip install httpx[socks] 或 pip install socksio")
            else:
                logger.error(f"OpenAI 依赖缺失: {e}")
        except Exception as e:
            error_msg = str(e).lower()
            if 'socks' in error_msg or 'socksio' in error_msg or 'proxy' in error_msg:
                logger.error(f"OpenAI 代理配置错误: {e}，如使用 SOCKS 代理请运行: pip install httpx[socks]")
            else:
                logger.error(f"OpenAI 兼容 API 初始化失败: {e}")
    
    def _init_model(self) -> None:
        """
        初始化 Gemini 模型
        
        配置：
        - 使用 gemini-3-flash-preview 或 gemini-2.5-flash 模型
        - 不启用 Google Search（使用外部 Tavily/SerpAPI 搜索）
        """
        try:
            import google.generativeai as genai
            
            # 配置 API Key
            genai.configure(api_key=self._api_key)
            
            # 从配置获取模型名称
            config = get_config()
            model_name = config.gemini_model
            fallback_model = config.gemini_model_fallback
            
            # 不再使用 Google Search Grounding（已知有兼容性问题）
            # 改为使用外部搜索服务（Tavily/SerpAPI）预先获取新闻
            
            # 尝试初始化主模型
            try:
                self._model = genai.GenerativeModel(
                    model_name=model_name,
                    system_instruction=self.SYSTEM_PROMPT,
                )
                self._current_model_name = model_name
                self._using_fallback = False
                logger.info(f"Gemini 模型初始化成功 (模型: {model_name})")
            except Exception as model_error:
                # 尝试备选模型
                logger.warning(f"主模型 {model_name} 初始化失败: {model_error}，尝试备选模型 {fallback_model}")
                self._model = genai.GenerativeModel(
                    model_name=fallback_model,
                    system_instruction=self.SYSTEM_PROMPT,
                )
                self._current_model_name = fallback_model
                self._using_fallback = True
                logger.info(f"Gemini 备选模型初始化成功 (模型: {fallback_model})")
            
        except Exception as e:
            logger.error(f"Gemini 模型初始化失败: {e}")
            self._model = None
    
    def _switch_to_fallback_model(self) -> bool:
        """
        切换到备选模型
        
        Returns:
            是否成功切换
        """
        try:
            import google.generativeai as genai
            config = get_config()
            fallback_model = config.gemini_model_fallback
            
            logger.warning(f"[LLM] 切换到备选模型: {fallback_model}")
            self._model = genai.GenerativeModel(
                model_name=fallback_model,
                system_instruction=self.SYSTEM_PROMPT,
            )
            self._current_model_name = fallback_model
            self._using_fallback = True
            logger.info(f"[LLM] 备选模型 {fallback_model} 初始化成功")
            return True
        except Exception as e:
            logger.error(f"[LLM] 切换备选模型失败: {e}")
            return False
    
    def is_available(self) -> bool:
        """检查分析器是否可用"""
        return self._model is not None or self._openai_client is not None
    
    def _call_openai_api(self, prompt: str, generation_config: dict) -> str:
        """
        调用 OpenAI 兼容 API
        
        Args:
            prompt: 提示词
            generation_config: 生成配置
            
        Returns:
            响应文本
        """
        config = get_config()
        max_retries = config.gemini_max_retries
        base_delay = config.gemini_retry_delay
        
        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    delay = base_delay * (2 ** (attempt - 1))
                    delay = min(delay, 60)
                    logger.info(f"[OpenAI] 第 {attempt + 1} 次重试，等待 {delay:.1f} 秒...")
                    time.sleep(delay)
                
                config = get_config()
                response = self._openai_client.chat.completions.create(
                    model=self._current_model_name,
                    messages=[
                        {"role": "system", "content": self.SYSTEM_PROMPT},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=generation_config.get('temperature', config.openai_temperature),
                    max_tokens=generation_config.get('max_output_tokens', config.ai_max_output_tokens),
                )
                
                if response and response.choices and response.choices[0].message.content:
                    return response.choices[0].message.content
                else:
                    raise ValueError("OpenAI API 返回空响应")
                    
            except Exception as e:
                error_str = str(e)
                is_rate_limit = '429' in error_str or 'rate' in error_str.lower() or 'quota' in error_str.lower()
                
                if is_rate_limit:
                    logger.warning(f"[OpenAI] API 限流，第 {attempt + 1}/{max_retries} 次尝试: {error_str[:100]}")
                else:
                    logger.warning(f"[OpenAI] API 调用失败，第 {attempt + 1}/{max_retries} 次尝试: {error_str[:100]}")
                
                if attempt == max_retries - 1:
                    raise
        
        raise Exception("OpenAI API 调用失败，已达最大重试次数")
    
    def _call_api_with_retry(self, prompt: str, generation_config: dict) -> str:
        """
        调用 AI API，带有重试和模型切换机制
        
        优先级：Gemini > Gemini 备选模型 > OpenAI 兼容 API
        
        处理 429 限流错误：
        1. 先指数退避重试
        2. 多次失败后切换到备选模型
        3. Gemini 完全失败后尝试 OpenAI
        
        Args:
            prompt: 提示词
            generation_config: 生成配置
            
        Returns:
            响应文本
        """
        # 如果已经在使用 OpenAI 模式，直接调用 OpenAI
        if self._use_openai:
            return self._call_openai_api(prompt, generation_config)
        
        config = get_config()
        max_retries = config.gemini_max_retries
        base_delay = config.gemini_retry_delay
        
        last_error = None
        tried_fallback = getattr(self, '_using_fallback', False)
        
        for attempt in range(max_retries):
            try:
                # 请求前增加延时（防止请求过快触发限流）
                if attempt > 0:
                    delay = base_delay * (2 ** (attempt - 1))  # 指数退避: 5, 10, 20, 40...
                    delay = min(delay, 60)  # 最大60秒
                    logger.info(f"[Gemini] 第 {attempt + 1} 次重试，等待 {delay:.1f} 秒...")
                    time.sleep(delay)
                
                response = self._model.generate_content(
                    prompt,
                    generation_config=generation_config,
                    request_options={"timeout": 120}
                )
                
                if response and response.text:
                    return response.text
                else:
                    raise ValueError("Gemini 返回空响应")
                    
            except Exception as e:
                last_error = e
                error_str = str(e)
                
                # 检查是否是 429 限流错误
                is_rate_limit = '429' in error_str or 'quota' in error_str.lower() or 'rate' in error_str.lower()
                
                if is_rate_limit:
                    logger.warning(f"[Gemini] API 限流 (429)，第 {attempt + 1}/{max_retries} 次尝试: {error_str[:100]}")
                    
                    # 如果已经重试了一半次数且还没切换过备选模型，尝试切换
                    if attempt >= max_retries // 2 and not tried_fallback:
                        if self._switch_to_fallback_model():
                            tried_fallback = True
                            logger.info("[Gemini] 已切换到备选模型，继续重试")
                        else:
                            logger.warning("[Gemini] 切换备选模型失败，继续使用当前模型重试")
                else:
                    # 非限流错误，记录并继续重试
                    logger.warning(f"[Gemini] API 调用失败，第 {attempt + 1}/{max_retries} 次尝试: {error_str[:100]}")
        
        # Gemini 所有重试都失败，尝试 OpenAI 兼容 API
        if self._openai_client:
            logger.warning("[Gemini] 所有重试失败，切换到 OpenAI 兼容 API")
            try:
                return self._call_openai_api(prompt, generation_config)
            except Exception as openai_error:
                logger.error(f"[OpenAI] 备选 API 也失败: {openai_error}")
                raise last_error or openai_error
        elif config.openai_api_key and config.openai_base_url:
            # 尝试懒加载初始化 OpenAI
            logger.warning("[Gemini] 所有重试失败，尝试初始化 OpenAI 兼容 API")
            self._init_openai_fallback()
            if self._openai_client:
                try:
                    return self._call_openai_api(prompt, generation_config)
                except Exception as openai_error:
                    logger.error(f"[OpenAI] 备选 API 也失败: {openai_error}")
                    raise last_error or openai_error
        
        # 所有方式都失败
        raise last_error or Exception("所有 AI API 调用失败，已达最大重试次数")
    
    def analyze(
        self, 
        context: Dict[str, Any],
        news_context: Optional[str] = None
    ) -> AnalysisResult:
        """
        分析单只股票
        
        流程：
        1. 格式化输入数据（技术面 + 新闻）
        2. 调用 Gemini API（带重试和模型切换）
        3. 解析 JSON 响应
        4. 返回结构化结果
        
        Args:
            context: 从 storage.get_analysis_context() 获取的上下文数据
            news_context: 预先搜索的新闻内容（可选）
            
        Returns:
            AnalysisResult 对象
        """
        code = context.get('code', 'Unknown')
        config = get_config()
        
        # 请求前增加延时（防止连续请求触发限流）
        request_delay = config.gemini_request_delay
        if request_delay > 0:
            logger.debug(f"[LLM] 请求前等待 {request_delay:.1f} 秒...")
            time.sleep(request_delay)
        
        # 优先从上下文获取股票名称（由 main.py 传入）
        name = context.get('stock_name')
        if not name or name.startswith('股票'):
            # 备选：从 realtime 中获取
            if 'realtime' in context and context['realtime'].get('name'):
                name = context['realtime']['name']
            else:
                # 最后从映射表获取
                name = STOCK_NAME_MAP.get(code, f'股票{code}')
        
        # 如果模型不可用，返回默认结果
        if not self.is_available():
            return AnalysisResult(
                code=code,
                name=name,
                sentiment_score=50,
                trend_prediction='震荡',
                operation_advice='持有',
                confidence_level='低',
                analysis_summary='AI 分析功能未启用（未配置 API Key）',
                risk_warning='请配置 Gemini API Key 后重试',
                success=False,
                error_message='Gemini API Key 未配置',
            )
        
        try:
            # 格式化输入（包含技术面数据和新闻）
            prompt = self._format_prompt(context, name, news_context)
            
            # 获取模型名称
            model_name = getattr(self, '_current_model_name', None)
            if not model_name:
                model_name = getattr(self._model, '_model_name', 'unknown')
                if hasattr(self._model, 'model_name'):
                    model_name = self._model.model_name
            
            logger.info(f"========== AI 分析 {name}({code}) ==========")
            logger.info(f"[LLM配置] 模型: {model_name}")
            logger.info(f"[LLM配置] Prompt 长度: {len(prompt)} 字符")
            logger.info(f"[LLM配置] 是否包含新闻: {'是' if news_context else '否'}")
            
            # 记录完整 prompt 到日志（INFO级别记录摘要，DEBUG记录完整）
            prompt_preview = prompt[:500] + "..." if len(prompt) > 500 else prompt
            logger.info(f"[LLM Prompt 预览]\n{prompt_preview}")
            logger.debug(f"=== 完整 Prompt ({len(prompt)}字符) ===\n{prompt}\n=== End Prompt ===")

            # 设置生成配置（从配置文件读取温度参数）
            config = get_config()
            generation_config = {
                "temperature": config.gemini_temperature,
                "max_output_tokens": config.ai_max_output_tokens,
            }

            # 根据实际使用的 API 显示日志
            api_provider = "OpenAI" if self._use_openai else "Gemini"
            logger.info(f"[LLM调用] 开始调用 {api_provider} API...")
            
            # 使用带重试的 API 调用
            start_time = time.time()
            response_text = self._call_api_with_retry(prompt, generation_config)
            elapsed = time.time() - start_time

            # 记录响应信息
            logger.info(f"[LLM返回] {api_provider} API 响应成功, 耗时 {elapsed:.2f}s, 响应长度 {len(response_text)} 字符")
            
            # 记录响应预览（INFO级别）和完整响应（DEBUG级别）
            response_preview = response_text[:300] + "..." if len(response_text) > 300 else response_text
            logger.info(f"[LLM返回 预览]\n{response_preview}")
            logger.debug(f"=== {api_provider} 完整响应 ({len(response_text)}字符) ===\n{response_text}\n=== End Response ===")
            
            # 解析响应
            result = self._parse_response(response_text, code, name)
            result.raw_response = response_text
            result.search_performed = bool(news_context)
            result.user_holding = context.get("user_holding")
            result.portfolio_profile = context.get("portfolio_profile")
            
            logger.info(f"[LLM解析] {name}({code}) 分析完成: {result.trend_prediction}, 评分 {result.sentiment_score}")
            
            return result
            
        except Exception as e:
            logger.error(f"AI 分析 {name}({code}) 失败: {e}")
            return AnalysisResult(
                code=code,
                name=name,
                sentiment_score=50,
                trend_prediction='震荡',
                operation_advice='持有',
                confidence_level='低',
                analysis_summary=f'分析过程出错: {str(e)[:100]}',
                risk_warning='分析失败，请稍后重试或手动分析',
                success=False,
                error_message=str(e),
            )
    
    def _format_prompt(
        self, 
        context: Dict[str, Any], 
        name: str,
        news_context: Optional[str] = None
    ) -> str:
        """
        格式化分析提示词（决策仪表盘 v2.0）
        
        包含：技术指标、实时行情（量比/换手率）、筹码分布、趋势分析、新闻
        
        Args:
            context: 技术面数据上下文（包含增强数据）
            name: 股票名称（默认值，可能被上下文覆盖）
            news_context: 预先搜索的新闻内容
        """
        cfg = get_config()
        code = context.get('code', 'Unknown')

        stock_name = context.get('stock_name', name)
        if not stock_name or stock_name == f'股票{code}':
            stock_name = STOCK_NAME_MAP.get(code, f'股票{code}')

        today = context.get('today') if isinstance(context.get('today'), dict) else {}
        rt = context.get('realtime') if isinstance(context.get('realtime'), dict) else {}
        chip = context.get('chip') if isinstance(context.get('chip'), dict) else {}
        trend = context.get('trend_analysis') if isinstance(context.get('trend_analysis'), dict) else {}
        rsi_val = (
            today.get('rsi')
            or today.get('rsi12')
            or trend.get('rsi')
            or trend.get('rsi12')
            or trend.get('rsi_12')
        )
        holding = context.get('user_holding') if isinstance(context.get('user_holding'), dict) else {}
        portfolio = context.get('portfolio_profile') if isinstance(context.get('portfolio_profile'), dict) else {}
        market_dist = portfolio.get('market_distribution', {}) if isinstance(portfolio, dict) else {}

        lines = [
            f"任务: 为 {stock_name}({code}) 生成决策仪表盘 JSON。",
            "规则: 严格 JSON 输出，不要 markdown 代码块，不要附加解释。",
            "",
            "【基础信息】",
            f"- date: {context.get('date', '未知')}",
            f"- code: {code}",
            f"- stock_name: {stock_name}",
            "",
            "【技术面】",
            f"- price: close={today.get('close')} open={today.get('open')} high={today.get('high')} low={today.get('low')} pct_chg={today.get('pct_chg')}",
            f"- ma: ma5={today.get('ma5')} ma10={today.get('ma10')} ma20={today.get('ma20')} ma_status={context.get('ma_status')}",
            f"- momentum: rsi={rsi_val}",
            f"- volume: {self._format_volume(today.get('volume'))}, amount: {self._format_amount(today.get('amount'))}",
        ]

        if rt:
            lines.extend([
                "【实时增强】",
                f"- rt_price={rt.get('price')} pre_close={rt.get('pre_close')} change_pct={rt.get('change_pct')} change_amount={rt.get('change_amount')}",
                f"- rt_ohlc: open={rt.get('open_price')} high={rt.get('high')} low={rt.get('low')}",
                f"- volume_ratio={rt.get('volume_ratio')} turnover_rate={rt.get('turnover_rate')} amplitude={rt.get('amplitude')}",
                f"- volume={self._format_volume(rt.get('volume'))} amount={self._format_amount(rt.get('amount'))}",
                f"- pe={rt.get('pe_ratio')} pb={rt.get('pb_ratio')} total_mv={self._format_amount(rt.get('total_mv'))} circ_mv={self._format_amount(rt.get('circ_mv'))}",
                f"- change_60d={rt.get('change_60d')} high_52w={rt.get('high_52w')} low_52w={rt.get('low_52w')}",
                f"- session: trade_session={rt.get('trade_session')} price_session={rt.get('price_session')} price_ts={rt.get('price_timestamp')} source={rt.get('source')}",
            ])

        if chip:
            lines.extend([
                "【筹码】",
                f"- profit_ratio={chip.get('profit_ratio')} avg_cost={chip.get('avg_cost')} c90={chip.get('concentration_90')} c70={chip.get('concentration_70')}",
                f"- chip_status={chip.get('chip_status')}",
            ])

        if trend:
            signal_reasons_raw = trend.get('signal_reasons')
            if isinstance(signal_reasons_raw, list):
                signal_reasons = [str(x) for x in signal_reasons_raw if x is not None]
            elif signal_reasons_raw:
                signal_reasons = [str(signal_reasons_raw)]
            else:
                signal_reasons = []

            risk_factors_raw = trend.get('risk_factors')
            if isinstance(risk_factors_raw, list):
                risk_factors = [str(x) for x in risk_factors_raw if x is not None]
            elif risk_factors_raw:
                risk_factors = [str(risk_factors_raw)]
            else:
                risk_factors = []

            lines.extend([
                "【趋势】",
                f"- trend_status={trend.get('trend_status')} ma_alignment={trend.get('ma_alignment')} trend_strength={trend.get('trend_strength')}",
                f"- trend_ma: current_price={trend.get('current_price')} ma5={trend.get('ma5')} ma10={trend.get('ma10')} ma20={trend.get('ma20')} ma60={trend.get('ma60')}",
                f"- bias_ma5={trend.get('bias_ma5')} bias_ma10={trend.get('bias_ma10')} bias_ma20={trend.get('bias_ma20')}",
                f"- volume_status={trend.get('volume_status')} volume_ratio_5d={trend.get('volume_ratio_5d')} volume_trend={trend.get('volume_trend')}",
                f"- support_ma5={trend.get('support_ma5')} support_ma10={trend.get('support_ma10')}",
                f"- support_levels={trend.get('support_levels')} resistance_levels={trend.get('resistance_levels')}",
                f"- macd: dif={trend.get('macd_dif')} dea={trend.get('macd_dea')} bar={trend.get('macd_bar')} status={trend.get('macd_status')} signal={trend.get('macd_signal')}",
                f"- rsi: rsi6={trend.get('rsi_6')} rsi12={trend.get('rsi_12')} rsi24={trend.get('rsi_24')} status={trend.get('rsi_status')} signal={trend.get('rsi_signal')}",
                f"- buy_signal={trend.get('buy_signal')} signal_score={trend.get('signal_score')}",
                f"- signal_reasons={self._truncate_text('; '.join(signal_reasons), 220)}",
                f"- risk_factors={self._truncate_text('; '.join(risk_factors), 220)}",
            ])

        if 'yesterday' in context:
            lines.extend([
                "【日对比】",
                f"- price_change_ratio={context.get('price_change_ratio')} volume_change_ratio={context.get('volume_change_ratio')}",
            ])

        if holding or portfolio:
            top_rows = portfolio.get('top_holdings') if isinstance(portfolio, dict) else []
            top_text = "; ".join(
                f"{x.get('code')}:{x.get('weight_pct')}%"
                for x in (top_rows or [])[:3]
                if isinstance(x, dict)
            )
            lines.extend([
                "【用户持仓】",
                f"- has_holding={'yes' if holding else 'no'} avg_cost={holding.get('avg_cost')} pnl_pct={holding.get('pnl_pct')} weight_pct={holding.get('weight_pct')}",
                f"- portfolio: holding_count={portfolio.get('holding_count')} total_weight={portfolio.get('total_weight_pct')} top3={portfolio.get('concentration_top3_pct')}",
                f"- market_distribution: CN={market_dist.get('CN')} HK={market_dist.get('HK')} US={market_dist.get('US')}",
                f"- top_holdings: {top_text or 'N/A'}",
            ])

        trimmed_news = self._truncate_text(news_context or "", cfg.ai_news_context_max_chars)
        lines.append("【舆情】")
        if trimmed_news:
            lines.append(trimmed_news)
        else:
            lines.append("无近期新闻，优先技术面。")

        if context.get('data_missing'):
            lines.append("【数据缺失警告】技术指标缺失处必须明确写“无法判断”。")

        lines.extend([
            "",
            "【输出要求】",
            "- 仅输出 JSON 对象。",
            "- stock_name 必须是正确中文名（不要“股票代码”占位名）。",
            "- operation_advice 若为减仓/卖出，必须给 reduce_price_trigger 与 reduce_plan。",
            "- 必须给 today_action / today_trigger / today_order_plan。",
            "- 必须给 strategy_execution.final_gate.verdict（通过/存疑/不建议）。",
            "- verdict 为存疑/不建议时，必须给 alternative_plan。",
            "- 必须给 battle_plan.sniper_points 的 ideal_buy/secondary_buy/stop_loss/take_profit。",
            "- strategy_execution.execution_plan 必须给 buy_size_pct / sell_size_pct / position_plan / add_reduce_triggers / invalidation。",
            "- intelligence 的四个子对象必须完整输出且关键字段非空：",
            "  * industry_boom.level",
            "  * company_analysis.positioning",
            "  * valuation_snapshot.valuation_conclusion",
            "  * expectation_gap.gap_verdict",
            "- industry_boom.evidence 必须包含 2-3 条要点，格式示例：1) ...；2) ...；3) ...。",
            "- company_analysis.positioning / growth_quality / core_risks 每个字段必须包含 2-3 条要点。",
            "- 禁止输出“行业分析数据缺失”“仅从技术面判断”等占位语。",
            "- 必须输出 dashboard.discretionary_advice.free_judgement 与 action_suggestion。",
            "- action_suggestion 必须可执行：包含触发条件、仓位建议、风控要点；并体现短线交易仓/趋势配置仓配比与对应动作。",
            "- 必须给出明确操作倾向（买入/加仓/持有/减仓/卖出/观望其一），不能只给模糊描述。",
            "- company_analysis / valuation_snapshot / expectation_gap 应优先使用已给上下文推断，不要轻易输出“无法判断（数据不足）”。",
            "- 以上字段禁止返回空字符串/N/A/null；若无法确定，写“无法判断（数据不足）”。",
            "",
            "最小 JSON 结构（可补充字段）：",
            "{\"stock_name\":\"\",\"sentiment_score\":0,\"trend_prediction\":\"\",\"operation_advice\":\"\",\"decision_type\":\"\",\"confidence_level\":\"\","
            "\"dashboard\":{\"core_conclusion\":{},\"data_perspective\":{},\"intelligence\":{\"industry_boom\":{\"level\":\"\",\"cycle_phase\":\"\",\"evidence\":\"\"},\"company_analysis\":{\"positioning\":\"\",\"growth_quality\":\"\",\"core_risks\":\"\"},\"valuation_snapshot\":{\"valuation_conclusion\":\"\",\"pe_pb_ps_percentile\":\"\",\"vs_industry_percentile\":\"\"},\"expectation_gap\":{\"gap_verdict\":\"\",\"market_expectation\":\"\",\"company_guidance\":\"\"}},\"battle_plan\":{},\"user_position_advice\":{},\"portfolio_risk_advice\":{},\"strategy_execution\":{\"final_gate\":{},\"execution_plan\":{\"buy_size_pct\":\"\",\"sell_size_pct\":\"\",\"position_plan\":\"\",\"add_reduce_triggers\":\"\",\"invalidation\":\"\"}},\"discretionary_advice\":{\"free_judgement\":\"\",\"action_suggestion\":\"\",\"confidence\":\"\",\"thesis\":\"\",\"counter_view\":\"\",\"invalidation\":\"\",\"alt_plan\":\"\",\"note\":\"\"}},"
            "\"analysis_summary\":\"\",\"key_points\":\"\",\"risk_warning\":\"\",\"buy_reason\":\"\"}",
        ])

        prompt = "\n".join(lines)
        return self._truncate_text(prompt, cfg.ai_prompt_max_chars)

    @staticmethod
    def _truncate_text(text: str, max_chars: int) -> str:
        """按字符预算裁剪文本，避免 prompt 过长。"""
        if not text:
            return ""
        if max_chars <= 0 or len(text) <= max_chars:
            return text
        return text[:max_chars].rstrip()
    
    def _format_volume(self, volume: Optional[float]) -> str:
        """格式化成交量显示"""
        if volume is None:
            return 'N/A'
        if volume >= 1e8:
            return f"{volume / 1e8:.2f} 亿股"
        elif volume >= 1e4:
            return f"{volume / 1e4:.2f} 万股"
        else:
            return f"{volume:.0f} 股"
    
    def _format_amount(self, amount: Optional[float]) -> str:
        """格式化成交额显示"""
        if amount is None:
            return 'N/A'
        if amount >= 1e8:
            return f"{amount / 1e8:.2f} 亿元"
        elif amount >= 1e4:
            return f"{amount / 1e4:.2f} 万元"
        else:
            return f"{amount:.0f} 元"
    
    def _parse_response(
        self, 
        response_text: str, 
        code: str, 
        name: str
    ) -> AnalysisResult:
        """
        解析 Gemini 响应（决策仪表盘版）
        
        尝试从响应中提取 JSON 格式的分析结果，包含 dashboard 字段
        如果解析失败，尝试智能提取或返回默认结果
        """
        try:
            # 清理响应文本：移除 markdown 代码块标记
            cleaned_text = response_text
            if '```json' in cleaned_text:
                cleaned_text = cleaned_text.replace('```json', '').replace('```', '')
            elif '```' in cleaned_text:
                cleaned_text = cleaned_text.replace('```', '')
            
            # 尝试找到 JSON 内容
            json_start = cleaned_text.find('{')
            json_end = cleaned_text.rfind('}') + 1
            
            if json_start >= 0 and json_end > json_start:
                json_str = cleaned_text[json_start:json_end]
                
                # 尝试修复常见的 JSON 问题
                json_str = self._fix_json_string(json_str)
                
                data = json.loads(json_str)
                if isinstance(data, list):
                    # 某些模型会返回 [{...}]，这里做容错
                    data = next((x for x in data if isinstance(x, dict)), {})
                if not isinstance(data, dict):
                    logger.warning("JSON 顶层非对象，降级为文本解析")
                    return self._parse_text_response(response_text, code, name)
                
                # 提取 dashboard 数据
                dashboard = data.get('dashboard', None)
                dashboard = self._normalize_dashboard_intelligence(dashboard)
                dashboard = self._enrich_dashboard_from_payload(dashboard, data)
                dashboard = self._normalize_dashboard_shape(dashboard)

                # 优先使用 AI 返回的股票名称（如果原名称无效或包含代码）
                ai_stock_name = data.get('stock_name')
                if ai_stock_name and (name.startswith('股票') or name == code or 'Unknown' in name):
                    name = ai_stock_name

                # 解析所有字段，使用默认值防止缺失
                # 解析 decision_type，如果没有则根据 operation_advice 推断
                decision_type = data.get('decision_type', '')
                if not decision_type:
                    op = data.get('operation_advice', '持有')
                    if op in ['买入', '加仓', '强烈买入']:
                        decision_type = 'buy'
                    elif op in ['卖出', '减仓', '强烈卖出']:
                        decision_type = 'sell'
                    else:
                        decision_type = 'hold'
                
                raw_score = data.get('sentiment_score', 50)
                normalized_score = self._normalize_sentiment_score(raw_score)
                if str(raw_score) != str(normalized_score):
                    logger.info(f"[评分归一] 原始sentiment_score={raw_score} -> 归一后={normalized_score}")

                result = AnalysisResult(
                    code=code,
                    name=name,
                    # 核心指标
                    sentiment_score=normalized_score,
                    trend_prediction=data.get('trend_prediction', '震荡'),
                    operation_advice=data.get('operation_advice', '持有'),
                    decision_type=decision_type,
                    confidence_level=data.get('confidence_level', '中'),
                    # 决策仪表盘
                    dashboard=dashboard,
                    # 走势分析
                    trend_analysis=data.get('trend_analysis', ''),
                    short_term_outlook=data.get('short_term_outlook', ''),
                    medium_term_outlook=data.get('medium_term_outlook', ''),
                    # 技术面
                    technical_analysis=data.get('technical_analysis', ''),
                    ma_analysis=data.get('ma_analysis', ''),
                    volume_analysis=data.get('volume_analysis', ''),
                    pattern_analysis=data.get('pattern_analysis', ''),
                    # 基本面
                    fundamental_analysis=data.get('fundamental_analysis', ''),
                    sector_position=data.get('sector_position', ''),
                    company_highlights=data.get('company_highlights', ''),
                    # 情绪面/消息面
                    news_summary=data.get('news_summary', ''),
                    market_sentiment=data.get('market_sentiment', ''),
                    hot_topics=data.get('hot_topics', ''),
                    # 综合
                    analysis_summary=data.get('analysis_summary', '分析完成'),
                    key_points=data.get('key_points', ''),
                    risk_warning=data.get('risk_warning', ''),
                    buy_reason=data.get('buy_reason', ''),
                    # 元数据
                    search_performed=data.get('search_performed', False),
                    data_sources=data.get('data_sources', '技术面数据'),
                    success=True,
                )
                return self._harden_analysis_result(result)
            else:
                # 没有找到 JSON，尝试从纯文本中提取信息
                logger.warning(f"无法从响应中提取 JSON，使用原始文本分析")
                return self._parse_text_response(response_text, code, name)
                
        except json.JSONDecodeError as e:
            logger.warning(f"JSON 解析失败: {e}，尝试从文本提取")
            return self._parse_text_response(response_text, code, name)
    
    def _fix_json_string(self, json_str: str) -> str:
        """修复常见的 JSON 格式问题"""
        import re
        
        # 移除注释
        json_str = re.sub(r'//.*?\n', '\n', json_str)
        json_str = re.sub(r'/\*.*?\*/', '', json_str, flags=re.DOTALL)
        
        # 修复尾随逗号
        json_str = re.sub(r',\s*}', '}', json_str)
        json_str = re.sub(r',\s*]', ']', json_str)
        
        # 确保布尔值是小写
        json_str = json_str.replace('True', 'true').replace('False', 'false')
        
        # fix by json-repair
        json_str = repair_json(json_str)
        
        return json_str
    
    def _parse_text_response(
        self, 
        response_text: str, 
        code: str, 
        name: str
    ) -> AnalysisResult:
        """从纯文本响应中尽可能提取分析信息"""
        # 尝试识别关键词来判断情绪
        sentiment_score = 50
        trend = '震荡'
        advice = '持有'
        
        text_lower = response_text.lower()
        
        # 简单的情绪识别
        positive_keywords = ['看多', '买入', '上涨', '突破', '强势', '利好', '加仓', 'bullish', 'buy']
        negative_keywords = ['看空', '卖出', '下跌', '跌破', '弱势', '利空', '减仓', 'bearish', 'sell']
        
        positive_count = sum(1 for kw in positive_keywords if kw in text_lower)
        negative_count = sum(1 for kw in negative_keywords if kw in text_lower)
        
        if positive_count > negative_count + 1:
            sentiment_score = 65
            trend = '看多'
            advice = '买入'
            decision_type = 'buy'
        elif negative_count > positive_count + 1:
            sentiment_score = 35
            trend = '看空'
            advice = '卖出'
            decision_type = 'sell'
        else:
            decision_type = 'hold'
        
        # 截取前500字符作为摘要
        summary = response_text[:500] if response_text else '无分析结果'
        
        result = AnalysisResult(
            code=code,
            name=name,
            sentiment_score=sentiment_score,
            trend_prediction=trend,
            operation_advice=advice,
            decision_type=decision_type,
            confidence_level='低',
            analysis_summary=summary,
            key_points='JSON解析失败，仅供参考',
            risk_warning='分析结果可能不准确，建议结合其他信息判断',
            raw_response=response_text,
            success=True,
        )
        return self._harden_analysis_result(result)

    @staticmethod
    def _normalize_sentiment_score(raw_score: Any) -> int:
        """
        将 AI 返回的评分统一归一到 0~100：
        - 0~1 视为比例，乘 100
        - 1~10 视为 10 分制，乘 10
        - 10~100 视为百分制，直接使用
        """
        try:
            v = float(raw_score)
        except Exception:
            return 50

        if v < 0:
            v = 0
        if v <= 1:
            v = v * 100.0
        elif v <= 10:
            v = v * 10.0
        # >10 认为已经是百分制
        if v > 100:
            v = 100
        return int(round(v))

    @staticmethod
    def _normalize_dashboard_intelligence(dashboard: Any) -> Dict[str, Any]:
        """
        规范化 dashboard.intelligence 结构，避免缺字段导致可靠性误判。
        """
        def _as_dict(value: Any) -> Dict[str, Any]:
            if isinstance(value, dict):
                return value
            if isinstance(value, list):
                first = next((x for x in value if isinstance(x, dict)), None)
                if isinstance(first, dict):
                    return first
            return {}

        dash = _as_dict(dashboard)
        intel = _as_dict(dash.get("intelligence", {}))

        def _v(value: Any) -> str:
            text = str(value or "").strip()
            if not text or text.upper() in {"N/A", "NA", "NULL", "NONE", "-"}:
                return "无法判断（数据不足）"
            if ("数据缺失" in text) or ("仅从技术面判断" in text):
                return "无法判断（数据不足）"
            return text

        industry = _as_dict(intel.get("industry_boom", {}))
        company = _as_dict(intel.get("company_analysis", {}))
        valuation = _as_dict(intel.get("valuation_snapshot", {}))
        gap = _as_dict(intel.get("expectation_gap", {}))

        industry_level = _v(industry.get("level"))
        industry_cycle = _v(industry.get("cycle_phase"))
        industry_evidence = GeminiAnalyzer._compose_industry_evidence(
            level=industry_level,
            cycle_phase=industry_cycle,
            evidence=_v(industry.get("evidence")),
            payload={},
        )
        intel["industry_boom"] = {
            "level": industry_level,
            "cycle_phase": industry_cycle,
            "evidence": industry_evidence,
        }
        company_positioning = GeminiAnalyzer._compose_brief_points(
            text=_v(company.get("positioning")),
            fallback_points=[],
        )
        company_growth = GeminiAnalyzer._compose_brief_points(
            text=_v(company.get("growth_quality")),
            fallback_points=[],
        )
        company_risks = GeminiAnalyzer._compose_brief_points(
            text=_v(company.get("core_risks")),
            fallback_points=[],
        )
        intel["company_analysis"] = {
            "positioning": company_positioning,
            "growth_quality": company_growth,
            "core_risks": company_risks,
        }
        intel["valuation_snapshot"] = {
            "valuation_conclusion": _v(valuation.get("valuation_conclusion")),
            "pe_pb_ps_percentile": _v(valuation.get("pe_pb_ps_percentile")),
            "vs_industry_percentile": _v(valuation.get("vs_industry_percentile")),
        }
        intel["expectation_gap"] = {
            "gap_verdict": _v(gap.get("gap_verdict")),
            "market_expectation": _v(gap.get("market_expectation")),
            "company_guidance": _v(gap.get("company_guidance")),
        }

        dash["intelligence"] = intel
        return dash

    @staticmethod
    def _enrich_dashboard_from_payload(dashboard: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        使用模型已返回的其它字段，对 dashboard 缺失维度做二次回填，减少“无法判断”。
        """
        def _as_dict(value: Any) -> Dict[str, Any]:
            if isinstance(value, dict):
                return value
            if isinstance(value, list):
                first = next((x for x in value if isinstance(x, dict)), None)
                if isinstance(first, dict):
                    return first
            return {}

        def _is_missing(value: Any) -> bool:
            t = str(value or "").strip()
            if not t:
                return True
            u = t.upper()
            if u in {"N/A", "NA", "NULL", "NONE", "-"}:
                return True
            return ("无法判断" in t) or ("数据不足" in t) or ("数据缺失" in t) or ("仅从技术面判断" in t)

        def _pick(*candidates: Any) -> str:
            for c in candidates:
                if not _is_missing(c):
                    return str(c).strip()
            return "无法判断（数据不足）"

        dash = _as_dict(dashboard)
        intel = _as_dict(dash.get("intelligence", {}))

        industry = _as_dict(intel.get("industry_boom", {}))
        company = _as_dict(intel.get("company_analysis", {}))
        valuation = _as_dict(intel.get("valuation_snapshot", {}))
        gap = _as_dict(intel.get("expectation_gap", {}))
        discretionary = _as_dict(dash.get("discretionary_advice", {}))

        # 兼容旧字段，补齐新结构字段
        company_positioning_raw = _pick(
            company.get("positioning"),
            payload.get("company_highlights"),
            payload.get("fundamental_analysis"),
            intel.get("latest_news"),
        )
        company_growth_raw = _pick(
            company.get("growth_quality"),
            intel.get("earnings_outlook"),
            payload.get("analysis_summary"),
        )
        company_risks_raw = _pick(
            company.get("core_risks"),
            payload.get("risk_warning"),
            "; ".join(str(x) for x in (intel.get("risk_alerts") or []) if x),
        )
        company["positioning"] = GeminiAnalyzer._compose_brief_points(
            text=company_positioning_raw,
            fallback_points=[
                f"业务定位线索：{payload.get('company_highlights')}",
                f"竞争与护城河线索：{payload.get('fundamental_analysis')}",
                f"近期动态线索：{intel.get('latest_news')}",
            ],
        )
        company["growth_quality"] = GeminiAnalyzer._compose_brief_points(
            text=company_growth_raw,
            fallback_points=[
                f"业绩线索：{intel.get('earnings_outlook')}",
                f"趋势线索：{payload.get('analysis_summary')}",
                f"市场反馈：{payload.get('market_sentiment')}",
            ],
        )
        company["core_risks"] = GeminiAnalyzer._compose_brief_points(
            text=company_risks_raw,
            fallback_points=[
                f"风险提示：{payload.get('risk_warning')}",
                f"风险事件：{'; '.join(str(x) for x in (intel.get('risk_alerts') or []) if x)}",
                "重点跟踪需求波动、价格压力与执行偏差",
            ],
        )

        valuation["valuation_conclusion"] = _pick(
            valuation.get("valuation_conclusion"),
            payload.get("fundamental_analysis"),
            "估值结论需结合PE/PB/PS动态确认",
        )
        valuation["pe_pb_ps_percentile"] = _pick(
            valuation.get("pe_pb_ps_percentile"),
            intel.get("valuation_percentile"),
        )
        valuation["vs_industry_percentile"] = _pick(
            valuation.get("vs_industry_percentile"),
            intel.get("industry_valuation_percentile"),
        )

        score = GeminiAnalyzer._normalize_sentiment_score(payload.get("sentiment_score", 50))
        gap["gap_verdict"] = _pick(
            gap.get("gap_verdict"),
            "预期偏乐观" if score >= 65 else ("预期中性" if score >= 45 else "预期偏谨慎"),
        )
        gap["market_expectation"] = _pick(
            gap.get("market_expectation"),
            payload.get("market_sentiment"),
            intel.get("sentiment_summary"),
        )
        gap["company_guidance"] = _pick(
            gap.get("company_guidance"),
            intel.get("earnings_outlook"),
            payload.get("news_summary"),
        )

        # 保底 AI 理解层，避免前端完全缺失
        discretionary["free_judgement"] = _pick(
            discretionary.get("free_judgement"),
            payload.get("analysis_summary"),
        )
        discretionary["action_suggestion"] = _pick(
            discretionary.get("action_suggestion"),
            payload.get("buy_reason"),
            f"当前建议：{payload.get('operation_advice') or '观望'}，按触发条件分批执行并严格风控。",
        )
        discretionary["note"] = _pick(
            discretionary.get("note"),
            "软建议仅作补充，不覆盖硬风控与最终闸门。",
        )

        industry_level = _pick(industry.get("level"), payload.get("sector_position"))
        industry_cycle = _pick(industry.get("cycle_phase"), payload.get("trend_prediction"))
        industry_evidence_raw = _pick(
            industry.get("evidence"),
            payload.get("news_summary"),
            payload.get("analysis_summary"),
            payload.get("trend_prediction"),
        )
        industry["level"] = industry_level
        industry["cycle_phase"] = industry_cycle
        industry["evidence"] = GeminiAnalyzer._compose_industry_evidence(
            level=industry_level,
            cycle_phase=industry_cycle,
            evidence=industry_evidence_raw,
            payload=payload,
        )

        intel["industry_boom"] = industry
        intel["company_analysis"] = company
        intel["valuation_snapshot"] = valuation
        intel["expectation_gap"] = gap
        dash["intelligence"] = intel
        dash["discretionary_advice"] = discretionary
        return dash

    @staticmethod
    def _compose_industry_evidence(level: Any, cycle_phase: Any, evidence: Any, payload: Dict[str, Any]) -> str:
        """
        将行业景气 evidence 统一为 2-3 条可读要点，避免过短/占位语。
        """
        def _is_missing(value: Any) -> bool:
            t = str(value or "").strip()
            if not t:
                return True
            u = t.upper()
            if u in {"N/A", "NA", "NULL", "NONE", "-"}:
                return True
            return ("无法判断" in t) or ("数据不足" in t) or ("数据缺失" in t) or ("仅从技术面判断" in t)

        def _short(value: Any, max_len: int = 52) -> str:
            t = str(value or "").strip().replace("\n", " ").replace("\r", " ")
            if len(t) > max_len:
                return t[:max_len] + "..."
            return t

        def _strip_index_prefix(value: str) -> str:
            p = str(value or "").strip()
            # 去除前缀编号，避免出现 "1) 1) xxx"
            return re.sub(r"^(?:\d+\s*[\)\.、]\s*)+", "", p).strip()

        points: List[str] = []
        raw_text = str(evidence or "").strip()
        normalized = (
            raw_text.replace("\n", "；")
            .replace("\r", "；")
            .replace("|", "；")
            .replace("。", "；")
            .replace(";", "；")
        )
        for raw in normalized.split("；"):
            p = _strip_index_prefix(raw.strip().lstrip("-•").strip())
            if not p:
                continue
            if p not in points:
                points.append(p)
            if len(points) >= 3:
                break

        if len(points) < 2 and not _is_missing(level):
            points.append(f"景气等级判断：{_short(level)}")
        if len(points) < 2 and not _is_missing(cycle_phase):
            points.append(f"周期阶段观察：{_short(cycle_phase)}")
        if len(points) < 3 and not _is_missing(payload.get("news_summary")):
            points.append(f"催化线索：{_short(payload.get('news_summary'))}")
        if len(points) < 3 and not _is_missing(payload.get("market_sentiment")):
            points.append(f"市场定价反馈：{_short(payload.get('market_sentiment'))}")
        if len(points) < 3 and not _is_missing(payload.get("analysis_summary")):
            points.append(f"景气跟踪重点：{_short(payload.get('analysis_summary'))}")

        deduped: List[str] = []
        for p in points:
            clean_p = _strip_index_prefix(p)
            if clean_p and (clean_p not in deduped):
                deduped.append(clean_p)
            if len(deduped) >= 3:
                break

        if not deduped:
            deduped = [
                "需求与订单边际变化需持续跟踪",
                "价格与库存节奏决定景气持续性",
                "政策与资本开支是下一阶段关键变量",
            ]
        elif len(deduped) == 1:
            deduped.append("需结合供需与政策变量二次验证")
            deduped.append("关注订单兑现与库存拐点信号")
        elif len(deduped) == 2:
            deduped.append("持续跟踪催化兑现节奏与景气延续性")

        return "；".join(deduped[:3])

    @staticmethod
    def _compose_brief_points(text: Any, fallback_points: List[str]) -> str:
        """
        将任意短文本规范为 2-3 条要点，适用于公司分析等字段。
        """
        def _is_missing(value: Any) -> bool:
            t = str(value or "").strip()
            if not t:
                return True
            u = t.upper()
            if u in {"N/A", "NA", "NULL", "NONE", "-"}:
                return True
            return ("无法判断" in t) or ("数据不足" in t) or ("数据缺失" in t) or ("仅从技术面判断" in t)

        def _short(value: Any, max_len: int = 52) -> str:
            t = str(value or "").strip().replace("\n", " ").replace("\r", " ")
            if len(t) > max_len:
                return t[:max_len] + "..."
            return t

        def _strip_index_prefix(value: str) -> str:
            p = str(value or "").strip()
            return re.sub(r"^(?:\d+\s*[\)\.、]\s*)+", "", p).strip()

        points: List[str] = []
        raw = str(text or "").strip()
        normalized = (
            raw.replace("\n", "；")
            .replace("\r", "；")
            .replace("|", "；")
            .replace("。", "；")
            .replace(";", "；")
        )
        for item in normalized.split("；"):
            p = _strip_index_prefix(item.strip().lstrip("-•").strip())
            if not p:
                continue
            if p not in points:
                points.append(p)
            if len(points) >= 3:
                break

        for fb in (fallback_points or []):
            if len(points) >= 3:
                break
            if _is_missing(fb):
                continue
            p = _strip_index_prefix(_short(fb))
            if p and p not in points:
                points.append(p)

        if not points:
            points = [
                "需补充经营与竞争数据后再提高结论置信度",
                "优先跟踪订单兑现、利润率与现金流表现",
                "结合管理层执行与行业景气同步验证",
            ]
        elif len(points) == 1:
            points.append("需结合财报与订单数据做二次确认")
            points.append("关注经营质量与执行节奏的持续性")
        elif len(points) == 2:
            points.append("持续跟踪关键变量兑现情况")

        return "；".join(points[:3])

    @staticmethod
    def _normalize_dashboard_shape(dashboard: Any) -> Dict[str, Any]:
        """
        统一 dashboard 结构，兼容模型把对象误返回成字符串/数组的情况。
        """
        def _as_dict(value: Any) -> Dict[str, Any]:
            if isinstance(value, dict):
                return value
            if isinstance(value, list):
                first = next((x for x in value if isinstance(x, dict)), None)
                if isinstance(first, dict):
                    return first
            return {}

        dash = _as_dict(dashboard)

        def _as_text(value: Any, default: str = "") -> str:
            if value is None:
                return default
            text = str(value).strip()
            return text or default

        core_raw = dash.get("core_conclusion", {})
        core = _as_dict(core_raw)
        if not core and core_raw:
            core = {"one_sentence": _as_text(core_raw, "分析完成")}
        core["position_advice"] = _as_dict(core.get("position_advice", {}))

        data_raw = dash.get("data_perspective", {})
        data_persp = _as_dict(data_raw)
        if not data_persp and data_raw:
            data_persp = {"summary": _as_text(data_raw)}
        data_persp["trend_status"] = _as_dict(data_persp.get("trend_status", {}))
        data_persp["price_position"] = _as_dict(data_persp.get("price_position", {}))
        data_persp["volume_analysis"] = _as_dict(data_persp.get("volume_analysis", {}))
        data_persp["chip_structure"] = _as_dict(data_persp.get("chip_structure", {}))

        battle_raw = dash.get("battle_plan", {})
        battle = _as_dict(battle_raw)
        if not battle and battle_raw:
            battle = {"summary": _as_text(battle_raw)}
        battle["sniper_points"] = _as_dict(battle.get("sniper_points", {}))
        battle["position_strategy"] = _as_dict(battle.get("position_strategy", {}))

        strategy_raw = dash.get("strategy_execution", {})
        strategy = _as_dict(strategy_raw)
        if not strategy and strategy_raw:
            strategy = {"execution_plan": {"position_plan": _as_text(strategy_raw)}}
        strategy["final_gate"] = _as_dict(strategy.get("final_gate", {}))
        strategy["execution_plan"] = _as_dict(strategy.get("execution_plan", {}))
        strategy["capital_flow"] = _as_dict(strategy.get("capital_flow", {}))
        strategy["scenario_playbook"] = _as_dict(strategy.get("scenario_playbook", {}))

        dash["core_conclusion"] = core
        dash["data_perspective"] = data_persp
        dash["battle_plan"] = battle
        dash["user_position_advice"] = _as_dict(dash.get("user_position_advice", {}))
        dash["portfolio_risk_advice"] = _as_dict(dash.get("portfolio_risk_advice", {}))
        dash["strategy_execution"] = strategy
        dash["discretionary_advice"] = _as_dict(dash.get("discretionary_advice", {}))
        return dash

    def _harden_analysis_result(self, result: AnalysisResult) -> AnalysisResult:
        """
        结果可靠性加固：
        - 统一 operation_advice 与 decision_type
        - 与 final_gate 冲突时执行降级
        - 关键维度缺失时，禁止激进建议并降低置信度
        """
        if result is None:
            return result

        dashboard = result.dashboard if isinstance(result.dashboard, dict) else {}
        strategy = dashboard.get("strategy_execution", {}) if isinstance(dashboard, dict) else {}
        final_gate = strategy.get("final_gate", {}) if isinstance(strategy, dict) else {}
        verdict = str(final_gate.get("verdict", "") or "").strip()

        # 1) 建议归一化 + 决策类型同步
        result.operation_advice = self._normalize_operation_advice(result.operation_advice)
        result.decision_type = self._decision_type_from_advice(result.operation_advice)

        # 2) 最终闸门冲突处理
        if verdict == "不建议":
            result.operation_advice = "观望"
            result.decision_type = "hold"
            if result.sentiment_score > 55:
                result.sentiment_score = 55
            result.confidence_level = "低"
        elif verdict == "存疑" and result.operation_advice in {"买入", "加仓", "强烈买入"}:
            result.operation_advice = "观望"
            result.decision_type = "hold"
            if result.sentiment_score > 60:
                result.sentiment_score = 60

        # 3) 关键维度完整性
        intel = dashboard.get("intelligence", {}) if isinstance(dashboard, dict) else {}
        missing = []
        if not (isinstance(intel.get("industry_boom"), dict) and intel.get("industry_boom", {}).get("level")):
            missing.append("industry_boom")
        if not (isinstance(intel.get("company_analysis"), dict) and intel.get("company_analysis", {}).get("positioning")):
            missing.append("company_analysis")
        if not (isinstance(intel.get("valuation_snapshot"), dict) and intel.get("valuation_snapshot", {}).get("valuation_conclusion")):
            missing.append("valuation_snapshot")
        if not (isinstance(intel.get("expectation_gap"), dict) and intel.get("expectation_gap", {}).get("gap_verdict")):
            missing.append("expectation_gap")

        if missing:
            if result.operation_advice in {"买入", "加仓", "强烈买入"}:
                result.operation_advice = "观望"
                result.decision_type = "hold"
            if result.sentiment_score > 58:
                result.sentiment_score = 58
            result.confidence_level = "低"
            note = f"关键维度缺失: {', '.join(missing[:6])}"
            if isinstance(strategy, dict):
                fg = dict(final_gate) if isinstance(final_gate, dict) else {}
                fg["verdict"] = fg.get("verdict") or "存疑"
                ap = str(fg.get("alternative_plan", "") or "").strip()
                fg["alternative_plan"] = f"{ap}；{note}".strip("；")
                strategy["final_gate"] = fg
                dashboard["strategy_execution"] = strategy
                result.dashboard = dashboard

        return result

    @staticmethod
    def _normalize_operation_advice(advice: str) -> str:
        t = str(advice or "").strip()
        if not t:
            return "观望"
        if "强烈买入" in t or "强买" in t:
            return "强烈买入"
        if "买入" in t:
            return "买入"
        if "加仓" in t:
            return "加仓"
        if "持有" in t:
            return "持有"
        if "观望" in t:
            return "观望"
        if "减仓" in t:
            return "减仓"
        if "卖出" in t or "清仓" in t:
            return "卖出"
        return "观望"

    @staticmethod
    def _decision_type_from_advice(advice: str) -> str:
        a = str(advice or "").strip()
        if a in {"买入", "加仓", "强烈买入"}:
            return "buy"
        if a in {"卖出", "减仓", "强烈卖出"}:
            return "sell"
        return "hold"
    
    def batch_analyze(
        self, 
        contexts: List[Dict[str, Any]],
        delay_between: float = 2.0
    ) -> List[AnalysisResult]:
        """
        批量分析多只股票
        
        注意：为避免 API 速率限制，每次分析之间会有延迟
        
        Args:
            contexts: 上下文数据列表
            delay_between: 每次分析之间的延迟（秒）
            
        Returns:
            AnalysisResult 列表
        """
        results = []
        
        for i, context in enumerate(contexts):
            if i > 0:
                logger.debug(f"等待 {delay_between} 秒后继续...")
                time.sleep(delay_between)
            
            result = self.analyze(context)
            results.append(result)
        
        return results


# 便捷函数
def get_analyzer() -> GeminiAnalyzer:
    """获取 Gemini 分析器实例"""
    return GeminiAnalyzer()


if __name__ == "__main__":
    # 测试代码
    logging.basicConfig(level=logging.DEBUG)
    
    # 模拟上下文数据
    test_context = {
        'code': '600519',
        'date': '2026-01-09',
        'today': {
            'open': 1800.0,
            'high': 1850.0,
            'low': 1780.0,
            'close': 1820.0,
            'volume': 10000000,
            'amount': 18200000000,
            'pct_chg': 1.5,
            'ma5': 1810.0,
            'ma10': 1800.0,
            'ma20': 1790.0,
            'volume_ratio': 1.2,
        },
        'ma_status': '多头排列 📈',
        'volume_change_ratio': 1.3,
        'price_change_ratio': 1.5,
    }
    
    analyzer = GeminiAnalyzer()
    
    if analyzer.is_available():
        print("=== AI 分析测试 ===")
        result = analyzer.analyze(test_context)
        print(f"分析结果: {result.to_dict()}")
    else:
        print("Gemini API 未配置，跳过测试")
