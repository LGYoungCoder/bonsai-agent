"""Channel adapters — credentials + connectivity test per platform.

Runtime chat loop is separate; this module only handles config + binding
verification so the UI can show a clear 绑定 / 未绑定 / 失败 status.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx


@dataclass
class ChannelSpec:
    kind: str
    label: str
    fields: list[dict]       # [{name, label, type, hint?}]
    required: list[str]      # field names that must be non-empty to be "configured"
    login_mode: str = "creds"   # "creds" | "qr"
    docs: str = ""


@dataclass
class TestResult:
    ok: bool
    message: str


KINDS: dict[str, ChannelSpec] = {
    "wechat": ChannelSpec(
        kind="wechat",
        label="微信个人号 (iLink)",
        fields=[
            {"name": "allowed_users", "label": "允许的 from_user_id (逗号分隔)",
             "type": "text", "hint": "空 = 任何给你发消息的人都会触发 agent(不推荐)"},
        ],
        required=[],
        login_mode="qr",
        docs="扫码登录后,在卡片的「收发循环」里按「启动」即可。勾选「开机自启」后,下次 `bonsai serve` 会自动拉起,不再需要命令行。",
    ),
    "feishu": ChannelSpec(
        kind="feishu",
        label="飞书 (Lark)",
        fields=[
            {"name": "app_id", "label": "App ID", "type": "text",
             "hint": "飞书开放平台 → 应用凭证"},
            {"name": "app_secret", "label": "App Secret", "type": "password"},
            {"name": "allowed_users", "label": "允许的 open_id (逗号分隔)",
             "type": "text", "hint": "空或 * 表示不限制"},
        ],
        required=["app_id", "app_secret"],
    ),
    "wecom": ChannelSpec(
        kind="wecom",
        label="企业微信 (WeCom)",
        fields=[
            {"name": "corp_id", "label": "企业 ID", "type": "text"},
            {"name": "agent_id", "label": "应用 AgentId", "type": "text"},
            {"name": "secret", "label": "应用 Secret", "type": "password"},
            {"name": "allowed_users", "label": "允许的 userid (逗号分隔)",
             "type": "text", "hint": "空或 * 表示不限制"},
        ],
        required=["corp_id", "agent_id", "secret"],
    ),
    "telegram": ChannelSpec(
        kind="telegram",
        label="Telegram",
        fields=[
            {"name": "bot_token", "label": "Bot Token", "type": "password",
             "hint": "@BotFather → /newbot"},
            {"name": "allowed_users", "label": "允许的 user_id (逗号分隔)",
             "type": "text", "hint": "留空等于所有人可用(不推荐)"},
        ],
        required=["bot_token"],
    ),
    "dingtalk": ChannelSpec(
        kind="dingtalk",
        label="钉钉",
        fields=[
            {"name": "client_id", "label": "AppKey / Client ID", "type": "text"},
            {"name": "client_secret", "label": "AppSecret / Client Secret", "type": "password"},
            {"name": "allowed_users", "label": "允许的 staff_id (逗号分隔)",
             "type": "text"},
        ],
        required=["client_id", "client_secret"],
    ),
    "qq": ChannelSpec(
        kind="qq",
        label="QQ 机器人 (qq-botpy)",
        fields=[
            {"name": "app_id", "label": "AppID (BotAppID)", "type": "text",
             "hint": "QQ 开放平台 → 应用管理 → 机器人 → AppID"},
            {"name": "app_secret", "label": "AppSecret", "type": "password"},
            {"name": "allowed_users", "label": "允许的 user_openid (逗号分隔)",
             "type": "text", "hint": "空或 * 表示不限制"},
        ],
        required=["app_id", "app_secret"],
        docs="需装 `pip install qq-botpy`。机器人需在 QQ 开放平台申请并通过审核。",
    ),
}


def is_configured(kind: str, cfg: dict) -> bool:
    spec = KINDS.get(kind)
    if not spec:
        return False
    return all((cfg.get(k) or "").strip() for k in spec.required)


def list_configured(channels_cfg: dict) -> list[str]:
    return [k for k, v in (channels_cfg or {}).items()
            if isinstance(v, dict) and v.get("enabled") and is_configured(k, v)]


def get_adapter(kind: str):
    """Return the adapter module for a kind. Lazy import to avoid SDK deps
    unless actually bound."""
    if kind == "feishu":
        return _Feishu
    if kind == "wecom":
        return _WeCom
    if kind == "telegram":
        return _Telegram
    if kind == "dingtalk":
        return _DingTalk
    if kind == "qq":
        return _QQ
    raise KeyError(f"unknown channel kind: {kind}")


# ───────────────────────── Per-adapter tests ──────────────────────────
# Each .test(cfg) → TestResult. Hits the vendor's cheapest auth endpoint.
# No SDK required — plain httpx.

class _Feishu:
    @staticmethod
    def test(cfg: dict) -> TestResult:
        try:
            r = httpx.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": cfg.get("app_id", ""),
                      "app_secret": cfg.get("app_secret", "")},
                timeout=10,
            )
            data = r.json()
            if data.get("code") == 0 and data.get("tenant_access_token"):
                return TestResult(True, f"tenant_access_token 获取成功 (expire {data.get('expire')}s)")
            return TestResult(False, f"飞书返回 code={data.get('code')} msg={data.get('msg')}")
        except Exception as e:
            return TestResult(False, f"请求失败: {e}")


class _WeCom:
    @staticmethod
    def test(cfg: dict) -> TestResult:
        try:
            r = httpx.get(
                "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                params={"corpid": cfg.get("corp_id", ""),
                        "corpsecret": cfg.get("secret", "")},
                timeout=10,
            )
            data = r.json()
            if data.get("errcode") == 0 and data.get("access_token"):
                return TestResult(True, f"access_token 获取成功 (expire {data.get('expires_in')}s)")
            return TestResult(False, f"企业微信返回 errcode={data.get('errcode')} errmsg={data.get('errmsg')}")
        except Exception as e:
            return TestResult(False, f"请求失败: {e}")


class _Telegram:
    @staticmethod
    def test(cfg: dict) -> TestResult:
        token = (cfg.get("bot_token") or "").strip()
        if not token:
            return TestResult(False, "bot_token 为空")
        try:
            r = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
            data = r.json()
            if data.get("ok"):
                u = data.get("result", {})
                return TestResult(True, f"@{u.get('username')} ({u.get('first_name')})")
            return TestResult(False, f"Telegram: {data.get('description')}")
        except Exception as e:
            return TestResult(False, f"请求失败: {e}")


class _DingTalk:
    @staticmethod
    def test(cfg: dict) -> TestResult:
        try:
            r = httpx.post(
                "https://api.dingtalk.com/v1.0/oauth2/accessToken",
                json={"appKey": cfg.get("client_id", ""),
                      "appSecret": cfg.get("client_secret", "")},
                timeout=10,
            )
            data = r.json()
            if data.get("accessToken"):
                return TestResult(True, f"accessToken 获取成功 (expire {data.get('expireIn')}s)")
            return TestResult(False, f"钉钉返回 code={data.get('code')} msg={data.get('message')}")
        except Exception as e:
            return TestResult(False, f"请求失败: {e}")


class _QQ:
    @staticmethod
    def test(cfg: dict) -> TestResult:
        app_id = (cfg.get("app_id") or "").strip()
        app_secret = (cfg.get("app_secret") or "").strip()
        if not app_id or not app_secret:
            return TestResult(False, "app_id / app_secret 为空")
        # QQ 开放平台 token 端点 (沙箱 + 正式同一 URL)
        try:
            r = httpx.post(
                "https://bots.qq.com/app/getAppAccessToken",
                json={"appId": app_id, "clientSecret": app_secret},
                timeout=10,
            )
            data = r.json()
            if data.get("access_token"):
                return TestResult(True,
                    f"access_token 获取成功 (expire {data.get('expires_in')}s)")
            return TestResult(False,
                f"QQ 返回 err={data.get('err_code') or data.get('code')} msg={data.get('message')}")
        except Exception as e:
            return TestResult(False, f"请求失败: {e}")
