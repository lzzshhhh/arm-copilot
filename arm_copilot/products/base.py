"""产品档案抽象：分阶段流程 + FAQ。

每个产品一份档案，供 get_workflow / search_faq 工具查询；
新增产品（如 Microduck）只需在这里注册一份新的 ProductProfile。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Step:
    title: str
    detail: str
    commands: list[str] = field(default_factory=list)
    verify: str = ""
    troubleshooting: list[str] = field(default_factory=list)


@dataclass
class ProductProfile:
    key: str
    name: str
    description: str
    phases: dict[str, list[Step]]  # install / configure / calibrate / next
    faqs: list[tuple[str, str]]    # (question, answer)


PRODUCTS: dict[str, ProductProfile] = {}


def register(profile: ProductProfile) -> None:
    """注册产品档案；key 重复说明配置冲突，立即抛错而不是静默覆盖。"""
    if profile.key in PRODUCTS:
        raise ValueError(f"产品档案重复注册: {profile.key}")
    PRODUCTS[profile.key] = profile
