---
name: git-commit-guide
description: 如何编写规范的 git commit message（类型、范围、主题、正文）
---

# Git Commit 规范

用户要求提交代码时，按 Conventional Commits 风格编写提交信息。

## 格式

```
<type>(<scope>): <subject>

<body>
```

- type：feat / fix / docs / refactor / chore / test
- subject：一句话说清「做了什么」，不超过 50 字符，用中文
- body：为什么这么做、影响范围（可选）

## 步骤

1. 先查看改动：git diff 与 git status；
2. 归纳改动主题，选择 type；
3. 编写 subject 与 body；
4. 提交前确认没有把密钥、临时文件加入版本库。
