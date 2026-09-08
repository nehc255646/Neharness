"""会话级工作目录。未绑定会话时退回 WORKDIR 根，便于单测。"""

from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar, Token
from pathlib import Path

from app.core.config import settings

_session_id: ContextVar[str | None] = ContextVar("workdir_session_id", default=None)
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def root_workdir() -> Path:
    p = Path(settings.workdir)
    if not p.is_absolute():
        p = (Path(__file__).resolve().parents[3] / settings.workdir).resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def session_dir_name(session_id: str) -> str:
    name = _UNSAFE.sub("_", (session_id or "").strip())[:80]
    if not name or name in {".", ".."}:
        return "default"
    return name


def bind_session(session_id: str | None) -> Token:
    return _session_id.set(session_id)


def reset_session(token: Token) -> None:
    _session_id.reset(token)


def current_session_id() -> str | None:
    return _session_id.get()


def current_workdir() -> Path:
    root = root_workdir()
    sid = _session_id.get()
    if not sid:
        return root
    target = (root / session_dir_name(sid)).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        target = (root / "default").resolve()
    target.mkdir(parents=True, exist_ok=True)
    return target


@contextmanager
def use_session_workdir(session_id: str):
    token = bind_session(session_id)
    try:
        yield current_workdir()
    finally:
        reset_session(token)
