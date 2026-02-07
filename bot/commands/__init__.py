# -*- coding: utf-8 -*-
"""
===================================
命令处理器模块
===================================

包含所有机器人命令的实现。
"""

from bot.commands.base import BotCommand
from bot.commands.help import HelpCommand
from bot.commands.status import StatusCommand
from bot.commands.analyze import AnalyzeCommand
from bot.commands.market import MarketCommand
from bot.commands.batch import BatchCommand
from bot.commands.history import HistoryCommand
from bot.commands.watchlist import WatchlistCommand
from bot.commands.position import PositionCommand
from bot.commands.reanalyze import ReanalyzeCommand

# 所有可用命令（用于自动注册）
ALL_COMMANDS = [
    HelpCommand,
    StatusCommand,
    AnalyzeCommand,
    MarketCommand,
    BatchCommand,
    HistoryCommand,
    ReanalyzeCommand,
    WatchlistCommand,
    PositionCommand,
]

__all__ = [
    'BotCommand',
    'HelpCommand',
    'StatusCommand',
    'AnalyzeCommand',
    'MarketCommand',
    'BatchCommand',
    'HistoryCommand',
    'ReanalyzeCommand',
    'WatchlistCommand',
    'PositionCommand',
    'ALL_COMMANDS',
]
