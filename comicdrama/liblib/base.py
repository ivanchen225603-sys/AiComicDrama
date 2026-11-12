"""Provider 公共校验与报价。本模块不含任何网络代码，也不读取凭据。"""

from __future__ import annotations

from .contracts import (
    DEFAULT_CAP_MICRO,
    DEFAULT_ESTIMATE_MICRO,
    MAX_PROMPT_CHARS,
    ProviderError,
    Quote,
    size_rejection,
)


class BaseMediaProvider:
    name = "base"

    def validate(self, prompt: str, *, width: int, height: int) -> tuple[str, int, int]:
        """只做本地可判定的拒绝；所有拒绝都明确「上游未执行」，可自动退还。

        尺寸用有界规则（见 `contracts.size_rejection`）而非固定成员白名单：
        产品的合法尺寸来自项目设定（默认 `res = "1080 × 1920 / 24fps"`），是开放集合，
        任何有限枚举都会在真实模式下把整条链路本地拒掉。
        """
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= MAX_PROMPT_CHARS:
            raise ProviderError(
                "prompt_invalid",
                f"prompt must be 1-{MAX_PROMPT_CHARS} characters",
                outcome="rejected",
            )
        reason = size_rejection(width, height)
        if reason is not None:
            raise ProviderError(
                "size_invalid",
                f"unsupported image size: {reason}",
                outcome="rejected",
            )
        return prompt.strip(), width, height

    def quote(self, prompt: str, *, width: int, height: int) -> Quote:
        self.validate(prompt, width=width, height=height)
        return Quote(
            estimated_micro=DEFAULT_ESTIMATE_MICRO,
            cap_micro=DEFAULT_CAP_MICRO,
            note=(
                "本地预占口径（100 积分，硬上限 120 积分）；"
                "LibTV 官方单次计费参数表尚未确认，真实用量需以后续对账为准。"
            ),
        )
