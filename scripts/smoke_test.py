#!/usr/bin/env python3
"""工作台项目 · 自动化冒烟测试（SPEC.md 第 9 节）。

只依赖 httpx；逐项检查并打印 PASS / FAIL / SKIP，任一 FAIL 则退出码非 0。
每项独立 try/except：单点失败不影响后续检查，也不会抛栈退出。

用法：
    python scripts/smoke_test.py                     # 默认 http://127.0.0.1:8080
    python scripts/smoke_test.py --base http://nas:8080
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import traceback
from typing import Any

import httpx

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
STAMP = dt.datetime.now().strftime("%H%M%S")
TITLE_PREFIX = f"冒烟-{STAMP}"
TIMEOUT = 30.0          # 普通请求
REPORT_TIMEOUT = 180.0  # 报告生成可能 30~60s


class Check:
    """一条检查结果。"""

    def __init__(self, key: str, name: str) -> None:
        self.key = key
        self.name = name
        self.status = FAIL
        self.detail = ""

    def set(self, status: str, detail: str = "") -> "Check":
        self.status = status
        self.detail = detail
        return self


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return None


def _dig_id(payload: Any) -> int | None:
    """从各种响应形状里挖出整数 id。"""
    if isinstance(payload, dict):
        for key in ("id", "task_id", "report_id", "rid", "pid", "project_id"):
            value = payload.get(key)
            if isinstance(value, int):
                return value
        for key in ("task", "report", "data", "item", "result", "project"):
            found = _dig_id(payload.get(key))
            if found is not None:
                return found
    return None


def _id_from_location(response: httpx.Response) -> int | None:
    location = response.headers.get("location") or ""
    tail = location.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
    return int(tail) if tail.isdigit() else None


def _monday(day: dt.date) -> str:
    return (day - dt.timedelta(days=day.weekday())).isoformat()


# --------------------------------------------------------------------------
# 各项检查：成功返回说明字符串（或 (说明, 数据)），失败直接抛异常
# --------------------------------------------------------------------------
def check_health(client: httpx.Client) -> str:
    """(a) /healthz"""
    response = client.get("/healthz", timeout=TIMEOUT)
    body = _json(response)
    if response.status_code == 200 and isinstance(body, dict) and body.get("ok") is True:
        return f"{response.status_code} {json.dumps(body, ensure_ascii=False)}"
    raise AssertionError(f"{response.status_code} {response.text[:200]}")


def check_project(client: httpx.Client) -> tuple[str, int | None]:
    """(b) 建项目（页面表单 POST），再用 /api/projects 回读"""
    name = f"{TITLE_PREFIX}-项目"
    response = client.post(
        "/projects",
        data={"name": name, "description": "冒烟测试项目", "color": "#6366f1"},
        timeout=TIMEOUT,
    )
    if response.status_code not in (200, 201, 303):
        raise AssertionError(f"POST /projects -> {response.status_code} {response.text[:160]}")
    listed = client.get("/api/projects", timeout=TIMEOUT)
    if listed.status_code != 200:
        raise AssertionError(f"GET /api/projects -> {listed.status_code}")
    payload = _json(listed)
    items = payload
    if isinstance(payload, dict):
        for key in ("projects", "items", "data"):
            if isinstance(payload.get(key), list):
                items = payload[key]
                break
    if not isinstance(items, list):
        raise AssertionError(f"/api/projects 结构未知：{str(payload)[:160]}")
    for item in items:
        if isinstance(item, dict) and item.get("name") == name:
            return f"POST {response.status_code}，回读命中 id={item.get('id')}", item.get("id")
    raise AssertionError(f"/api/projects 里没找到刚建的项目「{name}」")


def check_tasks(client: httpx.Client) -> tuple[str, dict[str, Any]]:
    """(c) 建 5 条边界工作：昨天 / 今天 / +3 / +4 / 无 DDL"""
    today = dt.date.today()
    plan = [
        ("yesterday", (today - dt.timedelta(days=1)).isoformat()),
        ("today", today.isoformat()),
        ("plus3", (today + dt.timedelta(days=3)).isoformat()),
        ("plus4", (today + dt.timedelta(days=4)).isoformat()),
        ("nodue", None),
    ]
    created: dict[str, Any] = {}
    for key, due in plan:
        payload = {
            "title": f"{TITLE_PREFIX}-{key}",
            "detail": "冒烟测试用边界工作",
            "done_criteria": "接口返回 2xx",
            "due_date": due,
            "status": "todo",
            "priority": 2,
            "week_start": _monday(today),
        }
        response = client.post("/api/tasks", json=payload, timeout=TIMEOUT)
        task_id: int | None = None
        if response.status_code in (200, 201):
            task_id = _dig_id(_json(response)) or _id_from_location(response)
        else:
            form = {k: v for k, v in payload.items() if v is not None}
            response = client.post("/tasks", data=form, timeout=TIMEOUT)
            if response.status_code in (200, 201, 303):
                task_id = _id_from_location(response)
        if task_id is None:
            raise AssertionError(
                f"创建 {key} 失败（/api/tasks -> {response.status_code}）：{response.text[:160]}"
            )
        created[key] = task_id
    return f"5 条工作已创建：{json.dumps(created, ensure_ascii=False)}", created


def check_dashboard(client: httpx.Client, created: dict[str, Any]) -> str:
    """(d) /api/dashboard 计数断言"""
    response = client.get("/api/dashboard", timeout=TIMEOUT)
    if response.status_code != 200:
        raise AssertionError(f"GET /api/dashboard -> {response.status_code}")
    payload = _json(response)
    if not isinstance(payload, dict):
        raise AssertionError("/api/dashboard 不是 JSON 对象")
    buckets = payload.get("buckets") if isinstance(payload.get("buckets"), dict) else payload
    counts = buckets.get("counts") if isinstance(buckets.get("counts"), dict) else {}

    problems: list[str] = []
    observed: dict[str, int] = {}
    for bucket in ("overdue", "today", "soon", "no_due"):
        items = buckets.get(bucket)
        if isinstance(items, list):
            observed[bucket] = len(items)
        elif isinstance(counts.get(bucket), int):
            observed[bucket] = counts[bucket]
        else:
            problems.append(f"{bucket} 缺失")
            continue
        if observed[bucket] < 1:
            problems.append(f"{bucket}=0（应 >=1）")

    soon_items = buckets.get("soon")
    if isinstance(soon_items, list):
        soon_titles = " ".join(str(item.get("title", "")) for item in soon_items if isinstance(item, dict))
        if "plus4" in soon_titles:
            problems.append("+4 天的工作错误地进了 soon 分区")

    detail = f"计数 {json.dumps(observed, ensure_ascii=False)}"
    if problems:
        raise AssertionError(detail + "；问题：" + "；".join(problems))
    return detail


def check_progress(client: httpx.Client, created: dict[str, Any]) -> str:
    """(e) 追加进展并回读"""
    task_id = created.get("today")
    if task_id is None:
        raise AssertionError("缺少用于追加进展的工作 id（(c) 未成功）")
    content = f"{TITLE_PREFIX}-进展-已追加"
    response = client.post(
        f"/api/tasks/{task_id}/progress", json={"content": content}, timeout=TIMEOUT
    )
    if response.status_code not in (200, 201):
        raise AssertionError(f"POST 进展 -> {response.status_code} {response.text[:160]}")
    detail = client.get(f"/api/tasks/{task_id}", timeout=TIMEOUT)
    if detail.status_code != 200:
        raise AssertionError(f"GET /api/tasks/{task_id} -> {detail.status_code}")
    if content not in detail.text:
        raise AssertionError("任务详情里没有刚追加的进展")
    return f"task {task_id} 已追加进展并回读命中"


def check_archive(client: httpx.Client, created: dict[str, Any]) -> str:
    """(f) 标记完成 -> 出现在归档 -> 恢复"""
    task_id = created.get("plus3")
    if task_id is None:
        raise AssertionError("缺少用于归档测试的工作 id（(c) 未成功）")
    title = f"{TITLE_PREFIX}-plus3"
    done = client.patch(f"/api/tasks/{task_id}", json={"status": "done"}, timeout=TIMEOUT)
    if done.status_code not in (200, 204):
        raise AssertionError(f"PATCH status=done -> {done.status_code} {done.text[:160]}")
    archived = client.get("/archive", timeout=TIMEOUT)
    if archived.status_code != 200:
        raise AssertionError(f"GET /archive -> {archived.status_code}")
    if title not in archived.text:
        raise AssertionError(f"/archive 里没出现已完成工作「{title}」")

    restored = client.patch(f"/api/tasks/{task_id}", json={"status": "todo"}, timeout=TIMEOUT)
    if restored.status_code not in (200, 204):
        raise AssertionError(f"PATCH status=todo -> {restored.status_code}")
    again = client.get("/archive", timeout=TIMEOUT)
    if title in again.text:
        raise AssertionError("恢复后仍留在归档页")
    return f"task {task_id} 完成进归档、恢复后离开归档"


def check_report_task(client: httpx.Client, created: dict[str, Any]) -> str:
    """(g) task 报告含 MOCK 声明或真实内容"""
    task_id = created.get("yesterday")
    if task_id is None:
        raise AssertionError("缺少用于报告测试的工作 id（(c) 未成功）")
    response = client.post(
        "/api/reports", json={"kind": "task", "task_id": task_id}, timeout=REPORT_TIMEOUT
    )
    if response.status_code not in (200, 201, 303):
        raise AssertionError(f"POST /api/reports(task) -> {response.status_code} {response.text[:200]}")
    rid = _dig_id(_json(response)) or _id_from_location(response)
    if rid is None:
        raise AssertionError(f"响应里没有报告 id：{response.text[:200]}")
    detail = client.get(f"/api/reports/{rid}", timeout=TIMEOUT)
    if detail.status_code != 200:
        raise AssertionError(f"GET /api/reports/{rid} -> {detail.status_code}")
    payload = _json(detail) or {}
    content = str(payload.get("content_md") or payload.get("content") or detail.text)
    marker = "MOCK" if "MOCK" in content else "正文"
    if "MOCK" not in content and len(content) < 100:
        raise AssertionError(f"报告正文既没有 MOCK 标记也太短（{len(content)} 字符）")
    return f"report {rid} 已生成（{len(content)} 字符，命中 {marker}）"


def check_report_custom(client: httpx.Client) -> str:
    """(h) custom 报告"""
    today = dt.date.today()
    start = (today - dt.timedelta(days=6)).isoformat()
    end = today.isoformat()
    response = client.post(
        "/api/reports",
        json={"kind": "custom", "start": start, "end": end},
        timeout=REPORT_TIMEOUT,
    )
    if response.status_code not in (200, 201, 303):
        raise AssertionError(f"POST /api/reports(custom) -> {response.status_code} {response.text[:200]}")
    rid = _dig_id(_json(response)) or _id_from_location(response)
    if rid is None:
        raise AssertionError(f"响应里没有报告 id：{response.text[:200]}")
    detail = client.get(f"/api/reports/{rid}", timeout=TIMEOUT)
    if detail.status_code != 200:
        raise AssertionError(f"GET /api/reports/{rid} -> {detail.status_code}")
    return f"custom 报告 {rid} 已生成（{start} ~ {end}）"


def check_llm_health(client: httpx.Client) -> str:
    """(i) /api/llm/health"""
    response = client.get("/api/llm/health", timeout=TIMEOUT)
    if response.status_code != 200:
        raise AssertionError(f"{response.status_code} {response.text[:160]}")
    payload = _json(response)
    if not isinstance(payload, dict) or "mode" not in payload:
        raise AssertionError(f"缺少 mode 字段：{str(payload)[:160]}")
    return f"mode={payload.get('mode')} ok={payload.get('ok')} model={payload.get('model')}"


def check_obsidian(client: httpx.Client) -> tuple[str, str]:
    """(j) Obsidian 搜索；vault 不可用时 SKIP 而不是 FAIL"""
    response = client.get("/api/obsidian/search", params={"q": "测试"}, timeout=TIMEOUT)
    payload = _json(response)
    if response.status_code == 200 and isinstance(payload, dict):
        if payload.get("ok") is False or payload.get("available") is False:
            return SKIP, "vault 未挂载或未建索引，跳过"
        results = payload.get("results")
        if isinstance(results, list):
            return PASS, f"返回 200，命中 {len(results)} 条"
        return PASS, "返回 200，结构里没有 results 字段（可接受）"
    if response.status_code in (400, 404, 409, 503):
        return SKIP, f"HTTP {response.status_code}，vault 未就绪，跳过"
    raise AssertionError(f"{response.status_code} {response.text[:160]}")


def check_settings_page(client: httpx.Client) -> str:
    """(k) /settings 页面 200"""
    response = client.get("/settings", timeout=TIMEOUT)
    if response.status_code != 200:
        raise AssertionError(f"{response.status_code} {response.text[:160]}")
    if "设置" not in response.text:
        raise AssertionError("页面返回 200 但不像设置页")
    return f"200，正文 {len(response.text)} 字符"


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="工作台项目冒烟测试")
    parser.add_argument("--base", default="http://127.0.0.1:8080", help="服务根地址")
    parser.add_argument("--verbose", action="store_true", help="失败时打印完整栈")
    args = parser.parse_args()

    checks: list[Check] = []
    created: dict[str, Any] = {}
    verbose = bool(args.verbose)

    def run(key: str, name: str, func, *func_args) -> Any:
        check = Check(key, name)
        checks.append(check)
        try:
            outcome = func(*func_args)
        except Exception as exc:
            check.set(FAIL, f"{type(exc).__name__}: {exc}")
            if verbose:
                traceback.print_exc()
            return None
        if isinstance(outcome, tuple) and len(outcome) == 2 and outcome[0] in (PASS, SKIP):
            check.set(outcome[0], str(outcome[1]))
            return None
        if isinstance(outcome, tuple) and len(outcome) == 2:
            check.set(PASS, str(outcome[0]))
            return outcome[1]
        check.set(PASS, str(outcome))
        return outcome

    print(f"== 工作台冒烟测试 == base={args.base} 标记={TITLE_PREFIX}")
    with httpx.Client(base_url=args.base, timeout=TIMEOUT, follow_redirects=False) as client:
        try:
            client.get("/healthz", timeout=5.0)
        except httpx.HTTPError as exc:
            print(f"!! 无法连接 {args.base}：{type(exc).__name__}: {exc}")
            print("!! 请先启动服务：python -m uvicorn app.main:app --port 8080")
            return 1

        run("a", "GET /healthz", check_health, client)
        run("b", "创建项目 / 回读 /api/projects", check_project, client)
        created = run("c", "创建 5 条边界工作", check_tasks, client) or {}
        run("d", "GET /api/dashboard 计数断言", check_dashboard, client, created)
        run("e", "追加进展", check_progress, client, created)
        run("f", "完成 -> 归档 -> 恢复", check_archive, client, created)
        run("g", "task 报告（MOCK 或真实内容）", check_report_task, client, created)
        run("h", "custom 报告", check_report_custom, client)
        run("i", "GET /api/llm/health", check_llm_health, client)
        run("j", "Obsidian 搜索", check_obsidian, client)
        run("k", "GET /settings 页面", check_settings_page, client)

    width = max(len(f"({c.key}) {c.name}") for c in checks) + 2
    line = "-" * (width + 64)
    print()
    print(line)
    print("检查项".ljust(width) + "结果    " + "说明")
    print(line)
    for check in checks:
        label = f"({check.key}) {check.name}"
        print(label.ljust(width) + check.status.ljust(8) + check.detail[:120])
    print(line)
    passed = sum(1 for c in checks if c.status == PASS)
    failed = sum(1 for c in checks if c.status == FAIL)
    skipped = sum(1 for c in checks if c.status == SKIP)
    print(f"汇总：PASS {passed} / FAIL {failed} / SKIP {skipped}（共 {len(checks)} 项）")
    if failed:
        print("存在 FAIL 项，退出码 1")
        return 1
    print("全部通过，退出码 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
