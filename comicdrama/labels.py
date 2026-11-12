"""标识文案的唯一来源：导出记录、成片容器元数据、前端徽章、示例素材说明都从这里取。

独立成模块是为了避开 import 环：compose.py 被 providers/mock.py 调用，而
providers/base.py 又被 providers/__init__.py 提前加载。把常量放在最底层，
任何一层都能安全导入，不会出现「同一个常量在三个地方各写一遍然后漂移」。

必须说清楚：技术标识 ≠ 画面水印，也 ≠ 备案已完成。法务与备案口径仍需业务确认。
"""

DEFAULT_AI_LABEL = "AI 生成"
AI_DISCLOSURE = "含 AI 生成内容（非真人实拍）"
