# 12｜技能与按需加载

> 预计时间：50 分钟 ｜ 前置：完成第 09 章（token 概念） ｜ 本章纯本地运行，不调用模型

随着 Agent 能力增加，系统中会出现一类面向具体任务的操作手册，例如怎样搜索网络、编写 commit message 或运行某个项目的测试。这些内容不同于长期生效的人设规则。如果全部放入系统提示词，每轮请求都要携带所有手册，即使当前任务并不需要它们。

官方的答案是 Skills（技能）：模型每轮只看到一份技能菜单，名字加一句话描述，真正用到哪个技能时，调用 skill 工具把那份手册按需加载进来。官方把这个设计叫渐进式加载，skill 包文档第 56 行写明：定义仍采用渐进式加载，get() 每次调用都向胜出提供方请求正文，而不是在注册表中缓存正文。

## 学习目标

完成本章后，你将能够：

- 解释技能摘要常驻、正文按需加载如何节省 token；
- 按 `skills/<name>/SKILL.md` 约定扫描技能目录；
- 解析 frontmatter，并把技能正文渲染为 `<skill_content>` 块；
- 说明每次重新读取正文而不缓存的行为意义。

## 12.1 原理：菜单常驻，正文按需

把技能拆成两层：

| 层 | 内容 | 什么时候给模型看 |
|----|------|------------------|
| 摘要 | name + 一句话 description | 每轮，菜单常驻 |
| 正文 | 完整的操作手册，SKILL.md | 按需，调用 skill 工具时 |

菜单常驻的成本极低，两个技能约 30 token；正文按需加载只在真正需要时付费，一份手册几十到几百 token。技能越多、正文越长，这个设计省得越多。50 个技能、每个 500 token 的正文，常驻注入每轮背着 25000 token，按需加载只在用到时付 500。

按需加载还可以及时反映文件变化。技能文件可能被用户编辑，本章 demo 第 ④ 节会验证：每次调用重新读取磁盘后，下一次加载会获得修改后的正文；如果长期缓存正文，Agent 可能继续使用旧指令。

第三个好处与第 06 章相呼应：每个技能在自己的 SKILL.md 中维护正文，系统提示词组装器只需要提供技能目录，不必内置每份手册的内容。

## 12.2 SkillCatalog：扫描、解析、加载

技能的存储形态是一个约定：`skills/<name>/SKILL.md`，文件开头是 frontmatter 元数据块，后面是正文：

```markdown
---
name: web-search-guide
description: 如何使用 Web Search 工具高效搜索网络、挑选来源、引用结果
---

# Web Search 使用指南

当用户的问题需要最新信息时，调用 web_search 工具。
...
```

目录实现：

```python
@dataclass(frozen=True)
class SkillSummary:
    name: str
    description: str


class SkillCatalog:
    def __init__(self, root: Path) -> None:
        self.root = root

    def list(self) -> list[SkillSummary]:
        """扫描目录，解析每个 SKILL.md 的 frontmatter。"""
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
        """渐进加载：每次调用都从磁盘重读正文，不缓存。"""
        skill_file = self.root / name / "SKILL.md"
        if not skill_file.is_file():
            raise FileNotFoundError(f"技能 {name} 不存在")
        text = skill_file.read_text(encoding="utf-8")
        return _strip_frontmatter(text)
```

三个要点：

- `list()` 只碰 frontmatter：摘要扫描时不读正文，目录操作的成本与技能正文大小无关。官方 list 返回的同样是胜出摘要，按名称排序。
- `load()` 无缓存：这是渐进式加载的核心语义。官方强调每次调用都请求正文，而不是在注册表缓存，教学版直译成每次 read_text。
- frontmatter 是约定：教学版手写一个十几行的解析器，只认 name 与 description 两行。官方用同一约定，配套校验与资源目录，原理一致。

## 12.3 render：skill_content 块

技能正文以什么形态交给模型？官方的规范是 renderSkillContent，渲染成 `<skill_content>` 标签块，官方文档第 44 行写明它是两条加载路径的唯一真源，无论加载由谁发起，模型看到的都是同一种形态：

```python
SKILL_CONTENT_OPEN = '<skill_content name="{name}">'
SKILL_CONTENT_CLOSE = "</skill_content>"

    def render(self, name: str) -> str:
        body = self.load(name)
        return f"{SKILL_CONTENT_OPEN.format(name=name)}\n{body}\n{SKILL_CONTENT_CLOSE}"
```

标签有三个作用：一是边界清晰，模型能明确区分技能指令与对话正文，知道该按哪段执行；二是名字在标签里，模型引用技能时指名道姓，界面也能解析出这条指令来自哪个技能；三是形态统一，无论技能来自磁盘、远程还是运行时注册，官方支持三种来源，模型看到的都是同一个形状。

## 12.4 运行完整示例

```bash
uv run python chapters/12-instructions-skills/src/demo.py
```

完整输出，本地确定性运行：

```
━━━ ① 目录消息：模型每轮看到的「技能菜单」 ━━━
  可用技能：
- git-commit-guide: 如何编写规范的 git commit message（类型、范围、主题、正文）
- web-search-guide: 如何使用 Web Search 工具高效搜索网络、挑选来源、引用结果
  （目录消息 120 字符 ≈ 30 token）

━━━ ② 模型请求 skill 工具 → 渐进加载 → <skill_content> 块 ━━━
  <skill_content name="web-search-guide">
# Web Search 使用指南

当用户的问题需要最新信息（新闻、版本号、价格）时，调用 web_search 工具。
...
  （加载后的指令体 ≈ 76 token）

━━━ ③ 账本：常驻注入 vs 按需加载 ━━━
  常驻注入（2 个技能全部加载）: 每轮 202 token
  按需加载（目录 + 1 个技能）  : 每轮 106 token
  每轮节省 ≈ 96 token；技能越多、正文越长，差距越大

━━━ ④ 每次重读不缓存：改文件立即生效 ━━━
  修改后再加载，新内容已生效: True
```

账本数字直观回答了 12.1 节的问题：两个小技能就省 96 token/轮，50 个大技能时差距是数量级的。第 ④ 节验证重读不缓存，改掉技能文件里的一个字，立刻加载就拿到新版本。

## 本章小结

- 技能存储约定：`skills/<name>/SKILL.md` + frontmatter
- `SkillCatalog`：摘要扫描不碰正文、渐进加载每次重读
- `render`：`<skill_content>` 标签块的三个作用
- 两个示例技能：web-search-guide、git-commit-guide

## 对照官方

| 官方实现 | 我们对应实现 | 说明 |
|----------|--------------|------|
| [`packages/skill/skill/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/skill/skill/README.zh.md) | `SkillCatalog` | 官方摘要目录在第 17 行，renderSkillContent 在第 44 行，渐进式加载在第 56 行，与本章一一对应 |
| 同上，第 9、60 行 | （练习 3） | 官方技能分层：宿主与 scope 分层、层内按 rank 裁决重名，运行时 skill 用 rank 250；教学版只有磁盘一个来源 |
| [`packages/skill/tool-skill/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/skill/tool-skill/README.zh.md) | （练习 2） | 官方把目录消息与 skill 工具做成一个模型面工具插件，模型经它触发加载 |

## 练习

1. **菜单质量。** 把两个技能的 description 改得含糊，只写做事的手册四个字，推演模型会在什么场景错误地调用或不调用它们；思考 description 为什么是技能目录里最重要的字段。
2. **skill 工具。** 按第 02 章的模式，把 `catalog.render` 包装成一个 skill 工具，参数是 name，挂进第 07 章的 Agent，让模型在真实对话里按需加载技能，需要 .env 跑真实对话。
3. **分层覆盖。** 给 SkillCatalog 加一个 `user_root` 参数，两个目录都有同名技能时用户目录胜出，这是官方分层与 rank 机制的简化版，实现并写一个演示。
4. **资源引用。** 官方技能正文可以引用同目录的图片等资源，renderSkillContent 会附上资源提示。设计一个同样的机制，正文里出现资源引用时加载器提示文件位置，讨论模型在纯文本接口下如何使用资源。
