"""请求模型与身份。"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
Identifier = Annotated[str, Field(pattern=ID_PATTERN)]


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, strict=True, allow_inf_nan=False
    )
    schema_version: Literal[1] = 1


class Principal(BaseModel):
    """当前请求的身份。owner 是数据隔离的边界：每个账号只看得到自己的项目与素材。"""

    owner: Identifier
