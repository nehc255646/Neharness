"""M3: 从 DB 重建 AgentLoop 上下文（模拟重启）"""

import asyncio
import uuid

import pytest

from app.agent import subagent as sa
from app.agent.loop import AgentLoop
from app.core.config import settings
from app.core.db import init_db
from app.persist import (
    ensure_session,
    list_subagent_runs,
    load_late_subagent_results,
    save_message,
    save_summary,
    upsert_subagent_run,
)


async def test_hydrate_rebuilds_history():
    ok = await init_db()
    if not ok:
        import pytest

        pytest.skip("MySQL unavailable")
    sid = f"ut_{uuid.uuid4().hex[:12]}"
    await ensure_session(sid, title="hydrate")
    await save_message(session_id=sid, agent_id="main", role="user", content="任务A")
    await save_message(session_id=sid, agent_id="main", role="assistant", content="已收到")

    agent = AgentLoop(sid)
    assert agent.history == []
    await agent.hydrate_from_db()
    assert [m["content"] for m in agent.history] == ["任务A", "已收到"]


async def test_hydrate_then_start_does_not_call_model():
    """有历史的会话 start 后保持 idle，直到 enqueue 新消息才调模型。"""
    ag = AgentLoop("ut_noauto")
    ag.history = [{"role": "user", "content": "old"}, {"role": "assistant", "content": "done"}]
    called: list = []

    async def fake_call(_messages):
        called.append(1)
        return {"text": "should-not", "tool_calls": [], "streamed": False}

    ag._call_model = fake_call  # type: ignore[method-assign]
    await ag.start()
    await asyncio.sleep(0.15)
    assert called == []
    assert ag.state == "idle"
    await ag.enqueue({"type": "user_message", "content": "hi"})
    for _ in range(80):
        await asyncio.sleep(0.02)
        if called:
            break
    assert called, "新用户消息后应调模型"
    await ag.stop()


async def test_hydrate_keeps_full_history_without_summary():
    ok = await init_db()
    if not ok:
        import pytest

        pytest.skip("MySQL unavailable")
    sid = f"ut_{uuid.uuid4().hex[:12]}"
    await ensure_session(sid, title="window")
    keep = settings.window_n * 2
    total = keep + 4
    for i in range(total):
        role = "user" if i % 2 == 0 else "assistant"
        await save_message(session_id=sid, agent_id="main", role=role, content=f"m{i}")
    agent = AgentLoop(sid)
    await agent.hydrate_from_db()
    assert len(agent.history) == total
    assert agent.history[0]["content"] == "m0"


async def test_hydrate_applies_window_slice():
    ok = await init_db()
    if not ok:
        import pytest

        pytest.skip("MySQL unavailable")
    sid = f"ut_{uuid.uuid4().hex[:12]}"
    await ensure_session(sid, title="window")
    keep = settings.window_n * 2
    for i in range(keep + 4):
        role = "user" if i % 2 == 0 else "assistant"
        await save_message(session_id=sid, agent_id="main", role=role, content=f"m{i}")
    await save_summary(sid, "已覆盖的旧对话")
    agent = AgentLoop(sid)
    await agent.hydrate_from_db()
    assert len(agent.history) == keep
    assert agent.history[0]["content"] == "m4"


async def test_hydrate_pairs_unmatched_tool_calls():
    ok = await init_db()
    if not ok:
        pytest.skip("MySQL unavailable")
    sid = f"ut_{uuid.uuid4().hex[:12]}"
    await ensure_session(sid, title="unpaired")
    await save_message(
        session_id=sid,
        agent_id="main",
        role="assistant",
        content="",
        tool_calls=[{"name": "shell", "args": {"command": "echo hi"}, "id": "c_unpaired"}],
    )
    agent = AgentLoop(sid)
    await agent.hydrate_from_db()
    tools = [m for m in agent.history if m.get("role") == "tool"]
    assert len(tools) == 1
    assert tools[0]["tool_call_id"] == "c_unpaired"
    assert "中断" in tools[0]["content"]


async def test_hydrate_feeds_back_late_subagent_results():
    """PLAN §2.4 时序兜底：主 agent done 期间完成的迟到结果，续聊 hydrate 时喂回且只喂一次"""
    ok = await init_db()
    if not ok:
        pytest.skip("MySQL unavailable")
    sid = f"ut_{uuid.uuid4().hex[:12]}"
    await ensure_session(sid, title="late")
    await save_message(session_id=sid, agent_id="main", role="user", content="任务B")
    wk_id = f"wk_{uuid.uuid4().hex[:8]}"
    await upsert_subagent_run(
        main_session_id=sid,
        subagent_id=wk_id,
        kind="worker",
        goal="子任务",
        status="done",
        result="迟到结果内容",
        late=True,
        finished=True,
    )
    agent = AgentLoop(sid)
    await agent.hydrate_from_db()
    fed = [m["content"] for m in agent.history if m["role"] == "user" and m["content"].startswith("[迟到子 agent 结果")]
    assert fed == [f"[迟到子 agent 结果 {wk_id}]\n迟到结果内容"]
    # 喂回后标记 fed_back，迟到队列清空
    remaining = await load_late_subagent_results(sid)
    assert remaining == []
    # 再次 hydrate：仅剩已落库的普通用户消息，不重复喂回
    agent2 = AgentLoop(sid)
    await agent2.hydrate_from_db()
    fed2 = [m for m in agent2.history if m["role"] == "user" and str(m["content"]).startswith("[迟到子 agent 结果")]
    assert len(fed2) == 1


async def test_hydrate_restores_running_interactive():
    ok = await init_db()
    if not ok:
        pytest.skip("MySQL unavailable")
    sid = f"ut_{uuid.uuid4().hex[:12]}"
    await ensure_session(sid, title="restore-interactive")
    await save_message(session_id=sid, agent_id="main", role="user", content="主对话不该出现在侧栏")
    sub_id = f"sub_{uuid.uuid4().hex[:8]}"
    await upsert_subagent_run(
        main_session_id=sid,
        subagent_id=sub_id,
        kind="interactive",
        behavior_desc="用户侧栏对话",
        goal="侧栏",
        status="running",
    )
    await save_message(session_id=sid, agent_id=sub_id, role="user", content="侧栏你好")
    await save_message(session_id=sid, agent_id=sub_id, role="assistant", content="收到侧栏")
    agent = AgentLoop(sid)
    try:
        await agent.hydrate_from_db()
        rec = sa.get_subagent(sub_id)
        assert rec is not None and rec.status == "running"
        loop = sa._loops.get(sub_id)
        assert loop is not None
        assert [m.get("content") for m in loop.history] == ["侧栏你好", "收到侧栏"]
        assert all("主对话不该出现" not in str(m.get("content")) for m in loop.history)
        built = loop._build_messages()
        assert any("主对话不该出现" in str(m.get("content")) for m in built)
        assert any("侧栏你好" in str(m.get("content")) for m in built)
    finally:
        await sa.stop_session_subagents(sid)
        sa.purge_session(sid)
        await agent.stop()


async def test_hydrate_interrupts_stale_workers():
    ok = await init_db()
    if not ok:
        pytest.skip("MySQL unavailable")
    sid = f"ut_{uuid.uuid4().hex[:12]}"
    await ensure_session(sid, title="interrupt-worker")
    wk_id = f"wk_{uuid.uuid4().hex[:8]}"
    await upsert_subagent_run(
        main_session_id=sid,
        subagent_id=wk_id,
        kind="worker",
        goal="切片任务",
        status="running",
    )
    agent = AgentLoop(sid)
    try:
        await agent.hydrate_from_db()
        runs = await list_subagent_runs(sid, kind="worker")
        assert len(runs) == 1
        assert runs[0].status == "error"
        assert "进程重启" in (runs[0].result or "")
        rec = sa.get_subagent(wk_id)
        assert rec is not None and rec.status == "error"
    finally:
        sa.purge_session(sid)
        await agent.stop()
