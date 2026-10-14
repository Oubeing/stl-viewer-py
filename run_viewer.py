#!/usr/bin/env python
"""NUM STL Viewer（Python 复现版）启动脚本。

    python run_viewer.py                  # 打开示例场景
    python run_viewer.py model.stl        # 打开模型
    python run_viewer.py a.stl b.stl      # 同时打开多个模型

无图形环境或只需要批处理时，请使用：python -m num_stl_viewer --cli --help
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from num_stl_viewer.viewer import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
