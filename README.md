# arm-copilot · 臂管家

具身智能产品部署 AI 助手：指导并**代为执行** SO-101 机械臂等开源具身智能产品的
安装、配置、校准、数据采集、训练与评估部署全流程。提供 **Web 网页向导**（主）与
**CLI 对话**（辅）两种使用形态，共享同一套安全工具层与产品知识档案。

## 功能

- **🪄 四关 13 步网页向导**：安装 → 连接 → 校准 → 采集/训练/评估，闯关解锁、断点续跑；
  前置设置卡（体检/连接口/电机编号/采集/训练）随流程插入对应步骤之前
- **🤖 对话助手**：VLA、模仿学习、ACT / Diffusion Policy 等概念解答；工具过程卡片化展示
- **🛡️ 安全执行**：白名单 + 风险分级 + 确认门的命令代执行（网页弹窗确认 / CLI y-n 确认）
- **📖 产品知识档案**：SO-101 分阶段流程（确切命令 + 验证 + 排障 + FAQ），后续升级为 RAG 检索

## 快速开始

要求 Python ≥ 3.10（LeRobot 本身要求 ≥ 3.12，建议直接用 3.12）。

```bash
git clone https://github.com/lzzshhhh/arm-copilot
cd arm-copilot
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

pip install -e ".[dev]"
cp .env.example .env   # 填入你的 LLM API 配置（对话功能用；不配也能跑向导）
```

`.env` 支持任何 OpenAI 兼容接口（GLM / DeepSeek / Qwen / OpenAI / 本地 vLLM），
示例见 [.env.example](.env.example)。

### Web 网页向导（推荐）

```bash
arm-copilot-web          # 或 python -m arm_copilot.web
arm-copilot-web --mock   # 模拟模式：所有命令仅模拟执行，无硬件也能完整演示
```

浏览器自动打开 `http://127.0.0.1:8000`：向导逐步引导，英文提示实时翻译成
"现在做什么"，采集/训练参数在页面填写，risky / hardware 级命令弹窗确认。
Web 使用细节与 API 一览见 [docs/web-ui.md](docs/web-ui.md)。

### CLI 对话

```bash
arm-copilot            # 或 python -m arm_copilot
arm-copilot --mock     # 演示/测试模式
```

会话内命令：`/reset` 清空上下文，`/exit` 退出。试一试（mock 模式）：

```
你 > 具身智能和 VLA 是什么关系？
你 > 帮我从零配置 SO-101 机械臂
```

## 安全机制

| 层级 | 机制 |
|---|---|
| 白名单 | 仅允许 `lerobot-*`、`pip`、`uv`、`python --version`、`python -m pip` |
| 黑名单 | 二次拦截 `rm` / `sudo` / `git` / `curl` 等单词与 `\|` `>` `;` `` ` `` 等连接符 |
| 风险分级 | `safe` 直接执行；`risky`（安装/训练类）需确认；`hardware`（驱动真实电机）红色确认 + 安全提示 |
| mock 模式 | `--mock` 模拟执行全部命令，无硬件也能演示与测试 |
| 运行日志 | 网页侧全部命令落盘 `arm_copilot/web/state/run-log.md`；CLI 侧写 `logs/session-*.md` |

## 支持的产品

- **SO-101**（Hugging Face LeRobot）：安装 → 配置（连接口 / 电机编号）→ 校准 →
  遥操作验证 → 双视角采集 → ACT 训练 → 真机评估复现

路线图中将加入 Microduck（Pollen Robotics × Hugging Face）等产品档案。

## 项目结构

```
arm_copilot/
├── cli.py / config.py        # CLI 入口与配置
├── agent/                    # LLM 客户端 / tool-calling 主循环 / 系统提示词
├── tools/                    # 安全工具层：白名单 shell、环境体检、知识检索
├── products/                 # 产品档案抽象 + SO-101 全流程档案
└── web/                      # Web UI（FastAPI + 无构建前端）
    ├── app.py                # 全部端点 + 启动入口（python -m arm_copilot.web）
    ├── flow.py               # 流程引擎：状态机 / 命令补丁 / 文案覆写
    ├── executor.py           # 子进程执行：超时分级 / PATH 补全 / 整树终止
    ├── runtime.py            # 全局组装：确认门 / 对话 / GPU 检测
    ├── events.py approvals.py servo_tool.py
    ├── static/               # 前端三件套（原生 HTML/JS/CSS）
    └── state/                # 运行时进度与日志（gitignore）

tests/                        # 全部测试（127 用例，无需网络与硬件）
docs/web-ui.md                # Web 使用说明与 API 文档
knowledge/                    # M3 RAG 语料目录（预留）
```

## 开发与测试

```bash
pip install -e ".[dev]"
pytest tests/
```

测试不依赖网络与硬件：LLM 用 FakeLLM 脚本化，命令执行走 mock。
其中有一条不变量测试：产品档案中的每条命令都必须通过安全白名单校验。

## 路线图

- **M1** ✅ 流程引擎：多步骤状态机、断点续跑、确认门（Web + CLI 双形态）
- **M2** ✅ 长任务管理：采集 / 训练 / 评估全流程网页化，双视角采集、checkpoint 管理
- **M3** RAG：摄取 LeRobot 官方文档与产品手册，chromadb 向量检索 + 引用回答
- **M4** 多产品：Microduck（`pollen-robotics/microduck`）档案、产品路由

## 参考

- [LeRobot 官方文档](https://huggingface.co/docs/lerobot/index)
- [SO-101 指南](https://huggingface.co/docs/lerobot/so101)
- [Microduck](https://pollen-robotics.com/microduck/)

## License

MIT
