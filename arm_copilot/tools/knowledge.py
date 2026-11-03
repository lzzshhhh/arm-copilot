"""get_workflow / search_faq 工具：查询内置产品档案（M3 将升级为 RAG 检索）。"""

from __future__ import annotations

from ..products import PRODUCTS
from .base import Tool

_PHASE_LABELS = {
    "install": "安装",
    "configure": "配置（串口与电机 ID）",
    "calibrate": "校准",
    "next": "后续阶段（采集 / 训练 / 评估）",
}


def _render_steps(product, phase_keys) -> str:
    parts = []
    for ph in phase_keys:
        steps = product.phases.get(ph, [])
        if not steps:
            continue
        parts.append(f"## 阶段：{_PHASE_LABELS.get(ph, ph)}")
        for i, s in enumerate(steps, start=1):
            block = [f"### {i}. {s.title}", s.detail]
            if s.commands:
                block.append("命令：")
                block.extend(f"  {c}" for c in s.commands)
            if s.verify:
                block.append(f"验证：{s.verify}")
            if s.troubleshooting:
                block.append("排障：")
                block.extend(f"- {t}" for t in s.troubleshooting)
            parts.append("\n".join(block))
    return "\n\n".join(parts) if parts else "（无内容）"


def make_workflow_tool() -> Tool:
    def handler(args: dict) -> str:
        key = str(args.get("product", "so101"))
        phase = str(args.get("phase", "all"))
        product = PRODUCTS.get(key)
        if product is None:
            return f"未知产品 {key}，当前支持：{', '.join(PRODUCTS)}"
        if phase == "all":
            keys = list(product.phases)
        elif phase in product.phases:
            keys = [phase]
        else:
            return f"未知阶段 {phase}，支持：{', '.join(product.phases)}（或 all）"
        return f"# {product.name} 部署流程\n\n{_render_steps(product, keys)}"

    return Tool(
        name="get_workflow",
        description=(
            "查询产品（当前支持 so101）的分阶段部署流程：每一步的做法、确切命令、"
            "验证方式与排障要点。回答操作类问题前应先查询本工具，不要凭记忆编造命令。"
            "引导用户逐步操作时按 phase 查单个阶段即可（更省上下文），仅在需要全流程总览时用 all。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "product": {"type": "string", "enum": list(PRODUCTS), "description": "产品 key"},
                "phase": {
                    "type": "string",
                    "enum": ["install", "configure", "calibrate", "next", "all"],
                    "description": "要查的阶段；优先查单个阶段，仅在需要总览时用 all（默认）",
                },
            },
        },
        handler=handler,
    )


def _faq_score(query: str, qa: tuple[str, str]) -> float:
    """字符级命中率打分（中文查询没有分词也能工作）。"""
    text = (qa[0] + qa[1]).lower()
    chars = {c for c in query.lower() if not c.isspace()}
    if not chars:
        return 0.0
    return sum(1 for c in chars if c in text) / len(chars)


def make_faq_tool() -> Tool:
    def handler(args: dict) -> str:
        key = str(args.get("product", "so101"))
        query = str(args.get("query", ""))
        product = PRODUCTS.get(key)
        if product is None:
            return f"未知产品 {key}，当前支持：{', '.join(PRODUCTS)}"
        ranked = sorted(product.faqs, key=lambda qa: _faq_score(query, qa), reverse=True)
        hits = [qa for qa in ranked[:3] if _faq_score(query, qa) >= 0.5]
        if not hits:
            return (
                "产品档案中未命中该问题（后续版本将接入 RAG 文档检索）。"
                "可换个问法重试，或基于通用具身智能知识回答并注明非官方口径。"
            )
        return "\n\n".join(f"Q: {q}\nA: {a}" for q, a in hits)

    return Tool(
        name="search_faq",
        description="在产品档案的常见问题（FAQ）中检索答案，适合概念、选型、注意事项类问题。",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "问题关键词"},
                "product": {"type": "string", "enum": list(PRODUCTS)},
            },
            "required": ["query"],
        },
        handler=handler,
    )
