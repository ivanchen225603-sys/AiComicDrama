"""漫剧生成 Provider 按需导入。

真实适配器不在包导入链上：功能阶段默认 mock，不应因 import 就触发凭据读取或建连。
"""

from __future__ import annotations

from .base import GENERATING_ACTIONS, DramaProvider, result
from .mock import MockDramaProvider

__all__ = ["GENERATING_ACTIONS", "DramaProvider", "MockDramaProvider", "result"]
