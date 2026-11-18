# listingtrail — 多家中介站二手房/出租：在售·下定·已售·下架流转、挂牌/成交价历史、跨门店重复挂牌归并。

运行栈：Python 3.14 + 标准库 `sqlite3` + `httpx` + `pytest`（离线，无需联网）

## 运行方式
读 `fixtures/input.json` → `loader` → `normalizer` → 其余处理阶段 → `keyer` → 幂等落 `sqlite`。
```text
make run     # 运行离线流程，输出摄取数量和落库行数
make demo    # 多来源限速 + 批次中断 + 策略更新 + 重启恢复的完整轨迹
make test    # 运行项目测试，包括流水线、幂等性、抓取策略与恢复场景
```

## 数据流与模块边界
每个业务阶段保持单一职责；`pipeline.py` 只负责按顺序调用阶段，不在编排层加入业务判断。
业务模块：`fetch_policy`, `identity`, `statusline`, `renderer`, `snapshot`。
通用模块：`loader / normalizer / keyer / store / pipeline / cli`。

阶段顺序：`normalizer → fetch_policy → identity → renderer → statusline → snapshot → keyer → store`。
每个阶段都能被单独调用；`pipeline` 只传批次上下文（批次号、位置、采集会话、历史房源）。

## 来源级采集策略（`fetch_policy.py`）
按来源显式配置请求方式、条件请求、渲染升级、重试、限速与熔断：
- **请求方式**：`plain` / `render`，可配 `render_on_block` 在普通请求被拦截后升级渲染；
  升级决策写入 `fetch_decision.escalation`（`from`/`to`/`reason`/`next_request`）。
- **条件请求**：普通请求携带 `If-None-Match` / `If-Modified-Since`，`304` 归为 `not_modified`。
- **限速**：按来源计算最小间隔；等待超过最大预算时本次不请求，记为暂时不可用。
- **熔断**：按来源累计失败；开路后默认不发请求，冷却结束按策略允许**半开探测**（可关闭），
  探测与否记录在 `decision.breaker_probe`。
- **失败类别**：区分 `blocked`（被拦截）、`temporarily_unavailable`（暂时不可用）、
  `empty_result`（合法空结果）、`delisted`（来源确认下架）、`not_modified`、`ok`。

策略集版本由内容确定性派生（`policy_set.version`），配置一改版本必变；决策会同时给出
`policy_version` / `decision_version` / `policy_fingerprint`。

来源策略可放在仓库根目录的 `fetch_policy.json`（或用 `LISTINGTRAIL_POLICY` 指向别处），
格式见 `fetch_policy.example.json`；文件缺失时使用内置默认值。`make run` 会用上该配置。

## 状态语义与身份语义
- **暂时失败不得变成下架事实**：`blocked` / `temporarily_unavailable` 不推进状态，
  快照标记 `credible=false`、`delisting_fact=false`，并保留 `last_good_snapshot`。
- **合法空结果 ≠ 下架**：结果页为空只记录“本次未见”，既有状态不变。
- **只有确认下架**（如 404/410、明确移除信号，且策略允许）才产生 `delisted` 事实。
- **挂牌身份稳定**：显式挂牌编号优先并于基线一致；无编号时用“来源 + 详情页 URL”派生，
  不掺入策略版本/失败类别/时间戳。落库时沿用历史主键与 `stable_since`。

## 追踪与恢复（`store.py`）
- `fetch_trace`：每条来源观测一行（决策版本、失败类别、请求方式、恢复位置）。
- `batch_checkpoint`：批次进度与会话状态（熔断/限速/条件请求校验器），中断后可续跑。
- `policy_versions`：策略版本首见/末见，用于识别“配置更新时批次未完成”。
- `events`：`batch_interrupted` / `batch_resumed` / `policy_changed_mid_batch` 只追加不改写。
- 恢复时沿用原批次号续跑，旧记录保持旧身份与状态，新记录使用新策略版本。

## 测试
测试夹具位于 `tests/`：`test_pipeline.py` 覆盖 happy-path 与幂等；`test_fetch_policy.py`
覆盖来源 A/B 差异、失败分类、熔断探测、渲染升级、批次中断与重启恢复、身份不变量。
