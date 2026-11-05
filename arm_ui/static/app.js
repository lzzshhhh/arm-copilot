/* ===== 臂管家 Web UI 前端逻辑（无构建、无外部依赖） ===== */
"use strict";

const S = {
  profile: null,
  progress: null,
  mock: false,
  chatEnabled: true,
  chatPending: false,
  currentApproval: null,
  cmdBuffer: [],
  lastOutputAt: 0,   // 最近一条命令输出的时间（判断命令是"在刷输出"还是"在等输入"）
  es: null,
};

const $ = (id) => document.getElementById(id);

/* 已知提示语的中文翻译：命令行出现这些话时，横幅告诉用户现在该做什么 */
const MOTOR_CN = {
  gripper: "夹爪", wrist_roll: "腕转", elbow_flex: "肘部",
  shoulder_rotation: "肩转", shoulder_pitch: "肩部", base_rotation: "底座",
  shoulder_pan: "肩平转", shoulder_lift: "肩抬放", wrist_flex: "腕弯",
};
const PROMPT_HINTS = [
  [/Svt\[info\]|Connecting camera|cameras? connected/i, () => "⏳ 相机与编码器启动中（约几秒）：现在别按任何方向键，等出现 Recording episode 再开始示教"],
  [/Map:\s*\d+%|Encoding|Saving episode|consolidat/i, () => "💾 正在保存/编码视频（可能一两分钟没有新输出）——耐心等，别点停止，否则数据会丢！"],
  [/Remove the USB cable/i, () => "👉 现在：把这臂的 USB 线拔掉 → 点「⏎ 继续」"],
  [/Reconnect the USB cable/i, () => "👉 现在：把 USB 线插回去，稍等它自动完成"],
  [/middle of its range of motion and press ENTER/i,
    () => "👉 现在：先把所有舵机摆到中立位（各关节都在行程中间的自然姿态）→ 再点「⏎ 继续」开始校准"],
  [/Recording positions\. Press ENTER to stop/i,
    () => "👉 录制中：舵机是松的，把每个关节慢慢扳过全程；全部动完后点「⏎ 继续」结束（数值跳动正常）"],
  [/type 'c' and press ENTER to run calibration/i, () => "👉 已有校准文件：点「⏎ 继续」沿用旧的；想重做点绿色「🔄 重新校准」"],
  [/Connect the controller board to the '(\w+)' motor only and press enter/i,
    (m) => `👉 现在：只把「${MOTOR_CN[m[1]] || m[1]}」关节的舵机单独接到控制板（其他断开）→ 点「⏎ 继续」`],
  [/Teleoperation started|Teleoperating|started teleoperation/i,
    () => "👉 遥操作进行中：手握 leader 臂移动；结束后点「⏹ 停止」"],
  [/Recording episode/i, () => "👉 采集中：示教完整动作后按 → 保存进入下一条；← 重录本条；ESC 或「⏹ 停止」结束"],
];

function matchPromptHint(line) {
  if (!line) return null;
  for (const [re, fn] of PROMPT_HINTS) {
    const m = line.match(re);
    if (m) return fn(m);
  }
  return null;
}

function showLiveHint(text) {
  const el = $("liveHint");
  el.textContent = text;
  el.hidden = false;
}

function clearLiveHint() { $("liveHint").hidden = true; }

/* ---------- 小工具 ---------- */

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

/* 迷你 Markdown：粗体 / 行内码 / 代码块 / 列表 / 标题（先整体转义，无 XSS 风险） */
function mdToHtml(md) {
  const esc = escapeHtml(md).replace(/`{3,}\n?/g, "```");
  const blocks = esc.split(/\n{2,}/);
  const out = [];
  for (const block of blocks) {
    if (block.startsWith("```")) {
      out.push(`<pre><code>${block.replace(/^```|\n?```$/g, "").trim()}</code></pre>`);
      continue;
    }
    const lines = block.split("\n");
    if (lines.every((l) => l.trim().startsWith("- ") || !l.trim())) {
      const items = lines.filter((l) => l.trim().startsWith("- "))
        .map((l) => `<li>${l.trim().slice(2)}</li>`).join("");
      if (items) { out.push(`<ul>${items}</ul>`); continue; }
    }
    let html = lines.map((l) => {
      l = l.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
           .replace(/`([^`]+)`/g, "<code>$1</code>");
      if (l.startsWith("### ")) return `<h5>${l.slice(4)}</h5>`;
      if (l.startsWith("## ")) return `<h4>${l.slice(3)}</h4>`;
      if (l.startsWith("# ")) return `<h4>${l.slice(2)}</h4>`;
      return l;
    }).join("<br>");
    out.push(`<p style="margin:0">${html}</p>`);
  }
  return out.join("");
}

let toastTimer = null;
function toast(text, isErr = false) {
  const el = $("toast");
  el.textContent = text;
  el.className = "toast" + (isErr ? " toast-err" : "");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 3200);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `请求失败（${res.status}）`);
  return data;
}

/* ---------- 渲染：向导 ---------- */

const STEP_VIEW = {
  pending:   { icon: "⏳", text: "待办" },
  running:   { icon: "▶️", text: "执行中" },
  done:      { icon: "✅", text: "完成" },
  failed:    { icon: "❌", text: "失败" },
  skipped:   { icon: "⏭️", text: "已跳过" },
};

function stepView(id) {
  const st = (S.progress.steps[id] || {}).status || "pending";
  return { status: st, ...STEP_VIEW[st] };
}

function renderProgress() {
  const p = S.progress;
  const finished = p.done_count + p.skipped_count;
  $("progressText").textContent = `${finished} / ${p.total}`;
  $("progressFill").style.width = `${p.total ? (finished / p.total) * 100 : 0}%`;

  const chips = [];
  for (const ph of S.profile.product.phases) {
    const sts = ph.steps.map((s) => (p.steps[s.id] || {}).status || "pending");
    const cls = sts.every((s) => s === "done" || s === "skipped") ? "done"
      : sts.includes("running") ? "doing" : "";
    chips.push(`<span class="chip ${cls}">${ph.emoji} ${phaseShort(ph)}</span>`);
  }
  $("phaseChips").innerHTML = chips.join("");
}

function phaseShort(ph) {
  return { install: "安装", configure: "连接", calibrate: "校准", next: "训练" }[ph.key] || ph.key;
}

function renderPhases() {
  const p = S.progress;
  const wrap = $("phases");
  wrap.innerHTML = "";
  for (const ph of S.profile.product.phases) {
    const sts = ph.steps.map((s) => (p.steps[s.id] || {}).status || "pending");
    const allDone = sts.every((s) => s === "done" || s === "skipped");
    const anyRunning = sts.includes("running");
    const phaseState = allDone
      ? `<span class="phase-state done">🏅 已通关</span>`
      : anyRunning ? `<span class="phase-state doing">⏳ 进行中</span>` : "";

    const sec = document.createElement("div");
    sec.className = "phase";
    sec.innerHTML = `<div class="phase-head">
        <span class="phase-title">${ph.emoji} ${escapeHtml(ph.label)}</span>${phaseState}
      </div>`;
    for (const step of ph.steps) sec.appendChild(renderStep(step));
    wrap.appendChild(sec);
  }
}

function renderStep(step) {
  const info = S.progress.steps[step.id] || {};
  const st = info.status || "pending";
  const unlocked = info.unlocked !== false;

  const div = document.createElement("div");
  div.className = `step st-${st}`;
  div.id = `step-${step.id.replace(".", "-")}`;

  const statusBadge = `<span style="font-size:.82rem;font-weight:700;color:var(--ink-soft)">${STEP_VIEW[st].icon} ${STEP_VIEW[st].text}</span>`;

  let buttons = "";
  if (st === "running") {
    buttons = `<button class="btn btn-orange" disabled>⏳ 执行中…</button>`;
  } else if (!unlocked) {
    buttons = `<button class="btn btn-gray locked-btn">🔒 未解锁</button>`;
  } else if (st === "done") {
    buttons = `<button class="btn btn-green run-btn" data-step="${step.id}">🔁 再跑一次</button>`;
  } else if (st === "failed") {
    buttons = `<button class="btn btn-red run-btn" data-step="${step.id}">🔁 再试一次</button>
               <button class="btn btn-ghost btn-sm skip-btn" data-step="${step.id}">跳过</button>`;
  } else if (st === "skipped") {
    buttons = `<button class="btn btn-blue run-btn" data-step="${step.id}">▶ 仍要执行</button>`;
  } else {
    buttons = `<button class="btn btn-blue run-btn" data-step="${step.id}">▶ 开始这一步</button>
               <button class="btn btn-ghost btn-sm skip-btn" data-step="${step.id}">跳过</button>`;
  }

  const cmds = step.commands.map((c) => `<code>${escapeHtml(c)}</code>`).join("");
  const guide = step.guide && step.guide.length
    ? `<details class="guide" open>
         <summary>📋 操作流程提示（什么时候点「继续」）</summary>
         <ol>${step.guide.map((g) => `<li>${escapeHtml(g)}</li>`).join("")}</ol>
       </details>` : "";
  const trouble = step.troubleshooting.length
    ? `<details class="trouble" ${st === "failed" ? "open" : ""}>
         <summary>🧯 遇到问题？点开排障提示</summary>
         <ul>${step.troubleshooting.map((t) => `<li>${escapeHtml(t)}</li>`).join("")}</ul>
       </details>` : "";

  div.innerHTML = `
    <div class="step-icon">${STEP_VIEW[st].icon}</div>
    <div class="step-body">
      <div class="step-title">${escapeHtml(step.title)} &nbsp;${statusBadge}</div>
      <div class="step-detail">${escapeHtml(step.detail)}</div>
      ${step.verify ? `<div class="step-verify">🔎 完成标志：${escapeHtml(step.verify)}</div>` : ""}
      ${guide}
      <div class="step-cmds">${cmds}</div>
      ${trouble}
    </div>
    <div class="step-side">${buttons}</div>`;
  return div;
}

function rerenderWizard() {
  renderProgress();
  renderPhases();
  $("portFollower").value = S.progress.ports.follower || "";
  $("portLeader").value = S.progress.ports.leader || "";
  if (!$("servoPort").value) $("servoPort").value = S.progress.ports.follower || "";
  // 采集设置回填（输入框正被编辑时不打扰）
  const rc = S.progress.record_config || {};
  if (document.activeElement !== $("repoIdInput")) $("repoIdInput").value = rc.repo_id || "";
  if (document.activeElement !== $("episodesInput")) $("episodesInput").value = rc.num_episodes ?? 50;
  $("cameraToggle").checked = !!rc.camera;
  if (document.activeElement !== $("cameraIndexInput")) $("cameraIndexInput").value = rc.camera_index ?? 0;
  const tc = S.progress.train_config || {};
  if (document.activeElement !== $("stepsInput")) $("stepsInput").value = tc.steps ?? 3000;
  if (document.activeElement !== $("batchInput")) $("batchInput").value = tc.batch_size ?? 16;
  $("deviceSelect").value = tc.device || "cpu";
  updateInteractiveButtons();
}

/* 真实模式下有命令在跑时，显示「继续 / 停止」按钮（find-port、校准都有"按回车"提示） */
function updateInteractiveButtons() {
  const show = !S.mock && S.progress && S.progress.running_step;
  $("inputBtn").hidden = !show;
  $("stopBtn").hidden = !show;
}

async function refreshProgress() {
  S.progress = await api("/api/progress");
  rerenderWizard();
}

/* ---------- 渲染：日志 / 端口 ---------- */

function consoleOpen() { $("console").classList.remove("collapsed"); $("consoleToggle").textContent = "收起"; }
function consoleToggle() {
  const c = $("console");
  c.classList.toggle("collapsed");
  $("consoleToggle").textContent = c.classList.contains("collapsed") ? "展开" : "收起";
}

/* 日志行数上限（防无限增长） */
const LOG_MAX_LINES = 300;

function appendLine(text, cls) {
  const body = $("consoleBody");
  const div = document.createElement("div");
  div.className = cls;
  div.textContent = text;
  body.appendChild(div);
  while (body.children.length > LOG_MAX_LINES) body.removeChild(body.firstChild);
  body.scrollTop = body.scrollHeight;
  return div;
}

function logLine(text, cls) { appendLine(text, cls); }

/* 实时输出区：校准录制阶段 ~50Hz 刷位置表（真实终端靠光标上移原地覆盖），
   网页里改为"实时块"——收起逐行历史，像终端一样原地刷新最新内容；
   命令结束或输出停歇后定格，只保留最后一屏。 */
const LIVE_MAX_LINES = 40;   // 实时块内展示的最新行数
const LIVE_TRIGGER = 9;      // 500ms 内超过该行数 → 切入实时模式
const LIVE_RENDER_MS = 100;  // 实时块重绘间隔（≈10fps，足够流畅且不卡）
const LIVE_QUIET_MS = 1500;  // 输出停歇超过该时长 → 定格退出实时模式
const live = {
  cmd: null, buf: [], times: [], nodes: [], block: null,
  renderTimer: null, prevArrival: 0,
};

function liveCommit() {
  if (live.renderTimer) { clearTimeout(live.renderTimer); live.renderTimer = null; }
  if (live.block) {
    live.block.textContent =
      "┈┈ 高频输出段（已结束，保留最后一屏）┈┈\n" + live.buf.slice(-LIVE_MAX_LINES).join("\n");
    live.block.classList.add("log-frozen");
    live.block = null;
  }
}

function liveReset(cmd) {
  liveCommit();
  live.cmd = cmd;
  live.buf = [];
  live.times = [];
  live.nodes = [];
  live.prevArrival = 0;
}

function liveStart() {
  const body = $("consoleBody");
  for (const n of live.nodes) n.remove();   // 收起该命令此前追加的散行
  live.nodes = [];
  live.block = document.createElement("div");
  live.block.className = "log-live";
  body.appendChild(live.block);
}

function liveRender() {
  live.renderTimer = null;
  if (!live.block) return;
  live.block.textContent =
    "┈┈ 实时输出（原地刷新中，无需翻动）┈┈\n" + live.buf.slice(-LIVE_MAX_LINES).join("\n");
  const body = $("consoleBody");
  body.scrollTop = body.scrollHeight;
}

function onCmdOutput(d) {
  const now = Date.now();
  if (live.cmd !== d.command) liveReset(d.command);
  // 停歇后恢复普通逐行模式
  if (live.block && live.prevArrival && now - live.prevArrival > LIVE_QUIET_MS) liveCommit();
  live.prevArrival = now;

  live.buf.push(d.line);
  if (live.buf.length > 400) live.buf.shift();
  live.times.push(now);
  live.times = live.times.filter((t) => now - t <= 500);

  if (!live.block) {
    if (live.times.length > LIVE_TRIGGER) liveStart();
    else {
      const n = appendLine(`  ${d.line}`, "log-out");
      live.nodes.push(n);
      if (live.nodes.length > 30) live.nodes.shift().remove();
    }
  }
  if (live.block && !live.renderTimer) live.renderTimer = setTimeout(liveRender, LIVE_RENDER_MS);
}

/* 解析 find-port 输出：结论句（真实交互输出）优先，其次列表式候选 */
function parsePortLines(text) {
  const verdict = (text || "").match(/MotorsBus is\s+'?(COM\d+|\/dev\/ttyACM\d+)'?/i);
  if (verdict) return [verdict[1]];
  return [...(text || "").matchAll(/\[\d+\]\s+(COM\d+|\/dev\/ttyACM\d+)/g)].map((m) => m[1]);
}

function showPortChips(ports) {
  const box = $("portChips");
  if (!ports.length) { box.innerHTML = ""; return; }
  const [a, b] = ports;
  const label = b ? `${a} 和 ${b}` : a;
  box.innerHTML = `<span class="chip pick" id="portPickChip">🔎 检测到端口：${escapeHtml(label)}，点这里自动填入 👈</span>`;
  $("portPickChip").onclick = () => {
    $("portFollower").value = a || "";
    $("portLeader").value = b || "";
    $("portSaveBtn").click();
  };
}

/* ---------- 确认弹窗 ---------- */

const RISK_VIEW = {
  safe: ["risk-safe", "🟢 safe · 查询类"],
  risky: ["risk-risky", "🟡 risky · 会改动软件环境"],
  hardware: ["risk-hardware", "🔴 hardware · 会驱动真实电机"],
};

function showApproval(d) {
  S.currentApproval = d.id;
  $("apReason").textContent = d.reason ? `这一步要执行：${d.reason}` : "";
  $("apCommand").textContent = d.command;
  const [cls, label] = RISK_VIEW[d.risk] || RISK_VIEW.safe;
  const badge = $("apRisk");
  badge.className = `risk-badge ${cls}`;
  badge.textContent = label;
  $("apWarn").hidden = d.risk !== "hardware";
  $("modal").hidden = false;
}

async function answerApproval(ok) {
  const id = S.currentApproval;
  if (!id) return;
  S.currentApproval = null;
  $("modal").hidden = true;
  try {
    await api(`/api/approvals/${id}`, { method: "POST", body: JSON.stringify({ approved: ok }) });
    logLine(ok ? "✅ 已确认执行" : "❌ 已取消执行", "log-info");
  } catch (e) {
    toast(e.message, true);
  }
}

/* ---------- SSE ---------- */

function connectSSE() {
  const es = new EventSource("/api/events");
  S.es = es;
  es.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    handleEvent(msg.kind, msg.data || {});
  };
  es.onerror = () => { /* EventSource 会自动重连 */ };
}

function handleEvent(kind, d) {
  switch (kind) {
    case "connected":
      refreshProgress().catch(() => {});
      break;

    case "step_update":
      refreshProgress().then(() => {
        if (d.status === "running") {
          const el = document.getElementById(`step-${(d.step || "").replace(".", "-")}`);
          if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
        }
      }).catch(() => {});
      if (d.status === "done") toast("🎉 这一步完成啦！");
      if (d.status === "failed") { toast("😕 这一步没成功，看看日志和排障提示", true); consoleOpen(); }
      break;

    case "cmd_start":
      consoleOpen();
      S.cmdBuffer = [];
      S.pendingRecal = false;
      $("reCalBtn").hidden = true;
      clearLiveHint();
      liveReset(d.command);
      logLine(`▶ ${d.command}`, "log-cmd");
      break;

    case "cmd_output":
      S.cmdBuffer.push(d.line);
      S.lastOutputAt = Date.now();
      // 已有校准文件时的选择提示：回车=沿用，c=重新校准
      if (d.line && d.line.includes("type 'c' and press ENTER to run calibration")) {
        S.pendingRecal = true;
        $("reCalBtn").hidden = false;
      }
      // 已知提示语 → 实时中文指令横幅（解决"不知道什么时候按回车"）
      const hint = matchPromptHint(d.line);
      if (hint) showLiveHint(hint);
      onCmdOutput(d);
      break;

    case "cmd_end": {
      liveCommit();
      clearLiveHint();
      const ok = d.exit_code === 0;
      logLine(ok ? `✅ 结束（退出码 0）` : `❌ 失败（退出码 ${d.exit_code}）`, ok ? "log-ok" : "log-err");
      if (ok && /find-port/.test(d.command || "")) {
        const ports = parsePortLines(S.cmdBuffer.join("\n"));
        if (ports.length > 1) showPortChips(ports); // 单端口后端会自动填入，多端口才需要手动选
      }
      break;
    }

    case "ports_autofilled":
      toast(`🔌 已自动填入并保存：${d.target === "follower" ? "从动臂 follower" : "主动臂 leader"} = ${d.port}`);
      if (d.both) setTimeout(() => toast("🎉 两臂端口都配好啦，已自动保存！"), 600);
      refreshProgress().catch(() => {});
      break;

    case "ports_detected":
      showPortChips(d.ports || []);
      toast("🔎 检测到多个端口，请在「端口设置」里选择填入", true);
      break;

    case "cmd_cancelled":
      logLine("🚫 你取消了这条命令", "log-info");
      toast("已取消，这一步回到待办状态");
      break;

    case "approval_required":
      showApproval(d);
      break;

    case "approval_resolved":
      if (S.currentApproval === d.id) { S.currentApproval = null; $("modal").hidden = true; }
      if (d.timeout) toast("⏰ 确认超时，已自动取消", true);
      break;

    case "flow_reset":
      refreshProgress().catch(() => {});
      toast("🔄 已重新开始全部关卡");
      break;

    case "chat_tool_start":
      addToolCard(d);
      break;
    case "chat_tool_end":
      finishToolCard(d);
      break;
    case "chat_reply":
      finishChat(d.text, false);
      break;
    case "chat_error":
      finishChat(d.message, true);
      break;

    case "error":
      toast(d.message || "出了点小问题", true);
      refreshProgress().catch(() => {});
      break;
  }
}

/* ---------- 对话 ---------- */

function addMessage(role, html) {
  const wrap = $("messages");
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  div.innerHTML = `<div class="avatar">${role === "user" ? "🙋" : "🤖"}</div>
                   <div class="bubble">${html}</div>`;
  wrap.appendChild(div);
  wrap.scrollTop = wrap.scrollHeight;
  return div;
}

let activeToolCards = [];
function addToolCard(d) {
  const wrap = $("messages");
  const div = document.createElement("div");
  div.className = "msg assistant";
  const args = (d.arguments || "").slice(0, 120);
  div.innerHTML = `<div class="avatar">🤖</div>
    <div class="bubble"><details class="toolcard" open>
      <summary>🔧 正在使用工具 ${escapeHtml(d.name || "")} …</summary>
      <pre>${escapeHtml(args)}</pre>
    </details></div>`;
  wrap.appendChild(div);
  wrap.scrollTop = wrap.scrollHeight;
  activeToolCards.push({ name: d.name, el: div.querySelector("details") });
}

function finishToolCard(d) {
  const idx = activeToolCards.findIndex((c) => c.name === d.name);
  const card = idx >= 0 ? activeToolCards.splice(idx, 1)[0] : activeToolCards.pop();
  if (!card) return;
  card.el.open = false;
  card.el.querySelector("summary").textContent = `🔧 工具 ${d.name || ""} 的结果（点开看）`;
  const pre = card.el.querySelector("pre");
  pre.textContent = (d.result || "").slice(0, 2000);
  const wrap = $("messages");
  wrap.scrollTop = wrap.scrollHeight;
}

let typingEl = null;
function setChatPending(v) {
  S.chatPending = v;
  $("sendBtn").disabled = v;
  $("chatInput").disabled = v;
  if (v) {
    typingEl = addMessage("assistant", "<span class='typing'>🤖 正在想…</span>");
  } else if (typingEl) {
    typingEl.remove();
    typingEl = null;
  }
}

function finishChat(text, isErr) {
  setChatPending(false);
  addMessage("assistant", isErr
    ? `<b style="color:var(--red-dark)">😖 ${escapeHtml(text || "出了点问题")}</b>`
    : mdToHtml(text || "（无输出）"));
}

async function sendChat() {
  if (S.chatPending) return;
  const input = $("chatInput");
  const text = input.value.trim();
  if (!text) return;
  if (!S.chatEnabled) { toast("对话功能未启用，请先配置模型", true); return; }
  input.value = "";
  addMessage("user", escapeHtml(text));
  try {
    await api("/api/chat", { method: "POST", body: JSON.stringify({ text }) });
    setChatPending(true);
  } catch (e) {
    addMessage("assistant", `<b style="color:var(--red-dark)">😖 ${escapeHtml(e.message)}</b>`);
  }
}

async function loadChat() {
  const data = await api("/api/chat/messages");
  S.chatEnabled = data.enabled;
  if (!data.enabled) {
    $("chatHint").hidden = false;
    $("chatHint").textContent = data.error || "对话功能未启用。";
    $("chatInput").disabled = true;
    $("sendBtn").disabled = true;
    return;
  }
  const wrap = $("messages");
  wrap.innerHTML = "";
  let hasAny = false;
  for (const m of data.messages) {
    hasAny = true;
    addMessage(m.role, m.role === "assistant" ? mdToHtml(m.content) : escapeHtml(m.content));
  }
  if (!hasAny) {
    addMessage("assistant",
      "你好呀！我是臂管家 🤖 关于 SO-101 机械臂的任何问题都可以问我，比如：<b>「SO-101 是什么？」</b>、<b>「为什么要校准？」</b>");
  }
}

/* ---------- 初始化 ---------- */

async function init() {
  // GPU 状态只查一次（服务端后台预热 torch，请求本身瞬间返回）
  api("/api/train-config").then((d) => {
    const opt = $("deviceSelect").querySelector('option[value="cuda"]');
    if (!opt) return;
    if (d.cuda_available === null) opt.textContent = "GPU (CUDA)——检测中…";
    else opt.textContent = d.cuda_available ? "GPU (CUDA) ✓ 可用" : "GPU (CUDA)——未装 CUDA 版 PyTorch";
  }).catch(() => {});
  S.profile = await api("/api/profile");
  S.mock = !!S.profile.mock;
  if (S.profile.mock) $("mockBadge").hidden = false;
  else $("realBadge").hidden = false;

  await refreshProgress();
  connectSSE();
  loadChat().catch(() => {});

  /* 标签切换 */
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.onclick = () => {
      document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      $("tab-wizard").hidden = tab.dataset.tab !== "wizard";
      $("tab-chat").hidden = tab.dataset.tab !== "chat";
    };
  });

  /* 向导按钮（事件委托，重渲染后依然有效） */
  $("phases").addEventListener("click", async (ev) => {
    const locked = ev.target.closest(".locked-btn");
    const run = ev.target.closest(".run-btn");
    const skip = ev.target.closest(".skip-btn");
    try {
      if (locked) {
        toast("🔒 这一步还没解锁：先完成前面的关卡～");
        return;
      }
      if (run) await api(`/api/steps/${run.dataset.step}/run`, { method: "POST" });
      else if (skip) {
        if (!confirm("确定跳过这一步吗？跳过后后面的步骤会解锁。")) return;
        await api(`/api/steps/${skip.dataset.step}/skip`, { method: "POST" });
      }
    } catch (e) { toast(e.message, true); }
  });

  $("resetBtn").onclick = async () => {
    if (!confirm("确定要清空全部进度、重新开始吗？")) return;
    try { await api("/api/progress/reset", { method: "POST" }); } catch (e) { toast(e.message, true); }
  };

  /* 环境体检 */
  $("envBtn").onclick = async () => {
    const btn = $("envBtn");
    btn.disabled = true;
    btn.textContent = "⏳ 体检中…";
    try {
      const data = await api("/api/env-check", { method: "POST" });
      $("envOut").hidden = false;
      $("envOut").textContent = data.output;
    } catch (e) { toast(e.message, true); }
    finally { btn.disabled = false; btn.textContent = "开始体检"; }
  };

  /* 端口 */
  $("portSaveBtn").onclick = async () => {
    try {
      await api("/api/ports", {
        method: "POST",
        body: JSON.stringify({ follower: $("portFollower").value.trim(), leader: $("portLeader").value.trim() }),
      });
      toast("💾 端口已保存");
      refreshProgress().catch(() => {});
    } catch (e) { toast(e.message, true); }
  };
  $("portSwapBtn").onclick = () => {
    const a = $("portFollower").value;
    $("portFollower").value = $("portLeader").value;
    $("portLeader").value = a;
    toast("🔄 已对调，记得点「保存端口」");
  };
  $("trainSaveBtn").onclick = async () => {
    const s = parseInt($("stepsInput").value, 10);
    const b = parseInt($("batchInput").value, 10);
    const dev = $("deviceSelect").value;
    if (!Number.isInteger(s) || s < 100 || s > 1000000) { toast("训练步数填 100–1000000", true); return; }
    if (!Number.isInteger(b) || b < 1 || b > 256) { toast("Batch Size 填 1–256", true); return; }
    try {
      await api("/api/train-config", { method: "POST", body: JSON.stringify({ steps: s, batch_size: b, device: dev }) });
      toast("💾 训练设置已保存");
      refreshProgress().catch(() => {});
    } catch (e) { toast(e.message, true); }
  };
  $("recordSaveBtn").onclick = async () => {
    const repo = $("repoIdInput").value.trim();
    const n = parseInt($("episodesInput").value, 10);
    const cam = $("cameraToggle").checked;
    const ci = parseInt($("cameraIndexInput").value, 10) || 0;
    if (!repo) { toast("数据集名称没填（格式：用户名/数据集名）", true); return; }
    if (!Number.isInteger(n) || n < 1 || n > 1000) { toast("采集数量填 1–1000 的整数", true); return; }
    try {
      await api("/api/record-config", {
        method: "POST",
        body: JSON.stringify({ repo_id: repo, num_episodes: n, camera: cam, camera_index: ci }),
      });
      toast("💾 采集设置已保存");
      refreshProgress().catch(() => {});
    } catch (e) { toast(e.message, true); }
  };

  /* 舵机 ID 工具 */
  const servoShow = (text) => { $("servoOut").hidden = false; $("servoOut").textContent = text; };
  $("servoScanBtn").onclick = async () => {
    const port = $("servoPort").value.trim();
    if (!port) { toast("先填端口（如 COM11）", true); return; }
    const btn = $("servoScanBtn");
    btn.disabled = true; btn.textContent = "⏳ 扫描中…";
    try {
      const data = await api(`/api/servo/scan?port=${encodeURIComponent(port)}`);
      if (!data.ok) { servoShow("❌ " + data.reason); toast(data.reason, true); return; }
      const lines = [`端口 ${data.port} 扫描结果：`];
      const entries = Object.entries(data.baudrates || {});
      if (!entries.length) lines.push("（没有发现任何电机——确认舵机已供电、线已接好）");
      for (const [baud, ids] of entries) lines.push(`  波特率 ${baud}: 在线 ID ${ids.join(", ")}`);
      lines.push(`共发现 ${data.total_online} 个 ID（注意：共用同一 ID 的多个舵机只显示为一个）`);
      servoShow(lines.join("\n"));
    } catch (e) { toast(e.message, true); }
    finally { btn.disabled = false; btn.textContent = "🔎 扫描"; }
  };
  $("servoSetBtn").onclick = async () => {
    const port = $("servoPort").value.trim();
    const newId = parseInt($("servoNewId").value.trim(), 10);
    if (!port) { toast("先填端口", true); return; }
    if (!Number.isInteger(newId) || newId < 1 || newId > 254) { toast("新 ID 填 1–6 的整数", true); return; }
    if (!confirm(`确认改号？\n\n端口 ${port} 上必须只接这一个舵机！\n它会被改成 ID ${newId}。`)) return;
    const btn = $("servoSetBtn");
    btn.disabled = true; btn.textContent = "⏳ 写入中…";
    try {
      const data = await api("/api/servo/set-id", {
        method: "POST",
        body: JSON.stringify({ port, new_id: newId }),
      });
      if (data.ok) {
        servoShow(data.changed
          ? `✅ 成功：ID ${data.old_id} → ${data.new_id}（波特率 ${data.baudrate}），已复验在线`
          : `ℹ️ ${data.reason}`);
        toast(data.changed ? `🎉 舵机已改为 ID ${data.new_id}` : data.reason);
      } else {
        servoShow("❌ " + data.reason);
        toast(data.reason, true);
      }
    } catch (e) { toast(e.message, true); }
    finally { btn.disabled = false; btn.textContent = "✏️ 写入 ID"; }
  };

  /* 日志抽屉 */
  $("consoleToggle").onclick = consoleToggle;

  /* 已有校准 → 选择重新校准（发送 c + 回车） */
  $("reCalBtn").onclick = async () => {
    if (!S.pendingRecal) return;
    S.pendingRecal = false;
    $("reCalBtn").hidden = true;
    try {
      await api("/api/interactive/input", {
        method: "POST",
        body: JSON.stringify({ text: "c\n", streaming: false }),
      });
      logLine("c 已发送：将重新执行校准流程", "log-info");
    } catch (e) { toast(e.message, true); }
  };

  /* 交互命令：继续（发送回车）/ 停止 */
  $("inputBtn").onclick = async () => {
    // 命令还在刷输出（如校准录制）→ 走控制台注入；静止的提示 → 走 stdin
    const streaming = Date.now() - S.lastOutputAt < 800;
    try {
      await api("/api/interactive/input", {
        method: "POST",
        body: JSON.stringify({ text: "\n", streaming }),
      });
      logLine(streaming ? "⏎ 已发送回车（录制中，注入控制台）" : "⏎ 已发送回车，命令继续执行", "log-info");
    } catch (e) { toast(e.message, true); }
  };

  /* 当前运行的步骤是否是遥操作（停止=正常完成） */
  const runningCommandKind = () => {
    const sid = S.progress && S.progress.running_step;
    if (!sid) return "";
    for (const ph of (S.profile && S.profile.product ? S.profile.product.phases : []))
      for (const st of ph.steps)
        if (st.id === sid) {
          if (st.commands.some((c) => c.includes("lerobot-teleoperate"))) return "teleop";
          if (st.commands.some((c) => c.includes("lerobot-record"))) return "record";
          return "";
        }
    return "";
  };
  const runningIsTeleop = () => runningCommandKind() === "teleop";

  $("stopBtn").onclick = async () => {
    const teleop = runningIsTeleop();
    const msg = teleop
      ? "确定结束遥操作吗？\n机械臂会松开断电，这一步将标记为 ✅ 完成。"
      : "确定停止正在运行的命令吗？\n这一步会被标记为 ❌ 失败（可重跑）。";
    if (!confirm(msg)) return;
    try {
      await api("/api/interactive/stop", { method: "POST" });
      toast(teleop ? "👋 遥操作已结束，本步完成" : "⏹ 已请求停止");
    } catch (e) { toast(e.message, true); }
  };

  /* 确认弹窗 */
  $("apOk").onclick = () => answerApproval(true);
  $("apNo").onclick = () => answerApproval(false);

  /* 对话 */
  $("sendBtn").onclick = sendChat;
  $("chatInput").addEventListener("keydown", (ev) => { if (ev.key === "Enter") sendChat(); });
  $("chatResetBtn").onclick = async () => {
    try {
      await api("/api/chat/reset", { method: "POST" });
      await loadChat();
      toast("🧹 对话已清空");
    } catch (e) { toast(e.message, true); }
  };
}

init().catch((e) => {
  console.error(e);
  document.body.insertAdjacentHTML(
    "afterbegin",
    `<div style="margin:16px;padding:14px;background:#ffecec;border-radius:12px;color:#c0392b">
       页面加载失败：${escapeHtml(e.message)}（请确认服务还在运行）
     </div>`
  );
});
