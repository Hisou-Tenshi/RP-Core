# fetch_url

## 何时用
- `search_web` 只给了标题/摘要，需要读正文
- 用户给了具体 https 链接（文档、公告、JSON API、raw 文件）
- 读 GitHub `raw.githubusercontent.com` / `api.github.com`（Actions 里会自动带 `GITHUB_TOKEN`）

## 参数
- `url`（必填）：http(s) 链接
- `format`（可选）：`auto` | `html` | `md` | `txt` | `json`，默认 `auto`
- `max_chars`（可选）：返回字符上限，默认约 28000

## 行为
- `html`：剥标签转纯文本
- `json`：格式化 JSON
- `md` / `txt`：原样返回（过长会截断）
- 单响应体上限约 512 KiB；全局有并发与最小间隔（适配 GHA）

## 限制
- **每轮最多 3 次** `fetch_url`
- 禁止 `file://`、`data:` 等非 http(s)
- 失败就换源或坦白，别死磕同一 URL

## 示例
```json
{"url": "https://example.com/readme.md", "format": "md"}
```

```json
{"url": "https://api.github.com/repos/owner/repo/releases/latest", "format": "json"}
```
