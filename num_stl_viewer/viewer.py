"""PyQt5 图形界面 —— 复现 NUM_STLViewer.exe 的窗口、菜单与交互。

界面结构（与原程序一致）：

    ┌ 菜单栏 File | View | Mesh | Light | Measurement | Extras | Help
    ├ 工具条：Open / Fit / 显示模式 / 测量 / 旋转动画 / 截图
    ├ 左侧停靠面板：Objects（对象列表 + 显示属性）与 Measurements
    └ 状态栏：单位、对象/面数、渲染耗时、帧率

交互：
    左键拖拽    旋转（按住 Ctrl 为绕 Z 轴旋转；开启 Rotate around Z 后左键即绕 Z）
    中键拖拽    平移（Shift + 左键 亦可）
    右键拖拽    平移；右键单击弹出上下文菜单
    Alt+左键    缩放（滚轮 / 触控板亦可）
    左键单击    测量模式下取点（自动吸附最近顶点）
    方向键      旋转   Shift+方向键 平移   Ctrl+方向键 绕 Z / 俯仰
    +/-         缩放
    F / Home    适应窗口      R  复位中心      1~8 标准视图    空格 播放/暂停旋转
"""

from __future__ import annotations

import copy
import json
import math
import os
import sys
import time
from collections import deque

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets

from . import stl_io
from .camera import STANDARD_VIEWS, Camera
from .i18n import Translator
from .lighting import LIGHT_PRESETS, LightSettings
from .macro import MacroRecorder, SpinConfig, SpinEngine
from .measure import MEASURE_DEFS, MeasureManager, snap_pick
from .mesh import Mesh
from .renderer import RenderOptions, SoftwareRenderer
from .scene import Object3D, Scene
from .settings import AppSettings, UnitSystem, ViewOptions

PICTURE_FILTER = "PNG 图片 (*.png);;JPEG 图片 (*.jpg);;BMP 图片 (*.bmp);;所有文件 (*)"
MODEL_FILTER = ("模型文件 (*.stl *.stpl *.ply *.stpack *.exe);;"
                "STL (*.stl);;PLY (*.ply);;自包含查看器 (*.exe *.stpack);;"
                "所有文件 (*)")

# 用颜色表达信息的着色模式（"彩色"开关即以是否落在这些模式内为准）
COLORED_MODES = ("auto", "component", "curvature", "orientation")
COLOR_MODE_CHOICES = ("auto", "uniform", "component", "curvature", "orientation")


def is_colored_mode(mode: str) -> bool:
    return mode in COLORED_MODES


# ==========================================================================
# 运行时状态
# ==========================================================================
class ViewerState:
    """把场景、相机、渲染器、设置等聚合成一个对象，供窗口与画布共享。"""

    def __init__(self, settings_path: str | None = None):
        self.settings_path = settings_path
        self.settings = AppSettings().load(settings_path)
        self.tr = Translator(self.settings.language)
        self.units = UnitSystem(self.settings.units)

        self.scene = Scene()
        self.camera = Camera(1024, 768)
        self.light = LightSettings()
        self.renderer = SoftwareRenderer()
        self.measures = MeasureManager()
        self.spin = SpinEngine(SpinConfig())
        self.macro = MacroRecorder()
        self.options = RenderOptions.from_view_options(self.settings.view)

        self.current_path: str | None = None
        self.clipboard_object = None
        self.rotate_around_z = False
        self.last_render_seconds = 0.0
        self.fps = 0.0
        self._frame_times: deque = deque(maxlen=240)

        self.settings.apply_to(self.camera, self.scene, self.light)
        self.options = self.render_options()

    # ---------------- 选项 ----------------
    def render_options(self, interactive: bool = False) -> RenderOptions:
        """把 ViewOptions + 运行期开关合成为渲染参数。"""
        opts = RenderOptions.from_view_options(self.settings.view)
        opts.transparent = bool(self.settings.view.transparent)
        opts.point_size = int(self.settings.view.point_size)
        opts.line_width = int(self.settings.view.line_width)
        opts.show_edges = bool(self.settings.view.show_edges)
        opts.gouraud = bool(self.settings.interpolate_normals)
        if interactive:
            opts.supersample = 1
        elif self.settings.antialias:
            opts.supersample = 2
        return opts

    def sync_view_options(self) -> None:
        """把运行期状态写回 ViewOptions（保存设置时调用）。"""
        v = self.settings.view
        v.wire = self.options.wire
        v.solid = self.options.solid
        v.color_mode = self.options.color_mode
        v.transparent = self.options.transparent
        v.show_axis = self.options.show_axis
        v.axis_labels = self.options.axis_labels
        v.show_grid = self.options.show_grid
        v.crosshair = self.options.crosshair
        v.point_size = self.options.point_size
        v.line_width = self.options.line_width
        v.show_edges = self.options.show_edges
        v.background = self.options.background
        v.background_top = self.options.background_top
        v.gradient_background = self.options.gradient_background
        v.orthographic = self.camera.orthographic
        self.settings.units = self.units.unit
        self.settings.language = self.tr.language

    def save_settings(self) -> str:
        self.sync_view_options()
        self.settings.capture_from(self.camera, self.scene, self.light)
        return self.settings.save(self.settings_path)

    def load_settings(self) -> str:
        self.settings.load(self.settings_path)
        self.tr.set_language(self.settings.language)
        self.units.unit = self.settings.units
        self.settings.apply_to(self.camera, self.scene, self.light)
        self.options = self.render_options()
        self.measures.set_color(self.settings.view.mesh_color)
        return self.settings_path or self.settings.default_path()

    # ---------------- 场景操作 ----------------
    def load_file(self, path: str) -> Object3D:
        mesh = Mesh.load(path, weld=bool(self.settings.auto_weld))
        name = os.path.splitext(os.path.basename(path))[0]
        obj = self.scene.add_mesh(mesh, name=name, source_path=path)
        self.current_path = path
        self.settings.add_recent(path)
        self.measures.clear()
        return obj

    def fit_view(self) -> None:
        if not self.scene.objects:
            return
        lo, hi = self.scene.visible_bounds()
        self.camera.fit(lo, hi)

    def statistics(self) -> dict:
        return self.scene.statistics()


# ==========================================================================
# 渲染画布
# ==========================================================================
class Canvas(QtWidgets.QWidget):
    """3D 视口：软件光栅化渲染 + 鼠标交互。"""

    statusMessage = QtCore.pyqtSignal(str)
    sceneChanged = QtCore.pyqtSignal()

    # 交互渲染目标帧率：约 40fps。低于它降分辨率，高于它提清晰度。
    TARGET_FRAME_SECONDS = 1.0 / 40.0
    MIN_QUALITY = 0.15             # 交互分辨率下限
    MIN_RENDER_EDGE = 96           # 交互位图最小边长（再小就看不出形状了）
    REFINE_DELAY_MS = 160          # 停止操作多久后开始精修全质量帧
    KEY_ROTATE_STEP = 5.0          # 方向键每次旋转的角度（度）

    def __init__(self, state: ViewerState, parent=None):
        super().__init__(parent)
        self.state = state
        self.setMinimumSize(320, 240)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setAutoFillBackground(False)

        self._image: QtGui.QImage | None = None
        self._buffer: np.ndarray | None = None
        self._dirty = True
        self._interactive = False
        self._animating = False
        self._drag_button = None
        self._drag_mode = None
        self._press_pos = QtCore.QPoint()
        self._last_pos = QtCore.QPoint()
        self._hover_measure = None

        # 拖拽时按实测耗时自适应分辨率，保证大模型也能流畅旋转
        self._quality_ceiling = float(
            np.clip(state.settings.interactive_quality, 0.15, 1.0))
        self._adaptive_scale = self._quality_ceiling
        # 渐进式渲染：先立刻出一张低分辨率快帧，空闲后再精修成整幅质量。
        # 静止帧 ss=2 要几百毫秒，若同步画完会阻塞事件循环 —— 拖拽期间鼠标
        # 事件只能排队，松手后模型「跳」一大截，观感就是又卡又不好控制。
        self._quick = True
        self._refine_timer = QtCore.QTimer(self)
        self._refine_timer.setSingleShot(True)
        self._refine_timer.timeout.connect(self._refine)
        self.setCursor(QtCore.Qt.OpenHandCursor)

    # ---------------- 渲染 ----------------
    def mark_dirty(self) -> None:
        self._dirty = True
        self.update()
        if not (self._interactive or self._animating):
            self._quick = True
            self._refine_timer.start(self.REFINE_DELAY_MS)

    def _refine(self) -> None:
        """空闲一段时间后把快帧替换成全质量帧。"""
        if self._interactive or self._animating or not self._quick:
            return
        self._quick = False
        self._dirty = True
        self.update()

    def resizeEvent(self, event) -> None:
        self.state.camera.set_viewport(self.width(), self.height())
        self.mark_dirty()
        super().resizeEvent(event)

    def paintEvent(self, event) -> None:
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), QtCore.Qt.black)
        if self._dirty or self._image is None:
            self._render()
        if self._image is not None:
            # 交互降分辨率后放大绘制，平滑缩放可显著减轻锯齿/块状感
            painter.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)
            painter.drawImage(self.rect(), self._image)
        self._draw_overlay(painter)

    def _scaled_size(self, scale: float) -> tuple:
        """按窗口比例缩放渲染尺寸。

        位图会被直接拉伸铺满整个视口，因此 w/h 必须与窗口比例严格一致。
        早先 w、h 各自 ``int(W*scale)`` 再各自 ``max(..., 32)``，比例会漂移
        （窄窗口下实测偏差可达 40%），放大后就表现为模型被拉扁或拉长。
        """
        W = max(self.width(), 1)
        H = max(self.height(), 1)
        scale = float(np.clip(scale, 0.05, 1.0))
        h = max(int(round(H * scale)), 1)
        h = min(max(h, self.MIN_RENDER_EDGE), H)
        w = max(int(round(W * h / H)), 1)
        return min(w, W), h

    def _render(self) -> None:
        state = self.state
        reduced = self._interactive or self._animating or self._quick
        scale = 1.0
        if reduced:
            scale = float(np.clip(self._adaptive_scale, 0.15, 1.0))
        w, h = self._scaled_size(scale)
        # 供 pick / ray 使用的视口始终按窗口尺寸；渲染内部会临时切换到 (w,h)
        state.camera.set_viewport(self.width(), self.height())
        opts = state.render_options(interactive=reduced)
        start = time.perf_counter()
        image = state.renderer.render(
            state.scene, state.camera, size=(w, h), options=opts,
            light=state.light, measurements=state.measures.items)
        elapsed = time.perf_counter() - start
        if self._interactive or self._animating:
            self._adapt_quality(scale, elapsed)
        self._buffer = np.ascontiguousarray(image)
        hh, ww, ch = self._buffer.shape
        fmt = QtGui.QImage.Format_RGBA8888 if ch == 4 else QtGui.QImage.Format_RGB888
        self._image = QtGui.QImage(self._buffer.data, ww, hh,
                                   self._buffer.strides[0], fmt)
        self._dirty = False
        state.last_render_seconds = state.renderer.last_stats.get("seconds", 0.0)
        now = time.perf_counter()
        state._frame_times.append(now)
        while len(state._frame_times) > 1 and now - state._frame_times[0] > 1.0:
            state._frame_times.popleft()
        span = state._frame_times[-1] - state._frame_times[0]
        state.fps = (len(state._frame_times) - 1) / span if span > 0 else 0.0

    def _adapt_quality(self, scale: float, seconds: float) -> None:
        """按上一帧耗时把交互分辨率往目标帧率附近调。

        上下阈值留出 1.35x / 0.6x 的迟滞带，避免在临界点附近来回抖动，
        导致画面忽清忽糊（这本身也会让人觉得"不稳定"）。
        """
        target = self.TARGET_FRAME_SECONDS
        if seconds > target * 1.35:
            self._adaptive_scale = max(self.MIN_QUALITY, scale * 0.75)
        elif seconds < target * 0.6:
            self._adaptive_scale = min(self._quality_ceiling, scale * 1.15)

    def set_interactive_quality(self, value: float) -> None:
        """由设置对话框调整交互分辨率上限。"""
        self._quality_ceiling = float(np.clip(value, 0.2, 1.0))
        self._adaptive_scale = min(self._adaptive_scale, self._quality_ceiling)

    def set_animating(self, value: bool) -> None:
        """旋转动画 / 宏播放期间同样降分辨率渲染，保证流畅。"""
        value = bool(value)
        if value == self._animating:
            return
        self._animating = value
        self.mark_dirty()               # 开/关都重绘（关闭时恢复全质量）

    def _draw_overlay(self, painter: QtGui.QPainter) -> None:
        """在视口上叠加测量提示与旋转状态（文字由 Qt 绘制）。"""
        state = self.state
        tr = state.tr
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        pending = state.measures.pending
        if pending is not None:
            need = pending.needed
            have = len(pending.points)
            text = tr.tr("measure_hint", have + 1, need)
            font = painter.font()
            font.setPointSize(max(font.pointSize(), 10))
            font.setBold(True)
            painter.setFont(font)
            metrics = QtGui.QFontMetrics(font)
            rect = metrics.boundingRect(text).adjusted(-10, -6, 10, 6)
            rect.moveTopLeft(QtCore.QPoint(12, 12))
            painter.fillRect(rect, QtGui.QColor(0, 0, 0, 150))
            painter.setPen(QtGui.QColor(255, 220, 90))
            painter.drawText(rect, QtCore.Qt.AlignCenter, text)
        if state.spin.config.enabled:
            painter.setPen(QtGui.QColor(120, 220, 160))
            font = painter.font()
            font.setPointSize(max(font.pointSize(), 9))
            painter.setFont(font)
            painter.drawText(12, self.height() - 12,
                             f"\u25b6 spin: {state.spin.config.kind} "
                             f"({state.spin.config.speed})")

    # ---------------- 鼠标 ----------------
    def _update_cursor(self) -> None:
        mode = self._drag_mode
        if mode in ("rotate", "rotate_z"):
            self.setCursor(QtCore.Qt.ClosedHandCursor)
        elif mode in ("pan", "zoom"):
            self.setCursor(QtCore.Qt.SizeAllCursor)
        elif mode == "measure" or self.state.measures.pending is not None:
            self.setCursor(QtCore.Qt.CrossCursor)
        else:
            self.setCursor(QtCore.Qt.OpenHandCursor)

    def mousePressEvent(self, event) -> None:
        self.setFocus()
        self._press_pos = event.pos()
        self._last_pos = event.pos()
        self._drag_button = event.button()
        state = self.state
        mods = event.modifiers()
        if event.button() == QtCore.Qt.LeftButton:
            if mods & QtCore.Qt.ShiftModifier:
                self._drag_mode = "pan"
            elif mods & QtCore.Qt.AltModifier:
                self._drag_mode = "zoom"
            elif (mods & QtCore.Qt.ControlModifier) or state.rotate_around_z:
                self._drag_mode = "rotate_z"
            elif state.measures.pending is not None:
                self._drag_mode = "measure"
            else:
                self._drag_mode = "rotate"
        elif event.button() == QtCore.Qt.MiddleButton:
            self._drag_mode = "pan"
        elif event.button() == QtCore.Qt.RightButton:
            self._drag_mode = "pan"          # 右键拖动平移；单击仍弹上下文菜单
        else:
            self._drag_mode = None
        self._interactive = self._drag_mode is not None
        if self._interactive:
            # 拖拽期间固定用快帧，别让精修计时器插进来抢时间
            self._quick = True
            self._refine_timer.stop()
        self._update_cursor()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_button is None:
            return
        pos = event.pos()
        dx = pos.x() - self._last_pos.x()
        dy = pos.y() - self._last_pos.y()
        self._last_pos = pos
        total = (pos - self._press_pos).manhattanLength()
        state = self.state
        settings = state.settings
        mode = self._drag_mode
        if mode == "measure" and total > 4:
            mode = self._drag_mode = "rotate"   # 测量模式下拖动即转为旋转
            self._update_cursor()
        if mode == "rotate":
            if settings.view.fixed_on_screen:
                state.camera.rotate_around_z(dx, settings.rotate_speed)
            else:
                state.camera.orbit(dx, -dy, settings.rotate_speed)
        elif mode == "rotate_z":
            state.camera.rotate_around_z(dx, settings.rotate_speed)
        elif mode == "pan":
            # 平移与"固定于屏幕"无关，始终可用
            state.camera.pan(dx, dy, settings.pan_speed)
        elif mode == "zoom":
            # 右键（Alt+左键）上下拖动缩放
            state.camera.dolly(-dy * 0.12, settings.zoom_speed)
        else:
            return
        self.mark_dirty()
        self.statusMessage.emit(self._status_text())

    def mouseReleaseEvent(self, event) -> None:
        state = self.state
        moved = (event.pos() - self._press_pos).manhattanLength()
        if event.button() == QtCore.Qt.LeftButton and moved <= 4:
            if state.measures.pending is not None:
                self._add_measure_point(event.pos())
            else:
                self._pick_info(event.pos())
        elif event.button() == QtCore.Qt.RightButton and moved <= 4:
            self._context_menu(event.globalPos())
        self._drag_button = None
        self._drag_mode = None
        self._interactive = False
        self._update_cursor()
        self.mark_dirty()        # 先出快帧，随后自动精修
        self.statusMessage.emit(self._status_text())

    def wheelEvent(self, event) -> None:
        # 支持鼠标滚轮（angleDelta）与触控板（pixelDelta）两种滚动源
        pd = event.pixelDelta()
        if pd is not None and pd.y() != 0:
            steps = pd.y() / 60.0
        else:
            steps = event.angleDelta().y() / 120.0
        if steps:
            self.state.camera.dolly(steps, self.state.settings.zoom_speed)
            self.mark_dirty()
            self.statusMessage.emit(self._status_text())
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == QtCore.Qt.LeftButton:
            self.state.fit_view()
            self.mark_dirty()

    def keyPressEvent(self, event) -> None:
        key = event.key()
        mods = event.modifiers()
        cam = self.state.camera
        handled = True
        if key == QtCore.Qt.Key_F or key == QtCore.Qt.Key_Home:
            self.state.fit_view()
        elif key == QtCore.Qt.Key_R:
            cam.reset_center()
        elif key == QtCore.Qt.Key_Space:
            cfg = self.state.spin.config
            cfg.enabled = not cfg.enabled
            self.state.spin.reset()
        elif QtCore.Qt.Key_1 <= key <= QtCore.Qt.Key_8:
            names = ["iso", "front", "back", "left", "right", "top", "bottom",
                     "iso_back"]
            cam.set_standard_view(names[key - QtCore.Qt.Key_1])
        elif key == QtCore.Qt.Key_Escape:
            self.state.measures.cancel()
        elif key in (QtCore.Qt.Key_Plus, QtCore.Qt.Key_Equal):
            cam.dolly(1.0, self.state.settings.zoom_speed)
        elif key in (QtCore.Qt.Key_Minus, QtCore.Qt.Key_Underscore):
            cam.dolly(-1.0, self.state.settings.zoom_speed)
        elif key in (QtCore.Qt.Key_Left, QtCore.Qt.Key_Right,
                     QtCore.Qt.Key_Up, QtCore.Qt.Key_Down):
            # 键盘用固定角度步进（与鼠标灵敏度设置无关），按一下走一格，可预期
            step = self.KEY_ROTATE_STEP
            move = 18.0
            left = key == QtCore.Qt.Key_Left
            right = key == QtCore.Qt.Key_Right
            up = key == QtCore.Qt.Key_Up
            down = key == QtCore.Qt.Key_Down
            if mods & QtCore.Qt.ShiftModifier:      # 平移
                cam.pan(move if right else (-move if left else 0.0),
                        -move if up else (move if down else 0.0),
                        self.state.settings.pan_speed)
            elif mods & QtCore.Qt.ControlModifier:  # 绕 Z / 俯仰
                if left:
                    cam.set_rotation_angles(yaw=cam.yaw - step)
                elif right:
                    cam.set_rotation_angles(yaw=cam.yaw + step)
                elif up:
                    cam.set_rotation_angles(pitch=cam.pitch + step)
                else:
                    cam.set_rotation_angles(pitch=cam.pitch - step)
            else:                                    # 轨道旋转
                if left:
                    cam.set_rotation_angles(yaw=cam.yaw - step)
                elif right:
                    cam.set_rotation_angles(yaw=cam.yaw + step)
                elif up:
                    cam.set_rotation_angles(pitch=cam.pitch + step)
                else:
                    cam.set_rotation_angles(pitch=cam.pitch - step)
        else:
            handled = False
        if not handled:
            super().keyPressEvent(event)
            return
        self.mark_dirty()
        self.statusMessage.emit(self._status_text())

    # ---------------- 拾取 / 测量 ----------------
    def _pick_info(self, pos: QtCore.QPoint) -> None:
        state = self.state
        hit = state.renderer.pick(state.scene, state.camera, pos.x(), pos.y())
        if hit is None:
            self.statusMessage.emit(state.tr.tr("ready"))
            return
        point = hit["point"]
        self.statusMessage.emit(
            f"{hit['object'].name}  #{hit['triangle']}  "
            f"{state.units.format_point(point)} {state.units.label}")

    def _add_measure_point(self, pos: QtCore.QPoint) -> None:
        state = self.state
        pending = state.measures.pending
        if pending is None:
            return
        plane = state.camera.forward()
        result = snap_pick(state.scene, state.camera, state.renderer,
                           pos.x(), pos.y())
        if result is None:
            point = state.camera.ray_to_plane(pos.x(), pos.y())
            snapped = False
        else:
            point = result["point"]
            snapped = result["snapped"]
        done = state.measures.add_point(point, units=state.units, plane=plane)
        if done is not None:
            self.statusMessage.emit(
                f"{state.tr.tr('measure_result')}: {done.value_text(state.units)}")
            self.sceneChanged.emit()
        else:
            have = len(pending.points)
            self.statusMessage.emit(
                state.tr.tr("measure_hint", have + 1, pending.needed))
        self.mark_dirty()

    def _context_menu(self, global_pos) -> None:
        menu = QtWidgets.QMenu(self)
        state = self.state
        tr = state.tr
        menu.addAction(tr.tr("zoom_fit"), self._fit)
        menu.addAction(tr.tr("reset_center"), self._reset_center)
        menu.addSeparator()
        for name in ("iso", "front", "top", "right"):
            menu.addAction(tr.tr("view_" + name),
                           lambda n=name: self._standard_view(n))
        menu.addSeparator()
        wire = menu.addAction(tr.tr("wire"), self._toggle_wire)
        wire.setCheckable(True)
        wire.setChecked(state.options.wire)
        solid = menu.addAction(tr.tr("solid"), self._toggle_solid)
        solid.setCheckable(True)
        solid.setChecked(state.options.solid)
        colored = menu.addAction(tr.tr("colored"), self._toggle_colored)
        colored.setCheckable(True)
        colored.setChecked(is_colored_mode(state.options.color_mode))
        menu.addSeparator()
        pending = state.measures.pending
        if pending is not None:
            menu.addAction(f"{tr.tr('measure_result')}: "
                           f"{pending.kind} ({len(pending.points)}/{pending.needed})",
                           lambda: None).setEnabled(False)
            menu.addAction(tr.tr("cancel"), self._cancel_measure)
        else:
            for kind in MEASURE_DEFS:
                menu.addAction(tr.tr(MEASURE_DEFS[kind][1]),
                               lambda k=kind: self.start_measure(k))
        menu.exec_(global_pos)

    def _fit(self) -> None:
        self.state.fit_view()
        self.mark_dirty()

    def _reset_center(self) -> None:
        self.state.camera.reset_center()
        self.mark_dirty()

    def _standard_view(self, name: str) -> None:
        self.state.camera.set_standard_view(name)
        self.mark_dirty()

    def _toggle_wire(self) -> None:
        self.state.options.wire = not self.state.options.wire
        self.state.options.solid = not self.state.options.wire
        self.mark_dirty()
        self.sceneChanged.emit()

    def _toggle_solid(self) -> None:
        self.state.options.solid = not self.state.options.solid
        if not self.state.options.solid:
            self.state.options.wire = True
        self.mark_dirty()
        self.sceneChanged.emit()

    def _toggle_colored(self) -> None:
        opts = self.state.options
        opts.color_mode = "uniform" if is_colored_mode(opts.color_mode) else "auto"
        for obj in self.state.scene.objects:
            obj.color_mode = opts.color_mode
            obj.auto_color = opts.color_mode != "uniform"
            obj.invalidate_colors()
        self.mark_dirty()
        self.sceneChanged.emit()

    def start_measure(self, kind: str) -> None:
        self.state.measures.start(kind)
        self.statusMessage.emit(
            self.state.tr.tr("measure_hint", 1, MEASURE_DEFS[kind][0]))
        self.mark_dirty()

    def _cancel_measure(self) -> None:
        self.state.measures.cancel()
        self.mark_dirty()

    # ---------------- 拖放 ----------------
    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path and os.path.isfile(path):
                window = self.window()
                if hasattr(window, "open_path"):
                    window.open_path(path)
        event.acceptProposedAction()

    # ---------------- 状态文本 ----------------
    def _status_text(self) -> str:
        state = self.state
        stats = state.statistics()
        mode = ""
        if self._drag_mode and self._drag_mode != "measure":
            mode = {"rotate": "旋转", "rotate_z": "绕Z旋转",
                    "pan": "平移", "zoom": "缩放"}.get(self._drag_mode, "")
            mode = f"{mode} | " if mode else ""
        return (f"{mode}{stats['objects']} obj | {stats['triangles']} tri | "
                f"{state.last_render_seconds * 1000:.0f} ms | "
                f"{state.fps:.1f} FPS | {state.units.label}")


# ==========================================================================
# 侧边面板
# ==========================================================================
class ObjectPanel(QtWidgets.QWidget):
    """对象列表（对应原程序的对象面板与对象右键菜单）。"""

    objectSelected = QtCore.pyqtSignal(int)
    objectToggleVisible = QtCore.pyqtSignal(int, bool)
    requestAction = QtCore.pyqtSignal(str, int)      # action, index
    measureSelected = QtCore.pyqtSignal(int)

    def __init__(self, state: ViewerState, parent=None):
        super().__init__(parent)
        self.state = state
        self._updating = False

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        self.title = QtWidgets.QLabel()
        self.title.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.title)

        self.list = QtWidgets.QListWidget()
        self.list.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.list.itemSelectionChanged.connect(self._on_selection)
        self.list.itemChanged.connect(self._on_item_changed)
        self.list.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)
        layout.addWidget(self.list, 3)

        self.measure_title = QtWidgets.QLabel()
        self.measure_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.measure_title)

        self.measure_list = QtWidgets.QListWidget()
        self.measure_list.setSelectionMode(
            QtWidgets.QAbstractItemView.SingleSelection)
        layout.addWidget(self.measure_list, 2)

        self.stats_label = QtWidgets.QLabel()
        self.stats_label.setWordWrap(True)
        self.stats_label.setStyleSheet("color: #888;")
        layout.addWidget(self.stats_label)

    # ---------------- 刷新 ----------------
    def refresh(self) -> None:
        tr = self.state.tr
        self.title.setText(tr.tr("objects"))
        self.measure_title.setText(tr.tr("measurement"))
        self._updating = True
        self.list.clear()
        for i, obj in enumerate(self.state.scene.objects):
            item = QtWidgets.QListWidgetItem(
                f"{obj.name}  [{obj.mesh.triangle_count}]")
            item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.Checked if obj.visible
                               else QtCore.Qt.Unchecked)
            if obj.transparent:
                item.setForeground(QtGui.QBrush(QtGui.QColor(150, 200, 255)))
            if obj.wire:
                item.setText(item.text() + "  (wire)")
            if obj.locked:
                item.setText(item.text() + "  [L]")
            self.list.addItem(item)
            if obj.active:
                self.list.setCurrentRow(i)
        self._updating = False

        self.measure_list.clear()
        for item in self.state.measures.items:
            text = f"{item.kind}: {item.value_text(self.state.units)}"
            self.measure_list.addItem(text)

        stats = self.state.statistics()
        self.stats_label.setText(
            f"{tr.tr('stat_objects')}: {stats['objects']} / {stats['visible']}\n"
            f"{tr.tr('stat_triangles')}: {stats['triangles']}\n"
            f"{tr.tr('stat_vertices')}: {stats['vertices']}")

    # ---------------- 事件 ----------------
    def _on_selection(self) -> None:
        if self._updating:
            return
        row = self.list.currentRow()
        if row >= 0:
            self.objectSelected.emit(row)

    def _on_item_changed(self, item) -> None:
        if self._updating:
            return
        row = self.list.row(item)
        self.objectToggleVisible.emit(row, item.checkState() == QtCore.Qt.Checked)

    def _context_menu(self, pos) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        tr = self.state.tr
        obj = self.state.scene.objects[row]
        menu = QtWidgets.QMenu(self)
        for key, action in (("visible", "visible"), ("wire", "wire"),
                            ("transparent", "transparent"),
                            ("always_on_top", "always_on_top"),
                            ("locked", "locked")):
            act = menu.addAction(tr.tr(key), lambda a=action: self.requestAction.emit(a, row))
            act.setCheckable(True)
            act.setChecked(bool(getattr(obj, action)))
        menu.addSeparator()
        menu.addAction(tr.tr("active"), lambda: self.requestAction.emit("active", row))
        menu.addSeparator()
        menu.addAction(tr.tr("object_properties"),
                       lambda: self.requestAction.emit("properties", row))
        menu.addAction(tr.tr("statistics"),
                       lambda: self.requestAction.emit("statistics", row))
        menu.addAction(tr.tr("copy_object"),
                       lambda: self.requestAction.emit("copy", row))
        menu.addAction(tr.tr("save_object") + " (STL)",
                       lambda: self.requestAction.emit("save_stl", row))
        menu.addSeparator()
        menu.addAction(tr.tr("repair_surface"),
                       lambda: self.requestAction.emit("repair", row))
        menu.addAction(tr.tr("generate_components"),
                       lambda: self.requestAction.emit("components", row))
        menu.addAction(tr.tr("remove_tri_intersected"),
                       lambda: self.requestAction.emit("remove_tri", row))
        menu.addSeparator()
        menu.addAction(tr.tr("delete_object"),
                       lambda: self.requestAction.emit("delete", row))
        menu.exec_(self.list.mapToGlobal(pos))


# ==========================================================================
# 对话框
# ==========================================================================
class LightDialog(QtWidgets.QDialog):
    """Light Settings：环境光/漫反射/高光、光源方向、颜色。"""

    def __init__(self, state: ViewerState, parent=None):
        super().__init__(parent)
        self.state = state
        tr = state.tr
        self.setWindowTitle(tr.tr("light_settings"))
        self.setMinimumWidth(420)
        self.light = state.light

        form = QtWidgets.QFormLayout(self)
        self.enabled = QtWidgets.QCheckBox(tr.tr("lights_enabled"))
        self.enabled.setChecked(self.light.enabled)
        form.addRow(self.enabled)

        self.ambient = self._slider(self.light.ambient)
        self.diffuse = self._slider(self.light.diffuse)
        self.specular = self._slider(self.light.specular)
        form.addRow(tr.tr("ambient"), self.ambient)
        form.addRow(tr.tr("diffuse"), self.diffuse)
        form.addRow(tr.tr("specular"), self.specular)

        self.shininess = QtWidgets.QSpinBox()
        self.shininess.setRange(1, 256)
        self.shininess.setValue(int(self.light.shininess))
        form.addRow(tr.tr("shininess"), self.shininess)

        self.primary = QtWidgets.QComboBox()
        self.secondary = QtWidgets.QComboBox()
        for name in LIGHT_PRESETS:
            self.primary.addItem(name, name)
            self.secondary.addItem(name, name)
        self.primary.setCurrentText(self.light.primary)
        self.secondary.setCurrentText(self.light.secondary)
        form.addRow(tr.tr("light_options") + " 1", self.primary)
        form.addRow(tr.tr("light_options") + " 2", self.secondary)

        self.light_color_btn = QtWidgets.QPushButton()
        self.shine_color_btn = QtWidgets.QPushButton()
        self._paint_button(self.light_color_btn, self.light.light_color)
        self._paint_button(self.shine_color_btn, self.light.shine_color)
        self.light_color_btn.clicked.connect(
            lambda: self._pick_color("light_color", self.light_color_btn))
        self.shine_color_btn.clicked.connect(
            lambda: self._pick_color("shine_color", self.shine_color_btn))
        form.addRow(tr.tr("light_color"), self.light_color_btn)
        form.addRow(tr.tr("shine_color"), self.shine_color_btn)

        self.two_sided = QtWidgets.QCheckBox(tr.tr("back_and_front"))
        self.two_sided.setChecked(self.light.two_sided)
        form.addRow(self.two_sided)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
            | QtWidgets.QDialogButtonBox.Reset)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QtWidgets.QDialogButtonBox.Reset).setText(tr.tr("reset_light_settings"))
        buttons.button(QtWidgets.QDialogButtonBox.Reset).clicked.connect(self._reset)
        form.addRow(buttons)

        for widget in (self.ambient, self.diffuse, self.specular):
            widget.valueChanged.connect(self._apply)
        self.shininess.valueChanged.connect(self._apply)
        self.primary.currentIndexChanged.connect(self._apply)
        self.secondary.currentIndexChanged.connect(self._apply)
        self.enabled.toggled.connect(self._apply)
        self.two_sided.toggled.connect(self._apply)

    def _slider(self, value: float) -> QtWidgets.QSlider:
        slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        slider.setRange(0, 100)
        slider.setValue(int(round(value * 100)))
        return slider

    def _paint_button(self, button, color) -> None:
        c = QtGui.QColor.fromRgbF(*color)
        button.setStyleSheet(f"background-color: {c.name()}; min-width: 80px;")
        button.setText(c.name())

    def _pick_color(self, attr: str, button) -> None:
        current = QtGui.QColor.fromRgbF(*getattr(self.light, attr))
        color = QtWidgets.QColorDialog.getColor(current, self)
        if color.isValid():
            setattr(self.light, attr, (color.redF(), color.greenF(), color.blueF()))
            self._paint_button(button, getattr(self.light, attr))
            self._apply()

    def _apply(self) -> None:
        self.light.enabled = self.enabled.isChecked()
        self.light.ambient = self.ambient.value() / 100.0
        self.light.diffuse = self.diffuse.value() / 100.0
        self.light.specular = self.specular.value() / 100.0
        self.light.shininess = float(self.shininess.value())
        self.light.set_primary(self.primary.currentData())
        self.light.set_secondary(self.secondary.currentData())
        self.light.two_sided = self.two_sided.isChecked()
        parent = self.parent()
        for name in ("_mark_dirty", "mark_dirty"):
            hook = getattr(parent, name, None)
            if callable(hook):
                hook()
                break

    def _reset(self) -> None:
        self.light.reset()
        self.ambient.setValue(int(self.light.ambient * 100))
        self.diffuse.setValue(int(self.light.diffuse * 100))
        self.specular.setValue(int(self.light.specular * 100))
        self.shininess.setValue(int(self.light.shininess))
        self.enabled.setChecked(self.light.enabled)
        self.primary.setCurrentText(self.light.primary)
        self.secondary.setCurrentText(self.light.secondary)
        self._apply()


class RotationDialog(QtWidgets.QDialog):
    """Rotation Angles：直接输入偏航/俯仰/滚转。"""

    def __init__(self, camera: Camera, tr: Translator, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr.tr("rotation_angles_title"))
        self.camera = camera
        form = QtWidgets.QFormLayout(self)
        self.yaw = QtWidgets.QDoubleSpinBox()
        self.pitch = QtWidgets.QDoubleSpinBox()
        self.roll = QtWidgets.QDoubleSpinBox()
        for box, value, lo, hi in ((self.yaw, camera.yaw, -360, 360),
                                   (self.pitch, camera.pitch, -89.9, 89.9),
                                   (self.roll, camera.roll, -180, 180)):
            box.setRange(lo, hi)
            box.setDecimals(2)
            box.setValue(value)
            box.setSingleStep(5.0)
        form.addRow(tr.tr("yaw"), self.yaw)
        form.addRow(tr.tr("pitch"), self.pitch)
        form.addRow(tr.tr("roll"), self.roll)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self) -> tuple:
        return self.yaw.value(), self.pitch.value(), self.roll.value()


class StatisticsDialog(QtWidgets.QDialog):
    """Statistics / Show STL File Size。"""

    def __init__(self, text: str, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(520, 460)
        layout = QtWidgets.QVBoxLayout(self)
        view = QtWidgets.QPlainTextEdit(text)
        view.setReadOnly(True)
        view.setFont(QtGui.QFont("Consolas", 10))
        layout.addWidget(view)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


class PointSizeDialog(QtWidgets.QDialog):
    """Mesh > Point Size / Line Width。"""

    def __init__(self, state: ViewerState, parent=None):
        super().__init__(parent)
        tr = state.tr
        self.setWindowTitle(f"{tr.tr('point_size')} / {tr.tr('line_width')}")
        form = QtWidgets.QFormLayout(self)
        self.point = QtWidgets.QSpinBox()
        self.point.setRange(1, 20)
        self.point.setValue(int(state.options.point_size))
        self.line = QtWidgets.QSpinBox()
        self.line.setRange(1, 20)
        self.line.setValue(int(state.options.line_width))
        form.addRow(tr.tr("point_size"), self.point)
        form.addRow(tr.tr("line_width"), self.line)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)


class ObjectPropertiesDialog(QtWidgets.QDialog):
    """Object Editor / 对象属性。"""

    def __init__(self, obj, tr: Translator, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr.tr("object_properties"))
        self.obj = obj
        form = QtWidgets.QFormLayout(self)
        self.name = QtWidgets.QLineEdit(obj.name)
        form.addRow(tr.tr("object"), self.name)

        self.color_btn = QtWidgets.QPushButton()
        self._paint(obj.color)
        self.color_btn.clicked.connect(self._pick_color)
        form.addRow(tr.tr("select_mesh_color"), self.color_btn)

        self.alpha = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.alpha.setRange(0, 100)
        self.alpha.setValue(int(obj.alpha * 100))
        form.addRow(tr.tr("transparency"), self.alpha)

        self.mode = QtWidgets.QComboBox()
        for key in COLOR_MODE_CHOICES:
            label = tr.tr("orientation_colors" if key == "orientation" else key)
            self.mode.addItem(label, key)
        idx = self.mode.findData(obj.color_mode)
        if idx >= 0:
            self.mode.setCurrentIndex(idx)
        form.addRow(tr.tr("colored"), self.mode)

        self.checks = {}
        for key in ("visible", "wire", "transparent", "always_on_top", "locked"):
            box = QtWidgets.QCheckBox(tr.tr(key))
            box.setChecked(bool(getattr(obj, key)))
            self.checks[key] = box
            form.addRow(box)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _paint(self, color) -> None:
        self._color = tuple(color)
        c = QtGui.QColor.fromRgbF(*self._color)
        self.color_btn.setStyleSheet(f"background-color: {c.name()};")
        self.color_btn.setText(c.name())

    def _pick_color(self) -> None:
        c = QtWidgets.QColorDialog.getColor(
            QtGui.QColor.fromRgbF(*self._color), self)
        if c.isValid():
            self._paint((c.redF(), c.greenF(), c.blueF()))

    def apply(self) -> None:
        obj = self.obj
        obj.name = self.name.text() or obj.name
        obj.color = self._color
        obj.alpha = self.alpha.value() / 100.0
        obj.color_mode = self.mode.currentData()
        obj.auto_color = obj.color_mode != "uniform"
        for key, box in self.checks.items():
            setattr(obj, key, box.isChecked())
        obj.invalidate_colors()


class ColorDialog(QtWidgets.QDialog):
    """Background Colors / Select Mesh Color / Measure Color。"""

    def __init__(self, title: str, color, parent=None, with_gradient: bool = False,
                 top_color=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        form = QtWidgets.QFormLayout(self)
        self.color = tuple(color)
        self.top = tuple(top_color or color)
        self.btn = QtWidgets.QPushButton()
        self._paint(self.btn, self.color)
        self.btn.clicked.connect(lambda: self._pick("color", self.btn))
        form.addRow(title, self.btn)
        self.gradient = None
        self.btn_top = None
        if with_gradient:
            self.gradient = QtWidgets.QCheckBox("Gradient Background")
            form.addRow(self.gradient)
            self.btn_top = QtWidgets.QPushButton()
            self._paint(self.btn_top, self.top)
            self.btn_top.clicked.connect(lambda: self._pick("top", self.btn_top))
            form.addRow("Top", self.btn_top)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _paint(self, button, color) -> None:
        c = QtGui.QColor.fromRgbF(*color)
        button.setStyleSheet(f"background-color: {c.name()};")
        button.setText(c.name())

    def _pick(self, which: str, button) -> None:
        current = QtGui.QColor.fromRgbF(*(self.color if which == "color" else self.top))
        c = QtWidgets.QColorDialog.getColor(current, self)
        if not c.isValid():
            return
        value = (c.redF(), c.greenF(), c.blueF())
        if which == "color":
            self.color = value
        else:
            self.top = value
        self._paint(button, value)


# ==========================================================================
# 主窗口
# ==========================================================================
class MainWindow(QtWidgets.QMainWindow):
    """NUM STL Viewer 主窗口。"""

    def __init__(self, state: ViewerState | None = None):
        super().__init__()
        self.state = state or ViewerState()
        self.setWindowTitle(self.state.tr.tr("app_title"))
        self.resize(1280, 860)
        self.setAcceptDrops(True)

        self.canvas = Canvas(self.state, self)
        self.setCentralWidget(self.canvas)
        self.canvas.statusMessage.connect(self._show_status)
        self.canvas.sceneChanged.connect(self._on_scene_changed)

        self.panel = ObjectPanel(self.state, self)
        dock = QtWidgets.QDockWidget(self.state.tr.tr("objects"), self)
        dock.setWidget(self.panel)
        dock.setAllowedAreas(QtCore.Qt.LeftDockWidgetArea |
                             QtCore.Qt.RightDockWidgetArea)
        self.addDockWidget(QtCore.Qt.LeftDockWidgetArea, dock)
        self.object_dock = dock
        self.panel.objectSelected.connect(self._select_object)
        self.panel.objectToggleVisible.connect(self._toggle_visible)
        self.panel.requestAction.connect(self._object_action)

        self._build_toolbar()
        self._build_menus()
        self._build_statusbar()
        self._create_timers()
        self.retranslate()
        self.panel.refresh()

        self._welcome_active = False
        if not self.state.scene.objects:
            self._load_welcome_scene()

    # ---------------- 启动场景 ----------------
    def _load_welcome_scene(self) -> None:
        """没有载入模型时，给出一个示例场景（与原程序的 New 对象一致）。"""
        self._welcome_active = True
        scene = self.state.scene
        scene.add_step_cylinder(radii=(9.0, 6.0), heights=(24.0, 36.0),
                                center=(0, 0, -30.0), name="blank")
        scene.add_box(size=(46, 46, 4), center=(0, 0, -32), name="fixture")
        for name in ("fixture",):
            obj = scene.find(name)
            if obj:
                obj.color = (0.55, 0.58, 0.62)
        self.state.camera.set_standard_view("iso")
        self.state.fit_view()
        self.panel.refresh()
        self._show_status(self.state.tr.tr("no_objects"))

    # ---------------- 工具条 ----------------
    def _build_toolbar(self) -> None:
        bar = QtWidgets.QToolBar("main")
        bar.setMovable(False)
        self.addToolBar(bar)
        self.toolbar = bar
        self._toolbar_actions = []

    # ---------------- 菜单 ----------------
    def _act(self, parent, key: str, slot, shortcut=None, checkable=False,
             checked=False, extra_key: str | None = None) -> QtWidgets.QAction:
        text = self.state.tr.tr(extra_key or key)
        action = QtWidgets.QAction(text, self)
        if shortcut:
            action.setShortcut(shortcut)
        action.setCheckable(checkable)
        action.setChecked(checked)
        if slot is not None:
            action.triggered.connect(slot)
        parent.addAction(action)
        self._toolbar_actions.append((action, extra_key or key))
        return action

    def _build_menus(self) -> None:
        state = self.state
        mbar = self.menuBar()
        self.menus = {}
        self.acts = {}

        # -------- File --------
        m = mbar.addMenu("")
        self.menus["file"] = m
        new_menu = m.addMenu("")
        self.menus["new"] = new_menu
        self.acts["new_box"] = self._act(new_menu, "new_box",
                                         lambda: self._new_primitive("box"))
        self.acts["new_cylinder"] = self._act(new_menu, "new_cylinder",
                                              lambda: self._new_primitive("cylinder"))
        self.acts["new_step_cylinder"] = self._act(
            new_menu, "new_step_cylinder", lambda: self._new_primitive("step_cylinder"))
        self.acts["new_tube"] = self._act(new_menu, "new_tube",
                                          lambda: self._new_primitive("tube"))
        self.acts["new_sphere"] = self._act(new_menu, "new_sphere",
                                            lambda: self._new_primitive("sphere"))
        m.addSeparator()
        self.acts["open"] = self._act(m, "open", self.open_dialog, "Ctrl+O")
        self.acts["recent"] = m.addMenu("")
        self.menus["recent"] = self.acts["recent"]
        self.acts["save_object"] = self._act(m, "save_object", self.save_active_stl,
                                             "Ctrl+S")
        self.acts["export_stl"] = self._act(m, "export_stl", self.save_active_stl)
        m.addSeparator()
        self.acts["save_picture"] = self._act(m, "save_picture",
                                              self.save_picture, "Ctrl+Shift+S")
        self.acts["save_multi_res_picture"] = self._act(
            m, "save_multi_res_picture", self.save_multi_res_picture)
        self.acts["save_picture_numroto"] = self._act(
            m, "save_picture_numroto", self.save_picture_numroto)
        self.acts["save_picture_numroto_transp"] = self._act(
            m, "save_picture_numroto_transp",
            lambda: self.save_picture_numroto(transparent=True))
        self.acts["copy_picture_clipboard"] = self._act(
            m, "copy_picture_clipboard", self.copy_picture_to_clipboard, "Ctrl+C")
        m.addSeparator()
        self.acts["copy_object"] = self._act(m, "copy_object", self.copy_object,
                                             "Ctrl+Shift+C")
        self.acts["paste_object"] = self._act(m, "paste_object", self.paste_object,
                                              "Ctrl+V")
        self.acts["delete_object"] = self._act(m, "delete_object", self.delete_object,
                                               "Delete")
        self.acts["delete_all_pictures"] = self._act(m, "delete_all_pictures",
                                                     self.delete_all_objects)
        m.addSeparator()
        units_menu = m.addMenu("")
        self.menus["units"] = units_menu
        self._units_group = QtWidgets.QActionGroup(self)
        for key, unit in (("unit_mm", "mm"), ("unit_inch", "inch")):
            act = QtWidgets.QAction(self.state.tr.tr(key), self)
            act.setCheckable(True)
            act.setChecked(self.state.settings.units == unit)
            act.triggered.connect(lambda checked, u=unit: self.set_units(u))
            self._units_group.addAction(act)
            units_menu.addAction(act)
            self.acts["unit_" + unit] = act
        self.acts["show_stl_filesize"] = self._act(
            m, "show_stl_filesize", self.show_file_size)
        self.acts["reload_object_settings"] = self._act(
            m, "reload_object_settings", self.reload_object_settings)
        self.acts["save_object_settings"] = self._act(
            m, "save_object_settings", self.save_object_settings)
        m.addSeparator()
        self.acts["close_window"] = self._act(m, "close_window", self.close, "Ctrl+Q")

        # -------- View --------
        m = mbar.addMenu("")
        self.menus["view"] = m
        self.acts["zoom_in"] = self._act(m, "zoom_in",
                                         lambda: self._zoom(0.8), "Ctrl++")
        self.acts["zoom_out"] = self._act(m, "zoom_out",
                                          lambda: self._zoom(1.25), "Ctrl+-")
        self.acts["zoom_fit"] = self._act(m, "zoom_fit", self._fit, "F")
        self.acts["rotate_around_z"] = self._act(
            m, "rotate_around_z", self._toggle_rotate_z, "Ctrl+Z", checkable=True)
        self.acts["rotation_angles"] = self._act(m, "rotation_angles",
                                                 self.show_rotation_dialog)
        m.addSeparator()
        views_menu = m.addMenu("")
        self.menus["standard_views"] = views_menu
        for name in ("iso", "top", "bottom", "front", "back", "left", "right"):
            self._act(views_menu, "view_" + name,
                      lambda checked=False, n=name: self._standard_view(n))
        m.addSeparator()
        self.acts["orthogonal"] = self._act(m, "orthogonal", self._toggle_ortho,
                                            "Ctrl+P", checkable=True,
                                            checked=self.state.camera.orthographic)
        self.acts["fixed_on_screen"] = self._act(m, "fixed_on_screen",
                                                 self._toggle_fixed, checkable=True)
        self.acts["full_screen"] = self._act(m, "full_screen", self._toggle_fullscreen,
                                             "F11", checkable=True)
        self.acts["crosshair"] = self._act(m, "crosshair", self._toggle_crosshair,
                                           checkable=True)
        self.acts["reset_center"] = self._act(m, "reset_center", self._reset_center,
                                              "R")
        self.acts["change_center"] = self._act(m, "change_center",
                                               self._change_center)
        self.acts["view_options"] = self._act(m, "view_options", self._view_options)

        # -------- Mesh --------
        m = mbar.addMenu("")
        self.menus["mesh"] = m
        self.acts["wire"] = self._act(m, "wire", self._set_wire, "W", checkable=True)
        self.acts["solid"] = self._act(m, "solid", self._set_solid, checkable=True,
                                       checked=True)
        self.acts["colored"] = self._act(m, "colored", self._set_colored, "C",
                                         checkable=True)
        self.acts["auto_colors"] = self._act(m, "auto_colors",
                                             lambda: self._set_color_mode("auto"))
        self.acts["uni_color"] = self._act(m, "uni_color",
                                           lambda: self._set_color_mode("uniform"))
        self.acts["component_colors"] = self._act(
            m, "component_colors", lambda: self._set_color_mode("component"))
        self.acts["curvature_colors"] = self._act(
            m, "curvature_colors", lambda: self._set_color_mode("curvature"))
        self.acts["orientation_colors"] = self._act(
            m, "orientation_colors", lambda: self._set_color_mode("orientation"))
        self.acts["transparent"] = self._act(m, "transparent", self._toggle_transparent,
                                             checkable=True)
        self.acts["back_and_front"] = self._act(m, "back_and_front",
                                                self._toggle_two_sided, checkable=True,
                                                checked=True)
        self.acts["show_edges"] = self._act(m, "show_edges", self._toggle_edges,
                                            checkable=True)
        m.addSeparator()
        self.acts["select_mesh_color"] = self._act(m, "select_mesh_color",
                                                   self._select_object_color)
        self.acts["point_size"] = self._act(m, "point_size", self._point_size_dialog)
        self.acts["object_editor"] = self._act(m, "object_editor",
                                               self._object_properties)

        # -------- Light --------
        m = mbar.addMenu("")
        self.menus["light"] = m
        self.acts["light_settings"] = self._act(m, "light_settings",
                                                self.show_light_dialog)
        self.acts["light_down"] = self._act(
            m, "light_down", lambda: self._set_light("down"))
        self.acts["light_down_left"] = self._act(
            m, "light_down_left", lambda: self._set_light("down_left"))
        self.acts["light_left"] = self._act(
            m, "light_left", lambda: self._set_light("left"))
        self.acts["light_up_left"] = self._act(
            m, "light_up_left", lambda: self._set_light("up_left"))
        self.acts["reset_light_settings"] = self._act(
            m, "reset_light_settings", self._reset_light)
        m.addSeparator()
        self.acts["background_colors"] = self._act(m, "background_colors",
                                                   self._background_colors)

        # -------- Measurement --------
        m = mbar.addMenu("")
        self.menus["measurement"] = m
        for kind in ("line2d", "line3d", "point3d", "circle2p", "circle3p",
                     "angle2d", "rzpoint"):
            key = MEASURE_DEFS[kind][1]
            self.acts["measure_" + kind] = self._act(
                m, key, lambda checked=False, k=kind: self.canvas.start_measure(k))
        m.addSeparator()
        self.acts["delete_first_measure"] = self._act(m, "delete_first_measure",
                                                      self._delete_first_measure)
        self.acts["delete_last_measure"] = self._act(m, "delete_last_measure",
                                                     self._delete_last_measure)
        self.acts["delete_all_measures"] = self._act(m, "delete_all_measures",
                                                     self._delete_all_measures)
        m.addSeparator()
        self.acts["measure_color"] = self._act(m, "measure_color",
                                                self._measure_color)
        units_menu2 = m.addMenu("")
        self.menus["measure_units"] = units_menu2
        for key, unit in (("unit_mm", "mm"), ("unit_inch", "inch")):
            act = QtWidgets.QAction(self.state.tr.tr(key), self)
            act.setCheckable(True)
            act.setChecked(self.state.settings.units == unit)
            act.triggered.connect(lambda checked=False, u=unit: self.set_units(u))
            units_menu2.addAction(act)
            self.acts["measure_unit_" + unit] = act

        # -------- Extras --------
        m = mbar.addMenu("")
        self.menus["extras"] = m
        lang_menu = m.addMenu("")
        self.menus["language"] = lang_menu
        self._lang_group = QtWidgets.QActionGroup(self)
        for code, name, native in Translator.language_names():
            act = QtWidgets.QAction(f"{name} ({native})", self)
            act.setCheckable(True)
            act.setChecked(code == self.state.tr.language)
            act.triggered.connect(lambda checked, c=code: self.set_language(c))
            self._lang_group.addAction(act)
            lang_menu.addAction(act)

        render_menu = m.addMenu("")
        self.menus["render_system"] = render_menu
        self.acts["enable_hw_accel"] = self._act(
            m, "enable_hw_accel", self._toggle_hw_accel, checkable=True,
            checked=self.state.settings.hardware_acceleration)
        for value in ("OpenGL", "Direct3D", "Software"):
            key = "render_" + value.lower()
            act = QtWidgets.QAction(value, self)
            act.setCheckable(True)
            act.setChecked(self.state.settings.render_system == value)
            act.triggered.connect(lambda checked=False, v=value:
                                  self._set_render_system(v))
            render_menu.addAction(act)
            self.acts[key] = act
        m.addSeparator()
        spin_menu = m.addMenu("")
        self.menus["spin"] = spin_menu
        self.acts["spin_enable"] = self._act(
            spin_menu, "spin_enable", self._toggle_spin, checkable=True)
        type_menu = spin_menu.addMenu("")
        self.menus["spin_type"] = type_menu
        self._spin_type_group = QtWidgets.QActionGroup(self)
        for key, kind in (("spin_horizontal", "horizontal"),
                          ("spin_vertical", "vertical"), ("spin_rx", "rx"),
                          ("spin_ry", "ry"), ("spin_rz", "rz"),
                          ("spin_random", "random")):
            act = QtWidgets.QAction(self.state.tr.tr(key), self)
            act.setCheckable(True)
            act.setChecked(self.state.spin.config.kind == kind)
            act.triggered.connect(lambda checked, k=kind: self._set_spin_type(k))
            self._spin_type_group.addAction(act)
            type_menu.addAction(act)
        dir_menu = spin_menu.addMenu("")
        self.menus["spin_direction"] = dir_menu
        self._spin_dir_group = QtWidgets.QActionGroup(self)
        for key, value in (("spin_positive", 1), ("spin_negative", -1)):
            act = QtWidgets.QAction(self.state.tr.tr(key), self)
            act.setCheckable(True)
            act.setChecked(self.state.spin.config.direction == value)
            act.triggered.connect(lambda checked, v=value: self._set_spin_direction(v))
            self._spin_dir_group.addAction(act)
            dir_menu.addAction(act)
        speed_menu = spin_menu.addMenu("")
        self.menus["spin_speed"] = speed_menu
        self._spin_speed_group = QtWidgets.QActionGroup(self)
        for key, value in (("speed_low", "low"), ("speed_medium", "medium"),
                           ("speed_high", "high")):
            act = QtWidgets.QAction(self.state.tr.tr(key), self)
            act.setCheckable(True)
            act.setChecked(self.state.spin.config.speed == value)
            act.triggered.connect(lambda checked, v=value: self._set_spin_speed(v))
            self._spin_speed_group.addAction(act)
            speed_menu.addAction(act)

        macro_menu = m.addMenu("")
        self.menus["macro"] = macro_menu
        self.acts["macro_start_recording"] = self._act(macro_menu, "macro_start_recording",
                                                       self._toggle_macro_record)
        self.acts["macro_execute"] = self._act(macro_menu, "macro_execute",
                                               self._play_macro)
        self.acts["macro_open"] = self._act(macro_menu, "macro_open", self._open_macro)
        self.acts["macro_save"] = self._act(macro_menu, "macro_save", self._save_macro)

        stl_menu = m.addMenu("")
        self.menus["stl_tools"] = stl_menu
        self.acts["repair_surface"] = self._act(stl_menu, "repair_surface",
                                                lambda: self._object_action("repair", -1))
        self.acts["generate_components"] = self._act(
            stl_menu, "generate_components",
            lambda: self._object_action("components", -1))
        self.acts["remove_tri_intersected"] = self._act(
            stl_menu, "remove_tri_intersected",
            lambda: self._object_action("remove_tri", -1))

        m.addSeparator()
        self.acts["statistics"] = self._act(m, "statistics", self.show_statistics)
        self.acts["object_editor2"] = self._act(m, "object_editor",
                                                self._object_properties)
        self.acts["load_default_settings"] = self._act(
            m, "load_default_settings", self.load_default_settings)
        self.acts["save_default_settings"] = self._act(
            m, "save_default_settings", self.save_default_settings)
        self.acts["reset_selected_defaults"] = self._act(
            m, "reset_selected_defaults", self.reset_default_settings)

        # -------- Help --------
        m = mbar.addMenu("")
        self.menus["help"] = m
        self.acts["about"] = self._act(m, "about", self.show_about)

        # 工具条
        for key in ("open", "zoom_fit", "wire", "solid", "colored", "transparent",
                    "show_edges", "reset_center", "orthogonal", "crosshair"):
            if self.acts.get(key) is not None:
                self.toolbar.addAction(self.acts[key])
        self.toolbar.addSeparator()
        for kind in ("line3d", "point3d", "circle3p", "angle2d", "rzpoint"):
            act = self.acts.get("measure_" + kind)
            if act:
                self.toolbar.addAction(act)
        self.toolbar.addSeparator()
        for key in ("spin_enable", "save_picture", "statistics"):
            act = self.acts.get(key)
            if act:
                self.toolbar.addAction(act)

    def _build_statusbar(self) -> None:
        bar = self.statusBar()
        self.status_label = QtWidgets.QLabel("")
        self.units_label = QtWidgets.QLabel("")
        self.render_label = QtWidgets.QLabel("")
        for widget in (self.status_label, self.units_label, self.render_label):
            widget.setMinimumWidth(140)
        bar.addPermanentWidget(self.units_label)
        bar.addPermanentWidget(self.status_label)
        bar.addPermanentWidget(self.render_label)
        bar.showMessage(self.state.tr.tr("ready"))

    def _create_timers(self) -> None:
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self._on_tick)
        self.timer.start()

    # ---------------- 翻译 ----------------
    def retranslate(self) -> None:
        tr = self.state.tr
        self.setWindowTitle(tr.tr("app_title"))
        for key, menu in self.menus.items():
            if key == "new":
                menu.setTitle(tr.tr("new_object"))
            elif key == "recent":
                menu.setTitle(tr.tr("recent_files"))
            elif key == "units":
                menu.setTitle(tr.tr("file_units"))
            elif key == "standard_views":
                menu.setTitle(tr.tr("standard_views"))
            elif key == "measure_units":
                menu.setTitle(tr.tr("measurement_units"))
            elif key == "language":
                menu.setTitle(tr.tr("language"))
            elif key == "render_system":
                menu.setTitle(tr.tr("render_system"))
            elif key == "spin":
                menu.setTitle(tr.tr("spin"))
            elif key == "spin_type":
                menu.setTitle(tr.tr("spin_type"))
            elif key == "spin_direction":
                menu.setTitle(tr.tr("spin_direction"))
            elif key == "spin_speed":
                menu.setTitle(tr.tr("spin_speed"))
            elif key == "macro":
                menu.setTitle(tr.tr("macro"))
            elif key == "stl_tools":
                menu.setTitle(tr.tr("stl_tools"))
            else:
                menu.setTitle(tr.tr(key))
        for action, key in self._toolbar_actions:
            if action is None:
                continue
            action.setText(tr.tr(key))
            action.setToolTip(tr.tr(key))
        self.object_dock.setWindowTitle(tr.tr("objects"))
        self.panel.refresh()
        self._update_statusbar()

    def _update_statusbar(self) -> None:
        state = self.state
        stats = state.statistics()
        self.status_label.setText(f"{stats['objects']} obj / {stats['triangles']} tri")
        self.units_label.setText(f"{state.tr.tr('units')}: {state.units.label}")
        self.render_label.setText(
            f"{state.last_render_seconds * 1000:.0f} ms | {state.fps:.1f} FPS")

    def _show_status(self, text: str) -> None:
        self.statusBar().showMessage(text, 4000)
        self._update_statusbar()

    def _on_scene_changed(self) -> None:
        """画布内部改变了显示状态时，同步菜单勾选与侧栏面板。"""
        opts = self.state.options
        for key, value in (("wire", opts.wire), ("solid", opts.solid),
                           ("colored", is_colored_mode(opts.color_mode)),
                           ("transparent", opts.transparent),
                           ("show_edges", opts.show_edges),
                           ("crosshair", opts.crosshair),
                           ("orthogonal", self.state.camera.orthographic)):
            act = self.acts.get(key)
            if act is not None:
                act.setChecked(bool(value))
        self.panel.refresh()
        self._update_statusbar()

    def _mark_dirty(self) -> None:
        self.canvas.mark_dirty()

    # ---------------- 周期任务 ----------------
    def _on_tick(self) -> None:
        state = self.state
        changed = False
        if state.spin.config.enabled:
            if state.spin.tick(state.camera):
                changed = True
        if state.macro.recording:
            state.macro.capture(state.camera)
        if state.macro.playback_active:
            if state.macro.playback_state(state.camera):
                changed = True
            else:
                self.statusBar().showMessage("macro playback finished", 3000)
        # 动画（旋转/宏播放）期间用交互质量渲染，避免逐帧全质量卡顿
        self.canvas.set_animating(
            bool(state.spin.config.enabled or state.macro.playback_active))
        if changed:
            self.canvas.mark_dirty()
            self._update_statusbar()

    # ---------------- 文件 ----------------
    def open_dialog(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, self.state.tr.tr("open"), self.state.settings.default_directory,
            MODEL_FILTER)
        if path:
            self.open_path(path)

    def open_path(self, path: str) -> None:
        tr = self.state.tr
        # 首次打开文件时先清掉启动示例场景：它只是个占位毛坯，留着会和
        # 刚打开的模型互相穿插，看起来像是渲染出错。
        if getattr(self, "_welcome_active", False):
            self._welcome_active = False
            self.state.scene.clear()
            self.state.measures.clear()
        try:
            obj = self.state.load_file(path)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(
                self, tr.tr("error"), tr.tr("file_open_error", path) + f"\n\n{exc}")
            return
        self.state.settings.default_directory = os.path.dirname(path)
        self.state.fit_view()
        self.panel.refresh()
        self.canvas.mark_dirty()
        self._recent_menu()
        stats = obj.mesh.statistics()
        self._show_status(
            f"{obj.name}: {stats.triangles} tri  "
            f"{stl_io.human_size(os.path.getsize(path))}")

    def _recent_menu(self) -> None:
        menu = self.acts["recent"]
        menu.clear()
        for path in self.state.settings.recent_files:
            menu.addAction(os.path.basename(path),
                           lambda checked=False, p=path: self.open_path(p))

    def save_active_stl(self) -> None:
        obj = self.state.scene.active_object() or (
            self.state.scene.objects[0] if self.state.scene.objects else None)
        if obj is None:
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, self.state.tr.tr("export_stl"),
            os.path.join(self.state.settings.default_directory, obj.name + ".stl"),
            "STL (*.stl)")
        if not path:
            return
        if path.lower().endswith(".txt"):
            stl_io.write_ascii_stl(path, obj.mesh.vertices, obj.mesh.triangles, obj.name)
        else:
            stl_io.write_binary_stl(path, obj.mesh.vertices, obj.mesh.triangles)
        self._show_status(f"{self.state.tr.tr('save')}: {path}")

    def _render_picture(self, transparent: bool = False,
                        supersample: int | None = None,
                        double: bool = False) -> np.ndarray:
        state = self.state
        opts = state.render_options()
        if transparent:
            opts.transparent_background = True
        if supersample:
            opts.supersample = max(int(supersample), 1)
        w, h = self.canvas.width(), self.canvas.height()
        if double:
            w, h = w * 2, h * 2
        return state.renderer.render(state.scene, state.camera, size=(w, h),
                                     options=opts, light=state.light,
                                     measurements=state.measures.items)

    def _save_picture(self, image: np.ndarray, title: str) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, title, os.path.join(self.state.settings.default_directory,
                                      "picture.png"), PICTURE_FILTER)
        if not path:
            return
        if path.lower().endswith(".png"):
            from .png import write_png

            write_png(path, image)
        else:  # 其它格式交给 Qt 编码
            buffer = np.ascontiguousarray(image)
            hh, ww, ch = buffer.shape
            fmt = QtGui.QImage.Format_RGBA8888 if ch == 4 else QtGui.QImage.Format_RGB888
            qimg = QtGui.QImage(buffer.data, ww, hh, buffer.strides[0], fmt)
            qimg.save(path)
        self._show_status(f"{self.state.tr.tr('save_picture')}: {path}")

    def save_picture(self) -> None:
        self._save_picture(self._render_picture(), self.state.tr.tr("save_picture"))

    def save_multi_res_picture(self) -> None:
        self._save_picture(self._render_picture(supersample=3, double=True),
                           self.state.tr.tr("save_multi_res_picture"))

    def save_picture_numroto(self, transparent: bool = False) -> None:
        title = self.state.tr.tr("save_picture_numroto_transp" if transparent
                                 else "save_picture_numroto")
        image = self._render_picture(transparent=transparent, supersample=2)
        self._save_picture(image, title)

    def copy_picture_to_clipboard(self) -> None:
        image = self._render_picture(supersample=2)
        buffer = np.ascontiguousarray(image)
        hh, ww, ch = buffer.shape
        fmt = QtGui.QImage.Format_RGBA8888 if ch == 4 else QtGui.QImage.Format_RGB888
        qimg = QtGui.QImage(buffer.data, ww, hh, buffer.strides[0], fmt).copy()
        QtWidgets.QApplication.clipboard().setImage(qimg)
        self._show_status(self.state.tr.tr("copy_picture_clipboard"))

    def show_file_size(self) -> None:
        path = self.state.current_path
        if not path:
            self._show_status(self.state.tr.tr("no_objects"))
            return
        report = stl_io.file_size_report(path)
        lines = [f"{k}: {v}" for k, v in report.items()]
        StatisticsDialog("\n".join(lines), self.state.tr.tr("show_stl_filesize"),
                         self).exec_()

    def save_object_settings(self) -> None:
        obj = self.state.scene.active_object()
        if obj is None:
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, self.state.tr.tr("save_object_settings"),
            os.path.join(self.state.settings.default_directory,
                         obj.name + ".obj.json"), "JSON (*.json)")
        if not path:
            return
        payload = {"object": obj.to_state(), "camera": self.state.camera.state(),
                   "light": self.state.light.state()}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1)
        self._show_status(f"{self.state.tr.tr('save_object_settings')}: {path}")

    def reload_object_settings(self) -> None:
        obj = self.state.scene.active_object()
        if obj is None:
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, self.state.tr.tr("reload_object_settings"),
            self.state.settings.default_directory, "JSON (*.json)")
        if not path:
            return
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        obj.set_state(payload.get("object", {}))
        self.state.camera.set_state(payload.get("camera", {}))
        self.state.light.set_state(payload.get("light", {}))
        self.panel.refresh()
        self.canvas.mark_dirty()

    # ---------------- 视图 ----------------
    def _zoom(self, factor: float) -> None:
        """factor < 1 放大，> 1 缩小。"""
        self.state.camera.dolly(-math.log(max(factor, 1e-6)) / 0.12)
        self.canvas.mark_dirty()

    def _fit(self) -> None:
        self.state.fit_view()
        self.canvas.mark_dirty()

    def _reset_center(self) -> None:
        self.state.camera.reset_center()
        self.canvas.mark_dirty()

    def _change_center(self) -> None:
        obj = self.state.scene.visible_objects()
        if obj:
            self.state.camera.change_center(obj[0].center())
            self.canvas.mark_dirty()

    def _standard_view(self, name: str) -> None:
        self.state.camera.set_standard_view(name)
        self.canvas.mark_dirty()

    def _toggle_rotate_z(self, checked: bool) -> None:
        """View > Rotate around Z：左键拖拽改为绕刀具回转轴旋转。"""
        self.state.rotate_around_z = checked
        self._show_status("rotate around Z: " + ("on" if checked else "off"))

    def _toggle_ortho(self, checked: bool) -> None:
        self.state.camera.set_orthographic(checked)
        self.state.settings.view.orthographic = checked
        self.canvas.mark_dirty()

    def _toggle_fixed(self, checked: bool) -> None:
        self.state.settings.view.fixed_on_screen = checked

    def _toggle_fullscreen(self, checked: bool) -> None:
        if checked:
            self.showFullScreen()
        else:
            self.showNormal()

    def _toggle_crosshair(self, checked: bool) -> None:
        self.state.options.crosshair = checked
        self.state.settings.view.crosshair = checked
        self.canvas.mark_dirty()

    def _slider_row(self, dialog, value: float, lo: int, hi: int,
                    suffix: str = "%"):
        """带数值回显的百分比滑条，返回 (slider, 可直接加入表单的容器)。"""
        slider = QtWidgets.QSlider(QtCore.Qt.Horizontal, dialog)
        slider.setRange(lo, hi)
        slider.setSingleStep(5)
        slider.setPageStep(10)
        slider.setValue(int(round(value)))
        label = QtWidgets.QLabel(f"{slider.value()}{suffix}")
        label.setMinimumWidth(52)
        slider.valueChanged.connect(lambda v: label.setText(f"{v}{suffix}"))
        row = QtWidgets.QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(slider, 1)
        row.addWidget(label)
        wrap = QtWidgets.QWidget(dialog)
        wrap.setLayout(row)
        return slider, wrap

    def _view_options(self) -> None:
        tr = self.state.tr
        settings = self.state.settings
        items = ["show_axis", "axis_labels", "show_grid", "crosshair",
                 "show_edges", "back_and_front", "gradient_background"]
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(tr.tr("view_options"))
        form = QtWidgets.QFormLayout(dialog)
        boxes = {}
        for key in items:
            box = QtWidgets.QCheckBox(tr.tr(key))
            box.setChecked(bool(getattr(self.state.options, key, False)))
            boxes[key] = box
            form.addRow(box)

        # ---- 鼠标灵敏度：直接对应"拖一下转多少" ----
        form.addRow(QtWidgets.QLabel(f"— {tr.tr('interaction')} —"))
        rotate = self._slider_row(dialog, settings.rotate_speed * 100, 5, 60)
        pan = self._slider_row(dialog, settings.pan_speed * 100, 20, 300)
        zoom = self._slider_row(dialog, settings.zoom_speed * 100, 2, 40)
        form.addRow(tr.tr("rotate_speed"), rotate[1])
        form.addRow(tr.tr("pan_speed"), pan[1])
        form.addRow(tr.tr("zoom_speed"), zoom[1])

        # ---- 渲染性能与网格处理 ----
        form.addRow(QtWidgets.QLabel(f"— {tr.tr('render_perf')} —"))
        quality = self._slider_row(dialog, settings.interactive_quality * 100, 15, 100)
        form.addRow(tr.tr("interactive_quality"), quality[1])

        antialias = QtWidgets.QCheckBox(tr.tr("antialias"), dialog)
        antialias.setChecked(bool(settings.antialias))
        form.addRow(antialias)

        auto_weld = QtWidgets.QCheckBox(tr.tr("auto_weld"), dialog)
        auto_weld.setChecked(bool(settings.auto_weld))
        form.addRow(auto_weld)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)

        if dialog.exec_() == QtWidgets.QDialog.Accepted:
            for key, box in boxes.items():
                setattr(self.state.options, key, box.isChecked())
            value = max(quality[0].value(), 1) / 100.0
            settings.interactive_quality = value
            settings.antialias = antialias.isChecked()
            settings.auto_weld = auto_weld.isChecked()
            settings.rotate_speed = max(rotate[0].value(), 1) / 100.0
            settings.pan_speed = max(pan[0].value(), 1) / 100.0
            settings.zoom_speed = max(zoom[0].value(), 1) / 100.0
            self.canvas.set_interactive_quality(value)
            self.state.settings.antialias = antialias.isChecked()
            self.canvas.mark_dirty()

    def show_rotation_dialog(self) -> None:
        dialog = RotationDialog(self.state.camera, self.state.tr, self)
        if dialog.exec_() == QtWidgets.QDialog.Accepted:
            yaw, pitch, roll = dialog.values()
            self.state.camera.set_rotation_angles(yaw, pitch, roll)
            self.canvas.mark_dirty()

    # ---------------- 显示模式 ----------------
    def _set_wire(self, checked: bool) -> None:
        self.state.options.wire = checked
        if checked:
            self.state.options.solid = False
            self.acts["solid"].setChecked(False)
        self.state.settings.view.wire = checked
        self.canvas.mark_dirty()

    def _set_solid(self, checked: bool) -> None:
        self.state.options.solid = checked
        if checked:
            self.state.options.wire = False
            self.acts["wire"].setChecked(False)
        self.state.settings.view.solid = checked
        self.canvas.mark_dirty()

    def _set_colored(self, checked: bool) -> None:
        """彩色 / 单色。两者都是"整件同色"，区别是按面还是逐顶点平滑着色。"""
        self._set_color_mode("auto" if checked else "uniform")

    def _set_color_mode(self, mode: str) -> None:
        self.state.options.color_mode = mode
        self.state.settings.view.color_mode = mode
        for obj in self.state.scene.objects:
            obj.color_mode = mode
            obj.auto_color = mode != "uniform"
            obj.invalidate_colors()
        # 传进来的可能不是 "auto"/"uniform"（分量、曲率、法向），此时开关按
        # "当前是否使用逐面取色" 置灰，避免勾选状态与实际显示不符。
        self.acts["colored"].setChecked(is_colored_mode(mode))
        self.canvas.mark_dirty()

    def _toggle_transparent(self, checked: bool) -> None:
        self.state.options.transparent = checked
        self.state.settings.view.transparent = checked
        self.canvas.mark_dirty()

    def _toggle_two_sided(self, checked: bool) -> None:
        self.state.light.two_sided = checked
        self.state.options.back_and_front = checked
        self.state.settings.view.back_and_front = checked
        self.canvas.mark_dirty()

    def _toggle_edges(self, checked: bool) -> None:
        self.state.options.show_edges = checked
        self.state.settings.view.show_edges = checked
        self.canvas.mark_dirty()

    def _select_object_color(self) -> None:
        obj = self.state.scene.active_object()
        color = obj.color if obj else self.state.options.background
        dialog = ColorDialog(self.state.tr.tr("select_mesh_color"), color, self)
        if dialog.exec_() == QtWidgets.QDialog.Accepted:
            targets = [obj] if obj else self.state.scene.objects
            for target in targets:
                target.color = dialog.color
                target.invalidate_colors()
            self.canvas.mark_dirty()

    def _point_size_dialog(self) -> None:
        dialog = PointSizeDialog(self.state, self)
        if dialog.exec_() == QtWidgets.QDialog.Accepted:
            self.state.options.point_size = dialog.point.value()
            self.state.options.line_width = dialog.line.value()
            self.state.settings.view.point_size = dialog.point.value()
            self.state.settings.view.line_width = dialog.line.value()
            self.canvas.mark_dirty()

    def _object_properties(self) -> None:
        obj = self.state.scene.active_object()
        if obj is None:
            return
        dialog = ObjectPropertiesDialog(obj, self.state.tr, self)
        if dialog.exec_() == QtWidgets.QDialog.Accepted:
            dialog.apply()
            self.panel.refresh()
            self.canvas.mark_dirty()

    # ---------------- 光照 ----------------
    def show_light_dialog(self) -> None:
        dialog = LightDialog(self.state, self)
        dialog.exec_()
        self.canvas.mark_dirty()

    def _set_light(self, direction: str) -> None:
        self.state.light.set_primary(direction)
        self.canvas.mark_dirty()

    def _reset_light(self) -> None:
        self.state.light.reset()
        self.canvas.mark_dirty()

    def _background_colors(self) -> None:
        scene = self.state.scene
        dialog = ColorDialog(self.state.tr.tr("background_colors"), scene.background,
                             self, with_gradient=True, top_color=scene.background_top)
        if dialog.gradient is not None:
            dialog.gradient.setChecked(scene.gradient_background)
        if dialog.exec_() == QtWidgets.QDialog.Accepted:
            scene.background = dialog.color
            scene.background_top = dialog.top
            if dialog.gradient is not None:
                scene.gradient_background = dialog.gradient.isChecked()
            self.state.options.background = scene.background
            self.state.options.background_top = scene.background_top
            self.state.options.gradient_background = scene.gradient_background
            self.canvas.mark_dirty()

    # ---------------- 测量 ----------------
    def _delete_first_measure(self) -> None:
        self.state.measures.delete_first()
        self.panel.refresh()
        self.canvas.mark_dirty()

    def _delete_last_measure(self) -> None:
        self.state.measures.delete_last()
        self.panel.refresh()
        self.canvas.mark_dirty()

    def _delete_all_measures(self) -> None:
        self.state.measures.delete_all()
        self.panel.refresh()
        self.canvas.mark_dirty()

    def _measure_color(self) -> None:
        dialog = ColorDialog(self.state.tr.tr("measure_color"),
                             self.state.measures.color, self)
        if dialog.exec_() == QtWidgets.QDialog.Accepted:
            self.state.measures.set_color(dialog.color)
            self.canvas.mark_dirty()

    # ---------------- 单位 ----------------
    def set_units(self, unit: str) -> None:
        self.state.units.unit = unit
        self.state.settings.units = unit
        self.state.measures.refresh_labels(self.state.units)
        for key in ("unit_mm", "unit_inch", "measure_unit_mm", "measure_unit_inch"):
            act = self.acts.get(key)
            if act is not None:
                act.setChecked(key.endswith(unit))
        self.panel.refresh()
        self._update_statusbar()
        self.canvas.mark_dirty()

    # ---------------- 语言 ----------------
    def set_language(self, code: str) -> None:
        self.state.tr.set_language(code)
        self.state.settings.language = code
        self.retranslate()
        self._show_status(f"language: {code}")

    # ---------------- 渲染系统 / 加速 ----------------
    def _set_render_system(self, value: str) -> None:
        self.state.settings.render_system = value
        for label in ("OpenGL", "Direct3D", "Software"):
            key = "render_" + label.lower()
            if key in self.acts:
                self.acts[key].setChecked(label == value)
        self._show_status(f"render system: {value}")

    def _toggle_hw_accel(self, checked: bool) -> None:
        self.state.settings.hardware_acceleration = checked
        self._show_status("hardware acceleration: " + ("on" if checked else "off"))

    # ---------------- 旋转动画 ----------------
    def _toggle_spin(self, checked: bool) -> None:
        self.state.spin.config.enabled = checked
        self.state.spin.reset()
        self.canvas.mark_dirty()

    def _set_spin_type(self, kind: str) -> None:
        self.state.spin.config.kind = kind
        self.state.spin.reset()

    def _set_spin_direction(self, value: int) -> None:
        self.state.spin.config.direction = value

    def _set_spin_speed(self, value: str) -> None:
        self.state.spin.config.speed = value

    # ---------------- 宏 ----------------
    def _toggle_macro_record(self) -> None:
        rec = self.state.macro
        if not rec.recording:
            rec.start()
            self.acts["macro_start_recording"].setText(
                self.state.tr.tr("macro_stop_recording"))
            self.statusBar().showMessage("recording...", 3000)
        else:
            n = rec.stop()
            self.acts["macro_start_recording"].setText(
                self.state.tr.tr("macro_start_recording"))
            self.statusBar().showMessage(f"macro frames: {n}", 4000)

    def _play_macro(self) -> None:
        rec = self.state.macro
        if not rec.macro.frames:
            self.statusBar().showMessage("empty macro", 3000)
            return
        rec.start_playback()

    def _open_macro(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, self.state.tr.tr("macro_open"),
            self.state.settings.default_directory, "JSON (*.json)")
        if not path:
            return
        try:
            n = self.state.macro.load(path)
            self.statusBar().showMessage(f"macro loaded: {n} frames", 4000)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, self.state.tr.tr("error"), str(exc))

    def _save_macro(self) -> None:
        if not self.state.macro.macro.frames:
            self.statusBar().showMessage("empty macro", 3000)
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, self.state.tr.tr("macro_save"),
            os.path.join(self.state.settings.default_directory, "macro.json"),
            "JSON (*.json)")
        if path:
            self.state.macro.save(path)
            self.statusBar().showMessage(f"saved: {path}", 4000)

    # ---------------- 新建图元 ----------------
    def _new_primitive(self, kind: str) -> None:
        self._welcome_active = False
        scene = self.state.scene
        if kind == "box":
            scene.add_box(size=(30, 30, 30), name="Box")
        elif kind == "cylinder":
            scene.add_cylinder(radius=10, height=40, name="Cylinder")
        elif kind == "step_cylinder":
            scene.add_step_cylinder(name="StepCylinder")
        elif kind == "tube":
            scene.add_tube(name="Tube")
        elif kind == "sphere":
            scene.add_sphere(name="Sphere")
        self.panel.refresh()
        self.canvas.mark_dirty()

    # ---------------- 对象操作 ----------------
    def _select_object(self, index: int) -> None:
        self.state.scene.set_active(index)
        self.panel.refresh()
        self.canvas.mark_dirty()

    def _toggle_visible(self, index: int, visible: bool) -> None:
        if 0 <= index < len(self.state.scene.objects):
            self.state.scene.objects[index].visible = visible
            self.canvas.mark_dirty()

    def _resolve_object(self, index: int):
        scene = self.state.scene
        if index is None or index < 0:
            return scene.active_object()
        if 0 <= index < len(scene.objects):
            return scene.objects[index]
        return None

    def _object_action(self, action: str, index: int = -1) -> None:
        state = self.state
        obj = self._resolve_object(index)
        if obj is None:
            self._show_status(state.tr.tr("no_objects"))
            return
        if action == "visible":
            obj.visible = not obj.visible
        elif action == "wire":
            obj.wire = not obj.wire
        elif action == "transparent":
            obj.transparent = not obj.transparent
        elif action == "always_on_top":
            obj.always_on_top = not obj.always_on_top
        elif action == "locked":
            obj.locked = not obj.locked
        elif action == "active":
            idx = state.scene.objects.index(obj)
            state.scene.set_active(idx)
        elif action == "properties":
            idx = state.scene.objects.index(obj)
            state.scene.set_active(idx)
            self._object_properties()
            return
        elif action == "statistics":
            self._show_mesh_statistics(obj)
            return
        elif action == "copy":
            state.clipboard_object = copy.deepcopy(obj)
            self._show_status(state.tr.tr("copy_object"))
            return
        elif action == "save_stl":
            idx = state.scene.objects.index(obj)
            state.scene.set_active(idx)
            self.save_active_stl()
            return
        elif action == "repair":
            info = obj.mesh.repair()
            obj.invalidate_colors()
            self.panel.refresh()
            self.canvas.mark_dirty()
            QtWidgets.QMessageBox.information(
                self, state.tr.tr("repair_surface"),
                f"weld_merged={info['weld_merged']}\n"
                f"degenerate_removed={info['degenerate_removed']}\n"
                f"triangles_flipped={info['triangles_flipped']}\n"
                f"loose_removed={info['loose_removed']}\n"
                f"before={info['before']} -> after={info['after']}")
            return
        elif action == "components":
            parts = obj.mesh.split_components()
            for part in parts:
                state.scene.add_mesh(part, name=part.name, activate=False)
            self.panel.refresh()
            self.canvas.mark_dirty()
            self._show_status(f"{state.tr.tr('generate_components')}: {len(parts)}")
            return
        elif action == "remove_tri":
            others = [o for o in state.scene.objects if o is not obj]
            if not others:
                self._show_status(state.tr.tr("no_objects"))
                return
            names = [o.name for o in others]
            name, ok = QtWidgets.QInputDialog.getItem(
                self, state.tr.tr("remove_tri_intersected"), state.tr.tr("objects"),
                names, 0, False)
            if not ok:
                return
            other = others[names.index(name)]
            removed = obj.mesh.remove_triangles_intersecting(other.mesh)
            obj.invalidate_colors()
            self.panel.refresh()
            self.canvas.mark_dirty()
            self._show_status(f"removed triangles: {removed}")
            return
        elif action == "delete":
            idx = state.scene.objects.index(obj)
            state.scene.remove_object(idx)
            self.panel.refresh()
            self.canvas.mark_dirty()
            return
        self.panel.refresh()
        self.canvas.mark_dirty()

    def copy_object(self) -> None:
        obj = self.state.scene.active_object()
        if obj is None:
            return
        self.state.clipboard_object = copy.deepcopy(obj)
        self._show_status(self.state.tr.tr("copy_object"))

    def paste_object(self) -> None:
        src = self.state.clipboard_object
        if src is None:
            self._show_status(self.state.tr.tr("no_objects"))
            return
        obj = Object3D(mesh=src.mesh.copy(), name=src.name + "_copy"
                       if hasattr(src, "name") else "copy")
        self.state.scene.objects.append(obj)
        self.state.scene.set_active(len(self.state.scene.objects) - 1)
        self.panel.refresh()
        self.canvas.mark_dirty()

    def delete_object(self) -> None:
        idx = self.state.scene.active_index
        if idx >= 0:
            self.state.scene.remove_object(idx)
            self.panel.refresh()
            self.canvas.mark_dirty()

    def delete_all_objects(self) -> None:
        self._welcome_active = False
        self.state.scene.clear()
        self.state.measures.clear()
        self.panel.refresh()
        self.canvas.mark_dirty()

    def _show_mesh_statistics(self, obj) -> None:
        text = obj.mesh.statistics().as_text()
        if obj.source_path and os.path.exists(obj.source_path):
            report = stl_io.file_size_report(obj.source_path)
            text += f"\n\n文件: {obj.source_path}\n大小: {report['human']}"
        StatisticsDialog(text, self.state.tr.tr("statistics_title"), self).exec_()

    def show_statistics(self) -> None:
        obj = self.state.scene.active_object()
        if obj is not None:
            self._show_mesh_statistics(obj)
        else:
            stats = self.state.statistics()
            text = "\n".join(f"{k}: {v}" for k, v in stats.items())
            StatisticsDialog(text, self.state.tr.tr("statistics_title"), self).exec_()

    # ---------------- 设置 ----------------
    def load_default_settings(self) -> None:
        path = self.state.load_settings()
        self.canvas.set_interactive_quality(
            self.state.settings.interactive_quality)
        self.panel.refresh()
        self.canvas.mark_dirty()
        self.retranslate()
        self._show_status(self.state.tr.tr("settings_loaded", path))

    def save_default_settings(self) -> None:
        path = self.state.save_settings()
        self._show_status(self.state.tr.tr("settings_saved", path))

    def reset_default_settings(self) -> None:
        self.state.settings.reset_to_factory()
        self.state.load_settings()
        self.canvas.set_interactive_quality(
            self.state.settings.interactive_quality)
        self.panel.refresh()
        self.canvas.mark_dirty()
        self.retranslate()

    def show_about(self) -> None:
        tr = self.state.tr
        from . import __version__

        QtWidgets.QMessageBox.about(
            self, tr.tr("about"),
            tr.tr("about_text") + f"\n\n{tr.tr('version')}: {__version__}\n"
            f"Python: {sys.version.split()[0]}")

    # ---------------- 关闭 ----------------
    def closeEvent(self, event) -> None:
        try:
            self.state.save_settings()
        except Exception:
            pass
        super().closeEvent(event)


# ==========================================================================
def main(argv=None) -> int:
    argv = list(sys.argv if argv is None else argv)
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)
    app = QtWidgets.QApplication(argv)
    app.setApplicationName("NUM STL Viewer")
    app.setOrganizationName("NUM (Python reproduction)")

    settings_path = None
    files = []
    args = argv[1:]
    i = 0
    while i < len(args):
        if args[i] in ("--settings", "-s") and i + 1 < len(args):
            settings_path = args[i + 1]
            i += 2
            continue
        files.append(args[i])
        i += 1

    state = ViewerState(settings_path)
    window = MainWindow(state)
    window.show()
    for path in files:
        if os.path.isfile(path):
            window.open_path(path)
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
