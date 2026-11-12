"""静态边界：身份 / 账务凭证不出存档、不随重置清空、不留真实密钥。

这些断言防的是「后来者好心办坏事」：把 accounts / sessions / orders 塞进
export_doc、reset() 的 DELETE 清单，或者在 payments 里写死一个看起来像密钥的
长字符串 —— 任何一条都会让「离线演示数据」变成身份泄漏面。

全部是结构 / 源码断言，不依赖运行顺序；唯一的运行时断言是 install() 后三张表
真实存在。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from comicdrama.providers.mock import MockDramaProvider
from comicdrama.service import DramaService
from tests.conftest import ROOT

DRAMA = ROOT / "comicdrama"
STORE_SOURCE = DRAMA / "store.py"
PAYMENTS_SOURCE = DRAMA / "payments.py"
RESOLVER_SOURCE = DRAMA / "pending_resolver.py"
CORE_JS = ROOT / "comicdrama" / "web" / "js" / "core.js"
IDENTITY_TABLES = ("drama_accounts", "drama_sessions", "drama_orders")

NEW_ERROR_CODES = (
    "account_invalid",
    "account_username_taken",
    "account_credentials_invalid",
    "account_locked",
    "account_session_required",
    "payment_amount_invalid",
    "payment_invalid_request",
    "payment_duplicate_request",
    "payment_request_conflict",
    "payment_order_missing",
    "payment_order_state",
    "payment_signature_invalid",
    "payment_payload_mismatch",
)


@pytest.fixture
def service(tmp_path: Path) -> DramaService:
    svc = DramaService(
        tmp_path / "drama.sqlite",
        MockDramaProvider(),
        admins=frozenset({"local"}),
        autopump=False,
    )
    svc.store.install()
    return svc


def function_body(source: str, header: str) -> str:
    """截取一个方法体：从 header 到下一个同级 def 之前。"""
    match = re.search(re.escape(header) + r"(.*?)\n    def ", source, re.DOTALL)
    assert match is not None, f"源码里找不到 {header}"
    return match.group(1)


# ---------------------------------------------------------------- 存档边界
def test_export_never_carries_identity_or_payment_credentials(service: DramaService):
    doc = service.store.export_doc("local")
    for forbidden in ("accounts", "sessions", "orders"):
        assert forbidden not in doc, f"导出存档不应包含 {forbidden}"

    text = json.dumps(doc, ensure_ascii=False)
    for leaked in ("password_hash", "token_hash", "password", "drama_session"):
        assert leaked not in text, f"导出存档泄漏 {leaked}"


def test_reset_does_not_delete_identity_or_payment_rows():
    source = STORE_SOURCE.read_text(encoding="utf-8")
    body = function_body(source, "def reset(self, owner: str) -> None:")
    block = re.search(r"for table in \((.*?)\):", body, re.DOTALL)
    assert block is not None, "reset() 里应有 for table in (...) 的删除清单"
    listed = block.group(1)
    assert "drama_charge" in listed, "清单解析错了：业务表必须在里面"
    for table in IDENTITY_TABLES:
        assert table not in listed, f"reset() 不能删除身份 / 账务凭证表 {table}"


def test_install_creates_the_three_identity_tables(service: DramaService):
    names = {
        str(row["name"])
        for row in service.store.read(
            lambda db: db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        )
    }
    for table in IDENTITY_TABLES:
        assert table in names, f"install() 之后表 {table} 必须真实存在"


# ---------------------------------------------------------------- 源码边界
def test_payments_module_holds_no_real_secret_literals():
    source = PAYMENTS_SOURCE.read_text(encoding="utf-8")
    assert "sk-" not in source, "不得出现 sk- 形态的密钥字面量"
    long_hex = re.findall(r"\b[0-9a-fA-F]{64}\b", source)
    assert long_hex == [], f"不得出现 64 位 hex 密钥字面量: {long_hex[:3]}"
    assert "COMIC_DRAMA_PAYMENT_SECRET" in source, "密钥只能按环境变量名读取"


def test_remote_resolver_does_not_make_network_calls():
    source = RESOLVER_SOURCE.read_text(encoding="utf-8")
    for token in ("httpx", "urllib", "aiohttp", "requests.get", "requests.post"):
        assert token not in source, f"pending_resolver 里不应出现网络调用 {token}"


def test_ui_has_user_facing_text_for_every_new_error_code():
    source = CORE_JS.read_text(encoding="utf-8")
    block = re.search(r"App\.ERROR_TEXT = \{(.*?)\n\};", source, re.DOTALL)
    assert block is not None, "core.js 里应有 App.ERROR_TEXT 映射"
    table = block.group(1)
    missing = [code for code in NEW_ERROR_CODES if f"{code}:" not in table]
    assert missing == [], f"ERROR_TEXT 缺少错误码: {missing}"


def test_schema_and_migration_both_carry_upstream_task_id():
    source = STORE_SOURCE.read_text(encoding="utf-8")
    schema = re.search(r"_SCHEMA = \((.*?)\n\)", source, re.DOTALL)
    assert schema is not None
    assert "upstream_task_id" in schema.group(1), "_SCHEMA 必须建 upstream_task_id 列"

    migrate = function_body(source, "def _migrate(self) -> None:")
    assert "upstream_task_id" in migrate, "_migrate 必须给老库补 upstream_task_id 列"


# ---------------------------------------------------------------- 导入边界
@pytest.mark.parametrize(
    "bad_task_id",
    [
        pytest.param(123, id="int"),
        pytest.param(3.5, id="float"),
        pytest.param({"nested": "x"}, id="object"),
    ],
)
def test_import_rejects_non_string_upstream_task_ids(service: DramaService, bad_task_id: Any):
    doc = service.store.export_doc("local")
    doc["charges"] = [
        {
            "id": "CH-import-1",
            "projectId": None,
            "action": "image-to-video",
            "requestId": "req-import-1",
            "price": 100,
            "cap": 120,
            "cost": 0,
            "state": "pending_review",
            "result": None,
            "note": "导入的待复核凭据",
            "createdAt": 0,
            "day": "2026-11-12",
            "upstreamTaskId": bad_task_id,
        }
    ]

    with pytest.raises(ValueError) as caught:
        service.store.import_doc("local", doc)
    assert "upstreamTaskId" in str(caught.value), "拒绝原因必须指向该字段"
    assert service.store.state("local")["charges"] == [], "拒绝导入时不能留下半截数据"


def test_import_accepts_a_string_upstream_task_id(service: DramaService):
    doc = service.store.export_doc("local")
    doc["charges"] = [
        {
            "id": "CH-import-2",
            "projectId": None,
            "action": "image-to-video",
            "requestId": "req-import-2",
            "price": 100,
            "cap": 120,
            "cost": 0,
            "state": "pending_review",
            "result": None,
            "note": "导入的待复核凭据",
            "createdAt": 0,
            "day": "2026-11-12",
            "upstreamTaskId": "up-import-1",
        }
    ]

    service.store.import_doc("local", doc)
    charge = service.store.state("local")["charges"][0]
    assert charge["upstreamTaskId"] == "up-import-1"
    assert charge["state"] == "pending_review"
