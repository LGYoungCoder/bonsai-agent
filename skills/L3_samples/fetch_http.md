---
name: fetch_http
keywords: [http, curl, 接口, api, httpx, requests, fetch, 调用接口]
created: 2026-04-22
verified_on: 2026-04-22
---

# 调外部 HTTP 接口

## 何时用

- 用户让"帮我调一下 XX 的接口"
- 拿到一个 URL,要取数据 / 发请求
- 写脚本对接某个 SaaS / 内部服务

## 先探后上

不要上来就循环扒全量。先用 **1 个请求** 摸清楚:

```python
import httpx
r = httpx.get(url, params={...}, timeout=10)
print(r.status_code, r.headers.get('content-type'))
print(r.text[:500])    # 看真实返回,别假设字段
```

关键看:

- `status_code`:非 2xx 先搞清为啥
- `content-type`:JSON / XML / HTML 决定下一步怎么 parse
- 前 500 字:字段名大小写、嵌套结构、是否有 `data` 包装、是否是 `{"code": 0, ...}` 风格

## 安全默认值

```python
httpx.get(url,
          timeout=10,                    # 必写,默认是 None = 永不超时
          follow_redirects=True,         # 多数场景要
          headers={"User-Agent": "bonsai/0.1"})
```

POST JSON:

```python
r = httpx.post(url, json=body, headers={"Authorization": f"Bearer {key}"}, timeout=30)
r.raise_for_status()
return r.json()
```

## 翻页 / 批量

拿到第一页确认结构之后才循环:

```python
out = []
page, total = 1, None
while True:
    r = httpx.get(base, params={"page": page, "size": 50}, timeout=15)
    r.raise_for_status()
    data = r.json()
    out.extend(data["items"])
    total = total or data["total"]
    if len(out) >= total or not data["items"]:
        break
    page += 1
    if page > 100:           # 兜底防死循环
        break
```

## Key 不要硬编码

- 走 `$ref:env:XXX` 或 `os.environ["XXX"]`
- 不要 `print(api_key)` / log 里打完整 key
- 粘贴的 key 检查是否有空格 / BOM(bonsai 的 adapter 会自动清洗,但自己写脚本要注意)

## 常见错

| 症状 | 原因 | 修法 |
|---|---|---|
| 401 / 403 | key 错 / 过期 / 权限不足 | 重新查 key,用 `/models` 或 `/ping` 端点验证 |
| 429 | 限流 | sleep 后重试,或用 exponential backoff |
| 连接超时 | 网络 / 防火墙 / 需要代理 | 看是否要 `proxies={...}` |
| SSL 错 | 中间人代理 / 证书过期 | 临时 `verify=False`(仅限调试,**不要** 上生产) |
| 返回 HTML 不是 JSON | 撞了登录页 / CDN 屏蔽 | 检查 URL + User-Agent,可能需要 cookie |

## 不要做

- ❌ 不设 timeout 的请求
- ❌ 把完整 key / 响应 body 打到日志
- ❌ 没先摸一次就开并发循环
- ❌ 手拼 querystring — 用 `params={}` 让库帮你 urlencode
