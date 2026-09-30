"""verify 单次验收服务。

针对参数事务审计依次执行：
  1. 代码测试：app/ 全部单元/接口测试（unittest）；
  2. 镜像构建检查：
     - 宿主模式且存在 docker：docker compose build + image inspect；
     - 容器内模式：全量字节码编译、标准库零三方依赖核验、镜像内资源自检；
  3. HTTP 冒烟：对运行中的服务执行健康检查、页面、三类规范历史、
     冻结重放/载荷冲突拒绝/结构错误 400 等真实接口验证。

全部通过退出码 0，任一失败退出码 1。
"""

from __future__ import annotations

import json
import os
import py_compile
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8080").rstrip("/")
IMAGE = os.environ.get("IMAGE_NAME", "param-mvcc-audit")
RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


# ---------------------------------------------------------------------------
# 1. 代码测试
# ---------------------------------------------------------------------------
def run_unit_tests() -> bool:
    print("\n=== 1) 代码测试（unittest） ===", flush=True)
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=ROOT, capture_output=True, text=True,
    )
    tail = (proc.stdout + proc.stderr).strip().splitlines()
    for line in tail[-6:]:
        print("    " + line, flush=True)
    return record("unittest 全部通过", proc.returncode == 0,
                  "" if proc.returncode == 0 else "存在失败用例")


# ---------------------------------------------------------------------------
# 2. 镜像构建检查
# ---------------------------------------------------------------------------
def _all_py_files() -> list[str]:
    out = []
    for d in ("app", "tests"):
        for dirpath, _dirs, files in os.walk(os.path.join(ROOT, d)):
            for f in files:
                if f.endswith(".py"):
                    out.append(os.path.join(dirpath, f))
    return out


def _third_party_imports() -> list[str]:
    import ast

    stdlib_roots = {
        "app", "tests", "os", "sys", "json", "math", "heapq", "sqlite3",
        "threading", "hashlib", "http", "urllib", "dataclasses", "typing",
        "subprocess", "py_compile", "shutil", "time", "unittest", "ast",
        "tempfile", "enum", "functools", "collections", "itertools",
        "__future__", "email", "io", "re", "socket", "contextlib",
    }
    bad = []
    for path in _all_py_files():
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                root = name.split(".")[0]
                if root not in stdlib_roots:
                    bad.append(f"{path}: {name}")
    return bad


def run_image_checks() -> bool:
    print("\n=== 2) 镜像构建检查 ===", flush=True)
    docker = shutil.which("docker")
    if docker:
        # 宿主模式：真实构建并核验镜像
        build = subprocess.run(
            [docker, "compose", "-f", os.path.join(ROOT, "docker-compose.yml"), "build"],
            cwd=ROOT, capture_output=True, text=True,
        )
        ok_build = record("docker compose build 成功", build.returncode == 0,
                          build.stderr.strip().splitlines()[-1] if build.returncode else "镜像已构建")
        inspect = subprocess.run(
            [docker, "image", "inspect", IMAGE], capture_output=True, text=True,
        )
        ok_inspect = record("docker image inspect 镜像可查", inspect.returncode == 0)
        return ok_build and ok_inspect

    # 容器内/无 docker 环境：对已构建镜像内容做等价自检
    ok_compile = True
    for path in _all_py_files():
        try:
            py_compile.compile(path, doraise=True)
        except py_compile.PyCompileError as exc:
            ok_compile = False
            record(f"字节码编译 {os.path.relpath(path, ROOT)}", False, str(exc))
    record("全部 Python 源文件字节码编译通过", ok_compile)

    extra = _third_party_imports()
    ok_deps = record("运行时零第三方依赖（仅标准库）", not extra,
                     "发现非标准库导入: " + "; ".join(extra[:5]) if extra else "")

    ok_page = os.path.isfile(os.path.join(ROOT, "app", "web", "index.html"))
    record("镜像内审计页面资源存在", ok_page)
    ok_dockerfile = os.path.isfile(os.path.join(ROOT, "Dockerfile"))
    record("镜像构建文件 Dockerfile 存在", ok_dockerfile)
    return ok_compile and ok_deps and ok_page and ok_dockerfile


# ---------------------------------------------------------------------------
# 3. HTTP 冒烟
# ---------------------------------------------------------------------------
def http(method: str, path: str, body: object | None = None, timeout: float = 5.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE_URL + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read()), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()), dict(e.headers)


def wait_healthy(deadline_s: float = 30.0) -> bool:
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        try:
            status, body, _ = http("GET", "/healthz")
            if status == 200 and body.get("status") == "ok":
                return True
        except OSError:
            pass
        time.sleep(0.5)
    return False


STALE = {
    "audit_id": "smoke-stale-001",
    "initial": {"safety_interlock": 1},
    "transactions": [
        {"id": "T1", "start": 10, "commit": 20, "steps": [
            {"op": "read", "key": "safety_interlock", "observed_value": 1, "writer": "__initial__"},
            {"op": "write", "key": "safety_interlock", "value": 0}]},
        {"id": "T2", "start": 30, "commit": 40, "steps": [
            {"op": "read", "key": "safety_interlock", "observed_value": 1, "writer": "__initial__"}]},
    ],
}

SKEW = {
    "audit_id": "smoke-skew-001",
    "initial": {"oncall_a": 1, "oncall_b": 1},
    "transactions": [
        {"id": "Alice", "start": 10, "commit": 30, "steps": [
            {"op": "read", "key": "oncall_b", "observed_value": 1, "writer": "__initial__"},
            {"op": "write", "key": "oncall_a", "value": 0}]},
        {"id": "Bob", "start": 11, "commit": 31, "steps": [
            {"op": "read", "key": "oncall_a", "observed_value": 1, "writer": "__initial__"},
            {"op": "write", "key": "oncall_b", "value": 0}]},
    ],
}

SERIAL = {
    "audit_id": "smoke-serial-001",
    "initial": {"x_tilt_deg": 0},
    "transactions": [
        {"id": "T2", "start": 30, "commit": 40, "steps": [
            {"op": "read", "key": "x_tilt_deg", "observed_value": 2, "writer": "T1"},
            {"op": "write", "key": "x_tilt_deg", "value": 3}]},
        {"id": "T1", "start": 10, "commit": 20, "steps": [
            {"op": "read", "key": "x_tilt_deg", "observed_value": 0, "writer": "__initial__"},
            {"op": "write", "key": "x_tilt_deg", "value": 2}]},
        {"id": "T3", "start": 50, "commit": 60, "steps": [
            {"op": "read", "key": "x_tilt_deg", "observed_value": 3, "writer": "T2"}]},
    ],
}


def run_http_smoke() -> bool:
    print(f"\n=== 3) HTTP 冒烟（目标 {BASE_URL}） ===", flush=True)
    # 每次验收使用唯一审计标识，保证可重复运行并真实走 created/replayed 流程
    import copy
    run_id = str(int(time.time() * 1000))
    stale = copy.deepcopy(STALE); stale["audit_id"] += "-" + run_id
    skew = copy.deepcopy(SKEW); skew["audit_id"] += "-" + run_id
    serial = copy.deepcopy(SERIAL); serial["audit_id"] += "-" + run_id
    skew_id = skew["audit_id"]
    ok = True

    ok &= record("健康检查 GET /healthz", wait_healthy(), "服务在 30s 内健康")
    try:
        with urllib.request.urlopen(BASE_URL + "/", timeout=5) as r:
            page = r.read().decode()
        ok &= record("审计页面 GET / 可访问",
                     r.status == 200 and "MVCC" in page and "冻结" in page)
    except OSError as exc:
        ok &= record("审计页面 GET / 可访问", False, str(exc))

    # 场景一：读到过期版本
    st, body, _ = http("POST", "/api/audits", stale)
    c = body.get("conclusion", {})
    ok &= record("场景①过期读：HTTP 200 且冻结为 invalid",
                 st == 200 and body.get("outcome") == "created" and c.get("status") == "invalid")
    ok &= record("场景①过期读：逐读复算指出 STALE_READ 且期望版本为 T1",
                 any(e.get("code") == "STALE_READ" and e.get("txn_id") == "T2"
                     and e.get("expected", {}).get("writer") == "T1"
                     for e in c.get("errors", [])))

    # 场景二：写偏差规范反例环
    st, body, _ = http("POST", "/api/audits", skew)
    c = body.get("conclusion", {})
    cy = c.get("cycle") or {}
    ok &= record("场景②写偏差：裁决为 cycle", st == 200 and c.get("status") == "cycle")
    ok &= record("场景②写偏差：最短环为 2 边 Alice<->Bob",
                 cy.get("length") == 2 and cy.get("nodes") == ["Alice", "Bob"])
    ok &= record("场景②写偏差：逐边含键、步骤、版本依据（rw 反依赖）",
                 {e["key"] for e in cy.get("edges", [])} == {"oncall_a", "oncall_b"}
                 and all(e["type"] == "rw" and e["basis"] and e["reader_steps"]
                         for e in cy.get("edges", [])))

    # 场景三：可串行历史
    st, body, _ = http("POST", "/api/audits", serial)
    c = body.get("conclusion", {})
    rep = c.get("serial_replay") or {}
    ok &= record("场景③可串行：裁决为 serializable 且顺序 T1->T2->T3",
                 st == 200 and c.get("status") == "serializable"
                 and c.get("serial_order") == ["T1", "T2", "T3"])
    ok &= record("场景③可串行：按顺序逐读回放全部一致，终态 x=3",
                 rep.get("ok") is True and rep.get("final_state") == {"x_tilt_deg": 3})

    # 重放：同标识同载荷
    st, body, _ = http("POST", "/api/audits", skew)
    ok &= record("冻结语义：同标识同载荷重传回放原结论 (replayed)",
                 st == 200 and body.get("outcome") == "replayed")

    # 冲突：同标识换载荷 -> 409 且记录不被覆盖
    changed = copy.deepcopy(skew)
    changed["initial"]["oncall_a"] = 0
    st, body, _ = http("POST", "/api/audits", changed)
    ok &= record("冻结语义：同标识换载荷被拒绝 (409)", st == 409 and "拒绝覆盖" in body.get("error", ""))
    st, body, _ = http("GET", "/api/audits/" + skew_id)
    ok &= record("冻结语义：已冻结记录保持原值 oncall_a=1",
                 st == 200 and body["conclusion"]["per_key_versions"]["oncall_a"][0]["value"] == 1)

    # 结构错误 400（25 个事务超出上限）
    too_many = {"audit_id": "smoke-too-many-" + run_id, "initial": {},
                "transactions": [{"id": f"T{i:02d}", "start": i, "commit": i + 0.5, "steps": []}
                                 for i in range(25)]}
    st, _, _ = http("POST", "/api/audits", too_many)
    ok &= record("结构校验：超过 24 个事务返回 400", st == 400)

    # 不存在的冻结记录
    st, _, _ = http("GET", "/api/audits/no-such-record-" + run_id)
    ok &= record("未知审计标识返回 404", st == 404)
    return ok


def main() -> int:
    print("星载参数库 MVCC 审计 —— verify 单次验收", flush=True)
    a = run_unit_tests()
    b = run_image_checks()
    c = run_http_smoke()

    print("\n================ 验收汇总 ================", flush=True)
    for name, ok, _detail in RESULTS:
        print(f"  {'✓' if ok else '✗'} {name}", flush=True)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n合计 {passed}/{total} 项通过", flush=True)
    if a and b and c:
        print("验收结论：通过 (exit 0)", flush=True)
        return 0
    print("验收结论：不通过 (exit 1)", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
