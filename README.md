# listingtrail — 多家中介站二手房/出租：在售·下定·已售·下架流转、挂牌/成交价历史、跨门店重复挂牌归并。

运行栈：Python 3.14 + 标准库 `sqlite3` + `httpx` + `pytest`（离线，无需联网）

## 运行方式
读 `fixtures/input.json` → `loader` → `normalizer` → 其余处理阶段 → `keyer` → 幂等落 `sqlite`。
```text
make run          # 运行离线流程，输出 {fed, rows}
make run-pages    # 用离线页面序列 fixtures/pages.json 演示动态页渲染与抽取分类
make test         # 运行流水线、幂等性、动态页编排与缓存边界测试
```
页面序列也可以直接喂给 CLI：`python -m listingtrail.cli --pages fixtures/pages.json --db /tmp/x.db --reset --detailed`。

## 数据流与模块边界
每个业务阶段保持单一职责；`pipeline.py` 只负责按顺序调用阶段，不在编排层加入业务判断。
业务模块：`identity`, `statusline`, `renderer`, `snapshot`, `fetch_policy`。
通用模块：`loader / normalizer / keyer / store / pipeline / cli`。

## 动态房源页：渲染后抽取编排

`fetch_policy → renderer → snapshot` 三段串起"怎么抓 → 抓到什么 → 能不能落库"：

### fetch_policy（抓取方式 + 等待/缓存边界）
- `method`：`static`（直接取 HTML）或 `dynamic`（需渲染/等待异步内容）。
  显式 `fetch_method` / `render_mode` / `fetch_policy.method` 优先；带页面序列的记录默认按
  `dynamic` 处理。
- `timeout_ms`：等待总预算；`retries`：整页重试次数；`cache_ttl_ms`：缓存硬边界；
  `on_partial`：局部成功时 `keep`（默认，保留可用字段）或 `retry`（整页重试）。
- `process(record)` 仍补 `_stage_fetch_policy = "ok"`，原有 happy-path 契约不变。

### renderer（带完成状态的抽取结果）
`render_page(record)` 返回的抽取结果始终带有：

| 字段 | 含义 |
| --- | --- |
| `status` | `complete` / `partial` / `failed` |
| `page_kind` | `first_screen` / `async` / `empty_shell` / `captcha` / `timeout` / `error` |
| `error_kind` | `empty_shell` / `captcha` / `timeout` / `http_error` / `blocked` / `parse_error` / `no_listings` |
| `partial_reason` | `async_unavailable` / `missing_fields` / `blocked` / `parse_error` |
| `listings` / `fields` / `missing_fields` | 已抽取到的可用数据 |
| `waited_ms` / `budget_exceeded` / `attempts` | 等待与重试的可观测边界 |
| `cache_state` | `disabled` / `miss` / `hit` / `invalidated` / `stored` / `refreshed` / `stale` |

关键不变量：

- **HTTP 200 ≠ 已取得房源**。空壳、验证码、超时、错误页一律 `failed`，且 `listings == []`。
- **局部成功保留可用字段**：首屏有房源但异步超时/被拦截时为 `partial`，行会带
  `render_partial = true` 与 `error_kind`，下游可自行决定是否补抓。
- **等待有明确边界**：各步骤 `wait_ms` 累计超过 `timeout_ms` 立即判超时，`waited_ms` 封顶于预算。
- **缓存以 TTL 为硬边界**：命中返回 `cache_state=hit` 且不重新渲染；过期重新渲染并回填
  （`refreshed`）；重新渲染失败则旧条目失效（`invalidated`），错误页不会写入缓存。
  仅当显式开启 `cache_fallback` 时才用最后一份完整缓存兜底（`cache_state=stale`）。
- **重复渲染幂等**：房源按稳定键去重，同一页面序列重复渲染行数不变。

### snapshot（完成状态向下游传播）
`process(record)` 把抽取结果转成快照，`failed` 页面不产生任何行；`partial` 行显式标注
`render_status` / `render_complete` / `render_partial`。需要落库的行通过 `record["_emit"]`
交给 `pipeline`，因此流水线对普通记录（无页面负载）的行为完全不变。

## 离线夹具
`fixtures/pages.json` 覆盖首屏 + 异步成功、首屏局部成功 + 等待超时、验证码、空壳、
重复渲染去重、服务端错误页六种页面序列；`tests/test_renderer.py` 用它们验证行数、
错误分类、缓存失效/恢复与重启后的幂等性。

## 持久化与测试
数据通过项目内 SQLite 存储，使用稳定键执行幂等写入；测试夹具位于项目测试目录，运行方式以 `Makefile` 为准。
