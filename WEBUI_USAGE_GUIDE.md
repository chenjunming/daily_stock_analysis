# 📊 股票分析系统 - WebUI 自选股管理完整指南

> 🎉 已成功部署自选股管理功能！现在你可以完全在 Web 界面上管理自选股，无需修改 .env 文件。

## ✨ 新功能简介

### 核心功能
- ✅ **数据库存储** - 自选股列表独立存储，不依赖 .env
- ✅ **Web 界面管理** - 在页面上直接添加/删除自选股
- ✅ **市场分组展示** - 按 A股、港股、美股 分组显示
- ✅ **历史报告查看** - 点击查看每只股票的分析历史
- ✅ **即时分析** - 点击分析按钮快速分析个股
- ✅ **响应式设计** - 支持手机、平板、桌面访问

---

## 🚀 快速开始

### 1️⃣ 启动 WebUI

```bash
cd /workspaces/daily_stock_analysis
./start-webui.sh
```

或者：
```bash
python3 main.py --webui-only
```

### 2️⃣ 访问页面

打开浏览器访问：
```
http://127.0.0.1:8000
```

### 3️⃣ 开始使用

#### 添加自选股
1. 在"📌 自选股"选项卡中
2. 输入股票代码（例如：600519）
3. 可选输入股票名称（例如：贵州茅台）
4. 选择市场类型（A股、港股、美股）
5. 点击"✚ 添加"按钮

#### 支持的股票格式
- **A股** - 6 位数字（例如：600519、000001）
- **港股** - HK + 5 位数字（例如：HK00700）
- **美股** - 1-5 字母（例如：AAPL、BRK.B）

#### 管理自选股
每个自选股卡片包含：
- **股票代码和名称** - 易于识别
- **📊 分析按钮** - 实时分析该股票
- **📜 历史按钮** - 查看历史分析报告
- **× 移除按钮** - 删除该自选股

#### 查看分析报告
1. 点击任意股票卡片的"📜 历史"按钮
2. 自动加载最近 5 条分析报告
3. 报告包含：
   - 分析时间
   - 操作建议（买/卖/持有）
   - 分析摘要
   - 情绪评分

---

## 🔌 API 接口文档

所有功能都提供了 HTTP API，支持外部调用或自动化。

### 添加自选股
```http
GET /watchlist/add?code=600519&name=贵州茅台&market=CN
```

**成功响应：**
```json
{
  "success": true,
  "code": "600519",
  "name": "贵州茅台",
  "market": "CN"
}
```

### 删除自选股
```http
GET /watchlist/remove?code=600519
```

**响应：**
```json
{
  "success": true
}
```

### 获取自选股列表（分组）
```http
GET /watchlist/list
```

**响应：**
```json
{
  "success": true,
  "data": {
    "CN": [
      {
        "id": 1,
        "code": "600519",
        "name": "贵州茅台",
        "market": "CN",
        "order_key": 0,
        "created_at": "2026-02-06T10:30:00"
      }
    ],
    "HK": [...],
    "US": [...]
  }
}
```

### 获取单市场自选股
```http
GET /watchlist/list?market=CN
```

### 获取股票分析历史
```http
GET /stock/analysis?code=600519&limit=10
```

**响应：**
```json
{
  "success": true,
  "code": "600519",
  "data": [
    {
      "code": "600519",
      "name": "贵州茅台",
      "operation_advice": "建议关注",
      "sentiment_score": 75,
      "analysis_summary": "技术面向好，建议关注……",
      "trend_prediction": "短期向上",
      "created_at": "2026-02-06T10:30:00"
    }
  ]
}
```

---

## 💾 数据库结构

自选股数据存储在 SQLite 数据库中：

### Watchlist 表
| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER | 主键（自增）|
| code | VARCHAR(10) | 股票代码（唯一） |
| name | VARCHAR(50) | 股票名称 |
| market | VARCHAR(10) | 市场类型（CN/HK/US） |
| order_key | INTEGER | 排序权重 |
| remarks | TEXT | 备注信息 |
| created_at | DATETIME | 创建时间 |
| updated_at | DATETIME | 更新时间 |

### 文件位置
```
./data/stock_analysis.db
```

---

## 🧪 测试功能

项目包含了完整的测试脚本，可以验证所有功能：

```bash
python3 test_watchlist.py
```

测试包括：
- ✅ 添加自选股
- ✅ 获取自选股列表
- ✅ 按市场分组
- ✅ 删除自选股
- ✅ 查询单市场股票

---

## ⚙️ 配置说明

### 环境变量

在 `.env` 文件中配置 WebUI 参数：

```bash
# WebUI 监听地址（127.0.0.1 仅本机访问）
WEBUI_HOST=127.0.0.1

# WebUI 监听端口
WEBUI_PORT=8000

# 是否启用 WebUI
WEBUI_ENABLED=true

# 数据库文件路径
DATABASE_PATH=./data/stock_analysis.db
```

### 数据库初始化

数据库会在首次启动时自动创建，无需手动初始化。

---

## 📊 使用场景

### 场景一：日常跟踪关注股票
1. 启动 WebUI
2. 添加 5-10 只重点关注的股票
3. 每日查看历史报告、观察分析趋势

### 场景二：分市场管理投资组合
1. A股 - 添加国内上市公司
2. 港股 - 添加香港上市公司
3. 美股 - 添加美国上市公司
4. 按市场分组查看和管理

### 场景三：自动化数据收集
1. 通过 API 接口批量添加自选股
2. 定期获取分析历史
3. 导出数据进行分析

---

## 🔄 工作流程示例

### Python 脚本示例

```python
from web.services import get_watchlist_service

# 获取服务
svc = get_watchlist_service()

# 添加自选股
svc.add_stock('600519', '贵州茅台', 'CN')
svc.add_stock('HK00700', '腾讯控股', 'HK')
svc.add_stock('AAPL', '苹果公司', 'US')

# 获取所有自选股（按市场分组）
result = svc.get_watchlist_grouped()
if result['success']:
    for market, stocks in result['data'].items():
        print(f"{market}：{len(stocks)} 只")

# 获取单只股票的分析历史
result = svc.get_stock_analysis('600519', limit=5)
if result['success']:
    for report in result['data']:
        print(f"{report['code']} - {report['operation_advice']}")

# 删除自选股
svc.remove_stock('AAPL')
```

### 命令行示例

```bash
# 添加自选股
curl "http://127.0.0.1:8000/watchlist/add?code=600519&name=贵州茅台&market=CN" | json_pp

# 获取自选股列表
curl "http://127.0.0.1:8000/watchlist/list" | json_pp

# 获取分析历史
curl "http://127.0.0.1:8000/stock/analysis?code=600519" | json_pp

# 删除自选股
curl "http://127.0.0.1:8000/watchlist/remove?code=600519" | json_pp
```

---

## 📝 常见操作

### 导入自选股列表

如果已有股票代码列表，可以通过脚本批量添加：

```python
stocks = [
    ('600519', '贵州茅台', 'CN'),
    ('000001', '平安银行', 'CN'),
    ('HK00700', '腾讯控股', 'HK'),
    ('AAPL', '苹果公司', 'US'),
]

from web.services import get_watchlist_service
svc = get_watchlist_service()

for code, name, market in stocks:
    result = svc.add_stock(code, name, market)
    if result['success']:
        print(f"✓ 已添加 {code}")
    else:
        print(f"✗ 添加失败 {code}: {result['error']}")
```

### 导出自选股列表

```bash
# 导出为 JSON
curl http://127.0.0.1:8000/watchlist/list | python3 -m json.tool > watchlist.json

# 导出为 CSV
python3 << 'EOF'
import json
import csv
import urllib.request

url = 'http://127.0.0.1:8000/watchlist/list'
with urllib.request.urlopen(url) as response:
    data = json.loads(response.read())
    
with open('watchlist.csv', 'w', newline='', encoding='utf-8') as f:
    writer = csv.writer(f)
    writer.writerow(['代码', '名称', '市场'])
    
    for market, stocks in data['data'].items():
        for stock in stocks:
            writer.writerow([stock['code'], stock['name'], stock['market']])
EOF
```

---

## ⚠️ 注意事项

1. **本地访问** - WebUI 默认仅在 127.0.0.1 监听，不能从外网访问
2. **数据安全** - 数据存储在本地数据库，无远程连接
3. **编码格式** - 确保股票代码为大写（实际会自动转换）
4. **市场代码** - CN（A股）、HK（港股）、US（美股）

---

## 🐛 故障排除

### 问题：添加自选股后页面没有显示

**解决方案：**
1. 检查浏览器控制台（F12）是否有错误
2. 刷新页面重新加载数据
3. 检查数据库文件：`./data/stock_analysis.db`

### 问题：API 调用返回 404

**解决方案：**
1. 确认 WebUI 正常运行
2. 检查访问地址是否正确
3. 查看 WebUI 日志文件

### 问题：删除后数据没有更新

**解决方案：**
1. 刷新浏览器页面
2. 检查网络连接
3. 查看浏览器控制台错误信息

---

## 📚 相关文档

- [完整功能列表](WEBUI_FEATURES.md)
- [部署指南](DEPLOY.md)
- [API 文档](README.md)
- [常见问题](FAQ.md)

---

## 🎯 后续规划

即将推出的功能：
- [ ] 实时数据更新推送
- [ ] 自选股批量导入/导出
- [ ] 分析报告详情展示
- [ ] 预警设置与通知
- [ ] 性能对标分析

---

## 💡 使用建议

1. **定期查看** - 每天花 5-10 分钟查看自选股最新报告
2. **保持同步** - 定期添加新关注的股票，删除不再关注的
3. **对标分析** - 结合市场分析报告进行综合判断
4. **记录决策** - 基于分析报告进行交易决策并记录

---

**更新时间**：2026-02-06  
**版本**：v2.0  
**状态**：✅ 功能完整，可投入使用
