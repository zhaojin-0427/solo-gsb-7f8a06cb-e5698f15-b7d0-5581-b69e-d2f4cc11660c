# Unicode 全文检索服务（FastAPI + SQLite）

持续写入文档时**在线切换分词规则**的全文检索服务：重建索引不阻塞文档写入，
检索始终命中完整的当前索引，切换原子完成，崩溃不误用半成品。

## 能力

- **写入 / 删除 / 查询 / 重建** 的 HTTP API；SQLite（WAL 模式）为唯一存储。
- **词元切分**：Unicode 字母数字（`str.isalnum`）的连续段，天然覆盖
  英文、CJK、数字混排；偏移量为**码点（code-point）偏移**。
- **可配置规则**：大小写折叠（case folding）与停用词表；停用词不建索引、
  不参与查询，但**保留原词位（位置不压缩）**，短语相邻匹配不会跨过停用词。
- **全词查询**与**相邻词短语查询**（查询用双引号包裹）；空查询、无有效词、
  全部为停用词一律 `400` 拒绝。
- 命中返回**文档 ID、文档版本与原文码点区间**，同一次查询的命中与原文
  必然属于同一文档版本（只读快照事务 + 读租约）。
- 重建三阶段：**回填（backfill）→ 追平（catch-up）→ 切换（switch）**；
  变更日志 + 水位线 + 单事务原子切换，保证：
  - 切换前已确认（已提交）的写入，切换后全部可查；
  - 删除不会因回填快照而复活；
  - 重建全程持有当前索引，写入口除每批几百行的短事务外**不被锁住**。
- 同一时刻只允许一个重建（并发请求得到 `409`）；取消丢弃候选索引；
  进程崩溃 / `SIGKILL` 后重启会**安全清理**未切换的半成品，当前索引不受影响；
  旧索引等所有在读请求结束（读租约归零）后才回收。
- 演练入口：重建可暂停在 `backfill` / `catchup` / `switch` 三个边界，
  供并发写删、查询与重启验证，见文末「演练」。

## 启动（Docker Compose）

```bash
docker compose up --build -d
curl -s http://localhost:8000/healthz
```

数据位于命名卷 `fts-data`（容器内 `/data/fts.db`）。

本地开发（无 Docker）：

```bash
python3 -m venv --without-pip .venv && python3 /tmp/get-pip.py  # 视环境而定
pip install -r requirements-dev.txt
FTS_DB_PATH=./data/fts.db uvicorn app.main:app --reload
```

## API

| 方法 & 路径 | 说明 |
| --- | --- |
| `PUT /documents/{id}` | body `{"text": "..."}`，写入/更新文档，同步进当前索引 |
| `DELETE /documents/{id}` | 删除（不存在返回 404），同步从当前索引摘除 |
| `GET /documents/{id}?version=N` | 取原文（可指定版本，用于核对命中间隔） |
| `GET /search?q=...` | 全词、多词 OR（裸写多词）或短语（双引号包裹） |
| `POST /rebuild` | 后台按新规则重建；body 见下 |
| `GET /rebuild` | 重建状态（阶段、进度、暂停点、水位线） |
| `POST /rebuild/resume` / `POST /rebuild/cancel` | 继续 / 取消 |
| `GET /meta` | 当前代、规则、水位线、各代状态、文档数 |
| `GET /healthz` | 健康检查 |

命中结构：

```jsonc
// 单词 GET /search?q=brown
{"query": ["brown"], "kind": "term", "gen": 3, "index_watermark": 42,
 "rules": {"case_fold": true, "stopwords": ["the"]},
 "count": 1, "truncated": false,
 "hits": [{"doc_id": "doc-001", "doc_version": 1,
           "spans": [[10, 15]]}]}             // [起, 止) 码点区间

// 短语 GET /search?q="quick fox"
{"kind": "phrase", ...,
 "hits": [{"doc_id": "...", "doc_version": 1,
           "spans": [[4, 13]],                // 整个短语的区间
           "term_spans": [[4, 9], [10, 13]]}]}
```

## 完整示例：重建期间持续读写

下面的命令在重建**追平边界暂停**，演示切换前后服务始终可读可写。
普通使用无需 `pause_at`，重建会一路跑完。

```bash
B=http://localhost:8000

# 1) 先写一批文档（全部即时可查，当前规则：大小写折叠、无停用词）
for i in $(seq 1 20); do
  curl -sX PUT "$B/documents/doc-$i" \
    -H 'Content-Type: application/json' \
    -d "{\"text\": \"the quick brown fox number $i\"}" >/dev/null
done
curl -s "$B/search?q=QUICK"          # 折叠命中 20 篇
curl -s "$B/search?q=the"            # "the" 还是正常词，命中 20 篇
curl -s "$B/search?q=%22quick%20fox%22"   # 相邻短语命中

# 2) 用新规则启动后台重建，并暂停在 catch-up 边界（演练用）
curl -sX POST "$B/rebuild" -H 'Content-Type: application/json' -d '{
  "rules": {"case_fold": true, "stopwords": ["the", "a"]},
  "pause_at": "catchup"}'
curl -s "$B/rebuild"                 # 轮询直到 "paused": "catchup"

# 3) 重建挂起期间：写入口与查询完全正常（都落在完整的当前索引上）
curl -sX PUT "$B/documents/doc-100" \
  -H 'Content-Type: application/json' \
  -d '{"text": "the late arriving document"}'
curl -sX PUT "$B/documents/doc-1" \
  -H 'Content-Type: application/json' \
  -d '{"text": "the quick brown fox number 1 REWRITTEN"}'
curl -sX DELETE "$B/documents/doc-2"
curl -s "$B/search?q=arriving"       # 立即命中 doc-100
curl -s "$B/search?q=the"            # 仍按旧规则：20 篇（doc-2 已删，规则尚未切换）

# 4) 放行；后台补完最后一段日志并在单事务内原子切换
curl -sX POST "$B/rebuild/resume"
while [ "$(curl -s "$B/rebuild" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("running"))')" = "True" ]; do
  sleep 0.2
done

# 5) 切换后：新规则生效；暂停期间的新增/修改/删除全部一致
curl -s "$B/search?q=the"            # 400：查询只剩停用词，被拒绝
curl -s "$B/search?q=arriving"       # doc-100 可查（切换前已确认的写入）
curl -s "$B/search?q=rewritten"      # doc-1 的新版可查
curl -s "$B/search?q=number" | grep -o 'doc-2' || echo "doc-2 stays deleted"
curl -s "$B/meta"                    # 只剩一个 current 代，旧代已回收
```

取消重建（候选索引被丢弃，当前规则不变）：

```bash
curl -sX POST "$B/rebuild/cancel"
```

## 一致性与切换协议（实现要点）

- **变更日志**：每次 upsert/delete 在更新文档的同一事务里向
  `changes(seq AUTOINCREMENT, doc_id, version, kind)` 追加一行，并把当前代
  水位线推进到该 `seq`；因此「文档变更」与「日志」不会出现事务间裂缝。
- **代（generation）**：每套规则一个完整索引（`tokens` 携带 `gen, term,
  doc_id, doc_version, pos, start, end`）。任一时点只有一个 `current` 代；
  重建目标为 `building`，切换提交后旧代变 `retired`。
- **回填**：在单个 WAL 只读事务里取快照与水位线 `W = max(seq)`，
  按快照中的活动文档逐块（500 篇）分词写入；每块只持有短写事务。
  回填期间提交的删除会在追平阶段通过日志重放摘除，不会复活。
- **追平**：循环重放 `seq > W` 的日志（upsert 先删该文档候选行再重建，
  delete 仅删除），每批之间让出写锁；在 `catchup` 边界可暂停。
- **切换**：暂停点放行后，在**一个 `BEGIN IMMEDIATE` 事务**里重放最后的
  日志尾段并翻转 `active_gen`/水位线、旧代置 retired——要么一起可见，
  要么一起不发生。因此已提交写入不会丢，删除不会复活。
- **查询一致性**：查询先取读租约固定 `gen`，再在单个只读事务里完成检索；
  命中带 `doc_version`，与 `GET /documents/{id}?version=N` 取到的原文一一
  对应，绝不会用旧索引的区间去切新版本文字。
- **旧代回收**：切换后先对旧代关闭新租约，等待所有在读租约归零，再删除
  旧代 token 行并回收无引用的历史版本行。
- **单胜者**：进程内非阻塞锁保证只有一个重建；重复 `POST /rebuild` 得 409。
- **崩溃语义（安全清理）**：未完成的重建在重启时连同其 `building` 代一并
  删除（重建只是旁路数据，当前代直到切换事务提交都不会被改写，所以不存在
  「误切换半成品」）；重启后可重新发起重建。切换已提交而进程在回收前退出时，
  启动时回收遗留的 retired 代。
- **暂停/恢复/取消**：三个屏障点（回填前后边界、追平空闲点、切换前）
  通过事件实现；取消幂等，任何阶段取消都走候选丢弃路径。

## 演练

`scripts/drill.py` 封装了三边界的暂停、并发写删与查询校验，并支持
**暂停期间重启进程**：

```bash
pip install -r requirements-dev.txt   # drill 脚本需要 httpx

# A) 不重启：回填边界
python scripts/drill.py --base-url http://localhost:8000 setup
python scripts/drill.py pause --at backfill
python scripts/drill.py resume
python scripts/drill.py verify

# B) catchup 边界 + 暂停时重启容器（验证崩溃安全清理与提交不丢）
python scripts/drill.py setup
python scripts/drill.py pause --at catchup \
  --restart-cmd "docker compose restart fts"
python scripts/drill.py verify --restarted

# C) switch 边界
python scripts/drill.py setup
python scripts/drill.py pause --at switch
python scripts/drill.py resume
python scripts/drill.py verify
```

测试（含真实子进程 `SIGKILL` 重启用例）：

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

## 已知边界

- 单进程多线程部署（SQLite WAL，写事务串行，读不阻塞写）；多副本部署需要
  以单写者方式编排，本镜像默认为单副本。
- 变更日志保留全部历史（重建追平依赖它）；如需截断，只能在没有重建时
  按当前代水位线做一次带回收的压实。
