# 12｜技能与按需加载

> 预计时间：50 分钟 ｜ 前置：完成第 09 章（token 概念） ｜ 本章纯本地运行，不调用模型

随着 Agent 能力增加，系统中会出现一类面向具体任务的操作手册，例如怎样搜索网络、编写 commit message 或运行某个项目的测试。这些内容不同于长期生效的人设规则。如果全部放入系统提示词，每轮请求都要携带所有手册，即使当前任务并不需要它们。

官方的答案是 Skills（技能）：模型每轮只看到一份技能菜单，名字加一句话描述，真正用到哪个技能时，调用 skill 工具把那份手册按需加载进来。这个设计叫渐进式加载：`get()` 每次都向胜出的提供方请求正文，而不是把正文永久缓存到注册表中。

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
            if not SKILL_NAME.fullmatch(skill_dir.name):
                raise ValueError(f"无效的技能目录名: {skill_dir.name!r}")
            frontmatter = _read_frontmatter(skill_file)
            if frontmatter["name"] != skill_dir.name:
                raise ValueError(
                    f"{skill_file} 的 name 必须与目录名 {skill_dir.name!r} 一致"
                )
            summaries.append(
                SkillSummary(
                    name=frontmatter["name"],
                    description=frontmatter["description"],
                )
            )
        return summaries

    def load(self, name: str) -> str:
        """渐进加载：每次调用都从磁盘重读正文，不缓存。"""
        if not SKILL_NAME.fullmatch(name):
            raise ValueError(f"无效的技能名: {name!r}")
        skill_file = self.root / name / "SKILL.md"
        if not skill_file.is_file():
            raise FileNotFoundError(f"技能 {name} 不存在")
        text = skill_file.read_text(encoding="utf-8")
        return _strip_frontmatter(text)
```

四个要点：

- `list()` 通过 `_read_frontmatter()` 读到第二个 `---` 就停止：摘要扫描不会加载正文，目录操作的成本与技能正文大小无关。缺失结束分隔符会立即报错。官方 list 返回的同样是胜出摘要，按名称排序。
- `load()` 无缓存：这是渐进式加载的核心语义。官方强调每次调用都请求正文，而不是在注册表缓存，教学版直译成每次 read_text。
- frontmatter 是约定：教学版手写一个十几行的解析器，只认 name 与 description 两行。官方用同一约定，配套校验与资源目录，原理一致。
- 名字也是安全边界：只接受 kebab-case，而且 frontmatter 的 `name` 必须等于目录名。这样 `../other-file` 不能借技能名穿越目录，目录与菜单也不会各说各话。

## 12.3 render：skill_content 块

技能正文以什么形态交给模型？官方的规范是 `renderSkillContent`，无论加载由谁发起，模型看到的都是同一种 `<skill_content>` 形态：

```python
    def render(self, name: str) -> str:
        body = self.load(name)
        base = (self.root / name).resolve()
        return "\n".join(
            [
                f'<skill_content name="{name}">',
                "<skill_resources>",
                f"Base directory for this skill: {base}",
                "Resolve relative paths mentioned by this skill against the base "
                "directory before using them. Load referenced resources only as needed.",
                "</skill_resources>",
                "",
                "<skill_instructions>",
                body,
                "</skill_instructions>",
                "</skill_content>",
            ]
        )
```

这个包装分成两块：`skill_resources` 告诉模型相对路径从哪个目录解析，并提醒它按需加载资源；`skill_instructions` 才是操作手册正文。外层标签同时保留技能名，让边界和来源一目了然。无论技能来自磁盘、远程还是运行时注册，官方都会渲染成这一统一形态。

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
<skill_resources>
Base directory for this skill: …/skills/web-search-guide
Resolve relati…
  （加载后的指令体 ≈ 162 token）

━━━ ③ 账本：常驻注入 vs 按需加载 ━━━
  常驻注入（2 个技能全部加载）: 每轮 374 token
  按需加载（目录 + 1 个技能）  : 每轮 192 token
  每轮节省 ≈ 182 token；技能越多、正文越长，差距越大

━━━ ④ 每次重读不缓存：改文件立即生效 ━━━
  修改后再加载，新内容已生效: True
```

账本数字直观回答了 12.1 节的问题：两个小技能就能节省约 182 token/轮，50 个大技能时差距是数量级的。第 ④ 节验证重读不缓存，改掉技能文件里的一个字，立刻加载就拿到新版本。

## 12.5 进入 Capstone

第 17 章从 Settings 的 `skills_root` 扫描 SkillCatalog，把名称和描述作为 Prompt 目录常驻，同时注册唯一的 `skill` 工具。模型选择名称后，工具才重新读取并渲染对应 `SKILL.md`；正文不会跟着每次模型请求重复发送。这保留了本章“菜单常驻、正文按需”的渐进式加载边界。

## 本章小结

- 技能存储约定：`skills/<name>/SKILL.md` + frontmatter
- `SkillCatalog`：摘要扫描不碰正文、渐进加载每次重读
- `render`：resources 与 instructions 分层的 `<skill_content>` 标签块
- 名称校验：kebab-case、目录名与 frontmatter 一致，阻止路径穿越和名称漂移
- 两个示例技能：web-search-guide、git-commit-guide

## 对照官方

| 官方实现 | 我们对应实现 | 说明 |
|----------|--------------|------|
| [`packages/skill/skill/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/141eb6fef83422698aef7a981029e843e8161534/packages/skill/skill/README.zh.md) | `SkillCatalog` | 官方同样提供摘要目录、统一内容渲染和每次重新取正文的渐进式加载 |
| 同上 | （未实现） | 官方还支持多来源 provider、宿主与 scope 分层、rank 裁决重名、缓存失效和调用记录；教学版只保留单个磁盘目录 |
| [`packages/skill/tool-skill/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/141eb6fef83422698aef7a981029e843e8161534/packages/skill/tool-skill/README.zh.md) | （练习 2） | 官方把目录消息与 skill 工具做成一个模型面工具插件，模型经它触发加载 |

## 练习

1. **菜单质量。** 把两个技能的 description 改得含糊，只写做事的手册四个字，推演模型会在什么场景错误地调用或不调用它们；思考 description 为什么是技能目录里最重要的字段。
2. **skill 工具。** 按第 02 章的模式，把 `catalog.render` 包装成一个 skill 工具，参数是 name，挂进第 07 章的 Agent，让模型在真实对话里按需加载技能，需要 .env 跑真实对话。
3. **分层覆盖。** 给 SkillCatalog 加一个 `user_root` 参数，两个目录都有同名技能时用户目录胜出，这是官方分层与 rank 机制的简化版，实现并写一个演示。
4. **资源引用。** 在技能目录加入 `references/example.md`，让正文引用它。打印 `render()` 结果，确认 base directory 足以解析相对路径；再讨论为什么资源应该按需读取，而不是随正文一次性全部注入。
