# SHOPPING Agent

一个支持自然语言需求与多商家报价比较的耳机购物 Demo。你可以浏览和筛选商品，也可以描述预算、用途和偏好，让 Agent 返回可核对的购买建议。

## 功能

- **传统购物**：搜索、筛选、查看商品详情与多个商家报价。
- **Agent Shopping**：将自然语言转换为结构化条件，返回最多三条推荐；候选不足时返回实际数量，需求冲突时先澄清。
- **硬约束筛选**：核对具体购买方案的到手价、库存、连接模式、兼容性、配送及售后。
- **分层排序**：分别比较商品适配度与商家报价，展示两组权重、评分贡献和取舍。
- **事实解释**：价格、库存、配送和售后来自本地数据，语言解释沿用后端事实。
- **本地启动器**：控制服务启停、查看配置、浏览字段与运行日志。

商品、商家、报价和测评分均为**模拟数据**。正式数据生成器可重建50家商家、500个商品及2728条报价；观测时点固定为2026-09-29，不代表实时价格或履约承诺。

## 技术栈与职责

Windows · Python · FastAPI · Pydantic · SQLite · DeepSeek Harness · HTML/CSS/JavaScript · Textual

LLM处理语言、意图与工具调用；本地Python/SQLite执行价格计算、硬约束和排序。模型通过购物工具取得后端决策，最终结果沿用同次请求的事实记录。密钥保存在服务端，不进入浏览器。

## 快速开始

已验证的本地环境为Windows 10 x64、Python 3.14.7。其他Python版本与操作系统的完整兼容性尚未验证。使用安装好的Python创建虚拟环境，然后显式使用项目解释器：

```powershell
git clone https://github.com/wgq846860/SHOPPING-Agent.git
Set-Location SHOPPING-Agent
$env:PYTHONUTF8 = '1'
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

首次运行生成正式模拟数据并初始化SQLite：

```powershell
.venv\Scripts\python.exe scripts\generate_demo_data.py --profile demo --seed 20260929 --out data\demo-v1
.venv\Scripts\python.exe scripts\validate_demo_data.py --data-dir data\demo-v1
.venv\Scripts\python.exe scripts\init_database.py --data-dir data\demo-v1
```

仓库包含生成器，不包含本机数据库和生成的数据文件。必须显式传入正式档参数；脚本默认小样本档位适合开发。数据库不会在每次启动时重建。

启动规则演示：

```powershell
$env:AGENT_INTENT_MODE = 'rule_based_demo'
.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

打开 [http://127.0.0.1:8000/](http://127.0.0.1:8000/)。在启动终端按Ctrl+C停止。

也可以双击根目录 `启动器.bat`，或运行：

```powershell
.venv\Scripts\python.exe -m scripts.launcher
```

启动器提供控制台、配置、字段和日志四页。模式选择只作用于启动的服务进程；配置向导仅在你明确保存时写入 `.env`。可选择端口，服务就绪后打开浏览器。

## 配置模型模式

- `rule_based_demo` 不调用模型；完成安装及数据初始化后可离线演示。
- `llm` 使用配置的远程模型API，需要联网并发送用户需求与必要候选上下文。候选上下文最多20项；密钥留在本机。

在本地 `.env` 设置以下变量，密钥不要写入源码或提交：

```dotenv
AGENT_INTENT_MODE=llm
LLM_PROVIDER=xiaomi-mimo
LLM_BASE_URL=https://api.xiaomimimo.com/v1
LLM_MODEL=mimo-v2.6-flash
LLM_API_KEY=your-local-api-key
LLM_TIMEOUT_SECONDS=120
AGENT_BRIDGE_TOKEN=your-local-random-bridge-token
```

已支持 `xiaomi-mimo` / `mimo` 和 `deepseek-official` / `deepseek`。MiMo必须显式设置Base URL与模型；其他供应商需验证协议兼容性。Harness SDK/runtime锁定为 `0.1.5rc1`。

进程环境变量优先于 `.env`。此前设置过规则演示时，在同一PowerShell中切回模型模式：

```powershell
$env:AGENT_INTENT_MODE = 'llm'
.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

缺少模型密钥或完整购物工具桥令牌时返回明确错误；模型解析失败不静默切换规则模式。语言解释失败可保留确定性推荐，并标记 `EXPLANATION_FALLBACK`。首版每次请求独立，无跨请求聊天记忆。

## 目录与接口

```text
backend/       接口、模型适配、数据库、业务服务与排序
frontend/      三个页面、交互、商品配图与3D展示
harness/       购物工具配置与Python业务桥
scripts/       数据生成、校验、初始化与启动器
tests/         自包含的Demo测试
docs/          数据字典、API与排序说明
```

| 接口 | 用途 |
| --- | --- |
| `GET /api/health` | 服务与数据库状态 |
| `GET /api/products` | 分页搜索与筛选 |
| `GET /api/products/{id}` | 商品详情 |
| `GET /api/products/{id}/offers` | 多商家报价 |
| `GET /api/merchants/{id}` | 商家详情 |
| `POST /api/agent-shopping` | 自然语言购物请求 |

请求示例：`{"query":"800元以内的无线降噪耳机，主要通勤，续航越长越好"}`。

详见[数据字典](docs/data_dictionary.md)、[API说明](docs/api_contract.md)和[排序说明](docs/ranking_baseline.md)。页面应通过服务访问，而非双击HTML文件。

## 测试

```powershell
.venv\Scripts\python.exe -m pytest
```

测试使用临时数据与模型替身，不需要真实API Key。此Demo提供商品比较与建议，不处理付款、订单或实际履约。

## 许可

项目采用 [MIT License](LICENSE)。随附Third-party组件及许可见[第三方声明](THIRD_PARTY_NOTICES.md)。
