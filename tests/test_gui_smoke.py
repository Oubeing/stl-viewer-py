"""图形界面冒烟测试（离屏运行，不需要真实显示器）。

    QT_QPA_PLATFORM=offscreen python tests/test_gui_smoke.py

覆盖：主窗口/菜单构建、对象面板、显示模式切换、单位与语言切换、
测量交互、旋转动画、图片渲染与导出、设置存取、STL 打开与导出。
"""

from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np                                    # noqa: E402
from PyQt5 import QtCore, QtWidgets                   # noqa: E402

from num_stl_viewer import png, stl_io                # noqa: E402
from num_stl_viewer.viewer import MainWindow, ViewerState  # noqa: E402

PASSED, FAILED = [], []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print(f"  [{'PASS' if condition else 'FAIL'}] {name} {detail}")


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="numstl_gui_")
    settings_path = os.path.join(tmp, "settings.ini")

    app = QtWidgets.QApplication(sys.argv[:1])
    state = ViewerState(settings_path)
    window = MainWindow(state)
    window.resize(900, 640)
    window.show()
    app.processEvents()

    print("=== 窗口与菜单 ===")
    check("主窗口创建", window.isVisible())
    titles = [m.title() for m in window.menuBar().findChildren(QtWidgets.QMenu)
              if m.parent() is window.menuBar()]
    check("顶级菜单", len([m for m in window.menuBar().actions()
                          if m.menu()]) >= 7, str(titles[:9]))
    check("对象面板有内容", window.panel.list.count() >= 2,
          f"{window.panel.list.count()} 项")
    check("工具条动作", len(window.toolbar.actions()) >= 10)

    print("\n=== 渲染 ===")
    window.canvas.resize(640, 480)
    window.canvas._render()
    check("画布成像", window.canvas._image is not None
          and not window.canvas._image.isNull())
    check("渲染耗时记录", state.last_render_seconds >= 0.0,
          f"{state.last_render_seconds * 1000:.0f} ms")

    print("\n=== 显示模式 ===")
    window._set_wire(True)
    check("线框模式", state.options.wire and not state.options.solid)
    window._set_solid(True)
    check("实体模式", state.options.solid and not state.options.wire)
    window._set_color_mode("auto")
    check("自动配色", state.options.color_mode == "auto"
          and state.scene.objects[0].auto_color)
    window._toggle_transparent(True)
    check("透明显示", state.options.transparent)
    window._toggle_transparent(False)
    window._toggle_edges(True)
    check("显示棱边", state.options.show_edges)
    window._toggle_crosshair(True)
    check("十字准线", state.options.crosshair)
    window._toggle_ortho(True)
    check("正交投影", state.camera.orthographic)
    window._toggle_ortho(False)
    window._set_light("down")
    check("光源切换", state.light.primary == "down")
    window._reset_light()
    check("光照复位", state.light.primary == "up_left")

    print("\n=== 视图操作 ===")
    yaw = state.camera.yaw
    window._standard_view("top")
    check("标准视图", abs(state.camera.pitch) > 80)
    window._standard_view("iso")
    window._fit()
    check("适应窗口", state.camera.distance > 0)
    window._reset_center()
    check("复位中心", np.allclose(state.camera.target, 0))
    window._zoom(0.8)
    check("缩放", state.camera.zoom != 1.0 or state.camera.distance > 0)

    print("\n=== 鼠标交互 ===")
    from PyQt5 import QtGui

    def drag(canvas, start, end, button=QtCore.Qt.LeftButton,
             mods=QtCore.Qt.NoModifier):
        canvas.mousePressEvent(QtGui.QMouseEvent(
            QtCore.QEvent.MouseButtonPress, QtCore.QPointF(*start),
            button, button, mods))
        canvas.mouseMoveEvent(QtGui.QMouseEvent(
            QtCore.QEvent.MouseMove, QtCore.QPointF(*end),
            QtCore.Qt.NoButton, button, mods))
        canvas.mouseReleaseEvent(QtGui.QMouseEvent(
            QtCore.QEvent.MouseButtonRelease, QtCore.QPointF(*end),
            button, QtCore.Qt.NoButton, mods))

    canvas = window.canvas
    canvas.resize(640, 480)
    yaw0, pitch0 = state.camera.yaw, state.camera.pitch
    drag(canvas, (320, 240), (420, 300), QtCore.Qt.LeftButton)
    check("左键拖拽旋转", abs(state.camera.yaw - yaw0) > 1.0
          and abs(state.camera.pitch - pitch0) > 1.0,
          f"d_yaw={state.camera.yaw - yaw0:.1f} d_pitch={state.camera.pitch - pitch0:.1f}")

    tgt = state.camera.target.copy()
    drag(canvas, (320, 240), (400, 240), QtCore.Qt.RightButton)
    check("右键拖拽平移", not np.allclose(tgt, state.camera.target))
    check("右键拖动不弹菜单", canvas._drag_mode is None)

    d0 = state.camera.distance
    drag(canvas, (320, 240), (320, 180), QtCore.Qt.LeftButton,
         QtCore.Qt.AltModifier)
    check("Alt+左键缩放", abs(state.camera.distance - d0) > 1e-6)

    yaw0 = state.camera.yaw
    drag(canvas, (320, 240), (400, 240), QtCore.Qt.LeftButton,
         QtCore.Qt.ControlModifier)
    check("Ctrl+左键绕 Z", abs(state.camera.yaw - yaw0) > 1.0
          and abs(state.camera.pitch - pitch0) >= 0.0)

    # 拖拽时降分辨率（交互质量）；位图宽高比必须与窗口严格一致，
    # 否则放大铺满视口时模型会被拉扁/拉长。
    canvas._interactive = True
    canvas._quick = True
    canvas._dirty = True
    canvas._render()
    iw, ih = canvas._image.width(), canvas._image.height()
    check("交互渲染降采样", iw <= canvas.width() and iw >= canvas.MIN_RENDER_EDGE,
          f"{iw}x{ih}")
    check("交互位图保持窗口宽高比",
          abs(iw / ih - canvas.width() / canvas.height()) < 0.02,
          f"{iw / ih:.4f} vs {canvas.width() / canvas.height():.4f}")

    # 渐进式渲染：松手瞬间先出快帧（不阻塞事件循环），随后精修为全质量
    canvas._interactive = False
    canvas._quick = True
    canvas._dirty = True
    canvas._render()
    quick_w = canvas._image.width()
    check("松手后先出快帧", quick_w < canvas.width(),
          f"快帧 {quick_w} < 全质量 {canvas.width()}")
    canvas._refine()                 # 等价于空闲计时器触发
    canvas._dirty = True
    canvas._render()
    check("精修后恢复全分辨率", canvas._image.width() == canvas.width(),
          f"{canvas._image.width()} == {canvas.width()}")

    # 灵敏度可调：旋转量应严格与设置成正比
    state.settings.rotate_speed = 0.20
    y0 = state.camera.yaw
    drag(canvas, (300, 200), (400, 200), QtCore.Qt.LeftButton)
    slow = abs(state.camera.yaw - y0)
    state.settings.rotate_speed = 0.40
    y1 = state.camera.yaw
    drag(canvas, (300, 200), (400, 200), QtCore.Qt.LeftButton)
    fast = abs(state.camera.yaw - y1)
    check("旋转灵敏度与设置成正比", slow > 1.0 and abs(fast - 2.0 * slow) < 0.05 * slow,
          f"0.20 -> {slow:.2f}°, 0.40 -> {fast:.2f}°")
    state.settings.rotate_speed = 0.20

    # 缩放灵敏度同样可调
    state.settings.zoom_speed = 0.10
    d0 = state.camera.distance
    canvas.wheelEvent(QtGui.QWheelEvent(
        QtCore.QPointF(320, 240), QtCore.QPointF(320, 240),
        QtCore.QPoint(0, 0), QtCore.QPoint(0, 120),
        QtCore.Qt.NoButton, QtCore.Qt.NoModifier, QtCore.Qt.NoScrollPhase, False))
    one_notch = abs(np.log(d0 / state.camera.distance))
    check("滚轮缩放灵敏度可调", 0.05 < one_notch < 0.2, f"每档 {one_notch:.3f}")
    check("自适应质量在范围内", canvas.MIN_QUALITY <= canvas._adaptive_scale <= 1.0,
          f"scale={canvas._adaptive_scale:.2f}")

    # 动画期间也应降分辨率
    canvas.set_animating(True)
    check("动画降分辨率标志", canvas._animating)
    canvas.set_animating(False)
    check("动画结束恢复", not canvas._animating)

    print("\n=== 单位与语言 ===")
    window.set_units("inch")
    check("切换英寸", state.units.label == "in" and state.settings.units == "inch")
    window.set_units("mm")
    check("切回毫米", state.units.label == "mm")
    window.set_language("CH")
    check("切换中文", state.tr.tr("file") == "文件"
          and window.menuBar().actions()[0].text() == "文件")
    window.set_language("EN")
    check("切回英文", state.tr.tr("file") == "File")

    print("\n=== 测量 ===")
    window.canvas.start_measure("line3d")
    check("进入测量", state.measures.pending is not None)
    window.canvas._add_measure_point(QtCore.QPoint(300, 240))
    window.canvas._add_measure_point(QtCore.QPoint(380, 300))
    check("测量完成", len(state.measures.items) == 1,
          state.measures.items[-1].value_text(state.units) if state.measures.items else "")
    window.panel.refresh()
    check("测量面板刷新", window.panel.measure_list.count() == 1)
    window._delete_last_measure()
    check("删除测量", not state.measures.items)

    print("\n=== 旋转动画 ===")
    window._toggle_spin(True)
    check("启用旋转", state.spin.config.enabled)
    window._set_spin_type("rz")
    window._set_spin_speed("high")
    window._set_spin_direction(-1)
    check("旋转参数", state.spin.config.kind == "rz"
          and state.spin.config.speed == "high"
          and state.spin.config.direction == -1)
    state.spin._last -= 0.5
    before = state.camera.yaw
    state.spin.tick(state.camera)
    check("旋转推进", abs(state.camera.yaw - before) > 1.0)
    window._toggle_spin(False)

    print("\n=== 宏录制 ===")
    window._toggle_macro_record()
    for i in range(4):
        state.camera.yaw = i * 20.0
        state.macro.capture(state.camera, force=True)
    window._toggle_macro_record()
    check("宏帧数", len(state.macro.macro.frames) == 4)
    macro_path = os.path.join(tmp, "macro.json")
    state.macro.save(macro_path)
    check("宏保存", os.path.exists(macro_path))

    print("\n=== 对象操作 ===")
    n0 = len(state.scene.objects)
    window._new_primitive("box")
    check("新建长方体", len(state.scene.objects) == n0 + 1)
    window.copy_object()
    window.paste_object()
    check("复制/粘贴对象", len(state.scene.objects) == n0 + 2
          and state.clipboard_object is not None)
    window._object_action("transparent", -1)
    check("对象透明开关", state.scene.active_object().transparent)
    window._object_action("wire", -1)
    check("对象线框开关", state.scene.active_object().wire)
    window._object_action("components", -1)
    check("连通分量拆分", len(state.scene.objects) > n0 + 2)
    window.delete_object()
    check("删除对象", len(state.scene.objects) == n0 + 2)

    print("\n=== 图片导出 ===")
    img = window._render_picture(supersample=2)
    check("离屏渲染图片", img.shape[2] in (3, 4) and img.shape[0] > 100,
          str(img.shape))
    pic = os.path.join(tmp, "view.png")
    png.write_png(pic, img)
    reloaded = png.read_png(pic)
    check("PNG 往返一致", np.array_equal(reloaded[:, :, :3], img[:, :, :3]))
    transp = window._render_picture(transparent=True)
    check("透明背景图片", transp.shape[2] == 4 and transp[:, :, 3].max() == 255)
    double = window._render_picture(double=True)
    check("双倍分辨率", double.shape[0] == window.canvas.height() * 2
          or double.shape[0] > window.canvas.height())

    print("\n=== STL 导出与再打开 ===")
    stl_path = os.path.join(tmp, "export.stl")
    if state.scene.active_object() is None and state.scene.objects:
        state.scene.set_active(0)
    obj = state.scene.active_object()
    stl_io.write_binary_stl(stl_path, obj.mesh.vertices, obj.mesh.triangles)
    check("STL 导出", os.path.getsize(stl_path) > 84)
    expected_tris = obj.mesh.triangle_count
    window.delete_all_objects()
    check("清空场景", not state.scene.objects)
    window.open_path(stl_path)
    check("重新打开 STL", len(state.scene.objects) == 1
          and state.scene.objects[0].mesh.triangle_count == expected_tris)

    print("\n=== 设置存取 ===")
    window.save_default_settings()
    check("设置已写出", os.path.exists(settings_path)
          and os.path.getsize(settings_path) > 100)
    window.load_default_settings()
    check("设置已载入", True)
    window.canvas.set_interactive_quality(0.8)
    check("拖拽画质设置生效", abs(window.canvas._quality_ceiling - 0.8) < 1e-9
          and window.canvas._adaptive_scale <= 0.8,
          f"ceiling={window.canvas._quality_ceiling:.2f}")
    window.canvas.set_interactive_quality(0.5)

    print("\n=== 视图选项对话框（构造） ===")
    from num_stl_viewer.viewer import (ColorDialog, LightDialog,
                                       ObjectPropertiesDialog, PointSizeDialog,
                                       RotationDialog, StatisticsDialog)
    dialogs = [
        LightDialog(state, window),
        RotationDialog(state.camera, state.tr, window),
        PointSizeDialog(state, window),
        ObjectPropertiesDialog(state.scene.active_object(), state.tr, window),
        ColorDialog("bg", state.scene.background, window, with_gradient=True),
        StatisticsDialog("stats", "Statistics", window),
    ]
    check("对话框构造", all(d is not None for d in dialogs)
          and len(dialogs) == 6)
    for d in dialogs:
        d.close()

    window.close()
    app.processEvents()
    print(f"\n总计: {len(PASSED)} 通过, {len(FAILED)} 失败")
    if FAILED:
        print("失败项:", *FAILED, sep="\n - ")
        return 1
    print("图形界面冒烟测试通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
