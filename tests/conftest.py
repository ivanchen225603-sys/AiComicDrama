import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# HTTP 测试一律打回环地址。开发机设置了 HTTP_PROXY 时，httpx 默认会把回环请求
# 交给代理；这里把回环地址并进 NO_PROXY（保留已有条目），让结果不依赖宿主网络配置。
_LOOPBACK = ("127.0.0.1", "localhost", "::1")
_entries = [item.strip() for item in (os.environ.get("NO_PROXY") or "").split(",") if item.strip()]
_entries.extend(item for item in _LOOPBACK if item not in _entries)
os.environ["NO_PROXY"] = os.environ["no_proxy"] = ",".join(_entries)
