"""漫剧数据存储：独立 drama.sqlite，规范化表 + 文档型 JSON 列。

刻意的数据模型边界：核心实体（项目 / 版本 / 资产 / 分镜 / 任务 / 流水 / 导出 /
素材记录 / 计价）用独立表；天然文档型的配置（剧本三层、音色字幕、时间线批注、
QA 勾选）以 JSON 列存放。这是第一版范围，不写成「已完成生产级数据建模」。

使用单个长连接 + check_same_thread=False，并且**同时**用两把锁串行：

- `asyncio.Lock`（服务层，按 owner 分）：保证「读-算-写」没有并发窗口；
- `threading.RLock`（本层）：保证同一时刻只有一个线程碰这个连接。

第二把锁不是冗余：store 的每次调用都走 `asyncio.to_thread`，落在**线程池的不同
线程**上。只有 asyncio 锁时，任何一处「不经过 owner 锁」的读（例如后台 pump 查
活跃任务、config 查今日消耗）就能和请求里的事务撞在同一个 sqlite 连接上，
后果不是异常而是**段错误**（check_same_thread=False 的前提下 C 层没有保护）。
RLock 用可重入版本，是因为 `run()` 里的事务内还会调用 `_fetch` 之类的辅助方法。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from .accounts import ACCOUNT_SCHEMA
from .compose import parse_resolution, parse_subtitle_mode
from .moderation import AUDIT_REJECT_MARK
from .payments import ORDER_SCHEMA
from .pricing import (
    CREDIT_LIMIT_MAX,
    MIGRATED_FROM_SEED,
    PRICE_T2I,
    cap_of,
    default_pricing,
    migrate_pricing,
    normalize_video,
    pricing_stale,
)
from .wallet import SEED_POINTS, DramaWallet, LegacyWallet

T = TypeVar("T")

# 导入校验里「字段级」失败的错误码。默认导入失败一律 invalid_import（整档结构坏了），
# 但字段级错误必须能让用户直接看出是哪个字段，服务层据此把 code 透传出去。
# 只用于新增字段（mediaHistory），不动既有 ValueError -> invalid_import 的映射。
IMPORT_FIELD_CODE = "invalid_field"

# 镜头产物历史上限。每镜头的可回退产物只保留最新 10 条：
# 一份历史最长 10 × (512 字节 URL + 元数据)，再长既没有产品价值，
# 又会让每次整档写回（replace_in 是整档重写）多搬运无用的 JSON。
MEDIA_HISTORY_MAX = 10
MEDIA_HISTORY_URL_MAX = 512
MEDIA_KINDS: frozenset[str] = frozenset({"image", "video"})


class ImportFieldError(ValueError):
    """导入文档的**字段级**校验失败：带自己的错误码，与 invalid_import 区分。"""

    code = IMPORT_FIELD_CODE


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS drama_projects("
    "id TEXT PRIMARY KEY,owner TEXT NOT NULL,name TEXT NOT NULL,"
    "season INTEGER NOT NULL DEFAULT 1,episode INTEGER NOT NULL DEFAULT 1,"
    "episode_title TEXT NOT NULL DEFAULT '',genre TEXT NOT NULL DEFAULT '',"
    "style TEXT NOT NULL DEFAULT '',ratio TEXT NOT NULL DEFAULT '',"
    "eps INTEGER NOT NULL DEFAULT 12,duration INTEGER NOT NULL DEFAULT 90,"
    "budget INTEGER NOT NULL DEFAULT 3000,res TEXT NOT NULL DEFAULT '',"
    "archived INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL DEFAULT '',"
    "script_layers TEXT NOT NULL DEFAULT '{}',frozen_version_id TEXT,"
    "dirty_since_freeze INTEGER NOT NULL DEFAULT 0,"
    "audio_config TEXT NOT NULL DEFAULT '{}',timeline_order TEXT NOT NULL DEFAULT '[]',"
    "timeline_notes TEXT NOT NULL DEFAULT '[]',"
    "qa TEXT NOT NULL DEFAULT '[false,false,false,false]',"
    "updated_at REAL NOT NULL DEFAULT 0,last_result TEXT NOT NULL DEFAULT '{}',"
    "film TEXT NOT NULL DEFAULT '{}',"
    # 成片交付选项挂在项目上：它们是「这一集要交付成什么样」的属性，与 `res`
    # （生成请求的规格）不是一回事，混用一个字段会让「改了生成规格」意外改掉交付格式。
    "subtitle_mode TEXT NOT NULL DEFAULT 'soft',"
    "output_resolution TEXT NOT NULL DEFAULT 'source')",
    "CREATE TABLE IF NOT EXISTS drama_script_versions("
    "id TEXT PRIMARY KEY,project_id TEXT NOT NULL,label TEXT NOT NULL,"
    "layers TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL DEFAULT '',"
    "note TEXT NOT NULL DEFAULT '')",
    "CREATE TABLE IF NOT EXISTS drama_assets("
    "id TEXT PRIMARY KEY,project_id TEXT NOT NULL,type TEXT NOT NULL,name TEXT NOT NULL,"
    "sub TEXT NOT NULL DEFAULT '',voice TEXT NOT NULL DEFAULT '',note TEXT NOT NULL DEFAULT '',"
    "prompt TEXT NOT NULL DEFAULT '',seed INTEGER NOT NULL DEFAULT 1,"
    "locked INTEGER NOT NULL DEFAULT 0,image_url TEXT NOT NULL DEFAULT '',"
    # 上游判定内容不合规时置 1：产物 URL 会被清空，但这一格要留下来告诉用户
    # 「这里不是没生成，是生成后被判违规并退款」，否则看起来就是个凭空变空的资产。
    "audit_rejected INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS drama_shots("
    "id TEXT PRIMARY KEY,project_id TEXT NOT NULL,seq INTEGER NOT NULL,"
    "dur INTEGER NOT NULL DEFAULT 3,size TEXT NOT NULL DEFAULT '中景',"
    "description TEXT NOT NULL DEFAULT '',prompt TEXT NOT NULL DEFAULT '',"
    "line TEXT NOT NULL DEFAULT '',move TEXT NOT NULL DEFAULT '固定镜头',"
    "refs TEXT NOT NULL DEFAULT '[]',state TEXT NOT NULL DEFAULT 'draft',"
    "dirty INTEGER NOT NULL DEFAULT 0,frames TEXT NOT NULL DEFAULT '[]',"
    "video_url TEXT NOT NULL DEFAULT '',image_url TEXT NOT NULL DEFAULT '',"
    "audit_rejected INTEGER NOT NULL DEFAULT 0,"
    # 产物历史（JSON 数组，最新在前，最多 10 条）：每次成功把 URL 写回镜头都追加
    # 一条，`restoreShotMedia` 只能从这份历史里回退。没有它，重新生成一次视频就把
    # 上一版地址永久覆盖掉了（文件还在，地址没了）。
    "media_history TEXT NOT NULL DEFAULT '[]')",
    "CREATE TABLE IF NOT EXISTS drama_tasks("
    "id TEXT PRIMARY KEY,project_id TEXT NOT NULL,owner TEXT NOT NULL,shot_id TEXT NOT NULL,"
    "type TEXT NOT NULL,state TEXT NOT NULL,percent INTEGER NOT NULL DEFAULT 0,"
    "reserve INTEGER NOT NULL,cost INTEGER NOT NULL,settled INTEGER NOT NULL DEFAULT 0,"
    "picked INTEGER,frames TEXT NOT NULL DEFAULT '[]',idem TEXT NOT NULL,"
    "charge_id TEXT NOT NULL,upstream_task_id TEXT,note TEXT NOT NULL DEFAULT '',"
    "created_at TEXT NOT NULL DEFAULT '',settled_day TEXT NOT NULL DEFAULT '',"
    # 这批关键帧是哪个 Provider 提交的。切模式时用来识别「离线模拟遗留任务」，
    # 给它们一个明确去向（自动退还预占），而不是无声冻结。
    "source_provider TEXT NOT NULL DEFAULT '',"
    # 任务产物类型：keyframe（关键帧 t2i）/ video（图生视频）。结算时据此决定写回
    # shot.image_url 还是 shot.video_url、流水文案写「关键帧」还是「视频」。
    # 默认值保证老库里的任务一律按关键帧处理，不需要数据迁移。
    "kind TEXT NOT NULL DEFAULT 'keyframe',"
    # 关键帧候选张数。必须落库：submitTasks 与 tick 是两次独立请求，tick 组装
    # Provider 请求时要用它填 `count`。不能事后从 cost/reserve 反推——管理员在
    # 两次请求之间改价就会算错，那等于按错误的张数出网。
    "candidates INTEGER NOT NULL DEFAULT 1)",
    "CREATE TABLE IF NOT EXISTS drama_ledger("
    "owner TEXT PRIMARY KEY,balance INTEGER NOT NULL,frozen INTEGER NOT NULL,"
    "retired_cost INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS drama_tx("
    "id TEXT PRIMARY KEY,owner TEXT NOT NULL,kind TEXT NOT NULL,label TEXT NOT NULL,"
    "amount INTEGER NOT NULL,note TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL DEFAULT '',"
    "project_id TEXT,task_id TEXT,seed INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS drama_exports("
    "id TEXT PRIMARY KEY,project_id TEXT NOT NULL,time TEXT NOT NULL,name TEXT NOT NULL,"
    "res TEXT NOT NULL,shots INTEGER NOT NULL,duration INTEGER NOT NULL,"
    # 导出记录必须指向真实的成片文件；离线示例也要带示例标识，不能只是留痕。
    "film_url TEXT NOT NULL DEFAULT '',artifact TEXT NOT NULL DEFAULT '',"
    "ai_label TEXT NOT NULL DEFAULT '',ai_disclosure TEXT NOT NULL DEFAULT '',"
    "sample_label TEXT NOT NULL DEFAULT '')",
    "CREATE TABLE IF NOT EXISTS drama_pricing("
    "owner TEXT PRIMARY KEY,t2i INTEGER NOT NULL,cap INTEGER NOT NULL,"
    "qps REAL NOT NULL,concurrency INTEGER NOT NULL,video TEXT,"
    # 迁移判据：迁移前的原值快照（如 "10/12/null"）。非空即「这行已经处理过」。
    # 不能用「当前值等于旧默认」judge 幂等——管理员事后把价改回 10 会被二次迁移。
    # 新开户写 'seed'（见 pricing.MIGRATED_FROM_SEED），因为种子行没有「迁移前原值」。
    "migrated_from TEXT NOT NULL DEFAULT '',"
    # 只读标记：生效价跌破上游成本时为 1。只用于诊断展示，不阻断任何动作。
    "pricing_stale INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS drama_media("
    "id TEXT PRIMARY KEY,owner TEXT NOT NULL,project_id TEXT,kind TEXT NOT NULL,"
    "label TEXT NOT NULL,payload TEXT NOT NULL DEFAULT '{}',"
    "provider TEXT NOT NULL DEFAULT 'mock',created_at TEXT NOT NULL DEFAULT '')",
    # 用户上传的 BGM / 参考图。文件本体在 drama_media 目录按 owner 隔离留存，
    # 这张表只存元数据；整份 state 导出/导入时一起带走，避免「导入后上传丢了」。
    "CREATE TABLE IF NOT EXISTS drama_uploads("
    "id TEXT PRIMARY KEY,owner TEXT NOT NULL,kind TEXT NOT NULL,name TEXT NOT NULL,"
    "url TEXT NOT NULL,bytes INTEGER NOT NULL DEFAULT 0,"
    "asset_type TEXT NOT NULL DEFAULT '',asset_id TEXT NOT NULL DEFAULT '',"
    "created_at TEXT NOT NULL DEFAULT '')",
    "CREATE TABLE IF NOT EXISTS drama_audit("
    "id TEXT PRIMARY KEY,owner TEXT NOT NULL,action TEXT NOT NULL,"
    "detail TEXT NOT NULL DEFAULT '',created_at REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS drama_owner_state(owner TEXT PRIMARY KEY,active_project_id TEXT,"
    # 用户自设的积分使用上限（JSON：single / total / used / since），见 service.CREDIT_LIMITS。
    "credit_limits TEXT NOT NULL DEFAULT '{}')",
    # 生成动作的扣费与幂等凭据。drama_tx 只记「余额变了多少」，而 settle 的 amount 恒为 0，
    # 因此「本次生成本身花了多少」与「今天一共花了多少」无法从 drama_tx 反推，必须单独记账。
    # state 取值：settled / released / pending_review / in_flight；
    # request_id 非空时 (owner, request_id) 唯一。
    # target 是「这次生成锁到哪个对象」（shot:<id> / asset:<id> / 空串=项目级）。
    # 刷新页面会换 requestId，幂等键认不出来，只能靠 target 挡住同一目标重复出网；
    # 前端也用它把 in_flight 凭据映射回镜头卡上的「生成中」。
    "CREATE TABLE IF NOT EXISTS drama_charge("
    "id TEXT PRIMARY KEY,owner TEXT NOT NULL,project_id TEXT,action TEXT NOT NULL,"
    "request_id TEXT NOT NULL DEFAULT '',price INTEGER NOT NULL,cap INTEGER NOT NULL,"
    "cost INTEGER NOT NULL,state TEXT NOT NULL DEFAULT 'settled',"
    "result TEXT NOT NULL DEFAULT '',note TEXT NOT NULL DEFAULT '',"
    "created_at REAL NOT NULL DEFAULT 0,day TEXT NOT NULL DEFAULT '',"
    "target TEXT NOT NULL DEFAULT '',"
    # 上游任务编号（generateUuid）。有编号才能按编号自动确认并结算；mock 路径
    # 没有真实 generateUuid，一律保持 NULL，**绝不伪造**一个占位编号去触发结算。
    "upstream_task_id TEXT DEFAULT NULL,"
    # 这笔凭据是真实上游还是本地模拟（存 Provider.name 原值，见 daily_spend）。
    # 日上限只统计真实消费，所以判定必须在预占当时落库：事后算不出来——同一个
    # owner 可能当天先跑离线演示、再切到真实模式。
    "source_provider TEXT NOT NULL DEFAULT '')",
    "CREATE UNIQUE INDEX IF NOT EXISTS drama_charge_request "
    "ON drama_charge(owner,request_id) WHERE request_id<>''",
    "CREATE INDEX IF NOT EXISTS drama_charge_day ON drama_charge(owner,day)",
    "CREATE INDEX IF NOT EXISTS drama_charge_project ON drama_charge(owner,project_id)",
    "CREATE INDEX IF NOT EXISTS drama_shots_project ON drama_shots(project_id,seq)",
    "CREATE INDEX IF NOT EXISTS drama_assets_project ON drama_assets(project_id,type)",
    "CREATE INDEX IF NOT EXISTS drama_tasks_owner ON drama_tasks(owner,project_id)",
    "CREATE INDEX IF NOT EXISTS drama_uploads_owner ON drama_uploads(owner,kind)",
    "CREATE INDEX IF NOT EXISTS drama_tx_owner ON drama_tx(owner,id)",
    # 账户 / 会话 / 充值订单是**身份与账务凭证**，不是演示业务数据：
    # 不进 export_doc/import_doc（跨库导出会泄露密码哈希、会话令牌与回调凭证），
    # 也不进 reset() 的 DELETE 清单（不随演示数据重置）。三张表仍在这一份
    # drama.sqlite 里，由同一个连接、同一把 RLock 串行访问。
    *ACCOUNT_SCHEMA,
    *ORDER_SCHEMA,
)

# 导出/导入**只**描述业务状态：`_state_in` 是唯一读入口、`replace_in` 是唯一写入口。
# 上面三张凭证表刻意不出现在这两个函数里，也不出现在 state 快照中；这条边界
# 由 `tests/test_boundaries.py` 静态断言锁住。

# 新开户的默认计价：由 pricing 单一源派生，不在这里硬写数字。
# 这里之所以是「函数返回的字典」而不是一份独立常量，是为了让改价只改 pricing.py
# 一处；前端 `comicdrama/web/js/store.js` 的 `A.DEFAULT_PRICING` 必须同值，
# 否则离线态会按旧价真实扣费（store.js 的 submitTasks 用 pricing.cap 预占）。
DEFAULT_PRICING: dict[str, Any] = default_pricing()
DEFAULT_SETTINGS: dict[str, Any] = {
    "genre": "都市悬疑",
    "style": "精致国漫",
    "ratio": "9:16 竖屏",
    "eps": 12,
    "duration": 90,
    "budget": 3000,
    "res": "1080 × 1920 / 24fps",
}

_SHOT_SEED: list[tuple[Any, ...]] = [
    (
        3,
        "全景",
        "雨夜的雾港码头，远处灯塔扫过湿漉漉的石阶",
        "雨夜 码头 冷蓝调 湿石阶 电影感",
        "（旁白）那封信，比船晚到了十一年。",
        "缓慢下摇",
        ["@场景:雾港码头"],
    ),
    (
        3,
        "中景",
        "林砚撑黑伞站定，抬头看向邮局方向",
        "中景 女性 黑伞 侧脸 冷光",
        "林砚：我还是来了。",
        "固定镜头",
        ["@角色:林砚", "@场景:雾港码头"],
    ),
    (
        2,
        "特写",
        "信封边角被雨水浸软，字迹微微晕开",
        "特写 信封 雨滴 纸张纹理",
        "",
        "微推",
        ["@道具:旧信封"],
    ),
    (
        4,
        "中近景",
        "周叙从邮局门廊走出，两人隔着雨幕对视",
        "双人 中近景 雨幕 对视 浅景深",
        "周叙：你收到信了。",
        "缓慢横移",
        ["@角色:林砚", "@角色:周叙", "@场景:街角邮局"],
    ),
    (
        2,
        "特写",
        "林砚握紧伞柄，指节发白",
        "特写 手部 伞柄 情绪张力",
        "",
        "固定镜头",
        ["@角色:林砚"],
    ),
    (
        3,
        "全景",
        "街灯亮起，两人之间隔着整条湿街",
        "全景 街灯 湿街 对称构图",
        "（旁白）有些话，一开口就成了告别。",
        "后拉",
        ["@场景:街角邮局", "@角色:林砚", "@角色:周叙"],
    ),
    (
        3,
        "近景",
        "旧公寓窗内亮起一盏暖灯",
        "近景 窗 暖光 内外冷暖对比",
        "",
        "上摇",
        ["@场景:旧公寓"],
    ),
    (
        2,
        "特写",
        "信纸摊开，最后一行字被灯光照亮",
        "特写 信纸 暖光 浅景深",
        "林砚：那就到这里吧。",
        "微推",
        ["@道具:旧信封"],
    ),
]

_ASSET_SEED: dict[str, list[tuple[Any, ...]]] = {
    "characters": [
        (
            "a1",
            "林砚",
            "女主",
            3,
            "声线 A · 清冷女中音",
            "深灰风衣 / 齐肩短发 / 左眉小疤",
            "女性 深灰风衣 短发 冷调",
            1,
        ),
        (
            "a2",
            "周叙",
            "男主",
            7,
            "声线 B · 低沉男声",
            "藏青大衣 / 金丝眼镜 / 身形偏高",
            "男性 藏青大衣 眼镜 冷调",
            1,
        ),
        ("a3", "房东太太", "配角 · 草稿", 11, "未绑定", "待补充三视图", "", 0),
    ],
    "scenes": [
        (
            "s1",
            "雾港码头",
            "主场景",
            2,
            "环境音：雨 / 海浪",
            "冷蓝调 / 夜 / 湿石阶",
            "码头 雨夜 冷蓝 灯塔",
            1,
        ),
        (
            "s2",
            "街角邮局",
            "主场景",
            5,
            "环境音：雨 / 门铃",
            "暖黄门廊 / 夜 / 玻璃雾气",
            "邮局 门廊 暖黄 雨夜",
            1,
        ),
        ("s3", "旧公寓", "草稿", 9, "环境音：室内", "待补场景四视图", "", 0),
    ],
    "props": [
        (
            "d1",
            "旧信封",
            "关键道具",
            13,
            "—",
            "牛皮纸 / 蓝黑墨水 / 边角磨损",
            "信封 牛皮纸 特写",
            1,
        ),
        ("d2", "黑伞", "草稿", 17, "—", "待补多角度", "", 0),
    ],
}

_LAYERS: dict[str, str] = {
    "logline": "十一年前寄丢的一封信，在雾港的雨夜被重新送回。收信人必须决定：拆开它，还是让它继续沉在海底。",
    "outline": "雨夜，林砚按信上的地址来到雾港码头；周叙在街角邮局等她；两人隔着雨幕对视，谁都没有先开口；结尾亮起旧公寓的一盏灯。",
    "body": (
        "【场景 1 · 雾港码头 · 夜 · 雨】\n（旁白）那封信，比船晚到了十一年。\n"
        "林砚撑黑伞站定，抬头看向邮局方向。\n林砚：我还是来了。\n\n"
        "【场景 2 · 街角邮局 · 夜 · 雨】\n周叙从门廊走出，两人隔着雨幕对视。\n"
        "周叙：你收到信了。\n（停顿）\n（旁白）有些话，一开口就成了告别。"
    ),
}

_AUDIO_SEED: dict[str, Any] = {
    "voices": [
        {
            "id": "v1",
            "character": "林砚",
            "voice": "声线 A · 清冷女中音",
            "lines": 2,
            "duration": "6.4s",
        },
        {
            "id": "v2",
            "character": "周叙",
            "voice": "声线 B · 低沉男声",
            "lines": 1,
            "duration": "3.1s",
        },
        {
            "id": "v3",
            "character": "旁白",
            "voice": "声线 C · 中性旁白",
            "lines": 2,
            "duration": "7.8s",
        },
    ],
    "subtitles": [
        {
            "id": "t1",
            "in": "00:00",
            "out": "00:03",
            "text": "那封信，比船晚到了十一年。",
            "checked": True,
        },
        {"id": "t2", "in": "00:03", "out": "00:06", "text": "我还是来了。", "checked": True},
        {"id": "t3", "in": "00:08", "out": "00:12", "text": "你收到信了。", "checked": False},
        {
            "id": "t4",
            "in": "00:16",
            "out": "00:19",
            "text": "有些话，一开口就成了告别。",
            "checked": False,
        },
    ],
    "bgm": {
        "name": "雨夜氛围",
        "voiceLevel": 100,
        "bgmLevel": 25,
        "ambient": "雨 / 海浪",
    },
}

_TX_SEED: list[tuple[Any, ...]] = [
    ("x1", "credit", "管理员加余额", 1000, "演示初始化", "11-09 10:12"),
    (
        "x2",
        "reserve",
        "单图 t2i 预占",
        -120,
        "余额转冻结 120（estimate 100 / cap 120）",
        "11-09 10:24",
    ),
    ("x3", "settle", "单图 t2i 结算", 0, "冻结内核销 80，余额不变", "11-09 10:24"),
    ("x4", "refund", "结算退差", 40, "120 - 80 退回余额", "11-09 10:24"),
    ("x5", "release", "生成失败释放预占", 60, "上游明确拒绝，未执行", "11-09 10:31"),
]


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(text: str | None, fallback: Any) -> Any:
    if not text:
        return fallback
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return fallback


def _now() -> str:
    return time.strftime("%m-%d %H:%M", time.localtime())


def _history_of(value: Any) -> list[Any]:
    """把「产物历史」归一成数组：非数组一律当空，绝不让脏值把写库整条打断。"""
    return list(value) if isinstance(value, list) else []


class DramaStore:
    """同步 SQLite 访问层；线程安全由本层的 RLock 保证。"""

    def __init__(self, path: Path, wallet: DramaWallet | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        # 账本读路径从这里走：默认是现成的 drama_ledger/drama_tx，
        # 切换成真账本时换一个实现即可，语义在端口上。
        self.wallet: DramaWallet = wallet or LegacyWallet()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        # 可重入：run() 的事务里还会调 _fetch / _exec 之类的辅助方法。
        self._lock = threading.RLock()
        with self._lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA busy_timeout=5000")
            self.db.execute("PRAGMA synchronous=NORMAL")

    # ---------- 线程级串行的原语 ----------
    # 所有触碰连接的路径都必须经过这三个方法之一（run / _fetch / _exec）。
    def _fetch(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.db.execute(sql, params).fetchall()

    def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.db.execute(sql, params).fetchone()

    def _exec(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self._lock:
            return int(self.db.execute(sql, params).rowcount or 0)

    def install(self) -> None:
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                for statement in _SCHEMA:
                    self.db.execute(statement)
                self._migrate()
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def _migrate(self) -> None:
        """给已存在的库补新列。

        CREATE TABLE IF NOT EXISTS 只对新库生效；老库（本功能阶段之前生成的
        drama.sqlite）不会因为 schema 常量变了而自动加列。这里按 table_info
        逐列检查，缺一补一，全部带默认值，因此对老数据是无损的。
        """
        additions: dict[str, list[tuple[str, str]]] = {
            "drama_ledger": [("retired_cost", "INTEGER NOT NULL DEFAULT 0")],
            "drama_owner_state": [("credit_limits", "TEXT NOT NULL DEFAULT '{}'")],
            "drama_assets": [
                ("image_url", "TEXT NOT NULL DEFAULT ''"),
                # 上游判违规后清空产物 URL，用这一格留痕（0 = 正常，1 = 判违规已退款）。
                ("audit_rejected", "INTEGER NOT NULL DEFAULT 0"),
            ],
            "drama_shots": [
                ("video_url", "TEXT NOT NULL DEFAULT ''"),
                # 关键帧真图（文生图 / 离线示例图）写回镜头，前端镜头卡直接显示。
                ("image_url", "TEXT NOT NULL DEFAULT ''"),
                # 图生视频被判违规：URL 清空 + 这一格置 1，页面才能说明原因。
                ("audit_rejected", "INTEGER NOT NULL DEFAULT 0"),
                # 产物历史（最新在前）。老库补成空数组：没有历史就是「无从回退」，
                # 这不是数据丢失——历史是从本功能上线那天才开始记的。
                ("media_history", "TEXT NOT NULL DEFAULT '[]'"),
            ],
            "drama_projects": [
                ("last_result", "TEXT NOT NULL DEFAULT '{}'"),
                # 成片地址挂在项目上：只存 lastResult 会被下一次动作覆盖，
                # 于是「合成成功 -> 点个剧本医生 -> 导出说没合成」。
                ("film", "TEXT NOT NULL DEFAULT '{}'"),
                # 老库的成片是按软字幕 + 原尺寸出的，所以这两个默认值就是
                # 「继续按老样子出片」，不会因为加列而改变既有成片的形态。
                ("subtitle_mode", "TEXT NOT NULL DEFAULT 'soft'"),
                ("output_resolution", "TEXT NOT NULL DEFAULT 'source'"),
            ],
            "drama_tasks": [
                # 关键帧的结算日期：单日上限必须把关键帧算进去，
                # 只按 drama_charge 统计会让关键帧完全逃出日上限。
                ("settled_day", "TEXT NOT NULL DEFAULT ''"),
                ("source_provider", "TEXT NOT NULL DEFAULT ''"),
                # 上游真实成本（关键帧走任务、不走凭据，所以两表都要有）。
                # **可空**：NULL = 未知（老库 / mock / 上游没回传），绝不用 0 顶替——
                # 0 会让「未知」与「免费」混为一谈，对账表最容易在这里骗自己。
                ("upstream_points", "INTEGER"),
                ("upstream_balance", "INTEGER"),
                # 产物类型与关键帧候选张数。老库任务一律补成 keyframe / 1，
                # 与它们当初的行为完全一致（那时只有单张关键帧一种任务）。
                ("kind", "TEXT NOT NULL DEFAULT 'keyframe'"),
                ("candidates", "INTEGER NOT NULL DEFAULT 1"),
            ],
            "drama_exports": [
                ("film_url", "TEXT NOT NULL DEFAULT ''"),
                ("artifact", "TEXT NOT NULL DEFAULT ''"),
                ("ai_label", "TEXT NOT NULL DEFAULT ''"),
                ("ai_disclosure", "TEXT NOT NULL DEFAULT ''"),
                ("sample_label", "TEXT NOT NULL DEFAULT ''"),
            ],
            "drama_charge": [
                # 老库里的凭据没有目标键：一律当「项目级」，不会让历史数据报错，
                # 只是这些老凭据不参与逐镜的「生成中」显示。
                ("target", "TEXT NOT NULL DEFAULT ''"),
                # 上游任务编号（可空）。老库 / mock / 上游没回传一律 NULL；
                # 有编号才允许按编号自动确认结算，绝不回填预算常量或伪造编号。
                ("upstream_task_id", "TEXT DEFAULT NULL"),
                # 这笔凭据是真实上游还是本地模拟。日上限只统计真实消费（用户裁决：
                # 「上限只为防烧上游真钱」），判定必须在预占当时落库：事后算不出来——
                # 同一个 owner 可能当天先跑离线演示再切真实模式。空值视为真实消费，
                # 见 daily_spend 的说明。
                ("source_provider", "TEXT NOT NULL DEFAULT ''"),
                # 上游真实成本与调用后余额。NULL = 未知（老库 / mock / 上游没回传），
                # 绝不用 0 顶替：0 会让「未知」和「免费」混为一谈。
                # 毛利不存列：它是 `cost - upstream_points` 的纯函数，存了会在改价后失真。
                ("upstream_points", "INTEGER"),
                ("upstream_balance", "INTEGER"),
            ],
            "drama_pricing": [
                # 迁移判据与只读标记：老库没有这两列，补上默认值即「未迁移、非 stale」，
                # 随后由 `_migrate_pricing_rows` 按字段独立迁移一次。
                ("migrated_from", "TEXT NOT NULL DEFAULT ''"),
                ("pricing_stale", "INTEGER NOT NULL DEFAULT 0"),
            ],
        }
        for table, columns in additions.items():
            with self._lock:
                have = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
                for name, spec in columns:
                    if name not in have:
                        self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")
        self._migrate_pricing_rows()

    def _migrate_pricing_rows(self) -> None:
        """把老库的旧默认计价按**字段独立**升级一次（幂等）。

        为什么逐字段：管理员当年可能只改过其中一个字段（例如只把 cap 调成 30、
        t2i 留 10），整组判据 `WHERE t2i=10 AND cap=12 AND video IS NULL` 会把这种行
        漏在外面，于是售价停在 10、低于上游实测成本 25，卖一单亏一单。

        幂等靠 `migrated_from` 非空，而不是「当前值等于旧默认」：否则管理员事后把价
        改回 10 会被二次迁移回 20，等于系统替他改了定价。
        """
        with self._lock:
            rows = self.db.execute(
                "SELECT owner,t2i,cap,video,migrated_from FROM drama_pricing"
            ).fetchall()
            for row in rows:
                if str(row["migrated_from"] or "").strip():
                    continue
                t2i, cap, video, marker = migrate_pricing(
                    int(row["t2i"]), int(row["cap"]), normalize_video(row["video"])
                )
                stale = 1 if pricing_stale({"t2i": t2i, "video": video}) else 0
                self.db.execute(
                    "UPDATE drama_pricing SET t2i=?,cap=?,video=?,migrated_from=?,pricing_stale=? "
                    "WHERE owner=?",
                    (t2i, cap, video, marker, stale, row["owner"]),
                )

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def run(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                result = fn(self.db)
                self.db.execute("COMMIT")
                return result
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def read(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        # 必须持锁：读路径同样落在连接上，与写事务并发会触发 C 层段错误。
        with self._lock:
            return fn(self.db)

    # ---------- 初始化 / 重置 ----------
    def ensure_owner(self, owner: str) -> None:
        def op(db: sqlite3.Connection) -> None:
            row = db.execute("SELECT 1 FROM drama_pricing WHERE owner=?", (owner,)).fetchone()
            if row is None:
                self._seed(db, owner)

        self.run(op)

    def _seed(self, db: sqlite3.Connection, owner: str) -> None:
        # 种子实体的 id 曾经是写死的（p-demo / S1..S8 / a1.. / v1..v3），
        # 而这些 id 在各自表里都是**全局**主键：同一个 drama.sqlite 上第二个
        # owner 一开户就会撞主键直接 500。多用户（bearer 模式两个 principal）
        # 必然踩到。这里统一加 owner 后缀，单用户行为与从前完全一致。
        tag = f"-{owner}"
        pid = f"p-demo{tag}"
        frozen_id = f"v3{tag}"
        settings = DEFAULT_SETTINGS
        # 新开户直接按 pricing 单一源落库，并把 migrated_from 标成 seed：
        # 种子行没有「迁移前原值」，留空会被下一次 install() 当成老行再迁一次。
        seeded = default_pricing()
        stale = 1 if pricing_stale(seeded) else 0
        db.execute(
            "INSERT INTO drama_pricing(owner,t2i,cap,qps,concurrency,video,migrated_from,"
            "pricing_stale) VALUES(?,?,?,?,?,?,?,?)",
            (
                owner,
                seeded["t2i"],
                seeded["cap"],
                seeded["qps"],
                seeded["concurrency"],
                normalize_video(seeded["video"]),
                MIGRATED_FROM_SEED,
                stale,
            ),
        )
        db.execute("INSERT INTO drama_ledger VALUES(?,?,?,?)", (owner, 980, 0, 0))
        db.execute(
            "INSERT OR REPLACE INTO drama_owner_state(owner,active_project_id) VALUES(?,?)",
            (owner, pid),
        )
        for tx_id, kind, label, amount, note, created in _TX_SEED:
            # drama_tx.id 是**全局**主键，而种子流水 id 是固定字符串。
            # 直接插会把「第二个 owner 开户」撞成 UNIQUE 冲突（多用户一上来就 500），
            # 因此种子 id 必须带 owner 前缀。
            db.execute(
                "INSERT INTO drama_tx(id,owner,kind,label,amount,note,created_at,seed) "
                "VALUES(?,?,?,?,?,?,?,1)",
                (f"seed-{owner}-{tx_id}", owner, kind, label, amount, note, created),
            )
        shot_ids = [f"S{i + 1}{tag}" for i in range(len(_SHOT_SEED))]
        db.execute(
            "INSERT INTO drama_projects(id,owner,name,season,episode,episode_title,genre,style,"
            "ratio,eps,duration,budget,res,archived,created_at,script_layers,frozen_version_id,"
            "dirty_since_freeze,audio_config,timeline_order,timeline_notes,qa,updated_at,"
            "last_result) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'{}')",
            (
                pid,
                owner,
                "雾港来信",
                1,
                1,
                "雨夜的信",
                settings["genre"],
                settings["style"],
                settings["ratio"],
                settings["eps"],
                settings["duration"],
                settings["budget"],
                settings["res"],
                0,
                "11-09 10:00",
                _j(_LAYERS),
                frozen_id,
                0,
                _j(_AUDIO_SEED),
                _j(shot_ids),
                "[]",
                _j([False, False, False, False]),
                time.time(),
            ),
        )
        for version in (
            ("v3", "v3 · 制作基准", "11-09 18:40", "冻结"),
            ("v2", "v2 · 归档", "11-09 17:02", "第二稿"),
            ("v1", "v1 · 归档", "11-09 15:20", "初稿"),
        ):
            db.execute(
                "INSERT INTO drama_script_versions VALUES(?,?,?,?,?,?)",
                (f"{version[0]}{tag}", pid, version[1], _j(_LAYERS), version[2], version[3]),
            )
        # _ASSET_SEED 元组是 (id, name, sub, seed, voice, note, prompt, locked)，
        # 与 drama_assets 的列顺序不同；这里显式写列名，避免位置错位把 prompt 写进 seed。
        for asset_type, items in _ASSET_SEED.items():
            for item in items:
                db.execute(
                    "INSERT INTO drama_assets(id,project_id,type,name,sub,voice,note,prompt,"
                    "seed,locked) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"{item[0]}{tag}",
                        pid,
                        asset_type,
                        item[1],
                        item[2],
                        item[4],
                        item[5],
                        item[6],
                        int(item[3]),
                        1 if item[7] else 0,
                    ),
                )
        for index, shot in enumerate(_SHOT_SEED):
            db.execute(
                "INSERT INTO drama_shots(id,project_id,seq,dur,size,description,prompt,line,"
                "move,refs,state,dirty,frames) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    shot_ids[index],
                    pid,
                    index,
                    shot[0],
                    shot[1],
                    shot[2],
                    shot[3],
                    shot[4],
                    shot[5],
                    _j(shot[6]),
                    "locked" if index < 4 else "draft",
                    0,
                    "[]",
                ),
            )

    def reset(self, owner: str) -> None:
        def op(db: sqlite3.Connection) -> None:
            project_ids = [
                r["id"] for r in db.execute("SELECT id FROM drama_projects WHERE owner=?", (owner,))
            ]
            for table, column in (
                ("drama_script_versions", "project_id"),
                ("drama_assets", "project_id"),
                ("drama_shots", "project_id"),
                ("drama_exports", "project_id"),
            ):
                db.executemany(f"DELETE FROM {table} WHERE {column}=?", [(p,) for p in project_ids])
            for table in (
                "drama_projects",
                "drama_tasks",
                "drama_tx",
                "drama_ledger",
                "drama_pricing",
                "drama_media",
                "drama_audit",
                "drama_owner_state",
                "drama_charge",
            ):
                # 刻意不含 drama_accounts / drama_sessions / drama_orders：账户、
                # 会话与充值订单是身份 / 账务凭证，不随「重置演示数据」一起清空。
                db.execute(f"DELETE FROM {table} WHERE owner=?", (owner,))
            self._seed(db, owner)

        self.run(op)

    # ---------- 状态快照 ----------
    def state(self, owner: str) -> dict[str, Any]:
        self.ensure_owner(owner)
        return self.read(lambda db: self._state_in(db, owner))

    def _state_in(self, db: sqlite3.Connection, owner: str) -> dict[str, Any]:
        projects: list[dict[str, Any]] = []
        rows = db.execute(
            "SELECT * FROM drama_projects WHERE owner=? ORDER BY created_at,id", (owner,)
        ).fetchall()
        for row in rows:
            pid = row["id"]
            versions = [
                {
                    "id": v["id"],
                    "label": v["label"],
                    "layers": _loads(v["layers"], {}),
                    "createdAt": v["created_at"],
                    "note": v["note"],
                }
                for v in db.execute(
                    "SELECT * FROM drama_script_versions WHERE project_id=? ORDER BY rowid", (pid,)
                )
            ]
            assets: dict[str, list[dict[str, Any]]] = {}
            for asset_type in ("characters", "scenes", "props"):
                assets[asset_type] = [
                    {
                        "id": a["id"],
                        "name": a["name"],
                        "sub": a["sub"],
                        "voice": a["voice"],
                        "note": a["note"],
                        "prompt": a["prompt"],
                        "seed": a["seed"],
                        "locked": bool(a["locked"]),
                        "imageUrl": a["image_url"],
                        # 上游判违规后这里是 True：资产图已清空，但要让页面能说明原因。
                        "auditRejected": bool(a["audit_rejected"]),
                    }
                    for a in db.execute(
                        "SELECT * FROM drama_assets WHERE project_id=? AND type=? ORDER BY rowid",
                        (pid, asset_type),
                    )
                ]
            shots = db.execute(
                "SELECT * FROM drama_shots WHERE project_id=? ORDER BY seq", (pid,)
            ).fetchall()
            shot_list = [
                {
                    "id": s["id"],
                    "dur": s["dur"],
                    "size": s["size"],
                    "desc": s["description"],
                    "prompt": s["prompt"],
                    "line": s["line"],
                    "move": s["move"],
                    "refs": _loads(s["refs"], []),
                    "state": s["state"],
                    "dirty": bool(s["dirty"]),
                    "videoUrl": s["video_url"],
                    "imageUrl": s["image_url"],
                    # 图生视频被判违规后这里是 True：视频 URL 已清空，页面据此说明
                    # 「这次生成未通过审核，已退款」，而不是显示成一个没生成过的空镜头。
                    "auditRejected": bool(s["audit_rejected"]),
                    # 产物历史（最新在前）。原样返回给前端与 export_doc；
                    # `restoreShotMedia` 只认这份列表里的 URL。
                    "mediaHistory": _loads(s["media_history"], []),
                }
                for s in shots
            ]
            frames = {s["id"]: _loads(s["frames"], []) for s in shots}
            exports = [
                {
                    "id": e["id"],
                    "time": e["time"],
                    "name": e["name"],
                    "res": e["res"],
                    "shots": e["shots"],
                    "duration": e["duration"],
                    "filmUrl": e["film_url"],
                    "artifact": e["artifact"],
                    "aiLabel": e["ai_label"],
                    "aiDisclosure": e["ai_disclosure"],
                    "sampleLabel": e["sample_label"],
                }
                for e in db.execute(
                    "SELECT * FROM drama_exports WHERE project_id=? ORDER BY rowid", (pid,)
                )
            ]
            audio = _loads(row["audio_config"], _AUDIO_SEED)
            projects.append(
                {
                    "id": pid,
                    "name": row["name"],
                    "season": row["season"],
                    "episode": row["episode"],
                    "episodeTitle": row["episode_title"],
                    "genre": row["genre"],
                    "style": row["style"],
                    "ratio": row["ratio"],
                    "eps": row["eps"],
                    "duration": row["duration"],
                    "budget": row["budget"],
                    "res": row["res"],
                    "archived": bool(row["archived"]),
                    "createdAt": row["created_at"],
                    "script": {
                        "layers": _loads(row["script_layers"], {}),
                        "dirtySinceFreeze": bool(row["dirty_since_freeze"]),
                        "frozenVersionId": row["frozen_version_id"],
                        "versions": versions,
                    },
                    "assets": assets,
                    "shots": shot_list,
                    "frames": frames,
                    "timeline": {
                        "order": _loads(row["timeline_order"], []),
                        "notes": _loads(row["timeline_notes"], []),
                    },
                    "audio": audio,
                    "qa": _loads(row["qa"], [False] * 4),
                    "exports": exports,
                    "lastResult": _loads(row["last_result"], {}),
                    # 成片地址（项目级，不被后续动作覆盖）
                    "film": _loads(row["film"], {}),
                    # 交付选项：合成时决定软/硬字幕与输出分辨率。放在项目上而不是
                    # 部署配置上，因为同一次部署里不同集可能要交付不同规格。
                    "subtitleMode": row["subtitle_mode"],
                    "outputResolution": row["output_resolution"],
                }
            )
        tasks = [
            {
                "id": t["id"],
                "projectId": t["project_id"],
                "shotId": t["shot_id"],
                "type": t["type"],
                "state": t["state"],
                "percent": t["percent"],
                "reserve": t["reserve"],
                "cost": t["cost"],
                "settled": bool(t["settled"]),
                "picked": t["picked"],
                "frames": _loads(t["frames"], []),
                "idem": t["idem"],
                "chargeId": t["charge_id"],
                "upstreamTaskId": t["upstream_task_id"],
                "note": t["note"],
                "createdAt": t["created_at"],
                "settledDay": t["settled_day"],
                "sourceProvider": t["source_provider"],
                # 上游真值读回来：`replace_in` 是唯一的整档重写入口，这里漏读，
                # 下一次存档就会把刚记下的上游成本洗成 NULL（与 source_provider 同款坑）。
                "upstreamPoints": t["upstream_points"],
                "upstreamBalance": t["upstream_balance"],
                # kind / candidates 同样必须读回来：tick 组装 Provider 请求时要按
                # kind 选 keyframe/image_to_video、按 candidates 填 count。漏读会让
                # 视频任务被当成关键帧出网（同一批预占、完全不同的产物）。
                "kind": t["kind"],
                "candidates": t["candidates"],
            }
            for t in db.execute("SELECT * FROM drama_tasks WHERE owner=? ORDER BY rowid", (owner,))
        ]
        ledger = self.wallet.ledger(db, owner)
        pricing_row = db.execute("SELECT * FROM drama_pricing WHERE owner=?", (owner,)).fetchone()
        media = [
            {
                "id": m["id"],
                "projectId": m["project_id"],
                "kind": m["kind"],
                "label": m["label"],
                "payload": _loads(m["payload"], {}),
                "provider": m["provider"],
                "createdAt": m["created_at"],
            }
            for m in db.execute(
                "SELECT * FROM drama_media WHERE owner=? ORDER BY rowid DESC LIMIT 50", (owner,)
            )
        ]
        uploads = [
            {
                "id": u["id"],
                "kind": u["kind"],
                "name": u["name"],
                "url": u["url"],
                "bytes": u["bytes"],
                "assetType": u["asset_type"],
                "assetId": u["asset_id"],
                "createdAt": u["created_at"],
            }
            for u in db.execute(
                "SELECT * FROM drama_uploads WHERE owner=? ORDER BY rowid", (owner,)
            )
        ]
        active = next((p["id"] for p in projects if not p["archived"]), None)
        saved = db.execute(
            "SELECT active_project_id,credit_limits FROM drama_owner_state WHERE owner=?", (owner,)
        ).fetchone()
        credit_limits = _loads(saved["credit_limits"], {}) if saved else {}
        saved_active = saved["active_project_id"] if saved else None
        if saved_active and any(x["id"] == saved_active and not x["archived"] for x in projects):
            active = saved_active
        return {
            "version": 1,
            "appVersion": "1.0.0",
            "creditLimits": credit_limits,
            "activeProjectId": active or (projects[0]["id"] if projects else None),
            "datasource": "server",
            "pricing": {
                "t2i": pricing_row["t2i"],
                "cap": pricing_row["cap"],
                "qps": pricing_row["qps"],
                "concurrency": pricing_row["concurrency"],
                # video 列是 TEXT affinity：写进去的整数会被存成 "300"。这里归一成
                # int|None，否则管理员把视频价改成 200 会被读成字符串而静默失效。
                "video": normalize_video(pricing_row["video"]),
                # 只读标记：生效价是否已跌破上游成本。读路径直接按当前值重算一遍
                # （而不是只读列），这样导入/手工改库之后标记不会与实际价脱节；
                # 列本身仍会被写入，供 SQL 诊断使用。
                "stale": pricing_stale({"t2i": pricing_row["t2i"], "video": pricing_row["video"]}),
                "migratedFrom": pricing_row["migrated_from"],
            },
            "projects": projects,
            "tasks": tasks,
            "ledger": ledger,
            "media": media,
            # 上传元数据随 state 回包、随 export/import 带走；文件本体在隔离目录。
            "uploads": uploads,
            # 生成动作的扣费凭据必须进快照：守恒自检要按它扣减（settle 流水 amount 恒为 0，
            # 从 drama_tx 反推不出成本），导出/导入也要带着它才自洽。
            "charges": [
                {
                    "id": c["id"],
                    "projectId": c["project_id"],
                    "action": c["action"],
                    "requestId": c["request_id"],
                    "price": c["price"],
                    "cap": c["cap"],
                    "cost": c["cost"],
                    "state": c["state"],
                    "result": _loads(c["result"], None),
                    "note": c["note"],
                    "createdAt": c["created_at"],
                    "day": c["day"],
                    "target": c["target"],
                    # 必须读回来：`replace_in` 是唯一的整档重写入口（存档/导入都走它），
                    # 这里漏读就会把上游任务编号洗成 NULL，待确认凭据再也自动结算不了。
                    "upstreamTaskId": c["upstream_task_id"],
                    # 必须读回来：`replace_in` 是唯一的整档重写入口（存档/导入都走它），
                    # 它按内存里的 charge 字典写库。这里不读，下次存档就会把一条 mock
                    # 凭据洗成「真实消费」（空值），于是重新开始占日上限——表现为
                    # 「同一进程能跑、重启/存档之后就撞 500」这种间歇性故障。
                    "sourceProvider": c["source_provider"],
                    # 同上：不读回来，下一次存档（`replace_in`）会把上游成本洗成 NULL。
                    "upstreamPoints": c["upstream_points"],
                    "upstreamBalance": c["upstream_balance"],
                    # 上游审核拒绝的标记：**不新增 DB 列**。表结构改动会牵动老库迁移、
                    # 导入导出与对账，代价远大于收益；note 里的固定标记随行落库，
                    # 这里按标记派生，读回来、导出导入、重启后都一致。
                    "auditRejected": AUDIT_REJECT_MARK in str(c["note"] or ""),
                }
                for c in db.execute(
                    "SELECT * FROM drama_charge WHERE owner=? ORDER BY rowid", (owner,)
                )
            ],
        }

    # ---------- 导入导出 ----------
    def export_doc(self, owner: str) -> dict[str, Any]:
        # 只导出 _state_in 描述的业务状态。账户哈希 / 会话令牌 / 回调凭证
        # 三张表都不在快照里，因此永远不会随存档离开这台机器。
        state = self.state(owner)
        state["datasource"] = "offline"
        return state

    def import_doc(self, owner: str, doc: dict[str, Any]) -> None:
        if not isinstance(doc, dict) or doc.get("version") != 1:
            raise ValueError("version 必须为 1")
        projects = doc.get("projects")
        ledger = doc.get("ledger")
        if not isinstance(projects, list) or not isinstance(ledger, dict):
            raise ValueError("需要 projects[] 与 ledger")
        if not projects:
            raise ValueError("projects[] 不能为空：至少要有一个项目")
        for project in projects:
            if not isinstance(project, dict) or not str(project.get("id") or ""):
                raise ValueError("每个项目都必须带非空 id")
            if "film" in project and not isinstance(project["film"], dict):
                raise ValueError("projects[].film 必须是对象（{videoUrl, at}）")
            shots = project.get("shots")
            if shots is not None and not isinstance(shots, list):
                raise ValueError("projects[].shots 必须是数组")
            for shot in shots or []:
                if not isinstance(shot, dict):
                    raise ValueError("projects[].shots 每一项都必须是对象")
                history = shot.get("mediaHistory")
                if history is None:
                    continue
                # 产物历史是「可回退地址的唯一白名单」：这里放进来什么 URL，
                # restoreShotMedia 之后就能把镜头写回什么 URL。所以形状必须死守，
                # 不能让导入文件塞进外部地址或任意 kind 去绕过前端校验。
                if not isinstance(history, list):
                    raise ImportFieldError("projects[].shots[].mediaHistory 必须是数组")
                for entry in history:
                    if not isinstance(entry, dict):
                        raise ImportFieldError("projects[].shots[].mediaHistory 每一项都必须是对象")
                    url = entry.get("url")
                    if not isinstance(url, str) or not url or len(url) > MEDIA_HISTORY_URL_MAX:
                        raise ImportFieldError(
                            "projects[].shots[].mediaHistory[].url 必须是长度不超过 "
                            f"{MEDIA_HISTORY_URL_MAX} 的字符串"
                        )
                    if str(entry.get("kind") or "") not in MEDIA_KINDS:
                        raise ImportFieldError(
                            "projects[].shots[].mediaHistory[].kind 只能是 image / video"
                        )
        limits = doc.get("creditLimits")
        if limits is not None:
            if not isinstance(limits, dict):
                raise ValueError("导入文件中的积分上限设置格式不正确，请检查后重试")
            checked_limits: dict[str, int | None] = {}
            for key in ("single", "total", "used"):
                value = limits.get(key)
                if value is None:
                    checked_limits[key] = None
                    continue
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < 0
                    or value > CREDIT_LIMIT_MAX
                ):
                    raise ValueError("导入文件中的积分上限数值不正确，请检查文件内容后重试")
                checked_limits[key] = value
            since = limits.get("since")
            if since is not None and not isinstance(since, str):
                raise ValueError("导入文件中的积分上限启用时间格式不正确，请检查后重试")
            total, used = checked_limits["total"], checked_limits["used"]
            if total is not None and used is not None and used > total:
                raise ValueError("导入文件中累计已用积分不能超过总量上限，请检查后重试")
        # 账本必须先自证合法，否则导入一份「余额随便写」的文档会立刻让后续
        # 每个动作都撞 ledger_invariant 409，等于把库锁死。
        balance = ledger.get("balance")
        frozen = ledger.get("frozen", 0)
        retired = ledger.get("retiredCost", 0)
        checked: dict[str, int] = {}
        for name, value in (("balance", balance), ("frozen", frozen), ("retiredCost", retired)):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"ledger.{name} 必须是非负整数，收到 {value!r}")
            checked[name] = value
        balance, frozen, retired = (
            checked["balance"],
            checked["frozen"],
            checked["retiredCost"],
        )
        tx = ledger.get("tx", [])
        if not isinstance(tx, list):
            raise ValueError("ledger.tx 必须是数组")
        for entry in tx:
            if not isinstance(entry, dict):
                raise ValueError("ledger.tx 每一项都必须是对象")
            amount = entry.get("amount", 0)
            if not isinstance(amount, int) or isinstance(amount, bool):
                raise ValueError("ledger.tx[].amount 必须是整数")
            if not str(entry.get("kind") or ""):
                raise ValueError("ledger.tx[].kind 不能为空")
        uploads = doc.get("uploads", [])
        if not isinstance(uploads, list):
            raise ValueError("uploads 必须是数组")
        for item in uploads:
            if not isinstance(item, dict):
                raise ValueError("uploads 每一项都必须是对象")
            url = str(item.get("url") or "")
            # 导入不能把外部 URL 塞进媒体链路：合成与下载只认本服务签发的本地地址。
            if not re.fullmatch(r"/api/v1/drama/assets/[0-9a-f]{16}", url):
                raise ValueError("uploads[].url 必须是本服务签发的本地素材地址")
        tasks = doc.get("tasks", [])
        if not isinstance(tasks, list):
            raise ValueError("tasks 必须是数组")
        for task in tasks:
            if not isinstance(task, dict):
                raise ValueError("tasks 每一项都必须是对象")
            for key in ("reserve", "cost"):
                value = task.get(key, 0)
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise ValueError(f"tasks[].{key} 必须是非负整数")
            if "settledDay" in task and not isinstance(task["settledDay"], str):
                raise ValueError("tasks[].settledDay 必须是日期字符串")
        charges = doc.get("charges", [])
        if not isinstance(charges, list):
            raise ValueError("charges 必须是数组")
        for charge in charges:
            if not isinstance(charge, dict):
                raise ValueError("charges 每一项都必须是对象")
            for key in ("price", "cap", "cost"):
                value = charge.get(key, 0)
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise ValueError(f"charges[].{key} 必须是非负整数")
            # 上游任务编号：字符串或 null，二者之外一律拒绝。不能让导入文件塞一个
            # 数字 / 对象进来，随后在自动结算里被当成有效编号去查询。
            uid = charge.get("upstreamTaskId")
            if uid is not None and (not isinstance(uid, str) or len(uid) > 256):
                raise ValueError("charges[].upstreamTaskId 必须是字符串或 null")
        expected = (
            SEED_POINTS
            + sum(int(t["amount"]) for t in tx if t.get("kind") == "credit" and not t.get("seed"))
            - sum(int(t.get("cost", 0)) for t in tasks if t.get("settled"))
            - sum(int(c.get("cost", 0)) for c in charges if str(c.get("state") or "") == "settled")
            - int(retired)
        )
        actual = int(balance) + int(frozen)
        if expected != actual:
            raise ValueError(
                f"账本不自洽：按初始 {SEED_POINTS} + 加余额 - 已结算成本 - 已退役成本 "
                f"应为 {expected}，但 balance+frozen = {actual}"
            )
        self.run(lambda db: self.replace_in(db, owner, doc))

    # ---------- 扣费 / 幂等查询 ----------
    def media_count(self, owner: str, project_id: str, kind: str) -> int:
        """按项目统计已留存的素材条数。

        state["media"] 只取最近 50 条，用它数音频会在素材多时少算；
        这里直接打表，口径是「本项目已产出的该类型素材条数」。

        kind 传的是**动作类型**（drama_media.kind 存的就是 action，例如
        "tts-preview"），不是素材容器类型：一张素材属于哪个动作是它的来源，
        容器类型看 payload 里的 assets[].kind。
        """
        row = self._fetchone(
            "SELECT COUNT(*) AS total FROM drama_media WHERE owner=? AND project_id=? AND kind=?",
            (owner, project_id, kind),
        )
        return int(row["total"]) if row is not None else 0

    def charge_by_request(self, owner: str, request_id: str) -> dict[str, Any] | None:
        row = self._fetchone(
            "SELECT * FROM drama_charge WHERE owner=? AND request_id=?",
            (owner, request_id),
        )
        return dict(row) if row is not None else None

    def daily_spend(self, owner: str, day: str) -> int:
        """当天已花**真钱**积分 = 已结算生成动作 + 当天结算的关键帧任务。

        原来只统计 `drama_charge`，关键帧任务完全不进日上限，
        于是「单日上限 60、当天关键帧花 140」还能继续提交，而页面「今日已用」显示 0。
        两笔钱都是真花的，必须进同一个口径。

        用户裁决：**日上限只为防烧上游真钱**，本地模拟（`provider.name == "mock"`）
        既不占额也不被额拦，所以两张表都要排除 `source_provider='mock'` 的行。

        为什么用 `<> 'mock'` 而不是 `= 'remote'`：空值（老库升级、导入的旧数据、
        任何没记身份的凭据）必须**继续计入**。漏算真钱比多算一次严重得多——按
        `= 'remote'` 写的话，老库升级后「今日已用」会突然归零，等于给所有人开了
        一个绕过上限的后门。`'mock'` 是唯一表示本地模拟的名字（见 providers/base.py）。
        """
        charges = self._fetchone(
            "SELECT COALESCE(SUM(cost),0) AS total FROM drama_charge "
            "WHERE owner=? AND day=? AND state='settled' "
            "AND COALESCE(source_provider,'')<>'mock'",
            (owner, day),
        )
        tasks = self._fetchone(
            "SELECT COALESCE(SUM(cost),0) AS total FROM drama_tasks "
            "WHERE owner=? AND settled=1 AND settled_day=? "
            "AND COALESCE(source_provider,'')<>'mock'",
            (owner, day),
        )
        return int(charges["total"] if charges is not None else 0) + int(
            tasks["total"] if tasks is not None else 0
        )

    def project_spend(self, owner: str, project_id: str) -> int:
        """项目维度已花积分 = 已结算关键帧任务成本 + 已结算生成动作成本。"""
        tasks = self._fetchone(
            "SELECT COALESCE(SUM(cost),0) AS total FROM drama_tasks "
            "WHERE owner=? AND project_id=? AND settled=1",
            (owner, project_id),
        )
        charges = self._fetchone(
            "SELECT COALESCE(SUM(cost),0) AS total FROM drama_charge "
            "WHERE owner=? AND project_id=? AND state='settled'",
            (owner, project_id),
        )
        return int(tasks["total"] if tasks is not None else 0) + int(
            charges["total"] if charges is not None else 0
        )

    def owners_with_active_tasks(self) -> list[str]:
        """有 queued / running 任务的 owner 列表，供服务端自动推进使用。

        为什么不让 pump 遍历全部 owner：一个闲置 owner 每轮都做一次完整
        state 读写毫无意义，而这个查询走 drama_tasks(owner,project_id) 索引，
        没有活动任务时就是一次空扫描。
        """
        rows = self._fetch(
            "SELECT DISTINCT owner FROM drama_tasks WHERE state IN ('queued','running','in_flight')"
        )
        return [str(row["owner"]) for row in rows]

    def owners_with_pending_upstream(self) -> list[str]:
        """有待确认凭据、且凭据带真实上游任务编号的 owner 列表。

        供 `DramaService.sweep_pending_charges()` 轻量扫描：没有编号的凭据
        （mock / 老库）没有查询对象，扫了也只会白写一次状态，所以 SQL 里直接
        用 `COALESCE(upstream_task_id,'')<>''` 过滤掉。
        """
        rows = self._fetch(
            "SELECT DISTINCT owner FROM drama_charge WHERE state='pending_review' "
            "AND COALESCE(upstream_task_id,'')<>''"
        )
        return [str(row["owner"]) for row in rows]

    def promote_in_flight_tasks(self, owner: str) -> int:
        """启动兜底：出网期间进程被杀，关键帧任务永久停在 in_flight。

        与 charge 的 in_flight 同一口径 —— 上游可能真的执行了，宁可保留冻结、
        转 held 让管理员带原因释放，也不能默认退款后让用户重试（二次扣上游的钱）。
        """
        note = "服务重启时关键帧生成还没有结果：按「上游结果未知」对待，需管理员复核释放"

        def op(db: sqlite3.Connection) -> int:
            return self._exec(
                "UPDATE drama_tasks SET state='held', percent=0, note=? "
                "WHERE owner=? AND state='in_flight'",
                (note, owner),
            )

        return self.run(op)

    def owners_with_in_flight_tasks(self) -> list[str]:
        rows = self._fetch("SELECT DISTINCT owner FROM drama_tasks WHERE state='in_flight'")
        return [str(row["owner"]) for row in rows]

    def owners_with_in_flight(self) -> list[str]:
        """有「已预占、结果还没落地」凭据的 owner 列表（启动兜底扫描用）。"""
        rows = self._fetch("SELECT DISTINCT owner FROM drama_charge WHERE state='in_flight'")
        return [str(row["owner"]) for row in rows]

    def promote_in_flight(self, owner: str) -> int:
        """把 `in_flight` 凭据转成 `pending_review`，返回处理条数。

        为什么必须扫：`in_flight` 是「钱已经预占、上游可能正在跑」的中间态，
        由两段式动作出网前写下。进程在出网期间被杀，这条凭据会永久停在
        in_flight —— 它不在 pending_review 里，管理页看不到，`resolveCharge`
        也不收，这笔冻结就没人能解。按「结果未知」处理是唯一安全的方向：
        上游可能真的执行了，宁可让管理员确认一次，也不能默认退款后重试。

        金额上不需要动账本：预占那笔本来就在 frozen 里，pending_review 的
        cost 恒为 0，守恒等式两边都不变。
        """
        rows = self._fetch(
            "SELECT id FROM drama_charge WHERE owner=? AND state='in_flight'", (owner,)
        )
        if not rows:
            return 0
        note = "服务重启时这次调用还没有结果：按「上游结果未知」对待，需管理员复核"

        def op(db: sqlite3.Connection) -> int:
            touched = 0
            for row in rows:
                touched += self._exec(
                    "UPDATE drama_charge SET state='pending_review', cost=0, note=? "
                    "WHERE owner=? AND id=? AND state='in_flight'",
                    (note, owner, row["id"]),
                )
            return touched

        return self.run(op)

    def media_rows(self, owner: str) -> list[dict[str, Any]]:
        """该 owner 的素材日志（用于清理时同步删掉对应记录）。"""
        rows = self._fetch(
            "SELECT id, kind, created_at FROM drama_media WHERE owner=? ORDER BY rowid",
            (owner,),
        )
        return [dict(row) for row in rows]

    def forget_media(self, owner: str, media_ids: list[str]) -> int:
        """按 id 删除素材日志行；返回实际删除条数。"""
        if not media_ids:
            return 0
        marks = ",".join("?" for _ in media_ids)
        return self._exec(
            f"DELETE FROM drama_media WHERE owner=? AND id IN ({marks})",
            (owner, *media_ids),
        )

    def replace_in(self, db: sqlite3.Connection, owner: str, doc: dict[str, Any]) -> None:
        """用完整 state 覆盖业务表；drama_media / drama_audit 是累积日志，不清空。

        服务端动作分发：读出 state -> 在内存里执行业务规则 -> 整体写回。
        业务规则确实在服务端执行，同时避免为每个动作重复写一套 SQL。
        数组顺序与 rowid 顺序一一对应，所以 unshift / append 的差异不会丢。
        """
        pids = [
            r["id"] for r in db.execute("SELECT id FROM drama_projects WHERE owner=?", (owner,))
        ]
        for table, column in (
            ("drama_script_versions", "project_id"),
            ("drama_assets", "project_id"),
            ("drama_shots", "project_id"),
            ("drama_exports", "project_id"),
        ):
            db.executemany(f"DELETE FROM {table} WHERE {column}=?", [(p,) for p in pids])
        for table in (
            "drama_projects",
            "drama_tasks",
            "drama_tx",
            "drama_ledger",
            "drama_charge",
            "drama_uploads",
        ):
            db.execute(f"DELETE FROM {table} WHERE owner=?", (owner,))
        for item in doc.get("uploads", []):
            db.execute(
                "INSERT INTO drama_uploads(id,owner,kind,name,url,bytes,asset_type,"
                "asset_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    str(item.get("id") or uuid.uuid4().hex[:12]),
                    owner,
                    str(item.get("kind") or ""),
                    str(item.get("name") or ""),
                    str(item.get("url") or ""),
                    int(item.get("bytes") or 0),
                    str(item.get("assetType") or ""),
                    str(item.get("assetId") or ""),
                    str(item.get("createdAt") or _now()),
                ),
            )
        for project in doc.get("projects", []):
            self._insert_project(db, owner, project)
        ledger = doc.get("ledger") or {}
        db.execute(
            "INSERT INTO drama_ledger VALUES(?,?,?,?)",
            (
                owner,
                int(ledger.get("balance", 0)),
                int(ledger.get("frozen", 0)),
                int(ledger.get("retiredCost") or 0),
            ),
        )
        for tx in ledger.get("tx", []):
            db.execute(
                "INSERT INTO drama_tx(id,owner,kind,label,amount,note,created_at,"
                "project_id,task_id,seed) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    str(tx.get("id", uuid.uuid4().hex[:8])),
                    owner,
                    str(tx.get("kind", "credit")),
                    str(tx.get("label", "")),
                    int(tx.get("amount", 0)),
                    str(tx.get("note", "")),
                    str(tx.get("time", _now())),
                    tx.get("projectId"),
                    tx.get("taskId"),
                    1 if tx.get("seed") else 0,
                ),
            )
        for task in doc.get("tasks", []):
            self._insert_task(db, owner, task)
        for charge in doc.get("charges", []):
            db.execute(
                "INSERT INTO drama_charge(id,owner,project_id,action,request_id,price,cap,"
                "cost,state,result,note,created_at,day,target,upstream_task_id,"
                "source_provider,upstream_points,upstream_balance) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(charge.get("id") or uuid.uuid4().hex[:12]),
                    owner,
                    charge.get("projectId"),
                    str(charge.get("action") or ""),
                    str(charge.get("requestId") or ""),
                    int(charge.get("price", 0)),
                    int(charge.get("cap", 0)),
                    int(charge.get("cost", 0)),
                    str(charge.get("state") or "settled"),
                    _j(charge.get("result")) if charge.get("result") is not None else "",
                    str(charge.get("note") or ""),
                    float(charge.get("createdAt") or 0.0),
                    str(charge.get("day") or ""),
                    str(charge.get("target") or ""),
                    # 存档与导入同一个入口：NULL = 没有真实编号（mock / 老库）。
                    charge.get("upstreamTaskId"),
                    # 存档与导入同一个入口：Provider.name 原值（"mock" / "remote"），
                    # 绝不写厂商名/通道名——`_settle_legacy_tasks` 拿它做等值比较，
                    # 记通道名会把同模式下正当在跑的任务误判成「切换模式遗留」。
                    str(charge.get("sourceProvider") or ""),
                    # 上游真值：NULL = 未知。存档/导入必须原样带走，
                    # 否则「记了又被下一次存档洗掉」，对账永远缺数。
                    charge.get("upstreamPoints"),
                    charge.get("upstreamBalance"),
                ),
            )
        db.execute(
            "INSERT INTO drama_owner_state(owner,active_project_id,credit_limits) VALUES(?,?,?) "
            "ON CONFLICT(owner) DO UPDATE SET active_project_id=excluded.active_project_id,"
            "credit_limits=excluded.credit_limits",
            (owner, doc.get("activeProjectId"), _j(doc.get("creditLimits") or {})),
        )
        pricing = doc.get("pricing") or DEFAULT_PRICING
        # 导入是「用户显式恢复的数据」：只归一化 video、重算 stale，
        # 不跑迁移（迁移是 install() 的升级动作，不能在导入时替用户改价）。
        imported_video = normalize_video(pricing.get("video"))
        db.execute(
            "UPDATE drama_pricing SET t2i=?,cap=?,qps=?,concurrency=?,video=?,pricing_stale=? "
            "WHERE owner=?",
            (
                int(pricing.get("t2i", PRICE_T2I)),
                int(pricing.get("cap", cap_of(PRICE_T2I))),
                float(pricing.get("qps", 1)),
                int(pricing.get("concurrency", 5)),
                imported_video,
                1 if pricing_stale({"t2i": pricing.get("t2i"), "video": imported_video}) else 0,
                owner,
            ),
        )

    def _insert_project(self, db: sqlite3.Connection, owner: str, p: dict[str, Any]) -> None:
        script = p.get("script") or {}
        audio = p.get("audio") or {}
        timeline = p.get("timeline") or {}
        shots = p.get("shots") or []
        shot_ids = [s.get("id", f"s-{i}") for i, s in enumerate(shots)]
        settings = DEFAULT_SETTINGS
        db.execute(
            "INSERT INTO drama_projects(id,owner,name,season,episode,episode_title,genre,style,"
            "ratio,eps,duration,budget,res,archived,created_at,script_layers,frozen_version_id,"
            "dirty_since_freeze,audio_config,timeline_order,timeline_notes,qa,updated_at,"
            "last_result,film,subtitle_mode,output_resolution) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                p.get("id", uuid.uuid4().hex[:8]),
                owner,
                p.get("name", "未命名"),
                int(p.get("season", 1)),
                int(p.get("episode", 1)),
                p.get("episodeTitle", ""),
                p.get("genre", settings["genre"]),
                p.get("style", settings["style"]),
                p.get("ratio", settings["ratio"]),
                int(p.get("eps", 12)),
                int(p.get("duration", 90)),
                int(p.get("budget", 3000)),
                p.get("res", settings["res"]),
                1 if p.get("archived") else 0,
                p.get("createdAt", _now()),
                _j(script.get("layers") or {}),
                script.get("frozenVersionId"),
                1 if script.get("dirtySinceFreeze") else 0,
                _j(audio),
                _j(timeline.get("order") or shot_ids),
                _j(timeline.get("notes") or []),
                _j(p.get("qa") or [False] * 4),
                time.time(),
                _j(p.get("lastResult") or {}),
                _j(p.get("film") or {}),
                # 落库前统一夹一次：`_insert_project` 是种子数据、新建项目与
                # import_doc 三条路的唯一汇合点，在这里夹就不会有人绕过校验
                # 把非法值写进库。非法值退回软字幕 / 不缩放（失败面更小的一侧）。
                parse_subtitle_mode(p.get("subtitleMode")),
                parse_resolution(p.get("outputResolution")),
            ),
        )
        pid = p.get("id")
        for version in script.get("versions", []):
            db.execute(
                "INSERT INTO drama_script_versions VALUES(?,?,?,?,?,?)",
                (
                    version.get("id"),
                    pid,
                    version.get("label", ""),
                    _j(version.get("layers") or {}),
                    version.get("createdAt", _now()),
                    version.get("note", ""),
                ),
            )
        for asset_type in ("characters", "scenes", "props"):
            for a in (p.get("assets") or {}).get(asset_type, []):
                # 显式列名：`drama_assets` 的列数随迁移增加，裸 VALUES 会在加列后错位。
                db.execute(
                    "INSERT INTO drama_assets(id,project_id,type,name,sub,voice,note,prompt,"
                    "seed,locked,image_url,audit_rejected) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        a.get("id"),
                        pid,
                        asset_type,
                        a.get("name", ""),
                        a.get("sub", ""),
                        a.get("voice", ""),
                        a.get("note", ""),
                        a.get("prompt", ""),
                        int(a.get("seed", 1)),
                        1 if a.get("locked") else 0,
                        str(a.get("imageUrl") or ""),
                        1 if a.get("auditRejected") else 0,
                    ),
                )
        frames = p.get("frames") or {}
        for index, s in enumerate(shots):
            db.execute(
                "INSERT INTO drama_shots(id,project_id,seq,dur,size,description,prompt,line,"
                "move,refs,state,dirty,frames,video_url,image_url,audit_rejected,media_history) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    s.get("id"),
                    pid,
                    index,
                    int(s.get("dur", 3)),
                    s.get("size", "中景"),
                    s.get("desc", ""),
                    s.get("prompt", ""),
                    s.get("line", ""),
                    s.get("move", "固定镜头"),
                    _j(s.get("refs") or []),
                    s.get("state", "draft"),
                    1 if s.get("dirty") else 0,
                    _j(frames.get(s.get("id"), [])),
                    str(s.get("videoUrl") or ""),
                    str(s.get("imageUrl") or ""),
                    1 if s.get("auditRejected") else 0,
                    # 写入侧也夹一次长度：导入文件可以带 50 条历史，但「最多 10 条」
                    # 是这一列的语义，不能让超长文档把不变量写坏。
                    _j(_history_of(s.get("mediaHistory"))[:MEDIA_HISTORY_MAX]),
                ),
            )
        for e in p.get("exports", []):
            db.execute(
                "INSERT INTO drama_exports(id,project_id,time,name,res,shots,duration,"
                "film_url,artifact,ai_label,ai_disclosure,sample_label) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    e.get("id"),
                    pid,
                    e.get("time", _now()),
                    e.get("name", ""),
                    e.get("res", ""),
                    int(e.get("shots", 0)),
                    int(e.get("duration", 0)),
                    str(e.get("filmUrl") or ""),
                    str(e.get("artifact") or ""),
                    str(e.get("aiLabel") or ""),
                    str(e.get("aiDisclosure") or ""),
                    str(e.get("sampleLabel") or ""),
                ),
            )

    @staticmethod
    def _insert_task(db: sqlite3.Connection, owner: str, t: dict[str, Any]) -> None:
        db.execute(
            "INSERT INTO drama_tasks(id,project_id,owner,shot_id,type,state,percent,reserve,"
            "cost,settled,picked,frames,idem,charge_id,upstream_task_id,note,created_at,"
            "settled_day,source_provider,upstream_points,upstream_balance,kind,candidates) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                t.get("id"),
                t.get("projectId"),
                owner,
                t.get("shotId"),
                t.get("type", ""),
                t.get("state", "queued"),
                int(t.get("percent", 0)),
                int(t.get("reserve", 0)),
                int(t.get("cost", 0)),
                1 if t.get("settled") else 0,
                t.get("picked"),
                _j(t.get("frames") or []),
                t.get("idem", ""),
                t.get("chargeId", ""),
                t.get("upstreamTaskId"),
                t.get("note", ""),
                t.get("createdAt", _now()),
                t.get("settledDay", ""),
                str(t.get("sourceProvider") or ""),
                # NULL 表示未知，不要用 `or 0` 兜底：0 是「免费」，不是「不知道」。
                t.get("upstreamPoints"),
                t.get("upstreamBalance"),
                # 缺省 keyframe / 1：老调用方不传也能落库，且语义与从前一致。
                str(t.get("kind") or "keyframe"),
                int(t.get("candidates") or 1),
            ),
        )
