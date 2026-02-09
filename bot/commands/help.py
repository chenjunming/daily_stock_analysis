# -*- coding: utf-8 -*-
"""
===================================
帮助命令
===================================

显示可用命令列表和使用说明。
"""

from typing import List

from bot.commands.base import BotCommand
from bot.models import BotMessage, BotResponse


class HelpCommand(BotCommand):
    """
    帮助命令
    
    显示所有可用命令的列表和使用说明。
    也可以查看特定命令的详细帮助。
    
    用法：
        /help         - 显示所有命令
        /help analyze - 显示 analyze 命令的详细帮助
    """
    
    @property
    def name(self) -> str:
        return "help"
    
    @property
    def aliases(self) -> List[str]:
        return ["h", "帮助", "?"]
    
    @property
    def description(self) -> str:
        return "显示帮助信息"
    
    @property
    def usage(self) -> str:
        return "/help [命令名]"
    
    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        """执行帮助命令"""
        # 延迟导入避免循环依赖
        from bot.dispatcher import get_dispatcher
        
        dispatcher = get_dispatcher()
        
        # 如果指定了命令名，显示该命令的详细帮助
        if args:
            cmd_name = args[0]
            command = dispatcher.get_command(cmd_name)
            
            if command is None:
                return BotResponse.error_response(f"未知命令: {cmd_name}")
            
            # 构建详细帮助
            help_text = self._format_command_help(command, dispatcher.command_prefix)
            return BotResponse.markdown_response(help_text)
        
        # 显示所有命令列表
        commands = dispatcher.list_commands(include_hidden=False)
        prefix = dispatcher.command_prefix
        
        help_text = self._format_help_list(commands, prefix)
        return BotResponse.markdown_response(help_text)
    
    def _format_help_list(self, commands: List[BotCommand], prefix: str) -> str:
        """格式化命令列表"""
        lines = [
            "📚 **股票分析助手 - 命令帮助**",
            "",
            "可用命令：",
            "",
        ]
        
        for cmd in commands:
            # 命令名和别名
            aliases_str = ""
            if cmd.aliases:
                # 过滤掉中文别名，只显示英文别名
                en_aliases = [a for a in cmd.aliases if a.isascii()]
                if en_aliases:
                    aliases_str = f" ({', '.join(prefix + a for a in en_aliases[:2])})"
            
            lines.append(f"• {prefix}{cmd.name}{aliases_str} - {cmd.description}")
            lines.append("")

        lines.extend([
            "",
            "---",
            f"💡 输入 {prefix}help <命令名> 查看详细用法",
            "",
            "**示例：**",
            "",
            f"• {prefix}analyze 301023 - 奕帆传动",
            "",
            f"• {prefix}reanalyze 301023 - 强制重跑单股分析",
            "",
            f"• {prefix}market - 查看大盘复盘",
            "",
            f"• {prefix}batch - 批量分析自选股",
            "",
            f"• {prefix}b holdings US - 按持仓批量分析美股",
            "",
            f"• {prefix}a pos - 一键分析当前持仓（摘要+详情按钮）",
            "",
            f"• {prefix}history 301023 - 查看单股历史报告列表",
            "",
            f"• {prefix}status - 查看系统状态",
            "",
            f"• {prefix}watchlist - 查看自选股列表",
            "",
            f"• {prefix}watchlist add 301023 奕帆传动 CN - 添加到自选股",
            "",
            f"• {prefix}position set 腾讯控股 320 8 HK - 设置个人持仓",
            "",
            f"• {prefix}position batch TSM 180 12; NVDA 700 8 - 批量写入持仓",
            "",
            f"• {prefix}position total 1000000 - 设置组合总资产(人民币)",
            "",
            f"• {prefix}position fx 6.94 0.888 - 设置换算汇率",
            "",
            f"• {prefix}position sell TSM 188 30 - 卖出并自动扣减股数",
            "",
            f"• {prefix}position sync longport - 从长桥同步持仓",
            "",
            f"• {prefix}position list - 查看持仓列表",
            "",
            f"• {prefix}position remove 腾讯控股 - 删除一条持仓",
            "",
            f"• {prefix}pos fast - 快速查看持仓(不拉实时)",
            "",
            f"• {prefix}position clear - 清空全部持仓",
        ])
        
        return "\n".join(lines)
    
    def _format_command_help(self, command: BotCommand, prefix: str) -> str:
        """格式化单个命令的详细帮助"""
        lines = [
            f"📖 **{prefix}{command.name}** - {command.description}",
            "",
            f"**用法：** `{command.usage}`",
            "",
        ]
        
        # 别名
        if command.aliases:
            aliases = [f"`{prefix}{a}`" if a.isascii() else f"`{a}`" for a in command.aliases]
            lines.append(f"**别名：** {', '.join(aliases)}")
            lines.append("")
        
        # 权限
        if command.admin_only:
            lines.append("⚠️ **需要管理员权限**")
            lines.append("")
        
        return "\n".join(lines)
