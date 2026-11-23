---
name: file-organizer
description: 整理工作空间文件，按类型分类、查找重复文件、生成目录清单
triggers: 整理文件, 文件分类, 查找重复
---

# 文件整理技能

当用户要求整理文件时：

1. 用 `list_dir` 查看目录结构
2. 用 `search_files` 按扩展名分类统计文件
3. 如果需要生成清单，用 `write_file` 输出一个 REPORT.md
4. 移动/删除文件前必须先征得用户确认
5. 输出整理建议，但不要擅自改动用户文件
