# hot-reindex search

一个基于 FastAPI + SQLite 的全文检索服务，核心能力是**在文档持续写入时切换分词规则并热重建索引**：
重建期间读写不中断，对外检索始终使用完整的当前索引，切换是原子的。

## 运行

```bash
docker compose up --build        # 服务监听 :8000，数据在命名卷 search-data 里
```

或本地运行：

```bash
pip install -r requirements.txt
SEARCH_DB=./search.db uvicorn app.main:app --port 8000
```

运行测试：

```bash
pip install -r requirements-dev.txt
pytest tests/
```

> 设计为单进程服务：读租约与重建 worker 是进程内对象，SQLite 本身也只允许单写者。

## 分词与查询

- 词元 = Unicode 字母/数字的连续段（`_`、标点、空白都是分隔符），偏移量为原文**码点**范围。
- 分词规则按索引代（generation）配置：`case_fold`（大小写折叠）与 `stopwords`（停用词表）。
- 停用词**不进入索引，但占据词位**（不压缩原词位）。因此 `"quick the fox"`（`the` 为停用词）
  能匹配原文 `quick the fox`，而 `"quick fox"` 不能——`fox` 在词位 2 而不是 1。
- 查询语法：空白分隔的多个**全词**词元 + 双引号包裹的**相邻词短语**，全部按 AND 组合。
- 空查询、或查询全部落在停用词上 → `400`。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| `PUT` | `/docs/{id}` | 写入/覆盖文档 `{"text": "..."}`，返回版本号 |
| `GET` | `/docs/{id}` | 读文档 |
| `DELETE` | `/docs/{id}` | 删除文档 |
| `GET` | `/search?q=...&limit=` | 检索，返回 `doc_id`、`version` 与码点范围 `spans` |
| `GET` | `/tokenizer` | 当前生效的分词规则与索引代 |
| `POST` | `/index/rebuild` | 启动后台重建，body `{"config": {...}}`（省略则沿用当前规则）；并发冲突返回 `409` |
| `GET` | `/index/rebuild` | 重建状态（阶段、水位线、暂停点、滞后量、各代读者数） |
| `POST` | `/index/rebuild/cancel` | 取消重建（半成品候选索引被清理，绝不切换） |
| `PUT` | `/admin/rebuild/pause-points` | 演练：武装暂停点 `{"points": ["backfill","catchup","switch"]}` |
| `GET` | `/admin/rebuild/pause-points` | 查看已武装/生效的暂停点 |
| `POST` | `/admin/rebuild/resume` | 从暂停点继续 |
| `GET` | `/admin/indexes` | 各索引代状态（building/active/retired/dropped/cancelled） |

## 示例：完整重建期间持续读写

```bash
B=localhost:8000

# 1) 写入一些文档
curl -X PUT $B/docs/1 -H 'content-type: application/json' \
  -d '{"text":"The quick brown fox jumps over the lazy dog"}'
curl -X PUT $B/docs/2 -H 'content-type: application/json' \
  -d '{"text":"Quick foxes are quick"}'

# 2) 全词与短语查询（短语需 URL 编码）
curl "$B/search?q=quick"
curl --get $B/search --data-urlencode 'q="quick brown fox"'

# 3) 用新的分词规则（清空停用词表）启动后台重建——写入口不被锁
curl -X POST $B/index/rebuild -H 'content-type: application/json' \
  -d '{"config":{"case_fold":true,"stopwords":[]}}'

# 4) 重建进行中照常读写：这些写入会同步进入当前索引并记入变更日志
curl -X PUT $B/docs/3 -H 'content-type: application/json' -d '{"text":"the end is near"}'
curl -X DELETE $B/docs/2
curl "$B/search?q=near"          # 立即可查（仍由当前索引 gen 1 服务）

# 5) 轮询直到切换完成
watch -n0.5 "curl -s $B/index/rebuild | jq '{state,gen,active_gen,lag}'"

# 6) 切换后：新规则生效，重建期间确认的写入全部可查，删除不复活
curl $B/tokenizer                # gen 2, stopwords: []
curl "$B/search?q=the"           # 停用词表已空 → 命中 1、3
curl "$B/search?q=foxes"         # 已删除 → 空
```

## 演练：暂停在回填 / 追平 / 切换边界

暂停点用于在关键边界验证并发行为（可持久化，重启后仍然生效）：

```bash
# 武装三个边界（也可在重建进行中动态设置）
curl -X PUT $B/admin/rebuild/pause-points -H 'content-type: application/json' \
  -d '{"points":["backfill","catchup","switch"]}'

curl -X POST $B/index/rebuild -H 'content-type: application/json' \
  -d '{"config":{"case_fold":true,"stopwords":[]}}'

curl $B/index/rebuild            # paused_at: "backfill" —— 候选索引还是空的
curl -X PUT $B/docs/a -H 'content-type: application/json' -d '{"text":"written during backfill"}'
curl -X POST $B/admin/rebuild/resume

curl $B/index/rebuild            # paused_at: "catchup" —— 正在追平变更日志
curl -X DELETE $B/docs/a
curl -X POST $B/admin/rebuild/resume

curl $B/index/rebuild            # paused_at: "switch" —— 切换前最后一刻
curl -X PUT $B/docs/b -H 'content-type: application/json' -d '{"text":"last second write"}'
curl -X POST $B/admin/rebuild/resume

curl $B/index/rebuild            # state: IDLE, active_gen 已切换
curl "$B/search?q=last"          # 切换前确认的写入全部可查
```

**重启演练**：在任意暂停点（或重建进行中）`kill -9` 服务进程，重新启动后服务会从
回填边界安全地继续未完成的重建（候选代回填是幂等的），再次在暂停点停下等待 `resume`；
半成品候选索引绝不会被切换上线。取消（`POST /index/rebuild/cancel`）同理：清理候选代，
保持当前索引不变。

## 设计

### 写路径

每次写入（upsert/delete）在**一个 SQLite 事务**内完成四件事：更新 `documents`、
追加 `change_log`（单调递增 `seq`）、更新**当前**索引代的倒排表、递增版本号。
因此对外服务的当前索引永远是完整的，重建期间写入口只被短事务占用。

### 重建协议（后台 worker）

1. **认领**：条件更新单例 `rebuild_state` 行（`state=IDLE → BACKFILL`），写入 fencing
   token（UUID）、新候选代、`snapshot_seq`（变更日志高水位）。并发重建只有一个胜者，
   其余得到 `409`。
2. **回填（BACKFILL）**：keyset 分页扫描 `documents`，为候选代建立倒排。幂等——
   重启后清空候选代重来。`seq > snapshot_seq` 的变更留给追平阶段。
3. **追平（CATCHUP）**：按 `seq` 顺序回放 `change_log`，水位线 `applied_seq` 每批持久化；
   upsert 重读当前文档文本（幂等收敛），delete 移除候选代倒排。直到没有剩余日志。
4. **切换（SWITCH）**：单个 `BEGIN IMMEDIATE` 事务内——在写锁下回放剩余日志
   （切换前已确认的写入全部进入候选代，删除不复活）、`active_gen` 原子翻转到候选代、
   旧代置为 `retired`、释放重建槽位、截断已应用的日志。崩溃则整体回滚，绝不半切换。

worker 的每个变更步骤都在事务内校验 fencing token；取消或崩溃恢复会吊销 token，
因此半成品候选索引不可能被误切换。

### 崩溃恢复与清理

启动时：若 `rebuild_state` 非 IDLE，则用新 token 接管并从回填边界恢复（`snapshot_seq`
仍有效）；无论何种情况，回收所有既非活跃代也非候选代的倒排数据。取消走同样的清理路径。

### 读一致性与旧索引回收

- 一次查询在**一个 WAL 读事务**内完成：活跃代号、倒排、文档原文来自同一快照，
  检索结果与原文必然属于同一文档版本——不会用旧索引定位新版文字。
- 查询对正在读取的索引代持有**读租约**（进程内计数）；切换后旧代进入 `retired`，
  GC 等待该代读者归零才删除其倒排（WAL 快照隔离同时为迟到的读者兜底）。

### 存储结构（单 SQLite 文件，WAL）

- `documents(doc_id, text, version)` — 文档与版本
- `change_log(seq, doc_id, op, version)` — 变更日志（水位线基准）
- `postings(gen, term, doc_id, positions)` — 各代倒排（positions 为词位 JSON 数组，
  停用词占词位但不入表；查询时按同代规则重切原文得到码点范围）
- `indexes(gen, status, config_json)` — 索引代注册表（每代绑定一份分词规则）
- `meta(key, value)` — `active_gen`、版本计数、武装的暂停点
- `rebuild_state`（单例行）— 状态机、fencing token、`snapshot_seq`/`applied_seq` 水位线、
  暂停点与 `paused_at`

### 目录

```
app/
  tokenizer.py   # Unicode 字母数字连续段分词，大小写折叠/停用词可配
  query.py       # 查询解析：全词 + 相邻词短语，空查询/全停用词拒绝
  index.py       # 倒排建立与匹配（含带词位间隔的短语匹配）
  store.py       # SQLite 连接与事务（单写连接 + WAL 读快照）
  service.py     # 写路径、检索、重建状态机、fencing、暂停演练、恢复与 GC
  main.py        # FastAPI 路由
tests/           # 分词、检索、重建、边界演练、崩溃恢复共 26 个用例
```
