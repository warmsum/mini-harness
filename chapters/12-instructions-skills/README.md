# 12｜Skills：按需加载的操作手册

> 预计时间：50 分钟 ｜ 前置：完成第 09 章（token 概念） ｜ 本章纯本地运行，不调用模型

随着 Agent 能力变多，出现了一类新的指令：不是「你是谁」这种长期
人设，而是「做某类具体任务时该怎么做」的操作手册——怎么搜索网络、
怎么写 commit message、怎么跑某个项目的测试。把这些手册全部塞进
系统提示词行不行？行，但代价是每一轮请求都要背着全部手册的 token，
哪怕这一轮根本用不上。

官方的答案是 **Skills**（技能）：模型每轮只看到一份**技能菜单**
（名字 + 一句话描述），真正用到哪个技能时，调用 `skill` 工具把
那份手册**按需加载**进来。官方把这个设计叫「渐进式加载」，原话
（`skill` 包文档第 56 行）是：「定义仍采用渐进式加载。get() 每次
调用都向胜出提供方请求正文，而不是在此注册表中缓存正文」。

## 12.1 原理：菜单常驻，正文按需

把「技能」拆成两层：

| 层 | 内容 | 什么时候给模型看 |
|----|------|------------------|
| 摘要（summary） | name + 一句话 description | **每轮**（菜单常驻） |
| 正文（body） | 完整的操作手册（SKILL.md） | **按需**（调用 skill 工具时） |

菜单常驻的成本极低（两个技能约 30 token），正文按需加载只在真正
需要时付费（一份手册几十到几百 token）。技能越多、正文越长，这个
设计省得越多——想象 50 个技能、每个 500 token 的正文：常驻注入
每轮背着 25000 token，按需加载只在用到时付 500。

按需加载还有第二个好处：**正文永远最新**。技能文件是用户随时
编辑的（本章 demo 第 ④ 节演示），「每次调用重读磁盘」让 Agent
拿到的永远是刚改过的手册。缓存正文则会让 Agent 拿着旧指令干活。

第三个好处与第 06 章呼应：技能正文由技能**自己**维护（一个技能
一个 SKILL.md 文件），系统提示词组装器不用知道任何技能的存在——
组织上彻底解耦。

## 12.2 SkillCatalog：扫描、解析、加载

技能的存储形态是一个约定：`skills/<name>/SKILL.md`，文件开头是
frontmatter（元数据块），后面是正文：

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
        """渐进加载：每次调用都从磁盘重读正文（不缓存）。"""
        skill_file = self.root / name / "SKILL.md"
        if not skill_file.is_file():
            raise FileNotFoundError(f"技能 {name} 不存在")
        text = skill_file.read_text(encoding="utf-8")
        return _strip_frontmatter(text)
```

值得注意的三点：

- **`list()` 只碰 frontmatter**：摘要扫描时不读正文——目录操作
  的成本与技能正文大小无关。
- **`load()` 无缓存**：这是「渐进式加载」的核心语义。官方 :56
  强调「每次调用都请求正文，而不是在注册表缓存」——教学版直译
  成「每次 read_text」。
- **frontmatter 是约定而非格式**：教学版手写一个十几行的解析器
  （`name:` / `description:` 两行），官方用 frontmatter 规范 +
  资源目录，原理一致。

## 12.3 render：<skill_content> 块

技能正文以什么形态交给模型？官方的规范是 `renderSkillContent`
（`skill` 包文档第 44 行）——渲染成 `<skill_content>` 标签块：

```python
SKILL_CONTENT_OPEN = '<skill_content name="{name}">'
SKILL_CONTENT_CLOSE = "</skill_content>"

    def render(self, name: str) -> str:
        body = self.load(name)
        return f"{SKILL_CONTENT_OPEN.format(name=name)}\n{body}\n{SKILL_CONTENT_CLOSE}"
```

标签有三个作用：一是**边界清晰**——模型能明确区分「技能指令」
与「对话正文」，知道该按哪段执行；二是**名字在标签里**——模型
引用技能时指名道姓，界面也能解析出「这条指令来自哪个技能」；
三是**形态统一**——无论技能来自磁盘、远程还是运行时注册（官方
支持三种来源），模型看到的都是同一个形状。

## 12.4 跑一遍完整 demo

```bash
uv run python chapters/12-instructions-skills/src/demo.py
```

完整输出（本地确定性运行）：

```
━━━ ① 目录消息：模型每轮看到的「技能菜单」 ━━━
  可用技能：
- git-commit-guide: 如何编写规范的 git commit message（类型、范围、主题、正文）
- web-search-guide: 如何使用 Web Search 工具高效搜索网络、挑选来源、引用结果
  （目录消息 120 字符 ≈ 30 token）

━━━ ② 模型请求 skill 工具 → 渐进加载 → <skill_content> 块 ━━━
  <skill_content name="web-search-guide">
# Web Search 使用指南
...
  （加载后的指令体 ≈ 76 token）

━━━ ③ 账本：常驻注入 vs 按需加载 ━━━
  常驻注入（2 个技能全部加载）: 每轮 202 token
  按需加载（目录 + 1 个技能）  : 每轮 106 token
  每轮节省 ≈ 96 token；技能越多、正文越长，差距越大

━━━ ④ 每次重读不缓存：改文件立即生效 ━━━
  修改后再加载，新内容已生效: True
```

账本数字直观回答了 12.1 节的第一个问题：两个小技能就省 96
token/轮，50 个大技能时差距是数量级的。第 ④ 节顺手验证了
「重读不缓存」——改掉技能文件里的一个字，立刻加载就拿到
新版本。

## 12.5 本章小结：亲手写了什么

- 技能存储约定：`skills/<name>/SKILL.md` + frontmatter
- `SkillCatalog`：摘要扫描（不碰正文）、渐进加载（每次重读）
- `render`：`<skill_content>` 标签块的三个作用
- 两个示例技能：web-search-guide、git-commit-guide

## 12.6 对照官方 DSH

| 官方实现 | 我们对应实现 | 说明 |
|----------|--------------|------|
| [`packages/skill/skill/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/skill/skill/README.zh.md) | `SkillCatalog` | 官方摘要目录（第 17 行 list）、渐进式加载（第 56 行）、`renderSkillContent`（第 44 行）与本章一一对应 |
| 同上（第 60 行） | （练习 3） | 官方技能分「来源层」：运行时注册、项目、用户根目录，按 rank 分层覆盖——教学版只有磁盘一个来源 |
| [`packages/skill/tool-skill/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/skill/tool-skill/README.zh.md) | （练习 2） | 官方把「目录消息 + skill 工具」做成一个模型面工具插件，模型经它触发加载 |

## 12.7 练习

1. **菜单质量**：把两个技能的 description 改得含糊（如「做事的
   手册」），推演模型会在什么场景错误地调用/不调用它们；思考
   description 为什么是技能目录里最重要的字段。
2. **skill 工具**：按第 02 章的模式，把 `catalog.render` 包装成
   一个 `skill` 工具（参数 name），挂进第 07 章的 Agent——让模型
   在真实对话里按需加载技能（需要 .env 跑真实对话）。
3. **分层覆盖**：给 SkillCatalog 加一个 `user_root` 参数，两个
   目录都有同名技能时用户目录胜出（官方 rank 机制的简化版），
   实现并写一个演示。
4. **资源引用**：官方技能正文可以引用同目录的图片等资源。设计
   一个「资源提示」机制（如正文里写 `![图](res.png)` 时加载器
   提示文件位置），讨论模型在纯文本接口下如何使用资源。
