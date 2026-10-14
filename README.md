# NUM STL Viewer —— Python 复现版

对 `NUM_STLViewer.exe` 的**从零 Python 复现实现**。原程序是 NUM（NUMROTO 刀具磨削软件）
随 3D 仿真结果分发的一个 STL/PLY 查看器。本仓库用纯 Python 重建了它的核心能力：
模型读写、轨道相机、**软件光栅化渲染**（线框 / 实体 / 彩色 / 透明）、
光照设置、坐标轴与十字线、2D/3D 测量、多对象场景、单位换算、多语言、
旋转动画、宏录制与截图导出。

> 渲染器为自研软件光栅化（Z-buffer + 画家算法 + 线性增量边函数），
> 仅依赖 numpy；自带 PNG 编解码器与 5×7 位图字体，**不依赖 Pillow / OpenGL / Qt 渲染**。
> 图形界面使用 PyQt5（可选，缺省时仍可用命令行模式）。

> **关于仓库范围**：本仓库只包含重新编写的 Python 实现。原商业程序的二进制
> （`NUM_STLViewer.exe`）、由其提取的字符串表（`_strings.txt`）以及原程序的界面配置
> （`NUM_STLViewer.ini`）**均不随仓库分发**，已在 `.gitignore` 中排除；下文的逆向结论
> 仅以文字形式保留，供理解实现依据。想运行本项目也不依赖这些文件。

---

## 1. 原程序逆向结论

| 项目 | 值 |
|------|----|
| 文件类型 | PE32 i386，Delphi / C++Builder VCL 应用 |
| 构建时间 | 2019-10-07 |
| 体积 | 5.19 MB |
| 子系统 | Windows GUI |
| 渲染后端 | OpenGL / Direct3D 双后端（NIRenderer3d 缓存） |
| 3D 库 | T3dStl / T3dLdiSolidPlus / TSolidOperationModel / TTriangle |
| 支持格式 | `*.stl` `*.stpack` `*.ply` `*.exe`（自包含导出件，内嵌 STL） |
| 多语言 | EN CH CS FR GR HU IT NL PL PO RO RU SP SW TU（15 种） |
| 菜单树 | File / View / Mesh / Light / Measurement / Extras / Help |

原程序菜单项（由字符串提取得到）包括 `miFile`、`miView`、`miMesh`、`miLight`、
`miMeasurement`、`miExtras`、`miSpin*`（旋转动画）、`miMovieMacro*`（宏录制）、
`miLang*`（语言切换）、`miSTL*`（格式相关）。这些都在复现版中一一对应。

---

## 2. 目录结构

```
stl-viewer-py/
├── requirements.txt           # numpy 必需，PyQt5 可选
├── run_viewer.py              # 便捷启动脚本
├── README.md                  # 本文档
├── .gitignore                 # 排除专有文件、缓存与运行产物
├── samples/                   # 示例模型与渲染结果
│   ├── drill_blank.stl        # 示例钻头毛坯（二进制 STL）
│   ├── drill_blank_ascii.stl  # 示例（ASCII STL）
│   ├── preview_solid.png      # 实体渲染样例
│   ├── preview_wire.png       # 线框渲染样例
│   ├── preview_color.png      # 分量着色样例
│   ├── preview_gui.png        # 图形界面截图
│   └── _drag_compare.png      # 拖拽降分辨率 vs 全分辨率 对齐对比
├── tests/
│   ├── test_core.py           # 103 项核心测试（数学/IO/网格/相机/渲染/视口/测量/宏/CLI）
│   ├── test_gui_smoke.py      # 63 项图形界面测试（含鼠标交互）
│   ├── check_drag_visual.py   # 拖拽画面对齐的可视化验证（输出对比图）
│   └── make_sample.py         # 生成示例模型与预览图
└── num_stl_viewer/            # 主包（13 个模块 + 入口）
    ├── __init__.py            # 包说明与版本
    ├── __main__.py            # 入口（GUI / --cli 分派）
    ├── mathutil.py            # 向量/矩阵/几何工具
    ├── stl_io.py              # STL / PLY / 内嵌模型读写
    ├── mesh.py                # 网格修复/统计/着色/剖切
    ├── camera.py              # 轨道相机（透视/正交/标准视图）
    ├── lighting.py            # 光照预设与着色模型
    ├── renderer.py            # 软件光栅化渲染器（核心）
    ├── measure.py             # 2D/3D 测量工具
    ├── scene.py               # 多对象场景与图元生成
    ├── i18n.py                # 15 种语言
    ├── settings.py            # INI 设置持久化（mm/inch、视图选项）
    ├── macro.py               # 旋转动画与宏录制
    ├── png.py                 # 自带 PNG 编解码器
    ├── font5x7.py             # 自带 5×7 位图字体
    ├── cli.py                 # 命令行接口
    └── viewer.py              # PyQt5 图形界面
```

---

## 3. 安装与运行

```bash
# 1) 安装依赖
pip install -r requirements.txt        # numpy>=1.22（必需），PyQt5>=5.15（可选）

# 2) 启动图形界面
python run_viewer.py                   # 打开示例场景
python run_viewer.py model.stl         # 打开单个模型
python run_viewer.py a.stl b.stl       # 同时打开多个模型

# 等价的模块方式
python -m num_stl_viewer model.stl

# 3) 无图形环境 / 批处理 → 命令行模式
python -m num_stl_viewer --cli --help
```

> 无 PyQt5 时，直接运行会给出安装提示，命令行模式不受影响。

---

## 4. 命令行接口（CLI）

```bash
python -m num_stl_viewer --cli <子命令> [参数]
```

### 4.1 `info` —— 统计模型与文件信息

```bash
python -m num_stl_viewer --cli info samples/drill_blank.stl
python -m num_stl_viewer --cli info model.stl --json   # JSON 输出
```

输出：三角形/顶点数、包围盒、表面积、体积、连通分量数、退化面数、
边界边数、非流形边数、水密性判断等。

### 4.2 `render` —— 渲染图片

```bash
# 实体 + 抗锯齿，双倍分辨率
python -m num_stl_viewer --cli render model.stl -o out.png --double --antialias

# 线框 + 网格 + 坐标轴，指定视角与尺寸
python -m num_stl_viewer --cli render model.stl -o wire.png --wire --grid --view iso

# 按连通分量着色
python -m num_stl_viewer --cli render model.stl -o parts.png --component-colors

# 法向彩虹图 / 平滑着色
python -m num_stl_viewer --cli render model.stl -o n.png --orientation
python -m num_stl_viewer --cli render model.stl -o s.png --smooth

# 透明背景（对应原程序 "Save Picture for NUMROTO"）
python -m num_stl_viewer --cli render model.stl -o out.png --transparent

# 任意朝向
python -m num_stl_viewer --cli render model.stl -o v.png --yaw 35 --pitch 20
```

常用参数：`-o/--output`、`--size WxH`、`--view {iso,iso_back,front,back,left,right,top,bottom}`、
`--yaw`、`--pitch`、`--ortho`、`--wire`、`--colored`（整件同色，默认）、
`--uniform`、`--component-colors`、`--curvature-colors`、`--orientation`、
`--smooth`、`--edges`、`--grid`、`--crosshair`、`--no-axis`、
`--transparent`、`--transparent-object`、`--alpha`、`--background`、`--gradient`、
`--supersample N`、`--antialias`、`--double`、`--no-weld`、`--json`。
载入时默认自动焊接重合顶点（STL 多为未焊接的三角汤），`--no-weld` 可关闭。

### 4.3 `convert` —— 转换 / 修复 STL

```bash
python -m num_stl_viewer --cli convert in.stl -o out.stl --ascii    # 转 ASCII
python -m num_stl_viewer --cli convert in.stl -o out.stl            # 转二进制（默认）
python -m num_stl_viewer --cli convert in.stl -o out.stl --weld --repair
python -m num_stl_viewer --cli convert a.stl b.stl --repair         # 批量，自动命名 *_out.stl
```

参数：`files...`（一个或多个输入）、`-o/--output`（单文件时指定输出）、
`--ascii`、`--weld`、`--repair`、`--tolerance`。输出目录不存在时会自动创建。

### 4.4 `components` —— 拆分为连通分量

```bash
python -m num_stl_viewer --cli components model.stl --weld \
    --out-prefix parts/part --min-triangles 1
```

参数：`file`（输入）、`--out-prefix`（输出前缀，目录自动创建）、
`--min-triangles`（小于该面数的分量跳过）、`--weld`、`--repair`。
输出命名为 `<prefix>_part01.stl`、`<prefix>_part02.stl`……

### 4.5 `measure` —— 测量计算

所有测量都以**文件为第一个参数**，用选项指定几何，点之间用 `;` 分隔：

```bash
# 两点距离
python -m num_stl_viewer --cli measure model.stl --line "0,0,-20;0,0,20"

# 三点定圆（圆心 / 半径 / 直径）
python -m num_stl_viewer --cli measure model.stl --circle "0,0,0;10,0,0;5,8,0"

# 两点定圆
python -m num_stl_viewer --cli measure model.stl --circle "0,0,0;10,0,0"

# 三点夹角（第一点为顶点）
python -m num_stl_viewer --cli measure model.stl --angle "0,0,0;1,0,0;0,1,0"

# 单点 / RZ 点（柱坐标，NUMROTO 场景）
python -m num_stl_viewer --cli measure model.stl --point "1,2,3"
python -m num_stl_viewer --cli measure model.stl --rz "10,0,5"

# 指定 Z 高度处的最大回转半径
python -m num_stl_viewer --cli measure model.stl --radius-at 5

# 英寸单位 + JSON 输出
python -m num_stl_viewer --cli measure model.stl --rz "10,0,5" --units inch --json
```

---

## 5. 图形界面

界面结构复刻原程序的菜单树：

- **文件 File**：打开 / 追加 / 关闭 / 导出 STL（ASCII、二进制）/ 保存图片
  （PNG、剪贴板、透明背景版）/ 最近文件 / 退出。
- **视图 View**：标准视图（等轴、前后左右、上下）/ 透视与正交切换 /
  重置 / 适配窗口 / 视图选项（显示坐标轴、网格、十字线、边线、背景渐变，
  以及**鼠标灵敏度**与**拖拽画质 / 抗锯齿 / 载入时焊接顶点**等选项）。
- **网格 Mesh**：焊接顶点 / 移除退化面 / 修复法线 / 翻转法线 /
  着色模式（整件同色、按分量、按曲率、按法向）/ 统计信息 / 分割连通分量 / 删除选中。
- **光照 Light**：预设（默认、工作室、金属、平坦…）、环境光/漫反射/高光调节。
- **测量 Measurement**：点、直线(3D)、直线(2D)、两点圆、三点圆、角度、RZ 点；
  标注显示在画布上；可清除。
- **附加 Extras**：旋转动画（多种轴向与速度）、宏录制/播放/存取、
  语言切换（15 种）、单位切换（mm / inch）。
- **帮助 Help**：关于对话框。

### 着色模式

实体视图默认是**整件同色 + 光照明暗**，也就是零件本来的样子；
要"用颜色表达信息"时才切到逐面取色的模式：

| 模式 | 含义 |
|------|------|
| 整件同色（默认，`auto`） | 每个对象一种本色，明暗全部交给光照 |
| 单色平面（`uniform`） | 同上，但关闭逐顶点插值，直接看到三角面片 |
| 按分量（`component`） | 每个连通分量一种颜色，用来区分零件/毛坯 |
| 按曲率（`curvature`） | 曲率热力图，找尖边与过渡区 |
| 按法向（`orientation`） | 法向彩虹图（R/G/B = 0.5+0.5·n），看表面朝向 |

> 早期版本把默认的 `auto` 接到了法向彩虹图上，实体视图看起来像一张花花的
> 贴图 —— 这正是之前反馈的"模型显示失真"的来源，现已拆分为独立的
> `orientation` 模式。开启"载入时焊接顶点"（默认开）后，`--smooth` 或
> 设置里的"插值法向"才能让曲面呈现平滑过渡。

### 鼠标 / 键盘操作

| 操作 | 功能 |
|------|------|
| 左键拖拽 | 轨道旋转 |
| Ctrl + 左键拖拽 / 开启 Rotate around Z | 绕 Z 轴（刀具回转轴）旋转 |
| 中键拖拽 | 平移 |
| 右键拖拽 | 平移 |
| 右键单击 | 弹出上下文菜单 |
| Shift + 左键拖拽 | 平移 |
| Alt + 左键拖拽 | 缩放 |
| 滚轮 / 触控板 | 缩放（支持 `pixelDelta`） |
| 左键单击 | 测量模式下取点（自动吸附最近顶点/边） |
| 双击 | 适配窗口 |
| 方向键 | 旋转；Shift+方向键 平移；Ctrl+方向键 绕 Z / 俯仰 |
| `+` / `-` | 缩放 |
| `F` / `Home` | 适配窗口 |
| `R` | 复位旋转中心 |
| `1`~`8` | 标准视图 |
| 空格 | 播放/暂停旋转动画 |
| `Esc` | 取消当前测量 |

光标会随操作模式变化（旋转=握拳、平移=十字箭头、测量=十字），拖拽时状态栏
同步显示当前操作与帧率。

### 性能与画质

**渐进式渲染**：任何一次视图变化都会先立刻画出一张低分辨率快帧，空闲
`160 ms` 后再把画面精修成全分辨率 + 抗锯齿。整幅 `ss=2` 渲染典型模型要
一二百毫秒，若同步画完会阻塞事件循环 —— 拖拽期间鼠标事件只能排队，
松手后模型"跳"一大截，观感就是又卡又不好控制。

**自适应分辨率**：拖拽旋转、平移、缩放以及旋转动画/宏播放期间降到
`View > 视图选项` 里设定的「拖拽画质」上限以下（默认 50%），并按实测帧耗时
往 40fps 收敛（慢了降到 75%，快了提到 115%，范围 15%–100%，上下阈值带
1.35×/0.6× 迟滞，避免画面忽清忽糊）。

**鼠标灵敏度可调**：`View > 视图选项` 里有旋转 / 平移 / 缩放三个滑条。
旋转默认 **0.20 度/像素**（此前硬编码 0.35，拖 300 px 就转 105 度，
随手一拖模型就跑掉了）。平移默认 1.0，即模型严格跟随光标。

**位图宽高比严格一致**：降分辨率位图由高度反推宽度（`w = round(W·h/H)`），
因此放大铺满视口时不会把模型拉扁/拉长。

**渲染视口**在每次渲染时都会与目标位图尺寸同步，降分辨率渲染与全分辨率
渲染的画面**完全对齐**，不会出现偏移或裁切。

---

## 6. 关键实现说明

- **软件光栅化**：三角形填充使用规范化重心坐标，`edge` 与深度均以
  `c + d·j` **线性增量**方式计算，深度只在真正命中的像素上求值
  （不再构造整幅 `(B,mh,mw)` 中间数组）。
- **按尺寸分桶分批**：一批三角形共用一块 `(B, mh, mw)` 矩形。若直接取批内
  `max(bw)/max(bh)`，一个 31×170 的三角形会把同批全部撑成 31×170，
  实测空洞率达 **8.0×**；改为按 bbox 边长的 2 次幂分桶后降到 **2.0×**，
  光栅化耗时随之下降一半以上。不透明路径遮挡由 Z 缓冲保证，分桶不影响正确性；
  透明混合路径仍保持全局"远 → 近"分批。
- **命中像素解算**：用 `flatnonzero` + `divmod` 由线性下标反解 `(批, 行, 列)`，
  再用 `row_base[tri] + ry*W + rx` 求像素下标 —— 只在 B 个元素里随机取值，
  比按 `(B*mh)` 规模去 `ys.ravel()` 里取快约 3 倍。
- **缓冲复用**：`color/depth/alpha` 三块全尺寸数组按渲染尺寸缓存复用，
  超采样时不再每帧 malloc/free 数十 MB；降采样直接对 uint8 视图分两步求和，
  省掉一份全尺寸 float32 副本。
- **坐标约定**：`Z` 为刀具/旋转轴（朝上），与原程序 NUMROTO 一致。
- **载入自动焊接**：STL 常把同一个几何点按三角形重复存储（三角汤），
  不焊接时每个三角形都是独立连通分量 —— 体积算不出、边界边满天飞
  （示例件 4212 顶点 / 4212 条边界边 → 焊接后 1027 顶点 / 640 条）。
  容差取包围盒对角线的 `1e-6`，可用 `--no-weld` 或视图选项关闭。
- **PNG**：`png.py` 自行实现 zlib + 逐行滤波，支持 8 位非隔行、全部滤波器，
  读写均不依赖第三方库。
- **字体**：`font5x7.py` 内置 5×7 位图字形，用于画布上的标注与 HUD 文字。
- **测量**：支持 `point3d`、`line3d`、`line2d`、`circle2p`、`circle3p`、
  `angle2d`、`rzpoint` 七种类型；`rzpoint` 输出柱坐标 (R, Z, θ)，服务磨削场景。
- **内嵌模型提取**：`stl_io.extract_embedded_mesh` 可从 `.exe` / `.stpack`
  中扫描并解出内嵌的 STL / zlib 流，对复原原程序的「自包含导出件」行为。

### 实测性能（932×707 画布，Windows / CPython 3.13）

| 规模 | 拖拽（50% + ss=1） | 静止精修（100% + ss=2） |
|------|-------------------|------------------------|
| 1.4k 面 | 16–19 ms（≈55fps） | 186 ms |
| 21k 面 | 26–31 ms | 140–235 ms |
| 101k 面 | 110–130 ms | 220–240 ms |

大幅面模型的拖拽代价主要落在"三角形数量"而非分辨率上，故自适应降分辨率
对它的收益有限；这类模型建议在视图选项里调低拖拽画质依赖度，或先用
Mesh > 分割连通分量拆开查看。

---

## 7. 测试

```bash
python tests/test_core.py         # 103 项：数学/IO/网格/相机/渲染/视口/测量/宏/命令行
python tests/test_gui_smoke.py    # 63 项：图形界面构造、鼠标交互、渲染与导出

python tests/make_sample.py       # 重新生成 samples/ 下的示例与预览图
python tests/check_drag_visual.py # 生成拖拽/静止对齐对比图与界面截图
```

当前状态：**核心 103/103 通过，图形界面 63/63 通过**。

---

## 8. 许可与来源

本项目以 **MIT 许可证**发布，详见 [LICENSE](LICENSE)。

本仓库为**教学/研究性质的重实现**，用于理解 `NUM_STLViewer.exe` 的行为。
原程序及其相关商标归 NUM 所有；本复现版不包含原程序的任何二进制代码，
亦不随仓库分发其提取产物。
