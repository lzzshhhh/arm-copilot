"""流程引擎：把 ProductProfile 展开成一键闯关向导。

规则：
- 步骤 ID = "{phase}.{i}"（如 configure.1），按 install→configure→calibrate→next 顺序闯关；
- 某一步解锁条件：排在它前面的所有步骤均为 done / skipped；
- 每条命令执行前走 arm_copilot.tools.shell 的白名单与风险分级，
  非安全级先经确认门（approve 回调），拒绝则整步回到待办；
- 进度持久化到 state/progress.json，重启断点续跑。
"""

from __future__ import annotations

import json
import platform
import re
import sys
import threading
import time
from pathlib import Path

from arm_copilot.products.base import ProductProfile, Step
from arm_copilot.tools import shell as shell_mod
from arm_copilot.tools.base import RISK_SAFE

from arm_ui.events import EventBus
from arm_ui.executor import (
    _kill_tree,
    run_mock_streaming,
    run_real_streaming,
    send_console_enter,
    write_stdin,
)

PHASE_ORDER = ["install", "configure", "calibrate", "next"]
PHASE_META = {
    "install": {"emoji": "📦", "label": "第一关 · 装好小助手的环境"},
    "configure": {"emoji": "🔌", "label": "第二关 · 连接和认识机械臂"},
    "calibrate": {"emoji": "🎯", "label": "第三关 · 校准让动作更准"},
    "next": {"emoji": "🚀", "label": "第四关 · 采集、训练和展示"},
}

# 流程结构版本：向导步骤增删后 +1，旧进度文件据此对齐（非 install 关卡重置）
FLOW_VERSION = 2

# arm_ui 注入的补充步骤（不改 arm_copilot 产品档案）：
# 在 configure.1 位置插入 leader 端口识别，让两臂端口都能被引导找出并自动填入
_LEADER_PORT_STEP = Step(
    title="识别 leader 臂端口",
    detail="把 follower 臂的 USB 拔下、插上 leader 臂的 USB，再运行。"
           "识别到的端口会自动填进上方「端口设置」并保存，不需要手动抄写。",
    commands=["lerobot-find-port"],
    verify="输出 [0] COMx [FEETECH] (available)，端口自动填入",
    troubleshooting=[
        "找不到端口：确认 leader 臂已供电、USB 线是数据线。",
        "和上一步一样可以用拔线法：拔线 → 点「继续」→ 看它识别哪个口。",
    ],
)

# 每步的细致操作流程提示（前端以「📋 操作流程提示」折叠块展示；键为步骤 ID）
_STEP_GUIDES: dict[str, list[str]] = {
    "configure.0": [
        "点「开始这一步」后，若日志出现 Remove the USB cable…：把 follower 臂的 USB 线拔掉 → 点「⏎ 继续」",
        "它会打印 The port of this MotorsBus is 'COMx'，这就是 follower 的端口，会自动填入上方端口设置",
        "按提示把 USB 线插回，稍候本步自动完成",
    ],
    "configure.1": [
        "点「开始这一步」前：拔掉 follower 的 USB，插上 leader 臂的 USB",
        "日志出现 Remove the USB cable…：拔掉 leader 的 USB → 点「⏎ 继续」",
        "它打印 The port of this MotorsBus is 'COMx'（leader 的端口，自动填入）→ 插回线，本步完成",
    ],
    "configure.2": [
        "这是「一次只设一个舵机」的流程，顺序：夹爪(6)→腕转(5)→肘(4)→肩转(3)→肩(2)→底座(1)",
        "每轮日志提示 Connect…'xxx' motor only：断开其他舵机，只把提示点名的那个关节的舵机接到控制板 → 点「⏎ 继续」",
        "如果你们的舵机 ID 以前设过（ID 1-6 各不相同），这一步直接点「跳过」即可——ID 永久保存在舵机里",
    ],
    "configure.3": [
        "与上一步相同流程，这次设置 leader 臂：一次只接一个、按提示点名接对应关节",
        "舵机 ID 以前设过的话直接「跳过」",
    ],
    "calibrate.0": [
        "若日志提示已有校准文件（Press ENTER to use…）：点「⏎ 继续」沿用旧的，或点绿色「🔄 重新校准」重做",
        "提示 Move…middle…：先把所有舵机摆到中立位（底座居中、大臂竖直、肘伸直、腕摆正、夹爪半开）→ 再点「⏎ 继续」",
        "提示 Recording positions…：舵机变松，把每个关节都慢慢扳过全程（数值表实时跳动是正常的）",
        "所有关节都动过后 → 再点一次「⏎ 继续」结束录制 → 自动保存，本步 ✅",
    ],
    "calibrate.1": [
        "⚠️ 与 follower 校准相同的流程，但**中立位必须和 follower 校准时摆成同一个姿态**（底座居中、大臂竖直、肘伸直、腕摆正、夹爪半开）——两臂零位不一致，遥操作就会错位",
        "已有文件选择 → 摆中立位 → 逐关节扳全程 → 点「⏎ 继续」结束",
        "leader 臂关节少一个（没有腕转），其他一样",
    ],
    "calibrate.2": [
        "开始前：两臂摆成大致相同的姿态（follower 启动瞬间会同步过去！）、清空 follower 周围、线缆留松弛",
        "点「开始」并红色确认后，follower 上电跟随——手握 leader 臂移动，follower 实时模仿",
        "先小幅慢速确认每个关节方向都对，再放开玩；结束后点「⏹ 停止」（步骤标 ❌ 是正常的）",
    ],
    "next.0": [
        "先在上方「📼 数据采集设置」填好数据集名称（用户名/数据集名）与数量——没填会被直接拦下",
        "开始前两臂姿态对齐；点开始后 follower 上电跟随 leader（红色确认弹窗）",
        "每条 episode：从起始位示教一遍完整任务动作 → 按 **→ 右箭头** 保存并进入下一条",
        "按 **← 左箭头** 作废当前条重录；按 **ESC** 或点「⏹ 停止」结束采集（已录保留）",
        "录满设定条数自动结束（pynput 全局监听装好后，任何窗口按方向键都有效）",
    ],
    "next.1": [
        "训练无需任何操作，可能持续很久——日志长时间不动是正常的",
        "结束会打印 checkpoint 保存路径，本步自动 ✅",
    ],
    "next.2": [
        "评估时 follower 会按学到的策略真实运动——务必先清空工作区",
        "跑完自动结束；表现不对劲点「⏹ 停止」",
    ],
}

ST_PENDING = "pending"
ST_RUNNING = "running"
ST_DONE = "done"
ST_FAILED = "failed"
ST_SKIPPED = "skipped"

_FINISHED = (ST_DONE, ST_SKIPPED)

# find-port 两种输出形态：
# 非交互列表 "[0] COM3 [FEETECH] (available)"（mock / 多端口候选）；
# 交互拔线结论 "The port of this MotorsBus is 'COM12'"（lerobot ≥0.6 真实输出）
_PORT_RE = re.compile(r"\[\d+\]\s+(COM\d+|/dev/ttyACM\d+)")
_MOTORBUS_RE = re.compile(r"motorsbus is\s+'?(COM\d+|/dev/ttyACM\d+)'?", re.IGNORECASE)

# 档案命令中的占位端口：ACM0 = follower，ACM1 = leader
_FOLLOWER_PLACEHOLDER = "/dev/ttyACM0"
_LEADER_PLACEHOLDER = "/dev/ttyACM1"

# 采集命令中的占位参数（arm_ui 层替换，不动 arm_copilot 档案）
_RECORD_REPO_PLACEHOLDER = "你的用户名/数据集名"
# lerobot 0.6 起条数参数挪到了 dataset 下：--num-episodes → --dataset.num_episodes
_EPISODES_ARG_RE = re.compile(r"--num-episodes=(\d+)")
_EPISODES_RE = re.compile(r"--dataset\.num_episodes=\d+")

# 第一关安装命令补丁：档案只装 feetech（电机驱动），但第四关的
# 采集/训练/评估分别还需要 dataset / training / evaluation 附加包，
# 一次装全，避免后期各步再缺库（arm_ui 层替换，不动原档案）。
_INSTALL_PATCH_FROM = 'pip install "lerobot[feetech]"'
# pynput：录制时全局键盘监听（→ 存条 / ← 重录 / ESC 结束），lerobot 不默认安装
_INSTALL_PATCH_TO = 'pip install "lerobot[feetech,dataset,training,evaluation]" pynput'


def _patch_command(command: str) -> str:
    """展示与执行共用的命令补丁：附加包装全 + 修正 0.6 的参数名 + 关闭上传。"""
    if command.strip() == _INSTALL_PATCH_FROM:
        command = _INSTALL_PATCH_TO
    command = _EPISODES_ARG_RE.sub(r"--dataset.num_episodes=\1", command)
    # 0.6 默认 push_to_hub=true：结束时连 HF 上传，网络不通会干等超时（数据其实早已本地保存）
    if command.startswith("lerobot-record") and "--dataset.push_to_hub" not in command:
        command = command.rstrip() + " --dataset.push_to_hub=false"
    # 训练同样默认上传（缺 repo_id 直接报错）；固定输出目录，让评估步骤的
    # outputs/train/act/checkpoints/last 路径始终有效（重跑前旧目录自动归档）
    if command.startswith("lerobot-train"):
        if "--push_to_hub" not in command:
            command += " --policy.push_to_hub=false"
        if "--output_dir" not in command:
            command += " --output_dir=outputs/train/act"
    return command


def _rotate_train_dir() -> None:
    """lerobot 不允许输出目录已存在（除非 resume）：重训前把旧结果归档改名。"""
    import shutil

    target = Path.cwd() / "outputs/train/act"
    if target.exists():
        backup = target.with_name(time.strftime("act_backup_%Y%m%d_%H%M%S"))
        try:
            shutil.move(str(target), str(backup))
        except OSError:
            pass


def _ensure_last_checkpoint(out: str) -> bool:
    """训练后处理：确保 checkpoints/last 指向最新的编号 checkpoint。

    lerobot 结束时会用符号链接创建 last，但 Windows 普通用户权限创建
    symlink 需要管理员/开发者模式（WinError 1314）——checkpoint 本身
    其实已保存完整。这里改用 junction（mklink /J，无需任何特权）补建；
    当输出证实训练只死在最后这步时返回 True，调用方据此把非零退出码
    视为训练成功。
    """
    import shutil
    import subprocess

    ckpt_root = Path.cwd() / "outputs" / "train" / "act" / "checkpoints"
    numbered = [d for d in ckpt_root.glob("*") if d.is_dir() and d.name.isdigit()]
    if not numbered:
        return False
    latest = max(numbered, key=lambda d: int(d.name))
    last = ckpt_root / "last"

    if last.exists() and not last.is_symlink():
        if last.resolve() == latest.resolve():
            return "update_last_checkpoint" in out and "1314" in out
        shutil.rmtree(last)  # 旧的真实目录/junction 指错了，重建
    elif last.is_symlink():
        last.unlink()

    if sys.platform == "win32":
        # junction 对目录与 symlink 等效，且创建不需要特权。
        # 不读输出：cmd 输出走 GBK 代码页，解码只会白抛异常
        r = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(last.absolute()), str(latest.absolute())],
            capture_output=True,
        )
        linked = r.returncode == 0
    else:
        try:
            last.symlink_to(latest)
            linked = True
        except OSError:
            linked = False

    return linked and "update_last_checkpoint" in out and "1314" in out


class FlowError(Exception):
    """面向用户的流程错误（会直接展示在界面上）。"""


def parse_ports(text: str) -> list[str]:
    """提取端口；交互式结论行（明确的唯一答案）优先，否则返回列表式候选。"""
    text = text or ""
    verdict = _MOTORBUS_RE.search(text)
    if verdict:
        return [verdict.group(1)]
    return _PORT_RE.findall(text)


def _trim_for_log(text: str, head: int = 2000, tail: int = 20000) -> str:
    """校准录制阶段 ~50Hz 刷位置表，一分钟就能产出上 MB——落盘只留头尾。"""
    if len(text) <= head + tail + 200:
        return text
    return (
        text[:head]
        + f"\n……（中间省略 {len(text) - head - tail} 字符的高频输出）……\n"
        + text[-tail:]
    )


class FlowEngine:
    def __init__(
        self,
        profile: ProductProfile,
        *,
        mock: bool,
        approve,
        bus: EventBus,
        mock_state: dict,
        state_path: Path,
        mock_line_delay: float = 0.12,
    ) -> None:
        self.profile = profile
        self.mock = mock
        self.approve = approve
        self.bus = bus
        self.mock_state = mock_state
        self.state_path = Path(state_path)
        self.mock_line_delay = mock_line_delay

        self._lock = threading.Lock()
        self.statuses: dict[str, str] = {}
        self.updated_at: dict[str, float] = {}
        self.ports = {"follower": "", "leader": ""}
        self.record_config: dict = {"repo_id": "", "num_episodes": 50, "camera": False, "camera_index": 0}
        self.train_config: dict = {"steps": 3000, "batch_size": 16, "device": "cpu"}

        # 展开成有序步骤表（configure 在位置 1 注入 leader 端口识别步骤）
        self.order: list[str] = []
        self.steps: dict[str, Step] = {}
        self.phase_steps: dict[str, list[str]] = {}
        for phase in PHASE_ORDER:
            base = list(profile.phases.get(phase, []))
            if phase == "configure":
                base.insert(1, _LEADER_PORT_STEP)
            sids: list[str] = []
            for i, step in enumerate(base):
                sid = f"{phase}.{i}"
                self.order.append(sid)
                self.steps[sid] = step
                self.statuses[sid] = ST_PENDING
                sids.append(sid)
            self.phase_steps[phase] = sids

        self.running_step: str | None = None
        self.live_handle: dict | None = None  # 当前真实命令的进程句柄（交互输入/停止用）
        self._user_stopped = False  # 用户主动点了「停止」（遥操作这类会话型命令视为正常结束）
        self.run_log_path = self.state_path.parent / "run-log.md"
        self._load()

    # ---------------- 运行日志（服务端落盘，报错可回溯） ----------------

    def _log_run(self, text: str) -> None:
        """追加运行日志；写失败不影响流程。单写者（同一时刻只有一个步骤线程）。"""
        try:
            self.run_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.run_log_path.open("a", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass

    def _log_command(self, step_id: str, cmd: str, code: int, out: str, err: str = "") -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        merged = "\n".join(x for x in (out.strip(), err.strip()) if x) or "(无输出)"
        merged = _trim_for_log(merged)
        self._log_run(
            f"\n## {stamp} · {step_id}（{self.profile.key}，mock={self.mock}）\n"
            f"$ {cmd}\n退出码 {code}\n```text\n{merged}\n```\n"
        )

    # ---------------- 持久化 ----------------

    def _save(self) -> None:
        data = {
            "product": self.profile.key,
            "flow_version": FLOW_VERSION,
            "statuses": self.statuses,
            "updated_at": self.updated_at,
            "ports": self.ports,
            "record_config": self.record_config,
            "train_config": self.train_config,
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass  # 进度写不进去不阻断流程

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if data.get("product") != self.profile.key:
            return
        # 向导结构变过版本：非 install 关卡的对齐不可靠，重置重来（这几步都很快）
        stale = data.get("flow_version") != FLOW_VERSION
        for sid, status in (data.get("statuses") or {}).items():
            if sid not in self.statuses:
                continue
            if stale and not sid.startswith("install."):
                continue  # 版本不匹配：非 install 关卡按待办处理
            if status in (ST_PENDING, ST_RUNNING, ST_DONE, ST_FAILED, ST_SKIPPED):
                self.statuses[sid] = ST_PENDING if status == ST_RUNNING else status
        ports = data.get("ports") or {}
        if isinstance(ports, dict):
            self.ports["follower"] = str(ports.get("follower") or "")
            self.ports["leader"] = str(ports.get("leader") or "")
        rc = data.get("record_config") or {}
        if isinstance(rc, dict):
            n = rc.get("num_episodes")
            ci = rc.get("camera_index")
            self.record_config = {
                "repo_id": str(rc.get("repo_id") or ""),
                "num_episodes": n if isinstance(n, int) and 1 <= n <= 1000 else 50,
                "camera": bool(rc.get("camera", False)),
                "camera_index": ci if isinstance(ci, int) and 0 <= ci <= 9 else 0,
            }
        tc = data.get("train_config") or {}
        if isinstance(tc, dict):
            self.train_config = {
                "steps": tc.get("steps") if isinstance(tc.get("steps"), int) and 1 <= tc.get("steps") <= 1000000 else 3000,
                "batch_size": tc.get("batch_size") if isinstance(tc.get("batch_size"), int) and 1 <= tc.get("batch_size") <= 256 else 16,
                "device": tc.get("device") if tc.get("device") in ("cpu", "cuda") else "cpu",
            }
        saved_at = data.get("updated_at") or {}
        if isinstance(saved_at, dict):
            self.updated_at = {k: float(v) for k, v in saved_at.items() if k in self.statuses}

    # ---------------- 采集参数 ----------------

    def set_record_config(self, repo_id: str, num_episodes, camera=False, camera_index=0) -> dict:
        repo_id = (repo_id or "").strip()
        if repo_id and ("/" not in repo_id or " " in repo_id):
            raise FlowError("数据集名称格式：用户名/数据集名（如 zhangsan/pick_cube，不含空格）")
        try:
            n = int(num_episodes)
        except (TypeError, ValueError):
            raise FlowError("采集数量请填整数（1–1000）")
        if not 1 <= n <= 1000:
            raise FlowError("采集数量请填 1–1000（建议 20–50 起步）")
        try:
            ci = int(camera_index)
        except (TypeError, ValueError):
            ci = 0
        if not 0 <= ci <= 9:
            raise FlowError("相机编号请填 0–9 的整数")
        with self._lock:
            self.record_config = {"repo_id": repo_id, "num_episodes": n,
                                  "camera": bool(camera), "camera_index": ci}
            result = dict(self.record_config)
        self._save()
        self.bus.publish("record_config_updated", result)
        return result

    def set_train_config(self, steps, batch_size, device="cpu") -> dict:
        try:
            s = int(steps)
        except (TypeError, ValueError):
            raise FlowError("训练步数请填整数")
        if not 1 <= s <= 1000000:
            raise FlowError("训练步数请填 1–1000000（正经训练 2000–5000 起步，1–5 步仅用于流程冒烟）")
        try:
            b = int(batch_size)
        except (TypeError, ValueError):
            raise FlowError("batch size 请填整数")
        if not 1 <= b <= 256:
            raise FlowError("batch size 请填 1–256（内存不足就调小）")
        if device not in ("cpu", "cuda"):
            device = "cpu"
        with self._lock:
            self.train_config = {"steps": s, "batch_size": b, "device": device}
            result = dict(self.train_config)
        self._save()
        self.bus.publish("train_config_updated", result)
        return result

    def _camera_arg(self) -> str:
        ci = self.record_config.get("camera_index") or 0
        # 640×480：SO-101 + lerobot 生态的标准采集分辨率（相机原生输出、
        # 官方教程与社区数据集一致）。CPU 训练嫌慢时的降级选项是 320×240
        #（解码提速约 4 倍，代价是细节损失，只建议冒烟阶段用）
        return (
            f'--robot.cameras="{{cam_0: {{type: opencv, index_or_path: {ci}, '
            f'width: 640, height: 480, fps: 30}}}}"'
        )

    def _resolve_local_dataset(self, repo_id: str) -> str:
        """解析本地实际数据集目录名。

        录制默认在 repo_id 后追加时间戳（usr/box_20261105_145706），
        而训练用裸名 usr/box 会因找不到精确目录转而连 Hub 查版本（离线即失败）。
        规则：精确目录优先，否则取带时间戳的最新版本。
        """
        if not repo_id or "/" not in repo_id:
            return repo_id
        base = Path.home() / ".cache/huggingface/lerobot" / repo_id.split("/")[0]

        def _has_data(p: Path) -> bool:
            # 收尾被中断的目录会留下 total_episodes=0 的空壳，不能作为训练数据
            try:
                info = json.loads((p / "meta/info.json").read_text(encoding="utf-8"))
                return int(info.get("total_episodes", 0)) >= 1
            except (OSError, ValueError, TypeError):
                return False

        if _has_data(base / repo_id.split("/")[1]):
            return repo_id
        candidates = [p for p in base.glob(repo_id.split("/")[1] + "_*") if _has_data(p)]
        if candidates:
            return f"{repo_id.split('/')[0]}/{max(candidates, key=lambda p: p.stat().st_mtime).name}"
        return repo_id

    def _apply_record_config(self, command: str) -> str:
        repo = self.record_config.get("repo_id") or ""
        use_cam = bool(self.record_config.get("camera"))

        # 评估：lerobot-eval 面向仿真环境；真机部署用 lerobot-rollout（补 robot 与端口）
        if command.startswith("lerobot-eval") and "--robot." not in command:
            port = self.ports.get("follower") or _FOLLOWER_PLACEHOLDER
            command = command.replace(
                "lerobot-eval", f"lerobot-rollout --robot.type=so101_follower --robot.port={port}", 1
            )
            if use_cam and "--robot.cameras" not in command:
                command += " " + self._camera_arg()
            return command

        if repo:
            if command.startswith("lerobot-record"):
                # 录制：用裸名，让它自己新建带时间戳的版本目录
                command = command.replace(_RECORD_REPO_PLACEHOLDER, repo)
                if use_cam and "--robot.cameras" not in command:
                    command += " " + self._camera_arg()
            else:
                # 训练等读取方：解析到本地实际存在的目录（含时间戳后缀）
                command = command.replace(_RECORD_REPO_PLACEHOLDER, self._resolve_local_dataset(repo))
        if command.startswith("lerobot-train"):
            tc = self.train_config
            if "--steps" not in command:
                command += f" --steps={tc.get('steps', 3000)}"
            if "--batch_size" not in command:
                command += f" --batch_size={tc.get('batch_size', 16)}"
            if tc.get("device") == "cuda" and "--policy.device" not in command:
                command += " --policy.device=cuda"
        n = self.record_config.get("num_episodes")
        if isinstance(n, int) and 1 <= n <= 1000:
            # 兼容新旧两种参数名，统一输出 0.6 的 --dataset.num_episodes
            command = re.sub(
                r"(?:--num-episodes|--dataset\.num_episodes)=\d+",
                f"--dataset.num_episodes={n}",
                command,
            )
        return command

    # ---------------- 查询 ----------------

    def step_id_valid(self, step_id: str) -> bool:
        return step_id in self.steps

    def unlocked(self, step_id: str) -> bool:
        """闯关规则：前面的步骤全部完成（或跳过）才解锁。"""
        for sid in self.order:
            if sid == step_id:
                return True
            if self.statuses.get(sid) not in _FINISHED:
                return False
        return False

    def check_can_run(self, step_id: str) -> None:
        """运行前同步校验，不通过抛 FlowError（HTTP 层转 4xx）。"""
        with self._lock:
            if not self.step_id_valid(step_id):
                raise FlowError("没有这一步哦，请刷新页面重试。")
            if self.running_step is not None:
                raise FlowError("上一步还在执行中，请等它结束～")
        if not self.unlocked(step_id):
            raise FlowError("请先完成前面的关卡，这一步还没解锁 🔒")

    def snapshot(self) -> dict:
        with self._lock:
            statuses = dict(self.statuses)
            updated_at = dict(self.updated_at)
            running = self.running_step
            ports = dict(self.ports)
            record_config = dict(self.record_config)
        done = sum(1 for s in statuses.values() if s == ST_DONE)
        skipped = sum(1 for s in statuses.values() if s == ST_SKIPPED)
        return {
            "product": self.profile.key,
            "steps": {
                sid: {
                    "status": statuses[sid],
                    "updated_at": updated_at.get(sid),
                    "unlocked": self.unlocked(sid),
                }
                for sid in self.order
            },
            "running_step": running,
            "ports": ports,
            "record_config": record_config,
            "train_config": dict(self.train_config),
            "done_count": done,
            "skipped_count": skipped,
            "total": len(self.order),
        }

    def profile_payload(self) -> dict:
        phases = []
        for phase in PHASE_ORDER:
            meta = PHASE_META[phase]
            steps = []
            for sid in self.phase_steps.get(phase, []):
                step = self.steps[sid]
                steps.append(
                    {
                        "id": sid,
                        "title": step.title,
                        "detail": step.detail,
                        # 展示层同步补丁（如安装命令带全附加包），所见即所跑
                        "commands": [_patch_command(c) for c in step.commands],
                        "verify": step.verify,
                        "troubleshooting": list(step.troubleshooting),
                        "guide": list(_STEP_GUIDES.get(sid, [])),
                    }
                )
            phases.append({"key": phase, "emoji": meta["emoji"], "label": meta["label"], "steps": steps})
        return {"key": self.profile.key, "name": self.profile.name, "phases": phases}

    # ---------------- 动作 ----------------

    def set_ports(self, follower: str, leader: str) -> dict:
        with self._lock:
            self.ports["follower"] = (follower or "").strip()
            self.ports["leader"] = (leader or "").strip()
            ports = dict(self.ports)
        self._save()
        return ports

    def skip_step(self, step_id: str) -> None:
        with self._lock:
            if not self.step_id_valid(step_id):
                raise FlowError("没有这一步哦。")
            if self.running_step is not None:
                raise FlowError("有步骤正在执行，不能跳过。")
            status = self.statuses.get(step_id)
            if status not in (ST_PENDING, ST_FAILED):
                raise FlowError("只有待办或失败的步骤可以跳过。")
            self.statuses[step_id] = ST_SKIPPED
            self.updated_at[step_id] = time.time()
        self._save()
        self.bus.publish("step_update", {"step": step_id, "status": ST_SKIPPED})

    def reset(self) -> None:
        with self._lock:
            for sid in self.order:
                self.statuses[sid] = ST_PENDING
            self.updated_at.clear()
            self.ports = {"follower": "", "leader": ""}
        self._save()
        self.bus.publish("flow_reset", {})

    def run_step(self, step_id: str) -> None:
        """执行一步（阻塞，由 HTTP 层包线程调用）。"""
        self.check_can_run(step_id)
        with self._lock:
            self.running_step = step_id
            self.statuses[step_id] = ST_RUNNING
            self.updated_at[step_id] = time.time()
        self.bus.publish("step_update", {"step": step_id, "status": ST_RUNNING})
        try:
            self._execute_step(step_id)
        except Exception as e:  # 引擎内部意外错误：整步置失败，不让线程裸死
            self._finish(step_id, ST_FAILED, error=f"{type(e).__name__}: {e}")
            self.bus.publish("error", {"step": step_id, "message": f"步骤执行异常：{e}"})
        finally:
            with self._lock:
                self.running_step = None
                self.live_handle = None
                self._user_stopped = False
            self._save()

    # ---------------- 交互（真实模式的"按回车"提示 / 主动停止） ----------------

    def send_input(self, text: str, streaming: bool = False) -> bool:
        """向当前运行的真实命令发送"回车"；mock 模式或无进程时返回 False。

        streaming=True 表示命令正在持续刷输出（如校准录制循环）——那种等待在
        Windows 上用的是 msvcrt 轮询控制台键盘，读不到 stdin 管道，必须走
        send_console_enter()；静止的 input() 提示则走 stdin 管道。
        """
        if self.mock or not self.running_step:
            return False
        if streaming:
            ok = send_console_enter() or write_stdin(self.live_handle, text)
        else:
            ok = write_stdin(self.live_handle, text)
        if ok:
            how = "控制台回车（录制中）" if streaming else "回车"
            self._log_run(
                f"\n> {time.strftime('%H:%M:%S')} 用户点击「继续」（发送：{how}）\n"
            )
            self.bus.publish("interactive_input", {"text": text.strip() or "⏎"})
        return ok

    def stop_current(self) -> bool:
        """停止当前运行的真实命令（整树终止）。

        遥操作这类会话型命令（lerobot-teleoperate）被用户结束时视为正常完成；
        其他命令终止后按失败处理。
        """
        if self.mock or not self.running_step:
            return False
        proc = (self.live_handle or {}).get("proc")
        if proc is None or proc.poll() is not None:
            return False
        _kill_tree(proc)
        self._user_stopped = True
        self._log_run(f"\n> {time.strftime('%H:%M:%S')} 用户点击「停止」，强制终止命令\n")
        self.bus.publish("interactive_stop", {})
        return True

    # ---------------- 内部 ----------------

    def _finish(self, step_id: str, status: str, error: str | None = None) -> None:
        with self._lock:
            self.statuses[step_id] = status
            self.updated_at[step_id] = time.time()
        self._save()
        self.bus.publish(
            "step_update", {"step": step_id, "status": status, "error": error}
        )

    def _apply_ports(self, command: str) -> str:
        follower = self.ports.get("follower") or ""
        leader = self.ports.get("leader") or ""
        if follower:
            command = command.replace(_FOLLOWER_PLACEHOLDER, follower)
        if leader:
            command = command.replace(_LEADER_PLACEHOLDER, leader)
        return command

    # ---------------- 端口自动捕获 ----------------

    def _maybe_capture_ports(self, step_id: str, cmd: str, code: int, out: str, err: str = "") -> None:
        """find-port 成功且结果唯一时，自动填入对应臂的端口并保存。多端口不瞎猜。"""
        if code != 0 or not cmd.strip().lower().startswith("lerobot-find-port"):
            return
        ports = parse_ports(out + "\n" + err)
        if not ports:
            return
        if len(ports) > 1:
            self.bus.publish(
                "ports_detected",
                {"ports": ports[:4], "hint": "检测到多个端口，请点击上方提示填入或手动选择"},
            )
            return
        port = ports[0]
        target = None
        if step_id == "configure.0":
            target = "follower"
        elif step_id == "configure.1":
            target = "leader"
        else:
            # 其他位置的 find-port：另一只臂已配置且端口不同时补空位
            if not self.ports.get("follower") and self.ports.get("leader") and port != self.ports["leader"]:
                target = "follower"
            elif not self.ports.get("leader") and self.ports.get("follower") and port != self.ports["follower"]:
                target = "leader"
        if target and not self.ports.get(target):
            self.ports[target] = port
            self._save()
            both = bool(self.ports.get("follower") and self.ports.get("leader"))
            self.bus.publish(
                "ports_autofilled",
                {"target": target, "port": port, "both": both},
            )

    def _emit_step(self, step_id: str, command: str) -> str | None:
        """发布 cmd_start 并做白名单校验；不通过时把原因当作输出广播并返回 None。"""
        risk = shell_mod.classify_risk(command)
        self.bus.publish("cmd_start", {"step": step_id, "command": command, "risk": risk})
        ok, why = shell_mod.validate(command)
        if not ok:
            self.bus.publish("cmd_output", {"step": step_id, "command": command, "line": f"拒绝执行：{why}"})
            self.bus.publish("cmd_end", {"step": step_id, "command": command, "exit_code": 1})
            return None
        return command

    def _execute_step(self, step_id: str) -> None:
        step = self.steps[step_id]
        for command in step.commands:
            cmd = self._apply_record_config(self._apply_ports(_patch_command(command)))

            def _block(hint: str, reason: str) -> None:
                self.bus.publish("cmd_start", {"step": step_id, "command": cmd, "risk": "safe"})
                self.bus.publish("cmd_output", {"step": step_id, "command": cmd, "line": hint})
                self.bus.publish("cmd_end", {"step": step_id, "command": cmd, "exit_code": 1})
                self._log_command(step_id, cmd, 1, hint)
                self._finish(step_id, ST_FAILED, error=reason)

            # 采集占位参数没被替换 = 数据集未设置：提前拦截
            if _RECORD_REPO_PLACEHOLDER in cmd:
                _block("✋ 还没设置数据集名称：请先在上方「📼 数据采集设置」填好 用户名/数据集名 并保存，再运行这一步。", "数据集未设置")
                return

            # Windows 上占位端口没被替换 = 端口未设置：提前拦截，别让命令跑废
            if platform.system() == "Windows" and (
                _FOLLOWER_PLACEHOLDER in cmd or _LEADER_PLACEHOLDER in cmd
            ):
                _block("✋ 还没设置端口：Windows 下不能用 /dev/ttyACM0，请先在上方「🔌 端口设置」填好实际端口（如 COM5）并保存，再运行这一步。", "端口未设置")
                return

            if self._emit_step(step_id, cmd) is None:
                self._log_command(step_id, cmd, 1, "命令未通过白名单校验")
                self._finish(step_id, ST_FAILED, error="命令未通过白名单校验")
                return

            risk = shell_mod.classify_risk(cmd)
            if risk != RISK_SAFE:
                # 确认门：拒绝/超时 → 整步回到待办（fail-closed）
                if not self.approve(cmd, risk, step.title):
                    self._log_run(
                        f"\n## {time.strftime('%Y-%m-%d %H:%M:%S')} · {step_id} · 用户取消了确认\n$ {cmd}\n"
                    )
                    self.bus.publish("cmd_cancelled", {"step": step_id, "command": cmd})
                    with self._lock:
                        self.statuses[step_id] = ST_PENDING
                        self.updated_at[step_id] = time.time()
                    self._save()
                    self.bus.publish("step_update", {"step": step_id, "status": ST_PENDING})
                    return

            def on_line(line: str, _cmd: str = cmd) -> None:
                self.bus.publish("cmd_output", {"step": step_id, "command": _cmd, "line": line})

            if cmd.startswith("lerobot-train"):
                _rotate_train_dir()  # 旧训练结果归档，避免 lerobot 的目录已存在报错

            out = err = ""
            if self.mock:
                code, out, err = run_mock_streaming(
                    cmd, self.mock_state, on_line, line_delay=self.mock_line_delay
                )
            else:
                handle: dict = {}
                self.live_handle = handle
                try:
                    code, out, _ = run_real_streaming(cmd, risk, on_output=on_line, handle=handle)
                finally:
                    self.live_handle = None

            self.bus.publish("cmd_end", {"step": step_id, "command": cmd, "exit_code": code})
            self._log_command(step_id, cmd, code, out, err)
            self._maybe_capture_ports(step_id, cmd, code, out, err)
            if cmd.startswith("lerobot-train"):
                # Windows 普通权限建不了 symlink，checkpoint 已完整时补
                # junction 让 checkpoints/last 可用；仅死在链接这一步的
                # 训练按成功处理（评估步骤依赖 last 路径）
                if _ensure_last_checkpoint(out):
                    self._log_run(
                        "\n> 训练已完成；last 链接已用 junction 自动补建"
                        "（Windows 普通权限无法创建符号链接）\n"
                    )
                    self._finish(step_id, ST_DONE)
                    return
            if code != 0:
                # 遥操作这类"会话型"命令本来就要手动结束：用户点停止 = 正常完成
                if self._user_stopped and cmd.lower().startswith("lerobot-teleoperate"):
                    self._log_run(
                        f"\n> {time.strftime('%H:%M:%S')} 遥操作会话由用户正常结束，视为完成\n"
                    )
                    self._finish(step_id, ST_DONE)
                    return
                self._finish(step_id, ST_FAILED, error=f"命令退出码 {code}，可展开排障提示看看")
                return

        self._finish(step_id, ST_DONE)
