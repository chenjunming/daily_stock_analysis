# 📊 股票分析系统 - 部署指南

> 本地开发环境部署完成! 🎉

## 🚀 快速开始

### 第一步：配置环境变量

编辑项目根目录下的 `.env` 文件，填入必要的配置：

#### 1. 配置 Gemini API Key（必填）

```bash
# 获取方式：
# 1. 访问 https://aistudio.google.com/
# 2. 点击 "Get API key"
# 3. 创建或选择项目
# 4. 复制 API key
# 5. 粘贴到下方

GEMINI_API_KEY=your_gemini_api_key_here
```

#### 2. 配置飞书通知（可选，用于接收分析结果）

```bash
# 获取方式：
# 1. 打开飞书群
# 2. 右上角 → 群设置 → 群管理工具 → 机器人
# 3. 添加内置机器人 → 自定义机器人
# 4. 创建机器人，获取 Webhook URL
# 5. 复制 URL 到下方

FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/your_key_here
```

#### 3. 配置自选股列表（可选，已有默认值）

```bash
# 沪深市场代码示例：
# 贵州茅台: 600519
# 比亚迪: 002594
# 宁德时代: 300750

STOCK_LIST=600519,300750,002594
```

### 第二步：启动 WebUI

**方式一：使用启动脚本（推荐）**

```bash
cd /workspaces/daily_stock_analysis
./start-webui.sh
```

**方式二：直接运行 Python**

```bash
cd /workspaces/daily_stock_analysis
python3 main.py --webui-only
```

**方式三：使用 webui.py**

```bash
cd /workspaces/daily_stock_analysis
python3 webui.py
```

### 第三步：访问 WebUI

打开浏览器，访问：

```
http://127.0.0.1:8000
```

## 📋 功能说明

### WebUI 主要功能

| 功能 | 说明 |
|------|------|
| **配置管理** | 编辑 API Key、自选股列表、通知配置等 |
| **手动分析** | 点击"开始分析"按钮，执行一次完整的股票分析 |
| **结果查看** | 实时查看分析报告和推荐买卖点 |
| **任务状态** | 查看历史分析任务的执行状态 |
| **定时任务** | 设置每日定时执行分析任务 |

### 分析报告内容

每份分析报告包括：

- 🎯 **核心结论** - AI 一句话核心观点
- 📊 **技术分析** - MA 趋势、支撑阻力位、强弱对比
- 💰 **买卖建议** - 精确的买入价、止损价、目标价
- 📋 **检查清单** - 每项条件的满足情况
- 📈 **市场环境** - 大盘、板块、热点信息

## 🔧 常见问题

### Q: 如何修改自选股列表？

**A:** 
1. 启动 WebUI
2. 在页面上修改"自选股列表"配置
3. 点击"保存配置"
4. 点击"开始分析"

或直接编辑 `.env` 文件中的 `STOCK_LIST` 参数。

### Q: 如何设置每日自动分析？

**A:**
1. 启动分析时选择"启用定时任务"
2. 设置每日执行时间（如 09:30）
3. 点击"开始分析"启动定时任务

或设置以下环境变量：
```bash
SCHEDULE_ENABLED=true
SCHEDULE_TIME=09:30
```

### Q: 如何关闭 WebUI？

**A:** 在终端按 `Ctrl+C` 停止服务

### Q: WebUI 访问超时？

**A:** 检查以下问题：
- 确认 WebUI 正常启动（看是否有错误日志）
- 确认访问地址是否正确（http://127.0.0.1:8000）
- 确认防火墙没有阻止端口 8000

### Q: 分析失败或无法访问 AI 模型？

**A:**
- 验证 Gemini API Key 是否正确填写
- 检查网络连接
- 如需使用代理，在 `.env` 中配置：
  ```bash
  USE_PROXY=true
  PROXY_HOST=127.0.0.1
  PROXY_PORT=10809
  ```

### Q: 飞书通知不工作？

**A:**
- 验证 Webhook URL 是否正确
- 检查网络连接和防火墙
- 查看日志文件了解错误信息：`./logs/`

## 📁 项目结构

```
/workspaces/daily_stock_analysis/
├── .env                    # 环境变量配置（需要填写）
├── main.py                 # 主程序入口
├── webui.py                # WebUI 快速启动
├── start-webui.sh          # WebUI 启动脚本
├── requirements.txt        # Python 依赖列表
├── data/                   # 数据库文件（自动创建）
├── logs/                   # 日志文件（自动创建）
├── src/                    # 核心分析模块
│   ├── analyzer.py         # 分析引擎
│   ├── config.py           # 配置管理
│   ├── notification.py     # 通知服务
│   └── ...
├── web/                    # WebUI 界面模块
│   ├── server.py           # HTTP 服务器
│   ├── handlers.py         # 请求处理
│   ├── templates.py        # HTML 模板
│   └── ...
├── bot/                    # 机器人模块
│   ├── handler.py          # 消息处理
│   ├── commands/           # 命令模块
│   └── platforms/          # 平台适配
└── data_provider/          # 数据源模块
    ├── akshare_fetcher.py  # AkShare 数据源
    ├── tushare_fetcher.py  # TuShare 数据源
    └── ...
```

## 🚀 进阶用法

### 直接运行分析（不启动 WebUI）

```bash
# 立即执行一次分析
python3 main.py

# 分析后不发送通知
python3 main.py --no-notify

# 仅进行数据获取，不进行 AI 分析
python3 main.py --dry-run

# 指定分析特定股票
python3 main.py --stocks 600519,000001

# 仅运行大盘复盘
python3 main.py --market-review

# 启用定时任务（每日自动执行）
python3 main.py --schedule
```

### 使用其他 AI 模型

如果不想用 Gemini（例如已有 DeepSeek 账号），可在 `.env` 中配置：

```bash
# DeepSeek 示例
OPENAI_API_KEY=your_openai_api_key_here
OPENAI_BASE_URL=https://api.x.ai/v1
OPENAI_MODEL=grok-4-latest
```

### Docker 部署（可选）

如需部署到服务器，可使用 Docker：

```bash
cd /workspaces/daily_stock_analysis

# 构建镜像
docker build -f docker/Dockerfile -t stock-analyzer .

# 运行容器
docker run -d \
  -p 8000:8000 \
  --env-file .env \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/logs:/app/logs \
  --name stock-analyzer \
  stock-analyzer
```

## 📞 获取帮助

- 📖 完整文档：[docs/full-guide.md](docs/full-guide.md)
- ❓ 常见问题：[docs/FAQ.md](docs/FAQ.md)
- 🚀 部署指南：[docs/DEPLOY.md](docs/DEPLOY.md)
- 🤖 Bot 配置：[docs/bot/](docs/bot/)

## 📝 下一步

1. ✅ 配置 API Key（Gemini）
2. ✅ 配置通知方式（飞书）
3. ✅ 启动 WebUI 并点击分析
4. 📊 查看分析结果
5. 🔄 设置定时任务自动分析

---

**祝你使用愉快！如有问题请查看文档或提交 Issue。** 🎉
