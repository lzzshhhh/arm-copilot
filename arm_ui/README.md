# 臂管家 Web UI（arm_ui）

一键式 SO-101 机械臂配置向导 + 对话助手。本目录是独立新增的界面层，
**不改动** `arm_copilot/` 原有代码，只从中复用：

- 工具安全层：白名单校验 `validate`、风险分级 `classify_risk`、mock 模拟 `mock_run`
- 确认门：`ToolContext.approve` 回调（Web 里由弹窗答复）
- 产品档案：`PRODUCTS["so101"]` 的四阶段流程（安装 → 连接配置 → 校准 → 采集/训练/评估）
- LLM Agent：`Agent.handle` + 工具注册表（对话页）

## 启动

```bash
# 在仓库根目录
pip install -r arm_ui/requirements.txt      # fastapi / uvicorn（一次性）

python arm_ui/app.py --mock                 # 🎭 模拟模式：无硬件完整演示，自动打开浏览器
python -m arm_ui --mock                     # 等价写法
python arm_ui/app.py                        # 真实模式：命令会在本机真实执行，注意安全
python arm_ui/app.py --mock --port 8123 --no-open
```

界面地址 `http://127.0.0.1:8000`，仅监听本机。

## 使用

- **🪄 配置向导**：闯关式四个关卡，共 13 步（configure 关卡含 arm_ui 注入的
  leader 端口识别步骤）。每步点「开始这一步」即可；
  黄色（risky）/红色（hardware）命令会先弹确认窗，红色代表机械臂会真实运动；
  真实模式下若命令等待"按回车"（如 find-port 拔线识别、校准摆姿势），
  日志抽屉会出现橙色「⏎ 我做好了，继续」按钮，红色「⏹ 停止」可随时终止当前命令；
  进度自动保存在 `arm_ui/state/progress.json`，关掉页面再开可断点续跑
  （向导结构变化时非 install 关卡自动重置对齐）；命令与输出完整落盘
  `arm_ui/state/run-log.md` 便于排障；「重新开始」清空全部进度。
- **🔌 端口设置**：第二关前两步（识别 follower / leader 端口）跑完 find-port 后，
  检测到唯一端口会**自动填入并保存**（弹提示告知）；两臂同时在线检测到多个端口时
  不瞎猜，给出可点击的候选；也可手动填 + 「🔄 对调」。
  后续命令里的 `/dev/ttyACM0|1` 会自动替换成实际端口；Windows 下端口未设置会提前拦截。
- **💬 问一问**：和终端版一样的 LLM 对话，工具调用过程以卡片展示；
  对话里触发的命令同样走确认弹窗。未配置 `.env` 模型时对话不可用，
  但向导完全不受影响。

## 目录结构

```
arm_ui/
├── app.py          # FastAPI 应用 + 启动入口（python arm_ui/app.py）
├── state.py        # 全局状态组装（向导 / 确认门 / 对话共享环境）
├── flow.py         # 流程引擎：状态机、端口替换、进度持久化
├── servo_tool.py   # 舵机 ID 工具：扫描 / 单舵机改号（复用 lerobot 协议，多电机在线拒绝）
├── approvals.py    # 确认门桥接：工作线程阻塞 ⇄ HTTP 答复（超时 fail-closed）
├── events.py       # 事件总线 + SSE
├── executor.py     # 命令执行器：mock 逐行回放 / 真实 Popen 流式输出
├── static/         # 前端（原生 HTML/CSS/JS，无构建）
├── tests/          # 流程引擎与 API 测试（pytest）
└── state/          # 运行时进度（progress.json），可随删
```

## 测试

```bash
pytest arm_ui/tests        # 仅 UI 层
pytest                     # 全仓库（含原有 tests/）
```

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/profile` | 产品档案（关卡/步骤）+ 版本 / mock / 对话可用性 |
| GET/POST | `/api/progress` · `/api/progress/reset` | 进度查询 / 重置 |
| POST | `/api/steps/{id}/run` · `/api/steps/{id}/skip` | 执行 / 跳过一步（202，事件走 SSE） |
| GET/POST | `/api/ports` | 端口读写（follower / leader） |
| GET/POST | `/api/record-config` | 采集参数（数据集名称 / 条数），执行时自动替换进 record 命令 |
| POST | `/api/env-check` | 环境体检（复用 env_check 工具） |
| GET | `/api/events` | SSE 事件流 |
| GET/POST | `/api/approvals` · `/api/approvals/{id}` | 确认门查询 / 答复 |
| POST | `/api/interactive/input` · `/api/interactive/stop` | 向交互式命令发送回车 / 停止当前命令 |
| GET/POST | `/api/servo/scan` · `/api/servo/set-id` | 舵机 ID 工具：扫描在线 ID / 单舵机改号（多电机在线拒绝执行） |
| POST | `/api/chat` · `/api/chat/reset` | 对话（202） / 清空 |
| GET | `/api/chat/messages` | 历史消息 |
