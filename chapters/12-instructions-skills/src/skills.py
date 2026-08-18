"""第 12 章：Skills —— 按需加载的指令体。

对应官方 packages/skill/skill。核心决策在官方文档第 56 行：
定义采用渐进式加载，get() 每次调用都向胜出提供方请求正文，
而不是在注册表里缓存正文。

教学版实现：
1. SkillCatalog —— 扫描目录，模型可见的只有 {name, description} 摘要；
2. load —— skill 工具被调用时才从磁盘读全文（每次重读，不缓存）；
3. render —— 渲染成 <skill_content> 块（官方 renderSkillContent 的形状）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# 官方 renderSkillContent 的标签形状（skill 包文档第 44 行）
SKILL_CONTENT_OPEN = '<skill_content name="{name}">'
SKILL_CONTENT_CLOSE = "</skill_content>"

# token 估算（沿用第 09 章的启发式：4 字符 ≈ 1 token）
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return -(-len(text) // CHARS_PER_TOKEN)


@dataclass(frozen=True)
class SkillSummary:
    """模型可见的技能摘要：只有名字与一句话描述。

    这是渐进加载的前提——目录里绝不出现技能正文，
    模型看到的是「有哪些技能可用」，而不是技能内容本身。"""

    name: str
    description: str


class SkillCatalog:
    """技能目录：root 下的每个子目录是一个技能，正文在 SKILL.md。"""

    def __init__(self, root: Path) -> None:
        self.root = root

    def list(self) -> list[SkillSummary]:
        """扫描目录，解析每个 SKILL.md 的 frontmatter（name/description）。"""
        summaries: list[SkillSummary] = []
        for skill_dir in sorted(self.root.iterdir()):
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.is_file():
                continue
            frontmatter = _parse_frontmatter(skill_file.read_text(encoding="utf-8"))
            summaries.append(
                SkillSummary(
                    name=frontmatter["name"],
                    description=frontmatter["description"],
                )
            )
        return summaries

    def load(self, name: str) -> str:
        """渐进加载：每次调用都从磁盘重读正文（不缓存）。

        为什么每次重读？技能文件可能被用户随时编辑——缓存会让
        Agent 拿着旧指令干活。官方文档第 56 行的每次调用都请求
        正文，正是为了拿到最新版本。"""
        skill_file = self.root / name / "SKILL.md"
        if not skill_file.is_file():
            raise FileNotFoundError(f"技能 {name} 不存在")
        text = skill_file.read_text(encoding="utf-8")
        return _strip_frontmatter(text)

    def render(self, name: str) -> str:
        """加载并渲染成 <skill_content> 块（模型读到指令的形态）。"""
        body = self.load(name)
        return f"{SKILL_CONTENT_OPEN.format(name=name)}\n{body}\n{SKILL_CONTENT_CLOSE}"

    def catalog_text(self) -> str:
        """目录消息：模型每轮都能看到的「技能菜单」（只有摘要）。"""
        lines = ["可用技能："]
        for summary in self.list():
            lines.append(f"- {summary.name}: {summary.description}")
        return "\n".join(lines)


def _parse_frontmatter(text: str) -> dict[str, str]:
    """解析 SKILL.md 开头的 --- 块（name/description 两行，教学版手写解析）。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("SKILL.md 必须以 --- frontmatter 开头")
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    if "name" not in fields or "description" not in fields:
        raise ValueError("frontmatter 必须包含 name 与 description")
    return fields


def _strip_frontmatter(text: str) -> str:
    """去掉 frontmatter，只返回正文。"""
    lines = text.splitlines()
    end = 1
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end = index + 1
            break
    return "\n".join(lines[end:]).strip()
