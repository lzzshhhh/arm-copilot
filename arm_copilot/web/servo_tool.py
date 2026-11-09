"""舵机 ID 直改工具。

场景：舵机编号被改乱（如多个舵机共用一个 ID）时，把目标舵机**物理上单独**
接到控制板，用本工具扫描它的当前 ID 并安全改写为新 ID。

安全设计：
- 写 ID 是按当前 ID 广播寻址的，总线上若有多个舵机会一起被改 →
  因此写入前强制扫描，发现多于一个电机立即拒绝；
- 全程复用 lerobot 的 FeetechMotorsBus（协议表 / 转矩解锁 / 寄存器写入），
  不自行实现协议；写入后重新扫描复验。
"""

from __future__ import annotations

MODEL = "sts3215"


class ServoToolError(Exception):
    """面向用户的工具错误（缺依赖 / 打不开串口等）。"""


def _deps():
    """惰性导入 lerobot：第一关未装完时工具报友好错误而不是拖垮整个应用。"""
    try:
        from lerobot.motors.feetech import FeetechMotorsBus
        from lerobot.motors.motors_bus import Motor, get_address
    except ImportError as e:
        raise ServoToolError("LeRobot 未安装，请先完成第一关的安装后再使用此工具。") from e
    return FeetechMotorsBus, Motor, get_address


def _make_bus(bus_cls, motor_cls, port: str, motor_id: int):
    """构造总线。norm_mode 只影响读数的归一化，与 ping/写 ID 无关。"""
    return bus_cls(port=port, motors={"target": motor_cls(id=motor_id, model=MODEL, norm_mode=None)})


def _baudrates(bus_cls) -> list[int]:
    return sorted(bus_cls.model_baudrate_table[MODEL], reverse=True)


def scan(port: str) -> dict:
    """逐波特率广播 ping，返回 {波特率: [在线电机 ID]}。只读，不写任何东西。"""
    try:
        FeetechMotorsBus, Motor, _get_address = _deps()
    except ServoToolError as e:
        return {"ok": False, "reason": str(e)}

    try:
        bus = _make_bus(FeetechMotorsBus, Motor, port, 1)
        bus.connect(handshake=False)
    except Exception as e:
        return {"ok": False, "reason": f"打开串口 {port} 失败：{e}（确认端口号正确、且没有步骤正在运行）"}

    found: dict[int, list[int]] = {}
    try:
        for baud in _baudrates(bus_cls=FeetechMotorsBus):
            bus.set_baudrate(baud)
            ids = bus.broadcast_ping()
            if ids:
                found[baud] = sorted(ids)
    except Exception as e:
        return {"ok": False, "reason": f"扫描出错：{e}"}
    finally:
        try:
            bus.disconnect(disable_torque=False)
        except Exception:
            pass

    return {
        "ok": True,
        "port": port,
        "baudrates": {str(b): ids for b, ids in found.items()},
        "total_online": sum(len(v) for v in found.values()),
    }


def set_id(port: str, new_id: int) -> dict:
    """把总线上唯一的那个舵机改成 new_id；多于一个在线时拒绝（fail-safe）。"""
    if not isinstance(new_id, int) or not 1 <= new_id <= 254:
        return {"ok": False, "reason": "新 ID 必须是 1–254 的整数（SO-101 关节用 1–6）。"}
    try:
        FeetechMotorsBus, Motor, get_address = _deps()
    except ServoToolError as e:
        return {"ok": False, "reason": str(e)}

    try:
        bus = _make_bus(FeetechMotorsBus, Motor, port, new_id)
        bus.connect(handshake=False)
    except Exception as e:
        return {"ok": False, "reason": f"打开串口 {port} 失败：{e}（确认端口号正确、且没有步骤正在运行）"}

    try:
        # 1) 定位当前电机：所有波特率里找到有应答的那个
        current: tuple[int, int] | None = None  # (baudrate, old_id)
        for baud in _baudrates(bus_cls=FeetechMotorsBus):
            bus.set_baudrate(baud)
            ids = bus.broadcast_ping()
            if not ids:
                continue
            if len(ids) != 1:
                return {
                    "ok": False,
                    "reason": (
                        f"波特率 {baud} 下发现了 {len(ids)} 个电机（ID：{sorted(ids)}）。"
                        "改 ID 的命令会同时打到所有同 ID 电机——请断开链路，"
                        "只保留要改的那一个舵机再试。"
                    ),
                }
            current = (baud, next(iter(ids)))
            break

        if current is None:
            return {"ok": False, "reason": "总线上没有发现电机：确认舵机已供电、线已接好、端口正确。"}

        baud, old_id = current
        if old_id == new_id:
            return {"ok": True, "changed": False, "old_id": old_id, "new_id": new_id,
                    "reason": f"它已经是 ID {new_id} 了，无需修改。"}

        # 2) 照 lerobot setup_motor 的顺序写：解锁转矩 → 写 ID 寄存器 → 复验
        bus.set_baudrate(baud)
        bus._disable_torque(old_id, MODEL)
        addr, length = get_address(bus.model_ctrl_table, MODEL, "ID")
        bus._write(addr, length, old_id, new_id)

        ids_after = bus.broadcast_ping() or {}
        if sorted(ids_after) != [new_id]:
            return {
                "ok": False,
                "reason": f"写入后复验失败（现在在线：{sorted(ids_after)}），请重新扫描确认状态。",
                "old_id": old_id,
            }
        return {"ok": True, "changed": True, "old_id": old_id, "new_id": new_id, "baudrate": baud}
    except Exception as e:
        return {"ok": False, "reason": f"写入出错：{e}"}
    finally:
        try:
            bus.disconnect(disable_torque=False)
        except Exception:
            pass


def torque_test(port: str, cycles: int = 30) -> dict:
    """上锁压力测试：反复 enable/disable 全部舵机，统计上锁失败（电源/接触问题会在此暴露）。

    电机会瞬间锁紧/放松，不会运动。返回 {总次数, 失败, 按电机, 错误类型}。
    """
    try:
        FeetechMotorsBus, Motor, _get_address = _deps()
    except ServoToolError as e:
        return {"ok": False, "reason": str(e)}

    names = ["base_rotation", "shoulder_pitch", "shoulder_rotation",
             "elbow_flex", "wrist_roll", "gripper"]
    motors = {n: Motor(id=i + 1, model=MODEL, norm_mode=None) for i, n in enumerate(names)}
    try:
        bus = FeetechMotorsBus(port=port, motors=motors)
        bus.connect(handshake=False)
    except Exception as e:
        return {"ok": False, "reason": f"打开串口 {port} 失败：{e}"}

    import time as _time

    fails: dict[str, int] = {}
    err_types: set[str] = set()
    try:
        for _ in range(cycles):
            for n in names:
                try:
                    bus.write("Torque_Enable", n, 1, num_retry=0)
                except Exception as e:
                    fails[n] = fails.get(n, 0) + 1
                    err_types.add(str(e).split("[")[-1].strip("]"))
            for n in names:
                try:
                    bus.write("Torque_Enable", n, 0, num_retry=0)
                except Exception:
                    pass
            _time.sleep(0.05)
    finally:
        for n in names:
            try:
                bus.write("Torque_Enable", n, 0, num_retry=0)
            except Exception:
                pass
        try:
            bus.disconnect(disable_torque=False)
        except Exception:
            pass

    total = cycles * len(names)
    return {
        "ok": True,
        "port": port,
        "total": total,
        "failed": sum(fails.values()),
        "by_motor": fails,
        "err_types": sorted(err_types),
        "pass": not fails,
    }


def _cli() -> int:
    """命令行入口：python -m arm_copilot.web.servo_tool COM11 [COM12 ...]"""
    import argparse
    import json

    parser = argparse.ArgumentParser(prog="servo-tool", description="舵机总线诊断：扫描 / 改号 / 上锁压测")
    parser.add_argument("ports", nargs="+", help="串口名，如 COM11 COM12")
    parser.add_argument("--set-id", type=int, default=None, help="（谨慎）把总线上唯一的舵机改成此 ID")
    parser.add_argument("--torque-test", type=int, default=None, metavar="N",
                        help="上锁压测 N 轮（电机锁紧/放松，不运动），验证电源与接触")
    args = parser.parse_args()

    for port in args.ports:
        if args.set_id is not None:
            r = set_id(port, args.set_id)
            print(f"{port} 改号:", json.dumps(r, ensure_ascii=False))
            continue
        if args.torque_test is not None:
            r = torque_test(port, args.torque_test)
            if not r.get("ok"):
                print(f"{port}: ❌ {r.get('reason')}")
                continue
            status = "✅ 通过，上锁稳定" if r["pass"] else f"❌ {r['failed']}/{r['total']} 次失败（供电/接触问题）"
            print(f"{port} 上锁压测 {r['total']} 次: {status}")
            if r["by_motor"]:
                print(f"  按电机: {r['by_motor']}  错误类型: {r['err_types']}")
            continue
        r = scan(port)
        if not r.get("ok"):
            print(f"{port}: ❌ {r.get('reason')}")
            continue
        entries = r.get("baudrates") or {}
        if not entries:
            print(f"{port}: 串口已打开，但没有任何舵机应答（查 5V 电源 / 链路插头）")
        for baud, ids in entries.items():
            missing = [i for i in range(1, 7) if i not in ids]
            print(f"{port}: 在线 ID {ids}" + (f"（缺 1-6 中的 {missing}）" if missing else "（1-6 齐了！）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
