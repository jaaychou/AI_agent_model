---
name: manim-animation
description: 用 3b1b 原版 ManimGL 制作数学/线性代数动画：参考《线性代数的本质》源码、写 ManimGL 脚本、渲染 mp4 并预览
triggers: 做动画, 数学动画, 线性代数动画, 3b1b, manim, 视频演示, eola, 可视化推导
---

# Manim 动画制作（3b1b 原版 ManimGL）

用户要做数学动画、线性代数可视化、模仿 3b1b《线性代数的本质》风格时用本技能。

## 环境（已集成）

- 引擎：`tools/manim`（github.com/3b1b/manim，ManimGL 原版），专用环境 `venv-manim`。
- 线代源码：`tools/manim_videos/_2016/eola/`，chapter0~chapter11 全 16 个文件，是 3b1b 原视频脚本。
- 工具：
  - `manim_status`：先查环境是否就绪。
  - `manim_examples`：列出线代每章的场景类名，参考动画思路。
  - `manim_render`：写脚本 → 渲染 mp4 → 弹窗预览。

## 标准流程

1. **想清楚动画讲什么**：一个 Scene 只讲一个概念（如"矩阵变换如何旋转向量"）。
2. **参考现成思路**（可选）：`manim_examples` 看线代有哪些场景，读对应 chapter 源码学动画逻辑。
   - 注意：这些是 2016 年老版 API（`OldTexText`、`CONFIG` 字典、`TexMobject`），与当前 ManimGL **不兼容**，不能直接渲染。只学思路。
3. **写当前 API 脚本**（`from manimlib import *`），定义 `Scene` 子类：

```python
from manimlib import *

class MyScene(Scene):
    def construct(self):
        t = Text("线性变换", font_size=48)
        self.play(FadeIn(t))
        self.wait(1)
        # 画向量、矩阵变换
        arrow = Arrow(ORIGIN, np.array([2, 1, 0]), buff=0, color=BLUE)
        self.play(GrowArrow(arrow))
        self.wait(2)
```

4. **渲染**：调用 `manim_render(code=上面脚本, scene="MyScene", resolution="medium", preview=True)`。
5. **看结果**：成功返回 mp4 路径并在 GUI 弹窗预览；失败返回真实报错，按报错修脚本重试。

## 当前 ManimGL 常用 API（别用老版）

- 文本：`Text("内容", font_size=48)`（老版是 `OldTexText`/`TexMobject`）
- 公式：`Tex(r"\\vec{v}")`
- 图形：`Arrow`、`Circle`、`Square`、`Line`、`Matrix`
- 布局：`VGroup`、`arrange()`、`next_to()`、`.to_edge(UP)`
- 动画：`FadeIn`、`FadeOut`、`Write`、`GrowArrow`、`Transform`、`ApplyMethod`
- 坐标：`np.array([x, y, 0])`；常量 `ORIGIN`/`UP`/`DOWN`/`LEFT`/`RIGHT`/`BLUE`/`RED`

## 注意事项（诚实边界）

- 渲染很吃 CPU/显卡，单次上限 300 秒会中止；先 `resolution="low"` 快速验证，再 medium/high。
- 建议开着资源守护，避免高分辨率动画把资源占满。
- manimgl 依赖 OpenGL/图形库；若渲染报缺 DLL 或图形错误，如实告诉用户环境限制，不假装成功。
- 老视频源码仅作参考，直接渲染大概率报错——这是 API 版本差异，如实说明。
