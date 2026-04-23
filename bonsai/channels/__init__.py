"""External chat channels (Feishu / WeCom / Telegram / DingTalk).

Each channel is a thin adapter: stores credentials in config.toml under
`[channels.<kind>]`, exposes test() to validate creds against the vendor API,
and a start() entrypoint used by `bonsai channel run`.

The registry is the single source of truth — adding a channel = one class here.
"""
from .registry import KINDS, get_adapter, list_configured

__all__ = ["KINDS", "get_adapter", "list_configured"]
