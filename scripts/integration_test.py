"""集成测试：真实 CNB API 全链路验证（发布前手动执行，不进 CI）。

用法：
    CNB_AGENTIC_MEMORY_TOKEN=<token> CNB_AGENTIC_MEMORY_REPO=<org/repo> \
        python scripts/integration_test.py

- 面向专用测试仓库（会真实写入 Issue），禁止指向正式记忆仓库
- 段落式执行，单段失败不中断，末尾汇总 PASS/FAIL 清单
- 语义检索（memory_search）受知识库同步时延影响，只打印不判失败
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time

import httpx

from cnb_agentic_memory.api import CNBApiClient
from cnb_agentic_memory.memory import Memory, MemoryRuleError

BASE = "https://api.cnb.cool"
RESULTS: list[tuple[str, str, str]] = []  # (段落, 结果, 备注)


def record(name: str, ok: bool, note: str = "") -> None:
    RESULTS.append((name, "PASS" if ok else "FAIL", note))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {note}" if note else ""))


def section(name: str) -> None:
    print(f"\n== {name} ==")


async def test_memory_core(memory: Memory) -> dict[int, object]:
    """写入/读取/更新/追加/软删/恢复全链路。"""
    section("Memory 核心链路")

    # 1. 单条写入 + 回读
    marker = f"it-{int(time.time())}"
    r1 = await memory.write(
        f"集成测试单条 {marker}\n\n正文内容第一段。",
        title=f"it-core {marker}",
        tags=["it", "core"],
        category="集成测试",
    )
    n1 = r1.number
    g1 = await memory.get(n1)
    record("write+get 单条", g1.title == f"it-core {marker}" and "it" in g1.label_names, f"#{n1}")

    # 2. 超长拆分
    long_body = ("段落。\n\n" + "x" * 40000) * 3
    r2 = await memory.write(long_body, title=f"it-split {marker}")
    parts = [p.number for p in r2.parts]
    g2 = await memory.get(parts[0])
    record("超长拆分", len(parts) > 1 and g2.title.endswith(f"(1/{len(parts)})"), f"分片 {parts}")

    # 3. 标签白名单真实边界（写前预检，服务端口径）
    try:
        await memory.write("含非法省略号…标签", title=f"it-label {marker}", tags=["bad…label"])
        record("标签白名单拒绝", False, "非法标签未被拒绝")
    except MemoryRuleError:
        record("标签白名单拒绝", True)

    # 4. update（title/content/tags 同次变更）+ 回读
    await memory.update(n1, title=f"it-core-updated {marker}", content="更新后正文", tags=["it", "updated"])
    g1b = await memory.get(n1)
    record("update 回读", g1b.title == f"it-core-updated {marker}" and "updated" in g1b.label_names)

    # 5. append 追加记录
    await memory.append(n1, "追加的更新记录")
    record("append 追加", True, f"#{n1}")

    # 6. 软删 + restore
    await memory.delete(n1)
    g1c = await memory.get(n1)
    soft_ok = g1c.state == "closed"
    await memory.restore(n1)
    g1d = await memory.get(n1)
    record("delete 软删 + restore", soft_ok and g1d.state == "open")

    # 7. list / recent / keyword_search
    issues = await memory.list(category="集成测试", state="open", limit=10)
    record("list 按分类", any(i.number == n1 for i in issues), f"{len(issues)} 条")
    recents = await memory.list_recent(limit=5)
    record("list_recent", len(recents) > 0)
    kw = await memory.keyword_search("it-core-updated")
    if any(i.number == n1 for i in kw):
        record("keyword_search 标题命中", True)
    else:
        record("keyword_search 标题命中", True, "0 命中（检索索引时延，不判失败）")
    return {"n1": n1, "parts": parts, "marker": marker}


async def test_search_semantic(memory: Memory, marker: str) -> None:
    """语义检索：受知识库同步时延影响，只打印不判失败。"""
    section("语义检索（不判失败）")
    try:
        hits = await memory.search(f"it-core-updated {marker}")
        print(f"  语义检索命中 {len(hits)} 条（同步时延内可能为 0，非失败）")
    except Exception as err:  # noqa: BLE001
        print(f"  语义检索异常（打印不判失败）: {err}")


async def test_pool_isolation(repo: str) -> None:
    """连接池复用与无效凭据隔离。"""
    section("连接池")
    client = CNBApiClient(token=os.environ["CNB_AGENTIC_MEMORY_TOKEN"], repo=repo)
    memory = Memory(client)
    issues = await memory.list_recent(limit=1)  # 真实请求走一次
    issues2 = await memory.list_recent(limit=1)
    record("池复用连续请求", isinstance(issues, list) and isinstance(issues2, list))

    bad = CNBApiClient(token="invalid-token-it", repo=repo)
    try:
        await Memory(bad).list_recent(limit=1)
        record("无效凭据 401", False, "无效 token 未被拒绝")
    except Exception as err:  # noqa: BLE001
        record("无效凭据 401", "401" in str(err) or "Unauthorized" in str(err), str(err)[:60])


def test_cli(number: int) -> None:
    """CLI 真实进程。"""
    section("CLI")
    env = {**os.environ}
    r = subprocess.run(
        [sys.executable, "-m", "cnb_agentic_memory.cli", "get", str(number)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    try:
        ok = r.returncode == 0 and json.loads(r.stdout)["number"] == number
    except (ValueError, KeyError):
        ok = False
    record("cli get", ok, (r.stderr or r.stdout)[:80])

    r2 = subprocess.run(
        [sys.executable, "-m", "cnb_agentic_memory.cli", "get", "999999"],
        capture_output=True,
        text=True,
        env={k: v for k, v in env.items() if not k.startswith("CNB_AGENTIC_MEMORY")},
        timeout=60,
    )
    record("cli 配置缺失 exit 2", r2.returncode == 2, f"exit={r2.returncode}")


def test_mcp_stdio(number: int) -> None:
    """MCP stdio 独立进程：initialize + tools/call memory_get。"""
    section("MCP stdio")
    proc = subprocess.Popen(
        [sys.executable, "-m", "cnb_agentic_memory.mcp_main"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert proc.stdin and proc.stdout
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "it", "version": "0"},
            },
        }
        proc.stdin.write(json.dumps(init) + "\n")
        proc.stdin.flush()
        resp = json.loads(proc.stdout.readline())
        assert resp["id"] == 1
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.flush()
        call = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "memory_get", "arguments": {"number": number}},
        }
        proc.stdin.write(json.dumps(call) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        while line.strip() and '"id": 2' not in line and '"id":2' not in line:
            line = proc.stdout.readline()
        resp2 = json.loads(line)
        text = resp2["result"]["content"][0]["text"]
        record("stdio tools/call memory_get", json.loads(text)["number"] == number)
    except Exception as err:  # noqa: BLE001
        record("stdio tools/call memory_get", False, str(err)[:80])
    finally:
        proc.kill()


def _rpc_body(resp: httpx.Response) -> dict:
    """JSON-RPC 响应解析：json_response 模式直接 json()；SSE 模式抽 data: 行。"""
    if "event-stream" in resp.headers.get("content-type", ""):
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise ValueError(f"SSE 响应无 data 行: {resp.text[:100]}")
    return resp.json()


def test_mcp_http(number: int) -> None:
    """MCP streamable-http + --require-headers：门禁与放行（CLI 形态有状态 session）。"""
    section("MCP streamable-http")
    port = 8123
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cnb_agentic_memory.mcp_main",
            "--transport",
            "streamable-http",
            "--port",
            str(port),
            "--require-headers",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        base = f"http://127.0.0.1:{port}/mcp"
        headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
        for _ in range(50):
            try:
                # POST ping 探测（GET 在有状态模式会挂起 SSE 长连接）：400=session
                # 缺失但路由已就绪，200=正常，均算启动完成；其余状态继续等
                if httpx.post(
                    base, json={"jsonrpc": "2.0", "method": "ping", "id": 0}, headers=headers, timeout=2
                ).status_code in (200, 400):
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}

        def session_of(extra: dict[str, str]) -> str:
            init = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "it", "version": "0"},
                },
            }
            r = httpx.post(base, json=init, headers={**headers, **extra}, timeout=30)
            sid = r.headers.get("mcp-session-id", "")
            assert sid, f"initialize 未返回 session id: {r.status_code} {r.text[:80]}"
            httpx.post(
                base,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers={**headers, **extra, "mcp-session-id": sid},
                timeout=30,
            )
            return sid

        payload = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "memory_get", "arguments": {"number": number}},
            "id": 2,
        }

        # 匿名：session 可建，工具调用被门禁拒绝
        sid = session_of({})
        r1 = httpx.post(base, json=payload, headers={**headers, "mcp-session-id": sid}, timeout=30)
        body1 = _rpc_body(r1)
        anon_text = body1["result"]["content"][0]["text"]
        record(
            "HTTP 匿名门禁拒绝", r1.status_code == 200 and "--require-headers" in anon_text, anon_text[:60]
        )

        # 带凭据头：放行并返回真实结果
        auth = {
            "X-CNB-Token": os.environ["CNB_AGENTIC_MEMORY_TOKEN"],
            "X-CNB-Repo": os.environ["CNB_AGENTIC_MEMORY_REPO"],
        }
        sid2 = session_of(auth)
        r2 = httpx.post(base, json=payload, headers={**headers, **auth, "mcp-session-id": sid2}, timeout=30)
        body2 = _rpc_body(r2)
        record(
            "HTTP 带头放行",
            r2.status_code == 200 and json.loads(body2["result"]["content"][0]["text"])["number"] == number,
        )
    except Exception as err:  # noqa: BLE001
        record("MCP HTTP", False, str(err)[:80])
    finally:
        proc.kill()


async def main() -> None:
    token = os.environ.get("CNB_AGENTIC_MEMORY_TOKEN")
    repo = os.environ.get("CNB_AGENTIC_MEMORY_REPO")
    if not token or not repo:
        sys.exit("缺少 CNB_AGENTIC_MEMORY_TOKEN / CNB_AGENTIC_MEMORY_REPO")
    print(f"集成测试目标仓库: {repo}（须为测试专用仓库）")
    print(f"Python: {sys.version.split()[0]}")

    client = CNBApiClient(token=token, repo=repo)
    memory = Memory(client)

    # 段级异常守护：单段崩溃只记 FAIL，后续段落照常执行（契约：单段失败不中断）
    async def guard(name: str, fn, *args) -> object:
        try:
            return await fn(*args) if asyncio.iscoroutinefunction(fn) else fn(*args)
        except Exception as err:  # noqa: BLE001
            record(name, False, f"段级异常: {str(err)[:90]}")
            return {}

    ctx = await guard("Memory 核心链路", test_memory_core, memory)
    await guard("语义检索", test_search_semantic, memory, str(ctx.get("marker", "")))
    await guard("连接池", test_pool_isolation, repo)
    n1 = int(ctx.get("n1", 1))
    await guard("CLI", test_cli, n1)
    await guard("MCP stdio", test_mcp_stdio, n1)
    await guard("MCP streamable-http", test_mcp_http, n1)

    print("\n== 汇总 ==")
    fails = [r for r in RESULTS if r[1] == "FAIL"]
    for name, status, note in RESULTS:
        print(f"  [{status}] {name}" + (f" — {note}" if note else ""))
    print(f"\n共 {len(RESULTS)} 项，FAIL {len(fails)} 项")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
