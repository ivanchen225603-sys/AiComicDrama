"""AI 漫剧工作台：从一句话创意到可下载成片。

- 未填写任何密钥时，画面、视频与配音使用本机示例素材，整条流程可离线跑通。
- 用户在「模型设置」页填写自己的 Liblib / 文本模型 / 配音密钥后，对应环节切换为真实生成。
"""

from .http import mount_drama_routes
from .service import DramaService

__all__ = ["DramaService", "mount_drama_routes"]
