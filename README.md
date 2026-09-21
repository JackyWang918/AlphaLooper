# AlphaLooper

本地运行的 Binance Alpha 浏览器交易辅助工具。用户手动登录并处理验证，程序接收指定的交易对、买卖方向、价格和数量，通过浏览器执行操作，并展示执行状态与结果。

## 当前状态

基础工程已初始化：前端环境状态页面、FastAPI 健康检查、SQLite 与 Alembic 迁移入口可运行。浏览器交易执行器、下单功能和策略尚未实现。

本机初始化环境：Python 3.12.13（uv 管理）、uv 0.10.12、Node.js 24.14.1、npm 11.11.0。Python 版本约束与锁文件位于 `backend`，前端使用 npm 锁文件。

详细设计见 [技术方案](docs/technical-plan.md)。

## 技术选型

| 部分 | 选型 |
| --- | --- |
| 后端 | Python、FastAPI |
| Python 项目与依赖管理 | uv，提交 `pyproject.toml` 和 `uv.lock` |
| 前端 | Vue 3、TypeScript、Vite |
| 前端依赖管理 | npm，提交 `package.json` 和 `package-lock.json` |
| 浏览器控制 | Playwright、本机 Chrome、有界面模式 |
| 数据库 | SQLite3、SQLAlchemy、Alembic |
| 实时状态 | WebSocket；断线后通过接口重新获取任务状态 |

前后端采用分离结构，放在同一个 Git 仓库中。初期仅在本机从源码启动，不考虑服务器部署、Docker、安装包或桌面应用封装。

## 目录结构（包括后续规划模块）

```text
AlphaLooper/
├─ frontend/                 # Vue 控制台
├─ backend/
│  ├─ app/
│  │  ├─ api/                # HTTP 和 WebSocket 接口
│  │  ├─ browser/            # Playwright 执行器
│  │  ├─ services/           # 指令与任务编排
│  │  ├─ models/             # 数据模型
│  │  └─ main.py             # FastAPI 入口
│  ├─ migrations/            # Alembic 数据库迁移
│  ├─ tests/
│  ├─ pyproject.toml
│  └─ uv.lock
├─ data/
│  ├─ alphalooper.db         # 本地 SQLite 数据库
│  └─ browser-profile/       # 独立 Chrome 用户目录
├─ logs/                     # 本地运行日志
├─ docs/
│  └─ technical-plan.md
├─ .gitignore
└─ README.md
```

## 第一阶段

交付一个有界面的浏览器下单验证工具：

1. 启动可控制的 Chrome 窗口，用户手动登录及处理验证。
2. 接收用户输入的 Alpha 交易链接，识别并核对交易对。
3. 在控制台输入买卖方向、价格和数量。
4. 支持“仅填表”：自动填写并核对页面实际值，不提交。
5. 支持显式提交：提交指定订单，处理正常确认弹窗，展示提交结果。
6. 展示浏览器状态、任务状态、错误信息与操作记录。

提交成功与成交成功分别记录。提交结果未知时暂停，先核对已有订单，不自动重新提交。第一阶段不包含自动选币、行情策略、超时撤单重挂或夜间无人值守交易。

## 本地运行约定

使用两个终端分别运行前后端。首次运行或更新依赖后执行依赖同步与数据库迁移。

后端，在 `backend` 目录执行：

```powershell
uv sync --locked
uv run alembic upgrade head
uv run uvicorn app.main:app --host 127.0.0.1 --port 18760
```

前端，在 `frontend` 目录执行：

```powershell
npm ci
npm run dev -- --host 127.0.0.1 --port 18761
```

访问地址为 `http://127.0.0.1:18761`，接口文档为 `http://127.0.0.1:18760/docs`。前端代理 `/api` 到后端。后端默认不启用自动重载，后续浏览器执行器将由后端管理。

使用本机 Chrome，不需要自行下载或匹配 ChromeDriver，也无需执行 `playwright install` 下载另一套浏览器。在 `backend` 目录执行 `uv run python scripts/check_browser.py` 可检查本机 Chrome 启动、输入和点击能力；该检查使用无头临时会话，不访问外部网站或使用登录数据。实际交易页面的有界面验证仍待后续完成。

前端检查：在 `frontend` 执行 `npm run build`（包含 TypeScript 检查）。后端检查：在 `backend` 执行 `uv run ruff check app migrations scripts`。

## Git 与本地数据

仅在项目根目录建立 Git 仓库，前后端可以在同一次提交中修改。

- 提交源码、文档、依赖清单、依赖锁文件和数据库迁移脚本。
- 排除 `.venv/`、`node_modules/`、前端构建产物、缓存、实际环境配置文件。
- 排除整个 `data/` 和 `logs/`，包括数据库及其 WAL/SHM 文件、浏览器登录数据和运行日志。
- 不将密码、令牌、Cookie 等会话信息写入源码或提交记录。

`.gitignore` 已创建。数据库及浏览器用户目录均属于本机运行数据，不随 Git 同步。
