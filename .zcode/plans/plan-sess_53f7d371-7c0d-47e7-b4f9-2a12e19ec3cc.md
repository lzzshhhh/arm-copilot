# 臂管家 Web UI —— 一键式配置向导 + 对话助手

## Context

arm-copilot 目前只有 Rich 终端 REPL，配置 SO-101 机械臂需要在对话中由 LLM 逐步驱动，对低年龄用户门槛过高。目标：新增一个**本地 Web 界面**，把「安装 → 连接配置 → 校准 → 采集/训练/评估」全流程封装成**按钮式向导**（点按钮执行、风险命令弹窗确认），同时保留 **LLM 对话问答**标签页。视觉风格：**活泼童趣**（大圆角卡片、明快配色、emoji 图标、大按钮大字体）。

核心原则：**向导不依赖 LLM**（确定性执行，无 API Key 也能完整走流程）；对话页复用现有 Agent。安全层（白名单 `validate`、风险分级 `classify_risk`、确认门、mock）全部复用，不重写。

## 架构

```
浏览器 (vanilla HTML/CSS/JS, 无构建步骤)
   │  REST(动作) + SSE(事件流)
FastAPI (arm_copilot/web/)
   │
   ├─ FlowEngine   ──→ 复用 tools/shell.py 的 validate/classify_risk/mock_run/format_result
   ├─ ApprovalBroker ──→ 阻塞式 approve 回调 ↔ HTTP 确认端点（向导与对话共用）
   ├─ Agent(现有)  ──→ on_event → SSE，ctx.approve 接 ApprovalBroker
   └─ PRODUCTS(现有 so101 档案)
```

## 新增文件

### 后端 `arm_copilot/web/`
- **`flow.py` — 流程引擎（核心）**
  - 从 `PRODUCTS["so101"]` 展开，步骤 ID = `{phase}.{i}`（如 `configure.1`），共 12 步
  - 状态机：`pending → running → done/failed`，支持 `skipped`（跳过）；当前阶段全部 done/skipped 后解锁下一阶段（闯关式）
  - `run_step()`：逐条命令执行 —— 校验 → 风险分级 → 非安全级走 ApprovalBroker → mock/真实执行 → 结果经 `format_result`；任一命令非 0 退出码则 failed
  - **端口替换**：进度里存 `ports: {follower, leader}`，执行前把命令中的 `/dev/ttyACM0|1` 替换为用户选择的端口（Windows 下如 COM3）；用正则从 `lerobot-find-port` 输出解析端口建议（复用 mock 输出格式 `\[(\d+)\] (COM\d+|/dev/ttyACM\d+)`）
  - **进度持久化**：`state/progress.json`（状态+端口+时间戳），启动时加载实现断点续跑；`.gitignore` 加 `state/`
- **`approvals.py` — ApprovalBroker**：`request(cmd, risk, reason)` 在工作线程创建审批项、发 SSE `approval_required`、`threading.Event` 阻塞（超时 10 分钟按拒绝，fail-closed）；`resolve(id, bool)` 由 HTTP 端点调用
- **`events.py`**：进程内 EventBus + SSE 流（`GET /api/events`）；事件：`cmd_start/cmd_output/cmd_end/step_update/approval_required/chat_tool_start/chat_reply/error`
- **`state.py`**：AppState 组装 —— `load_config()` 失败时**不崩溃**（标记对话不可用、向导照常），共享 `ToolContext.mock_state`，互斥锁保证同一时刻只跑一个步骤/一轮对话
- **`app.py`**：FastAPI 工厂 + `main()`（uvicorn 127.0.0.1:8000，`--mock`/`--port`/`--no-open` 参数，自动 `webbrowser.open`）；端点：
  - `GET /api/profile`（阶段/步骤全量数据）、`GET/POST /api/progress`、`POST /api/progress/reset`
  - `POST /api/steps/{id}/run`、`POST /api/steps/{id}/skip`、`GET/POST /api/ports`
  - `POST /api/env-check`（直接调 `make_env_check_tool(ctx).handler({})`）
  - `GET /api/events`（SSE）、`POST /api/approvals/{id}`
  - `POST /api/chat`、`POST /api/chat/reset`（后台线程跑 `Agent.handle`，事件进 SSE）

### 前端 `arm_copilot/web/static/`（index.html + app.js + style.css，纯手写无构建）
- **标签页 1「配置向导」（默认）**：顶部 🤖 臂管家标题 + 模拟模式徽章 + 总进度条（x/12）；「环境体检」卡片；端口选择卡片（主/从臂端口，点击自动填入 find-port 解析结果）；四关纵向步骤条（📦 安装环境 → 🔌 连接配置 → 🎯 校准 → 🚀 采集·训练·评估），每步大卡片：emoji、标题、友好说明、状态徽章（⏳待办/▶️执行中/✅完成/❌失败）、大按钮「▶ 开始这一步」；失败时自动展开排障提示；底部可折叠输出控制台（流式滚动）；**确认弹窗**：命令+风险徽章+目的说明，hardware 级红色醒目警告（"机械臂将真实运动，请确保周围安全！"）
- **标签页 2「💬 问一问」**：气泡对话流、大输入框；工具调用渲染为可折叠小卡片「🔧 正在查询…」；对话触发的命令确认也用同一弹窗；自写 ~40 行迷你 Markdown 渲染（粗体/代码块/列表），不引外部 CDN
- 童趣样式：CSS 变量、大圆角(16px+)、渐变浅色背景、明快主色（蓝绿+橙点缀）、≥16px 字体、按钮大触摸区、运行动画

### 修改
- `pyproject.toml`：dependencies 加 `fastapi>=0.110`、`uvicorn>=0.29`；dev 加 `httpx>=0.27`；scripts 加 `arm-copilot-ui = "arm_copilot.web.app:main"`
- `arm_copilot/tools/shell.py`：新增 `run_real_streaming(command, risk, on_output)`（Popen 逐行读、同超时表），供 web 层实时输出；现有 `_run_real` 与工具路径不动
- `README.md`：Web UI 用法章节

## 测试与验证
- 新增 `tests/test_flow.py`：步骤状态流转、端口替换、进度持久化往返、审批拒绝→步骤回到待办、mock 执行→done、阶段解锁规则
- 新增 `tests/test_web_api.py`：FastAPI TestClient —— profile/progress、mock 模式经 API 完整跑一步（SSE 收到事件）、审批 resolve 流程、chat 端点注入 FakeLLM、并发 409
- 现有 3 个测试文件必须全部通过（`pytest`）
- 端到端手验：`arm-copilot-ui --mock` → 浏览器自动打开 → 环境体检 → 依次点完安装三步 → 选端口 → 触发电机步骤看到红色确认弹窗（确认/取消都试）→ 校准通关 → 对话页问"SO-101 是什么"看工具卡片与回复
