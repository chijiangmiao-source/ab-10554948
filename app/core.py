"""MVCC 多版本可串行化分析核心。

输入一次审计载荷（审计标识、初始键值、至多 24 个事务），输出：

1. 逐读复算：每次读取是否确为事务开始前最新已提交版本（快照隔离语义）；
2. 多版本串行化图：写读(ww→wr)/写写(ww)/读写反依赖(rw)三类边；
3. 裁决：无环给出按事务标识稳定裁决的拓扑串行顺序；
   有环给出边数最少、同长度按事务标识序最小的简单环。

本模块只做纯计算，不涉及 HTTP 与存储，便于单元测试。
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from typing import Any

INITIAL = "__initial__"
MAX_TRANSACTIONS = 24

READ = "read"
WRITE = "write"

# 边类型：wr 写读依赖，ww 写写依赖，rw 读写反依赖
WR = "wr"
WW = "ww"
RW = "rw"


class PayloadShapeError(ValueError):
    """载荷结构层面错误（无法形成可冻结的审计结论），对应 HTTP 400。"""


@dataclass
class Step:
    op: str
    key: str
    no: int  # 1-based 步骤序号
    # write
    value: Any = None
    has_value: bool = False
    # read 声明：observed_value / writer 至少一个
    observed_value: Any = None
    has_observed_value: bool = False
    writer: str | None = None


@dataclass
class Txn:
    tid: str
    start: float
    commit: float
    steps: list[Step] = field(default_factory=list)
    # key -> (value, step_no)，事务内最后一次写入构成其对外版本
    writes: dict[str, tuple[Any, int]] = field(default_factory=dict)


@dataclass
class Version:
    writer: str  # INITIAL 或事务标识
    value: Any
    commit_time: float | None  # 初始版本为 None
    step_no: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "writer": self.writer,
            "value": self.value,
            "commit_time": self.commit_time,
            "step_no": self.step_no,
        }


# ---------------------------------------------------------------------------
# 载荷解析（结构性校验）
# ---------------------------------------------------------------------------

def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise PayloadShapeError(msg)


def parse_payload(payload: Any) -> tuple[str, dict[str, Any], dict[str, Txn]]:
    """解析并做结构性校验，返回 (audit_id, initial, txns_by_id)。

    语义层面的问题（读到过期版本、时间矛盾等）不在此抛出，
    而是作为冻结结论中的 invalid 裁决返回。
    """
    _require(isinstance(payload, dict), "载荷必须是 JSON 对象")
    audit_id = payload.get("audit_id")
    _require(isinstance(audit_id, str) and audit_id.strip() != "", "audit_id 必须是非空字符串")
    _require(len(audit_id) <= 128, "audit_id 长度不得超过 128")

    initial = payload.get("initial", {})
    _require(isinstance(initial, dict), "initial 必须是键值对象")
    for k in initial:
        _require(isinstance(k, str) and k != "", "initial 的键必须是非空字符串")

    raw_txns = payload.get("transactions")
    _require(isinstance(raw_txns, list), "transactions 必须是数组")
    _require(len(raw_txns) <= MAX_TRANSACTIONS, f"事务数量不得超过 {MAX_TRANSACTIONS}")
    _require(len(raw_txns) > 0, "至少需要一个事务")

    txns: dict[str, Txn] = {}
    for i, raw in enumerate(raw_txns):
        where = f"transactions[{i}]"
        _require(isinstance(raw, dict), f"{where} 必须是对象")
        tid = raw.get("id")
        _require(isinstance(tid, str) and tid.strip() != "", f"{where}.id 必须是非空字符串")
        _require(tid != INITIAL, f"{where}.id 不得使用保留标识 {INITIAL}")
        _require(tid not in txns, f"事务标识重复: {tid}")
        start, commit = raw.get("start"), raw.get("commit")
        _require(_is_number(start), f"{where}.start 必须是有限数字")
        _require(_is_number(commit), f"{where}.commit 必须是有限数字")

        raw_steps = raw.get("steps")
        _require(isinstance(raw_steps, list), f"{where}.steps 必须是数组")
        txn = Txn(tid=tid, start=float(start), commit=float(commit))
        for j, rs in enumerate(raw_steps):
            sno = j + 1
            _require(isinstance(rs, dict), f"{where}.steps[{j}] 必须是对象")
            op = rs.get("op")
            _require(op in (READ, WRITE), f"{where}.steps[{j}].op 必须是 read 或 write")
            key = rs.get("key")
            _require(isinstance(key, str) and key != "", f"{where}.steps[{j}].key 必须是非空字符串")
            if op == WRITE:
                _require("value" in rs, f"{where}.steps[{j}] 写步骤必须包含 value")
                st = Step(op=WRITE, key=key, no=sno, value=rs["value"], has_value=True)
                txn.writes[key] = (rs["value"], sno)
            else:
                has_v = "observed_value" in rs
                has_w = "writer" in rs
                _require(has_v or has_w,
                         f"{where}.steps[{j}] 读步骤必须声明 observed_value（观察到的值）或 writer（写入者）")
                _require(not has_w or (isinstance(rs["writer"], str) and rs["writer"] != ""),
                         f"{where}.steps[{j}].writer 必须是非空字符串")
                st = Step(
                    op=READ, key=key, no=sno,
                    observed_value=rs.get("observed_value"), has_observed_value=has_v,
                    writer=rs["writer"] if has_w else None,
                )
            txn.steps.append(st)
        txns[tid] = txn
    return audit_id, dict(initial), txns


# ---------------------------------------------------------------------------
# 版本链与逐读复算
# ---------------------------------------------------------------------------

def _build_version_chains(
    initial: dict[str, Any], txns: dict[str, Txn]
) -> tuple[dict[str, list[Version]], list[dict[str, Any]]]:
    """构造每个键的已提交版本链，返回 (chains, 语义错误列表)。"""
    errors: list[dict[str, Any]] = []
    keys = set(initial)
    for t in txns.values():
        keys.update(t.writes)

    chains: dict[str, list[Version]] = {}
    for key in keys:
        chain: list[Version] = []
        if key in initial:
            chain.append(Version(INITIAL, initial[key], None, None))
        raw_writers = [
            (t.commit, tid, t.writes[key][0], t.writes[key][1])
            for tid, t in txns.items()
            if key in t.writes
        ]
        raw_writers.sort(key=lambda c: (c[0], c[1]))
        prev_commit: float | None = None
        for commit, tid, value, step_no in raw_writers:
            if prev_commit is not None and commit == prev_commit:
                errors.append({
                    "code": "AMBIGUOUS_COMMIT_ORDER",
                    "key": key,
                    "commit_time": commit,
                    "message": f"键 {key} 上存在提交时刻同为 {commit} 的多个写入者，版本先后无法裁定",
                })
            chain.append(Version(tid, value, commit, step_no))
            prev_commit = commit
        chains[key] = chain
    return chains, errors


def _snapshot_version(chain: list[Version], start: float) -> Version | None:
    """严格“开始前”最新已提交版本：commit_time < start。"""
    found: Version | None = None
    for v in chain:
        if v.commit_time is None or v.commit_time < start:
            found = v
        else:
            break
    return found


def analyze(payload: Any) -> dict[str, Any]:
    """执行完整分析，返回可序列化/可冻结的结论字典。"""
    audit_id, initial, txns = parse_payload(payload)

    errors: list[dict[str, Any]] = []
    for tid, t in txns.items():
        if t.start >= t.commit:
            errors.append({
                "code": "INVALID_TIME_RANGE",
                "txn_id": tid,
                "start": t.start,
                "commit": t.commit,
                "message": f"事务 {tid} 的开始时刻({t.start})必须严格早于提交时刻({t.commit})",
            })

    chains, chain_errors = _build_version_chains(initial, txns)
    errors.extend(chain_errors)

    # ---------------- 逐读复算 ----------------
    read_reports: list[dict[str, Any]] = []
    for tid in sorted(txns):
        t = txns[tid]
        own_written: set[str] = set()
        for st in t.steps:
            if st.op == WRITE:
                own_written.add(st.key)
                continue
            rec: dict[str, Any] = {
                "txn_id": tid,
                "step_no": st.no,
                "key": st.key,
                "declared": {},
                "resolved": None,
                "read_your_writes": False,
                "ok": False,
                "basis": "",
            }
            if st.has_observed_value:
                rec["declared"]["observed_value"] = st.observed_value
            if st.writer is not None:
                rec["declared"]["writer"] = st.writer

            chain = chains.get(st.key, [])

            # 声明的写入者必须是已知事务或初始版本
            declared_writer_exists = True
            if st.writer is not None and st.writer != INITIAL and st.writer not in txns:
                declared_writer_exists = False
                errors.append({
                    "code": "UNKNOWN_WRITER",
                    "txn_id": tid,
                    "step_no": st.no,
                    "key": st.key,
                    "declared_writer": st.writer,
                    "message": f"事务 {tid} 第 {st.no} 步声明的写入者 {st.writer} 不存在",
                })

            if st.key in own_written:
                # 快照隔离下的读己之所写：观察到本事务此前的写入版本
                value, wstep = t.writes[st.key]
                resolved = Version(tid, value, t.commit, wstep)
                rec["read_your_writes"] = rec["ok"] = True
                rec["resolved"] = resolved.to_dict()
                rec["basis"] = f"读己之所写：解析为事务 {tid} 第 {wstep} 步写入的版本"
            else:
                resolved = _snapshot_version(chain, t.start)
                if resolved is None:
                    rec["basis"] = f"键 {st.key} 无初始值且在开始时刻 {t.start} 前没有任何已提交版本"
                    errors.append({
                        "code": "READ_UNINITIALIZED_KEY",
                        "txn_id": tid,
                        "step_no": st.no,
                        "key": st.key,
                        "message": rec["basis"],
                    })
                else:
                    rec["resolved"] = resolved.to_dict()
                    ok = True
                    reasons = []
                    if st.has_observed_value and st.observed_value != resolved.value:
                        ok = False
                        reasons.append(
                            f"声明观察值 {st.observed_value!r}，开始前最新已提交版本值为 {resolved.value!r}"
                        )
                    if st.writer is not None and declared_writer_exists and st.writer != resolved.writer:
                        ok = False
                        reasons.append(
                            f"声明写入者 {st.writer}，开始前最新已提交版本的写入者为 {resolved.writer}"
                        )
                    rec["ok"] = ok
                    vis = "初始版本" if resolved.writer == INITIAL else (
                        f"事务 {resolved.writer} 在时刻 {resolved.commit_time} 提交的版本（第 {resolved.step_no} 步写入）"
                    )
                    rec["basis"] = (
                        f"开始时刻 {t.start} 的快照中，键 {st.key} 最新已提交版本为{vis}"
                        + ("；读取声明与版本链一致" if ok else "；" + "；".join(reasons))
                    )
                    if not ok:
                        errors.append({
                            "code": "STALE_READ",
                            "txn_id": tid,
                            "step_no": st.no,
                            "key": st.key,
                            "declared": rec["declared"],
                            "expected": resolved.to_dict(),
                            "message": f"事务 {tid} 第 {st.no} 步读取键 {st.key} 未读到开始前最新已提交版本："
                                       + "；".join(reasons),
                        })
            read_reports.append(rec)

    result: dict[str, Any] = {
        "audit_id": audit_id,
        "status": "invalid",
        "errors": errors,
        "per_key_versions": {
            key: [v.to_dict() for v in chains[key]]
            for key in sorted(chains)
        },
        "reads": read_reports,
        "graph": None,
        "serial_order": None,
        "serial_replay": None,
        "cycle": None,
        "summary": "",
    }

    if errors:
        result["status"] = "invalid"
        result["summary"] = (
            f"核验未通过：发现 {len(errors)} 处输入错误（如读取过期版本），不构造串行化图。"
        )
        return result

    # ---------------- 构造多版本串行化图 ----------------
    edges: dict[tuple[str, str, str, str], dict[str, Any]] = {}

    def add_edge(kind: str, u: str, v: str, key: str,
                 version: Version, reader_steps: list[int], writer_steps: list[int],
                 basis: str) -> None:
        if u == v:
            return  # 自环（读己之所写等）不参与跨事务裁决
        ek = (kind, u, v, key)
        e = edges.get(ek)
        if e is None:
            edges[ek] = {
                "type": kind,
                "from": u,
                "to": v,
                "key": key,
                "version": version.to_dict(),
                "reader_steps": sorted(set(reader_steps)),
                "writer_steps": sorted(set(writer_steps)),
                "basis": basis,
            }
        else:
            e["reader_steps"] = sorted(set(e["reader_steps"]) | set(reader_steps))
            e["writer_steps"] = sorted(set(e["writer_steps"]) | set(writer_steps))

    ids = sorted(txns)

    # wr：读步骤解析到的版本由另一事务写入 => 写入者 -> 读取者
    for rec in read_reports:
        rv = rec["resolved"]
        if rv is None or rv["writer"] == INITIAL or rv["writer"] == rec["txn_id"]:
            continue
        add_edge(
            WR, rv["writer"], rec["txn_id"], rec["key"],
            Version(rv["writer"], rv["value"], rv["commit_time"], rv["step_no"]),
            [rec["step_no"]], [rv["step_no"]],
            f"事务 {rec['txn_id']} 第 {rec['step_no']} 步读取键 {rec['key']} 时，"
            f"观察到事务 {rv['writer']} 第 {rv['step_no']} 步写入、时刻 {rv['commit_time']} 提交的版本，"
            f"故 {rv['writer']} 必须先于 {rec['txn_id']}",
        )

    for key in sorted(chains):
        chain = chains[key]
        writers = [v for v in chain if v.writer != INITIAL]
        # ww：同一键相邻版本的写入者按提交时刻先后 => 旧 -> 新
        for a, b in zip(writers, writers[1:]):
            add_edge(
                WW, a.writer, b.writer, key, b,
                [], [s for s in (a.step_no, b.step_no) if s is not None],
                f"键 {key} 上事务 {a.writer}（时刻 {a.commit_time} 提交）的版本被"
                f"事务 {b.writer}（时刻 {b.commit_time} 提交）的版本覆盖，故 {a.writer} 必须先于 {b.writer}",
            )
        # rw：读取者读到某版本，而另一事务写入该键的后继版本 => 读取者 -> 后继写入者
        for rec in read_reports:
            if rec["key"] != key or rec["resolved"] is None:
                continue
            reader = rec["txn_id"]
            rw_writer = rec["resolved"]["writer"]
            idx = next(
                (i for i, v in enumerate(chain)
                 if v.writer == rw_writer
                 and (rw_writer == INITIAL or v.step_no == rec["resolved"]["step_no"])),
                None,
            )
            if idx is None or idx + 1 >= len(chain):
                continue
            nxt = chain[idx + 1]
            if nxt.writer == reader:
                continue
            own = "（读己之所写版本）" if rec["read_your_writes"] else ""
            add_edge(
                RW, reader, nxt.writer, key, nxt,
                [rec["step_no"]], [nxt.step_no] if nxt.step_no is not None else [],
                f"事务 {reader} 第 {rec['step_no']} 步{own}读取键 {key} 的版本后，"
                f"事务 {nxt.writer} 第 {nxt.step_no} 步写入的版本成为其后继，"
                f"反依赖要求 {reader} 必须先于 {nxt.writer}",
            )

    adj: dict[str, set[str]] = {tid: set() for tid in ids}
    for (_, u, v, _key) in edges:
        adj[u].add(v)

    graph = {
        "nodes": ids,
        "edges": [edges[k] for k in sorted(edges, key=lambda e: (e[0], e[1], e[2], e[3]))],
    }
    result["graph"] = graph

    cycles = _find_simple_cycles(ids, adj)
    if cycles:
        def canonical(cyc: list[str]) -> tuple[str, ...]:
            t = tuple(cyc)
            i = min(range(len(t)), key=lambda p: t[p])
            return t[i:] + t[:i]

        best_raw = min(cycles, key=lambda c: (len(c), canonical(c)))
        rot = canonical(best_raw)
        cyc_edges = []
        for i, u in enumerate(rot):
            v = rot[(i + 1) % len(rot)]
            # 同一起止可能因不同键存在多条边，全部列出
            cyc_edges.extend(
                e for e in graph["edges"] if e["from"] == u and e["to"] == v
            )
        result["status"] = "cycle"
        result["cycle"] = {
            "length": len(rot),
            "nodes": list(rot),
            "edges": cyc_edges,
        }
        result["summary"] = (
            f"多版本串行化图存在环：最短闭环含 {len(rot)} 条边，"
            f"事务序列 {' -> '.join(rot)} -> {rot[0]}，历史不可串行化（典型为写偏差/快照隔离反例）。"
        )
        return result

    # ---------------- 无环：稳定拓扑裁决 + 串行回放复算 ----------------
    order = _topo_order_stable(ids, adj)
    result["serial_order"] = order

    state: dict[str, Any] = dict(initial)
    replay_reads: list[dict[str, Any]] = []
    replay_ok = True
    for tid in order:
        local: dict[str, Any] = {}
        for st in txns[tid].steps:
            if st.op == WRITE:
                local[st.key] = st.value
            else:
                val = local.get(st.key, state.get(st.key))
                expect = next(
                    r for r in read_reports
                    if r["txn_id"] == tid and r["step_no"] == st.no
                )
                ok = expect["resolved"] is not None and val == expect["resolved"]["value"]
                replay_ok &= ok
                replay_reads.append({
                    "txn_id": tid, "step_no": st.no, "key": st.key,
                    "value": val, "matches_declared": ok,
                })
        state.update(local)
    result["serial_replay"] = {
        "order": order,
        "ok": replay_ok,
        "final_state": state,
        "reads": replay_reads,
    }
    result["status"] = "serializable"
    result["summary"] = (
        f"多版本串行化图无环，历史可串行化；稳定裁决串行顺序为 {' -> '.join(order)}，"
        f"按该顺序逐步回放，全部读取复算{'一致' if replay_ok else '不一致'}。"
    )
    return result


# ---------------------------------------------------------------------------
# 图算法
# ---------------------------------------------------------------------------

def _topo_order_stable(ids: list[str], adj: dict[str, set[str]]) -> list[str]:
    """Kahn 拓扑排序，候选节点间按事务标识取最小，保证裁决稳定可复现。"""
    indeg = {tid: 0 for tid in ids}
    for u in ids:
        for v in adj[u]:
            indeg[v] += 1
    heap = [tid for tid in ids if indeg[tid] == 0]
    heapq.heapify(heap)
    order: list[str] = []
    while heap:
        u = heapq.heappop(heap)
        order.append(u)
        for v in sorted(adj[u]):
            indeg[v] -= 1
            if indeg[v] == 0:
                heapq.heappush(heap, v)
    return order


def _tarjan_scc(nodes: set[str], adj: dict[str, set[str]]) -> list[set[str]]:
    index = 0
    stack: list[str] = []
    on: set[str] = set()
    idx: dict[str, int] = {}
    low: dict[str, int] = {}
    comps: list[set[str]] = []

    def strong(v: str) -> None:
        nonlocal index
        idx[v] = low[v] = index
        index += 1
        stack.append(v)
        on.add(v)
        for w in adj[v]:
            if w not in nodes:
                continue
            if w not in idx:
                strong(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], idx[w])
        if low[v] == idx[v]:
            comp: set[str] = set()
            while True:
                w = stack.pop()
                on.discard(w)
                comp.add(w)
                if w == v:
                    break
            comps.append(comp)

    for v in sorted(nodes):
        if v not in idx:
            strong(v)
    return comps


def _find_simple_cycles(ids: list[str], adj: dict[str, set[str]]) -> list[list[str]]:
    """Johnson 算法枚举全部简单环（节点数 <= 24，递归深度安全）。"""
    cycles: list[list[str]] = []
    pos = 0
    while pos < len(ids):
        sub_nodes = set(ids[pos:])
        comps = _tarjan_scc(sub_nodes, adj)
        target: tuple[str, set[str]] | None = None
        for comp in comps:
            cyclic = len(comp) > 1 or any(v in adj[v] for v in comp)
            if cyclic:
                m = min(comp)
                if target is None or m < target[0]:
                    target = (m, comp)
        if target is None:
            break
        start, comp = target
        blocked: set[str] = set()
        blocked_map: dict[str, set[str]] = {u: set() for u in comp}
        path: list[str] = []

        def unblock(u: str) -> None:
            todo = [u]
            while todo:
                x = todo.pop()
                if x in blocked:
                    blocked.discard(x)
                    todo.extend(blocked_map[x])
                    blocked_map[x].clear()

        def circuit(v: str) -> bool:
            found = False
            path.append(v)
            blocked.add(v)
            for w in sorted(adj[v]):
                if w not in comp:
                    continue
                if w == start:
                    cycles.append(path[:])
                    found = True
                elif w not in blocked:
                    if circuit(w):
                        found = True
            if found:
                unblock(v)
            else:
                for w in adj[v]:
                    if w in comp:
                        blocked_map[w].add(v)
            path.pop()
            return found

        circuit(start)
        pos = ids.index(start) + 1
    return cycles
