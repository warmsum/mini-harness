"""第 12 章 demo：渐进加载的 token 账本。

运行（无需 API，纯本地）：
    uv run python chapters/12-instructions-skills/src/demo.py

演示：
1. 目录消息：模型每轮看到的「技能菜单」只有摘要（算 token 账）；
2. 模拟模型请求调用 skill 工具 → 渐进加载正文 → <skill_content> 块；
3. 账本对比：常驻注入全部技能 vs 按需加载一个技能，差了多少 token；
4. 验证「每次重读不缓存」：修改技能文件后再加载，拿到的是新内容。
"""

from __future__ import annotations

from pathlib import Path

from skills import SkillCatalog, estimate_tokens


def section(title: str) -> None:
    print(f"\n━━━ {title} ━━━")


def main() -> None:
    skills_root = Path(__file__).resolve().parent / "skills"
    catalog = SkillCatalog(skills_root)

    section("① 目录消息：模型每轮看到的「技能菜单」")
    menu = catalog.catalog_text()
    print(f"  {menu}")
    menu_tokens = estimate_tokens(menu)
    print(f"  （目录消息 {len(menu)} 字符 ≈ {menu_tokens} token）")

    section("② 模型请求 skill 工具 → 渐进加载 → <skill_content> 块")
    rendered = catalog.render("web-search-guide")
    print(f"  {rendered[:200]}…")
    body_tokens = estimate_tokens(rendered)
    print(f"  （加载后的指令体 ≈ {body_tokens} token）")

    section("③ 账本：常驻注入 vs 按需加载")
    all_loaded = sum(
        estimate_tokens(catalog.render(s.name)) for s in catalog.list()
    )
    print(f"  常驻注入（2 个技能全部加载）: 每轮 {menu_tokens + all_loaded} token")
    print(f"  按需加载（目录 + 1 个技能）  : 每轮 {menu_tokens + body_tokens} token")
    print(
        f"  每轮节省 ≈ {all_loaded - body_tokens} token；"
        "技能越多、正文越长，差距越大"
    )

    section("④ 每次重读不缓存：改文件立即生效")
    skill_file = skills_root / "git-commit-guide" / "SKILL.md"
    original = skill_file.read_text(encoding="utf-8")
    try:
        skill_file.write_text(
            original.replace("不超过 50 字符", "不超过 60 字符"),
            encoding="utf-8",
        )
        reloaded = catalog.load("git-commit-guide")
        print(f"  修改后再加载，新内容已生效: {'不超过 60 字符' in reloaded}")
    finally:
        skill_file.write_text(original, encoding="utf-8")  # 还原，保持 demo 幂等


if __name__ == "__main__":
    main()
