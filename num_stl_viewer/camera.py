"""轨道相机 —— 对应原程序 View 菜单（Zoom / Rotate around Z / Rotation Angles /
Orthogonal / Reset center / Change center / Fixed on screen）。

世界坐标：Z 为刀具回转轴，Z 向上。
相机由 目标点 + 方位角 yaw + 俯仰角 pitch + 滚转 roll + 距离 distance 决定，
支持透视与正交两种投影。
"""

from __future__ import annotations

import numpy as np

from . import mathutil as mu

WORLD_UP = np.array([0.0, 0.0, 1.0])

# 标准视角预设：(yaw_deg, pitch_deg)
STANDARD_VIEWS = {
    "iso": (45.0, 30.0),
    "iso_back": (-135.0, 30.0),
    "front": (-90.0, 0.0),
    "back": (90.0, 0.0),
    "left": (180.0, 0.0),
    "right": (0.0, 0.0),
    "top": (-90.0, 89.9),
    "bottom": (-90.0, -89.9),
}


class Camera:
    """轨道相机。"""

    def __init__(self, width: int = 1024, height: int = 768):
        self.width = int(width)
        self.height = int(height)
        self.target = np.zeros(3, dtype=np.float64)
        self.yaw = 45.0          # 绕 Z 轴方位角（度）
        self.pitch = 30.0        # 俯仰角（度）
        self.roll = 0.0          # 绕视线滚转（度）
        self.distance = 200.0
        self.fov = 30.0          # 竖直视场角（度）
        self.orthographic = False
        self.ortho_height = 1.0  # 正交视口世界高度
        self.zoom = 1.0          # 附加缩放系数（>1 放大）
        self.near = 0.1
        self.far = 10000.0
        self._model_radius = 100.0

    # ---------------- 尺寸 ----------------
    def set_viewport(self, width: int, height: int) -> None:
        self.width = max(int(width), 1)
        self.height = max(int(height), 1)

    @property
    def aspect(self) -> float:
        return self.width / max(self.height, 1)

    # ---------------- 朝向 ----------------
    def offset_vector(self) -> np.ndarray:
        """从目标点指向相机的单位向量。"""
        y = np.radians(self.yaw)
        p = np.radians(np.clip(self.pitch, -89.99, 89.99))
        return np.array([np.cos(p) * np.cos(y), np.cos(p) * np.sin(y), np.sin(p)])

    def eye(self) -> np.ndarray:
        return self.target + self.offset_vector() * self.distance

    def forward(self) -> np.ndarray:
        return -self.offset_vector()

    def up(self) -> np.ndarray:
        f = self.forward()
        up = WORLD_UP
        if abs(float(np.dot(f, up))) > 0.9999:
            up = np.array([0.0, 1.0, 0.0])
        if abs(self.roll) > 1e-9:
            up = mu.rotation_matrix(f, np.radians(self.roll)) @ up
        return mu.normalize(up)

    def right(self) -> np.ndarray:
        return mu.normalize(np.cross(self.forward(), self.up()))

    def distance_range(self) -> tuple:
        """(near, far)，随模型尺寸自适应。"""
        r = max(self._model_radius, 1e-6)
        near = max(self.distance * 0.002, r * 1e-4, 1e-6)
        far = self.distance + r * 8.0 + 10.0
        if self.orthographic:
            near = max(self.distance - r * 4.0, 1e-6)
            far = self.distance + r * 4.0 + 10.0
        return near, far

    # ---------------- 矩阵 ----------------
    def view_matrix(self) -> np.ndarray:
        return mu.look_at(self.eye(), self.target, self.up())

    def projection_matrix(self) -> np.ndarray:
        near, far = self.distance_range()
        if self.orthographic:
            h = self.visible_height()
            return mu.orthographic(h * self.aspect, h, near, far)
        return mu.perspective(self.fov, self.aspect, near, far)

    def visible_height(self) -> float:
        """视口上下边缘在世界中的高度（正交模式下含缩放）。"""
        if self.orthographic:
            return max(self.ortho_height / max(self.zoom, 1e-6), 1e-9)
        return 2.0 * self.distance * np.tan(np.radians(self.fov) * 0.5) / max(self.zoom, 1e-6)

    def world_per_pixel(self) -> float:
        return self.visible_height() / max(self.height, 1)

    # ---------------- 投影 ----------------
    def project(self, points: np.ndarray) -> tuple:
        """世界点 -> 屏幕坐标。

        返回 (screen_xy (N,2) float, depth (N,), valid (N,) bool)。
        screen 原点在左上角，depth 为 NDC 深度（越小越近）。
        """
        pts = np.asarray(points, dtype=np.float64)
        if pts.size == 0:
            return np.zeros((0, 2)), np.zeros(0), np.zeros(0, dtype=bool)
        view = self.view_matrix()
        cam = mu.transform_points(view, pts)
        proj = self.projection_matrix()
        clip = mu.transform_points(proj, cam)  # 使用 m[:3,:3] 部分需要单独处理 w
        # 手工计算齐次裁剪坐标，保留 w
        h = np.concatenate([cam, np.ones((cam.shape[0], 1))], axis=1)
        clip4 = h @ proj.T
        w = clip4[:, 3]
        w = np.where(np.abs(w) < 1e-12, 1e-12, w)
        ndc = clip4[:, :3] / w[:, None]
        screen = np.empty((pts.shape[0], 2))
        screen[:, 0] = (ndc[:, 0] * 0.5 + 0.5) * self.width
        screen[:, 1] = (0.5 - ndc[:, 1] * 0.5) * self.height
        valid = np.isfinite(ndc).all(axis=1) & (w > 0) if not self.orthographic \
            else np.isfinite(ndc).all(axis=1)
        return screen, ndc[:, 2], valid

    def project_camera_space(self, cam_points: np.ndarray) -> tuple:
        """相机空间点 -> 屏幕坐标（渲染器内部使用，避免重复变换）。"""
        cam = np.asarray(cam_points, dtype=np.float64)
        proj = self.projection_matrix()
        h = np.concatenate([cam, np.ones((cam.shape[0], 1))], axis=1)
        clip = h @ proj.T
        w = clip[:, 3]
        w = np.where(np.abs(w) < 1e-12, 1e-12, w)
        ndc = clip[:, :3] / w[:, None]
        screen = np.empty((cam.shape[0], 2))
        screen[:, 0] = (ndc[:, 0] * 0.5 + 0.5) * self.width
        screen[:, 1] = (0.5 - ndc[:, 1] * 0.5) * self.height
        return screen, ndc[:, 2], ndc[:, :3]

    def to_camera_space(self, points: np.ndarray) -> np.ndarray:
        return mu.transform_points(self.view_matrix(), np.asarray(points, dtype=np.float64))

    # ---------------- 交互 ----------------
    # 灵敏度集中在这里的默认值上，GUI 会用「视图选项」里的设置覆盖它们。
    # 早期 orbit 固定 0.35 度/像素：拖 300px 就转 105°，一次随手拖动模型就
    # 跑掉了，这正是「随便操作一下变化太大」的来源。
    ROTATE_SPEED = 0.20          # 轨道旋转：度 / 像素
    PAN_SPEED = 1.0              # 平移：1.0 表示严格跟随光标
    DOLLY_RATE = 0.10            # 滚轮：每档 (120°) 的指数缩放系数

    def orbit(self, dx_pixels: float, dy_pixels: float,
              speed: float | None = None) -> None:
        """左键拖拽：绕目标点旋转。"""
        speed = self.ROTATE_SPEED if speed is None else float(speed)
        self.yaw -= dx_pixels * speed
        self.pitch = float(np.clip(self.pitch + dy_pixels * speed, -89.9, 89.9))

    def rotate_around_z(self, dx_pixels: float, speed: float | None = None) -> None:
        """Rotate around Z：只改变方位角（沿机床回转轴转）。"""
        speed = self.ROTATE_SPEED if speed is None else float(speed)
        self.yaw -= dx_pixels * speed

    def set_rotation_angles(self, yaw=None, pitch=None, roll=None) -> None:
        if yaw is not None:
            self.yaw = float(yaw)
        if pitch is not None:
            self.pitch = float(np.clip(pitch, -89.9, 89.9))
        if roll is not None:
            self.roll = float(roll)

    def pan(self, dx_pixels: float, dy_pixels: float,
            factor: float | None = None) -> None:
        """中键拖拽：平移目标点。``factor=1.0`` 时模型严格跟随光标。"""
        factor = self.PAN_SPEED if factor is None else float(factor)
        scale = self.world_per_pixel() * factor
        self.target = self.target - self.right() * dx_pixels * scale + self.up() * dy_pixels * scale

    def dolly(self, steps: float, rate: float | None = None) -> None:
        """滚轮：缩放（改变距离或正交视口）。``steps`` 为滚轮档数。"""
        rate = self.DOLLY_RATE if rate is None else float(rate)
        factor = float(np.exp(-rate * steps))
        if self.orthographic:
            self.ortho_height = float(np.clip(self.ortho_height * factor, 1e-6, 1e9))
        else:
            self.distance = float(np.clip(self.distance * factor, 1e-6, 1e9))

    def zoom_by_factor(self, factor: float) -> None:
        self.zoom = float(np.clip(self.zoom * factor, 0.01, 100.0))

    # ---------------- 复位 / 适配 ----------------
    def fit(self, bounds_lo, bounds_hi, margin: float = 1.25, keep_orientation: bool = True) -> None:
        """Zoom Fit：使整个包围盒进入视野。"""
        lo = np.asarray(bounds_lo, dtype=np.float64)
        hi = np.asarray(bounds_hi, dtype=np.float64)
        center = (lo + hi) * 0.5
        radius = float(np.linalg.norm(hi - lo) * 0.5)
        self.target = center
        self._model_radius = max(radius, 1e-6)
        size = hi - lo
        if radius < 1e-9:
            radius = 1.0
            size = np.array([1.0, 1.0, 1.0])
        # 正交视口高度直接取包围盒在屏幕上的投影高度
        h = float(np.linalg.norm(size))
        self.ortho_height = max(h * margin, 1e-6)
        if not keep_orientation:
            self.yaw, self.pitch, self.roll = 45.0, 30.0, 0.0
        # 透视距离：让外接球落在竖直视场内（水平方向按宽高比放宽）
        vfov = np.radians(self.fov)
        hfov = 2.0 * np.arctan(np.tan(vfov * 0.5) * self.aspect)
        limiting = min(vfov, hfov)
        self.distance = max(radius / np.tan(limiting * 0.5) * margin, radius * 1.05)
        self.distance = max(self.distance, 1e-6)
        self.zoom = 1.0
        near, far = self.distance_range()
        self.near, self.far = near, far

    def reset_center(self) -> None:
        """Reset center：把目标点移回世界原点。"""
        self.target = np.zeros(3, dtype=np.float64)

    def change_center(self, point) -> None:
        """Change center：把旋转中心移到指定点。"""
        self.target = np.asarray(point, dtype=np.float64)

    def set_standard_view(self, name: str) -> None:
        if name in STANDARD_VIEWS:
            self.yaw, self.pitch = STANDARD_VIEWS[name]
            self.roll = 0.0

    def set_orthographic(self, enabled: bool) -> None:
        """View > Orthogonal 切换；切换时保持视觉尺寸大致连续。"""
        if enabled == self.orthographic:
            return
        if enabled:
            self.ortho_height = self.visible_height()
        else:
            self.distance = max(self.visible_height() /
                                (2.0 * np.tan(np.radians(self.fov) * 0.5)), 1e-6)
        self.orthographic = bool(enabled)

    # ---------------- 拾取 ----------------
    def ray(self, x: float, y: float) -> tuple:
        """屏幕像素 -> 世界射线 (origin, direction)。"""
        ndc_x = (x / max(self.width, 1)) * 2.0 - 1.0
        ndc_y = 1.0 - (y / max(self.height, 1)) * 2.0
        if self.orthographic:
            h = self.visible_height() * 0.5
            w = h * self.aspect
            origin = self.target + self.right() * ndc_x * w + self.up() * ndc_y * h
            return origin, self.forward()
        near = self.distance_range()[0]
        tan_half = np.tan(np.radians(self.fov) * 0.5)
        direction = mu.normalize(
            self.forward()
            + self.right() * ndc_x * tan_half * self.aspect
            + self.up() * ndc_y * tan_half
        )
        return self.eye(), direction

    def ray_to_plane(self, x: float, y: float, plane_point=None,
                     plane_normal=None) -> np.ndarray:
        """屏幕像素投射到平面；默认投射到过目标点且垂直于视线的平面。"""
        origin, direction = self.ray(x, y)
        if plane_point is None:
            plane_point = self.target
        if plane_normal is None:
            plane_normal = self.forward()
        hit = mu.ray_plane_intersection(origin, direction,
                                        np.asarray(plane_point, dtype=np.float64),
                                        mu.normalize(np.asarray(plane_normal, dtype=np.float64)))
        return hit if hit is not None else self.target.copy()

    # ---------------- 序列化 ----------------
    def state(self) -> dict:
        return {
            "target": self.target.tolist(),
            "yaw": self.yaw,
            "pitch": self.pitch,
            "roll": self.roll,
            "distance": self.distance,
            "fov": self.fov,
            "orthographic": self.orthographic,
            "ortho_height": self.ortho_height,
            "zoom": self.zoom,
        }

    def set_state(self, state: dict) -> None:
        if not state:
            return
        if "target" in state:
            self.target = np.asarray(state["target"], dtype=np.float64)
        for key in ("yaw", "pitch", "roll", "distance", "fov", "ortho_height", "zoom"):
            if key in state:
                setattr(self, key, float(state[key]))
        if "orthographic" in state:
            self.orthographic = bool(state["orthographic"])

    def copy(self) -> "Camera":
        cam = Camera(self.width, self.height)
        cam.set_state(self.state())
        return cam
