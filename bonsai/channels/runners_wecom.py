"""WeCom runner — placeholder.

WeCom's official inbound flow is **callback URL + signature verification**
(AES + SHA1). It requires a public HTTPS endpoint — there is no Stream SDK
or long-polling API. Running WeCom on a single-user machine therefore needs
a reverse proxy (frp / ngrok / cloudflare tunnel) out of scope for the MVP.

Credential configuration + connectivity test live in registry.py already;
you can bind WeCom in the UI to verify your corp_id/secret, just not
receive messages yet.

If you want WeCom inbound:
  1. Stand up a public HTTPS receiver (e.g. frp → localhost:18533).
  2. In WeCom admin, point the app's "接收消息" URL at it.
  3. Implement the receiver by mirroring runners_feishu.py's structure
     and decoding the AES-encrypted POST bodies.
"""

from __future__ import annotations

from pathlib import Path


def run_wecom(root: Path, cfg, **_kwargs) -> None:  # noqa: ARG001
    raise RuntimeError(
        "WeCom runner is not implemented — it requires a public HTTPS "
        "callback URL. See runners_wecom.py docstring for the path forward."
    )
