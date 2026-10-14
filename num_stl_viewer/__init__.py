"""
num_stl_viewer —— NUM_STLViewer.exe 的 Python 复现实现
=========================================================

原程序信息（逆向自 NUM_STLViewer.exe, PE32 i386, Delphi/C++Builder VCL,
构建时间 2019-10-07, 5.19 MB, 子系统 Windows GUI）:

    * 名称        : NUM STL Viewer / NUMROTO-3D STL 查看器
    * 渲染引擎    : OpenGL / Direct3D 双后端，带 NIRenderer3d 缓存
    * 3D 库        : T3dStl / T3dLdiSolidPlus / TSolidOperationModel / TTriangle
    * 支持格式    : *.stl  *.stpack  *.ply  *.exe(自包含导出件)
    * 多语言      : EN CH CS FR GR HU IT NL PL PO RO RU SP SW TU
    * 由 NUM（NUMROTO 刀具磨削软件）随 3D 仿真结果一起分发

本包用纯 Python 复现其核心能力: 模型读写、轨道相机、软件光栅化渲染
（线框/实体/彩色/透明）、光照设置、坐标轴/十字线、测量工具、
多对象场景、单位换算、多语言、旋转动画、宏录制与截图导出。

依赖: numpy（必需），PyQt5（图形界面可选）。
"""

__version__ = "1.0.0"
__all__ = [
    "mathutil",
    "stl_io",
    "mesh",
    "camera",
    "lighting",
    "renderer",
    "measure",
    "scene",
    "i18n",
    "settings",
    "macro",
    "png",
]
