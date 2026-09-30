# 星载参数库 · MVCC 多版本可串行化冻结审计

地面批处理并发提交星载参数事务时，本服务核验各事务的读取版本与最终写入能否解释为同一串行执行，
避免两个彼此独立的安全修改在快照隔离下共同越过联锁。结论经真实 HTTP 接口冻结存储，不可覆盖。

## 能力

- **逐读复算（先核验）**：按“开始前最新已提交版本”（`commit_time < start`）的快照隔离语义，
  为每个读步骤解析应观察版本，与步骤声明的 `observed_value` / `writer` 逐项比对；
  过期读、未知写入者、未初始化键、时间矛盾等作为**输入错误**冻结为 `invalid`。
- **多版本串行化图（后构造）**：核验全部通过后精确构造三类边——
  - `wr` 写读依赖：读步骤观察到的版本写入者 → 读取者；
  - `ww` 写写依赖：同一键版本链上相邻版本，旧写入者 → 新写入者；
  - `rw` 读写反依赖：读取者 → 所读版本的后继版本写入者（写偏差的根源）。
  每条边携带**相关键、读/写步骤序号、关联版本与中文版本依据**。
- **稳定裁决**：
  - 图无环：Kahn 拓扑排序，候选节点按事务标识取最小，返回**一条稳定串行顺序**，
    并按该顺序逐读回放复算，给出最终键值；
  - 图有环：Johnson 枚举全部简单环，返回**边数最少**、同长度按事务标识序列
    （旋转规范化后）字典序最小的闭环，逐边列出依据。
- **冻结语义**：同 `audit_id` 重传相同载荷（内容规范化哈希，字段顺序无关）回放原结论；
  同标识换载荷返回 `409` 拒绝，已冻结记录不被覆盖。
- **页面**：结构化编辑器（≤24 个事务）、三个一键规范样例（过期读 / 写偏差环 / 可串行历史）；
  任何草稿修改立即清除当前展示的旧证据。

## 运行（Docker Compose，宿主机端口可配置）

```bash
# 默认宿主机端口 8080
docker compose up -d app

# 自定义宿主机端口
HOST_PORT=9090 docker compose up -d app
# 或写入 .env（见 .env.example）
```

- 页面： http://localhost:8080/
- 健康检查： http://localhost:8080/healthz （compose 与镜像内均配置 healthcheck）

## verify 单次验收服务

```bash
# compose 中的 verify 服务：等 app 健康后执行，完成即退出，退出码报告验收结果
docker compose --profile verify run --rm verify
echo $?   # 0=通过，1=不通过
```

验收三阶段：

1. **代码测试**：`tests/` 全部 unittest（31 用例：解析校验、逐读复算、wr/ww/rw 图、
   最短环与字典序裁决、三环、读己之所写、冻结存储、HTTP 真实接口）；
2. **镜像构建检查**：宿主有 docker 时执行 `docker compose build` + `docker image inspect`；
   容器内无 docker 时执行等价自检（全量字节码编译、零第三方依赖核验、镜像资源核验）；
3. **HTTP 冒烟**：健康检查、页面、过期读→`invalid`、写偏差→2 边 rw 环、
   可串行→顺序 T1→T2→T3 与回放一致、重放 `replayed`、换载荷 `409` 且记录不变、
   >24 事务 `400`、未知标识 `404`。冒烟每次使用唯一审计标识，可重复运行。

无 docker 的宿主也可直接运行：

```bash
python3 -m app.server &        # 零第三方依赖，仅需 Python 3.10+
python3 verify.py              # 宿主上若有 docker 会做真实镜像构建检查
```

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/audits` | 提交载荷；首次 `outcome=created`，同载荷 `replayed`，换载荷 `409` |
| `GET`  | `/api/audits/<audit_id>` | 取回已冻结结论，不存在 `404` |
| `GET`  | `/healthz` | 健康检查 |
| `GET`  | `/` | 审计页面 |

载荷示例：

```json
{
  "audit_id": "orbit-safety-001",
  "initial": { "oncall_a": 1, "oncall_b": 1 },
  "transactions": [
    {
      "id": "Alice", "start": 10, "commit": 30,
      "steps": [
        {"op": "read",  "key": "oncall_b", "observed_value": 1, "writer": "__initial__"},
        {"op": "write", "key": "oncall_a", "value": 0}
      ]
    }
  ]
}
```

- 写步骤必须含 `value`（任意 JSON 值）；读步骤必须声明 `observed_value`（观察到的值）
  和/或 `writer`（写入者事务标识，初始版本用 `__initial__`）。
- 结论 `status`：`serializable`（含 `serial_order` / `serial_replay`）、
  `cycle`（含 `cycle.nodes` / `cycle.edges`）、`invalid`（含 `errors` 与逐读复算）。

## 目录

```
app/core.py     纯计算：解析、版本链、逐读复算、MV-串行化图、最短环/拓扑裁决
app/store.py    SQLite 冻结存储（规范化哈希、重放/冲突）
app/server.py   标准库 HTTP 服务与 JSON API
app/web/        审计页面（结构化编辑 + 冻结结论展示）
tests/          unittest 代码测试
verify.py       单次验收：代码测试 + 镜像构建检查 + HTTP 冒烟
Dockerfile / docker-compose.yml   镜像与编排（含 healthcheck、verify 服务）
```
