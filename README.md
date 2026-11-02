# arm-copilot · 臂管家

具身智能产品部署 AI 助手：以对话方式指导并**代为执行** SO-101 机械臂等开源具身智能产品的
安装、配置、校准、数据采集与训练部署全流程。

## 功能

- **具身智能问答**：VLA、模仿学习、ACT / Diffusion Policy、数据采集等概念解答
- **命令行操作**：通过白名单 + 分级确认的 `run_command` 工具，代用户执行
  `lerobot-*` / `pip` 命令，完成环境体检、安装、电机 ID 设置、校准等
- **产品知识档案**：SO-101 分阶段流程（确切命令 + 验证 + 排障 + FAQ），后续升级为 RAG 检索

## 快速开始

要求 Python ≥ 3.10（LeRobot 本身要求 ≥ 3.12，建议直接用 3.12）。

```bash
git clone <本仓库地址>
cd arm-copilot
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

pip install -e .
cp .env.example .env   # 填入你的 LLM API 配置
```

`.env` 支持任何 OpenAI 兼容接口（GLM / DeepSeek / Qwen / OpenAI / 本地 vLLM），示例见
[.env.example](.env.example)。

```bash
arm-copilot            # 或 python -m arm_copilot
arm-copilot --mock     # 演示/测试模式：所有命令仅模拟执行，不触碰真实硬件
```

会话内命令：`/reset` 清空上下文，`/exit` 退出。

试一试（mock 模式）：

```
你 > 具身智能和 VLA 是什么关系？
你 > 帮我从零配置 SO-101 机械臂
```

助手会先做环境体检（`env_check`），再逐步引导安装 → 找串口 → 设电机 ID → 校准 →
遥操作验证，risky / hardware 级命令执行前都会请求确认。

## 安全机制

| 层级 | 机制 |
|---|---|
| 白名单 | 仅允许 `lerobot-*`、`pip`、`uv`、`python --version`、`python -m pip` |
| 黑名单 | 二次拦截 `rm` / `sudo` / `git` / `curl` 等单词与 `\|` `>` `;` `` ` `` 等连接符 |
| 风险分级 | `safe` 直接执行；`risky`（安装类）需 y/n 确认；`hardware`（会驱动真实电机）额外显示安全提示 |
| mock 模式 | `--mock` 模拟执行全部命令，无硬件也能演示与测试 |
| 会话日志 | 命令与输出写入 `logs/session-*.md` |

## 支持的产品

- **SO-101**（Hugging Face LeRobot）：安装 → 配置（串口 / 电机 ID）→ 校准 → 遥操作验证，
  以及采集 / 训练 / 评估预览

路线图中将加入 Microduck（Pollen Robotics × Hugging Face）等产品档案，见下。

## 项目结构

```
arm_copilot/
├── cli.py               # Rich 终端聊天入口
├── config.py            # .env / 环境变量配置
├── agent/
│   ├── llm.py           # OpenAI 兼容客户端
│   ├── loop.py          # 从 0 实现的 tool-calling 主循环
│   └── prompts.py       # 系统提示词（人设 + 安全规则）
├── tools/
│   ├── base.py          # 工具注册表（schema / 风险分级）
│   ├── shell.py         # run_command：白名单 + 确认 + mock
│   ├── diagnostics.py   # env_check 环境体检
│   └── knowledge.py     # get_workflow / search_faq
└── products/
    ├── base.py          # 产品档案抽象
    └── so101.py         # SO-101 全流程档案
```

## 开发与测试

```bash
pip install -e ".[dev]"
pytest
```

测试不依赖网络与硬件：LLM 用 FakeLLM 脚本化，命令执行走 mock。
其中有一条不变量测试：产品档案中的每条命令都必须通过安全白名单校验。

## 路线图

- **M1** 流程引擎：多步骤状态机（前置检查 → 执行 → 验证 → 排障）、断点续跑、会话持久化
- **M2** 长任务管理：`lerobot-record` / `lerobot-train` 后台运行与进度查询；后训练
  （ACT / SmolVLA / pi0）选型引导
- **M3** RAG：摄取 LeRobot 官方文档与产品手册，chromadb 向量检索 + 引用回答
- **M4** 多产品：Microduck（`pollen-robotics/microduck`）档案、产品路由、可选 Web UI

## 参考

- [LeRobot 官方文档](https://huggingface.co/docs/lerobot/index)
- [SO-101 指南](https://huggingface.co/docs/lerobot/so101)
- [Microduck](https://pollen-robotics.com/microduck/)

## License

MIT
