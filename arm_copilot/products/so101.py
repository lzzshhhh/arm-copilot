"""SO-101 机械臂产品档案（依据 LeRobot 官方文档 v0.5.x 整理）。

注意：所有 commands 都必须能通过 tools.shell.validate 的白名单校验
（tests/test_products.py 中有对应的不变量测试）。
"""

from __future__ import annotations

from .base import PRODUCTS, ProductProfile, Step

_KEY = "so101"


def _build() -> ProductProfile:
    install = [
        Step(
            title="确认 Python 版本（LeRobot 要求 ≥ 3.12）",
            detail="查看 Python 版本。过低请先安装 3.12+（Windows 安装时勾选 Add to PATH），"
                   "建议为 LeRobot 单独建虚拟环境，避免与现有项目冲突。",
            commands=["python --version"],
            verify="输出 ≥ 3.12",
            troubleshooting=[
                "Windows 下 python 指向旧版本：调整 PATH 顺序，或用启动器指定新版。",
                "多版本共存推荐用 conda/uv 单独建环境。",
            ],
        ),
        Step(
            title="安装 LeRobot 与 Feetech 舵机 SDK",
            detail="[feetech] 附加依赖会装上 Feetech STS3215 舵机通信 SDK，这是 SO-101 电机的驱动。",
            commands=['pip install "lerobot[feetech]"'],
            verify="命令成功结束无报错，下一步用 pip show lerobot 确认",
            troubleshooting=[
                '下载慢或超时：pip install -i https://pypi.tuna.tsinghua.edu.cn/simple "lerobot[feetech]"',
                '需要最新源码版：克隆 huggingface/lerobot 仓库后 pip install -e ".[feetech]"',
            ],
        ),
        Step(
            title="验证安装",
            detail="确认 lerobot 已装好、版本可见。",
            commands=["pip show lerobot"],
            verify="输出 Name: lerobot 与 Version",
            troubleshooting=["命令找不到：确认虚拟环境已激活，或重开终端让 PATH 生效。"],
        ),
    ]

    configure = [
        Step(
            title="连接 follower 臂并识别串口",
            detail="给 follower 臂断电 → USB 线连上位机 → 重新上电，然后扫描串口。"
                   "记下端口号（Linux 如 /dev/ttyACM0，Windows 如 COM3）。",
            commands=["lerobot-find-port"],
            verify="输出形如 [0] /dev/ttyACM0 [FEETECH] (available)",
            troubleshooting=[
                "找不到端口：换一根 USB 数据线（纯充电线不行）、换 USB 口。",
                "Linux 串口权限：把用户加入 dialout 组后重新登录。",
                "Windows：设备管理器确认串口设备无黄色感叹号，必要时装 CH340/串口驱动。",
            ],
        ),
        Step(
            title="设置 follower 电机 ID（会驱动电机）",
            detail="先把 follower 六个关节摆到装配中立位，再运行命令。脚本会逐个扫描电机并按"
                   " base→shoulder→elbow→wrist→gripper 顺序写入 ID 1–6；过程中电机有轻微动作。"
                   "把 --robot.port 换成上一步记下的实际端口。",
            commands=["lerobot-setup-motors --robot.type=so101_follower --robot.port=/dev/ttyACM0"],
            verify="每个电机 ID 写入成功；完成后给机械臂断电重启",
            troubleshooting=[
                "扫描不到电机：确认电机电源已接、端口正确。",
                "多个电机同 ID 属正常现象（新电机出厂均为 ID 1），按脚本提示逐个处理。",
            ],
        ),
        Step(
            title="设置 leader 电机 ID（会驱动电机）",
            detail="leader 臂同样流程：先找端口，再设置 ID。leader 通常不含夹爪负载，但电机 ID 顺序相同。",
            commands=[
                "lerobot-find-port",
                "lerobot-setup-motors --teleop.type=so101_leader --teleop.port=/dev/ttyACM1",
            ],
            verify="leader 各电机 ID 写入成功；完成后断电重启",
            troubleshooting=["与 follower 相同：优先检查线缆、端口与供电。"],
        ),
    ]

    calibrate = [
        Step(
            title="校准 follower 臂（会驱动电机）",
            detail="校准把电机原始读数映射为关节角度，是遥操作与数据采集正确性的前提；"
                   "重新装配或更换电机后必须重新校准。按终端提示依次把各关节摆到指定姿态并回车记录。",
            commands=["lerobot-calibrate --robot.type=so101_follower --robot.port=/dev/ttyACM0"],
            verify="提示校准数据已保存到 ~/.cache/huggingface/lerobot/calibration/",
            troubleshooting=[
                "超时无响应：检查端口是否正确、电源功率是否足够。",
                "校准前确认已运行过 setup-motors 设置好电机 ID。",
            ],
        ),
        Step(
            title="校准 leader 臂（会驱动电机）",
            detail="leader 臂按同样的提示流程校准。",
            commands=["lerobot-calibrate --teleop.type=so101_leader --teleop.port=/dev/ttyACM1"],
            verify="校准数据已保存",
            troubleshooting=["同 follower。"],
        ),
        Step(
            title="遥操作验证（会驱动电机）",
            detail="手握 leader 臂缓慢运动，follower 应同步跟随，Ctrl+C 结束。"
                   "开始前清空工作区，确认两臂无碰撞风险。",
            commands=[
                "lerobot-teleoperate --robot.type=so101_follower --robot.port=/dev/ttyACM0 "
                "--teleop.type=so101_leader --teleop.port=/dev/ttyACM1",
            ],
            verify="follower 平滑跟随 leader，无异响与明显偏差",
            troubleshooting=[
                "跟随方向反了或角度不对：重新校准对应臂。",
                "抖动严重：检查供电电压（建议独立 5V 电源，电流 ≥ 3A）。",
            ],
        ),
    ]

    next_phase = [
        Step(
            title="数据采集（遥操作示教）",
            detail="用 leader 臂示教，录制 episodes 数据集并上传到 Hugging Face Hub。"
                   "把「你的用户名/数据集名」替换为实际值；需要相机时按 --help 查看相机参数。"
                   "命令较长，建议先运行 lerobot-record --help 查看当前版本参数。",
            commands=[
                "lerobot-record --robot.type=so101_follower --robot.port=/dev/ttyACM0 "
                "--teleop.type=so101_leader --teleop.port=/dev/ttyACM1 "
                "--dataset.repo_id=你的用户名/数据集名 --num-episodes=50",
            ],
            verify="数据集目录生成 episodes 数据，可回放检查",
            troubleshooting=["单个 episode 失败可重录；总时长不足会明显影响训练效果。"],
        ),
        Step(
            title="训练（后训练 policy）",
            detail="常见选择：ACT（精细操作、训练快）、Diffusion Policy（稳定）、"
                   "SmolVLA / pi0（VLA 路线，泛化好但算力要求高）。先用小步数试跑通，再放大。",
            commands=["lerobot-train --policy.type=act --dataset.repo_id=你的用户名/数据集名"],
            verify="产出 checkpoints 目录，loss 正常下降",
            troubleshooting=["显存不足：减小 batch size 或帧数；CPU 也能跑 ACT，只是慢。"],
        ),
        Step(
            title="评估部署",
            detail="加载训练好的 checkpoint 在真机上评估；命令参数以 lerobot-eval --help 为准。",
            commands=["lerobot-eval --policy.path=outputs/train/act/checkpoints/last"],
            verify="机械臂能完成示教任务的近似复现",
            troubleshooting=["表现差：增加数据量/episodes 数量，或换 policy 类型对比。"],
        ),
    ]

    faqs = [
        (
            "SO-101 是什么？和 SO-100 有什么区别？",
            "SO-101 是 Hugging Face LeRobot 社区的标准机械臂平台（TheRobotStudio 设计），"
            "6 自由度 + 夹爪，leader/follower 各 6 个 Feetech STS3215 舵机。"
            "相比 SO-100：改进了齿轮比与结构件，装配更简单、动作更准，开箱即配 lerobot。",
        ),
        (
            "需要哪些硬件？",
            "leader + follower 两套臂（3D 打印结构件 + 各 6 个 Feetech STS3215 舵机 + 控制板）、"
            "5V ≥3A 电源（每臂一份）、USB 数据线、上位机（树莓派 / Ubuntu / Windows / macOS 均可，"
            "Python ≥ 3.12）。训练数据需要视觉时再加 1–2 个 USB 相机。",
        ),
        (
            "为什么必须校准？什么时候要重新校准？",
            "舵机原始编码与关节真实角度之间存在装配偏移，校准就是建立这个映射并保存到本地缓存；"
            "遥操作同步、数据采集的角度正确性都依赖它。重新装配、更换电机/结构件后必须重新校准。",
        ),
        (
            "lerobot-find-port 找不到设备怎么办？",
            "依次检查：① 机械臂已供电且上电顺序正确（先断电插 USB 再上电）；② USB 线是数据线；"
            "③ 换 USB 口；④ Linux 下把用户加入 dialout 组重新登录；"
            "⑤ Windows 设备管理器确认驱动正常。",
        ),
        (
            "校准时电机不动或报超时？",
            "常见原因：端口选错、没先运行 setup-motors 设电机 ID、电源功率不足（舵机需要 5V ≥3A）。"
            "先跑 env_check 与 find-port 复查连接。",
        ),
        (
            "采集多少条数据合适？选什么 policy？",
            "经验值 50–100 条 episodes 起步（每条含成功完成任务全过程）。policy 选择："
            "ACT 适合精细操作、训练快；Diffusion Policy 更稳；SmolVLA/pi0 属 VLA 大模型路线，"
            "泛化好但需要更多算力。先用小配置跑通流程再放大。",
        ),
        (
            "Windows 上能用吗？",
            "可以原生使用，也可 WSL2。注意 WSL2 访问 USB 串口需要 usbipd-win 挂载，"
            "新手建议原生 Windows 或 Ubuntu。树莓派 4/5 是社区最常见的机载方案。",
        ),
    ]

    return ProductProfile(
        key=_KEY,
        name="SO-101 机械臂",
        description="Hugging Face LeRobot 生态的开源 6 轴机械臂（leader-follower 遥操作 + 模仿学习）",
        phases={"install": install, "configure": configure, "calibrate": calibrate, "next": next_phase},
        faqs=faqs,
    )


PRODUCTS[_KEY] = _build()
