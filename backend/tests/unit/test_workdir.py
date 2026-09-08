"""会话工作区隔离与路径消毒。"""

from app.core.workdir import (
    current_workdir,
    root_workdir,
    session_dir_name,
    use_session_workdir,
)
from app.tools.files import read, write


def test_session_dir_name_strips_traversal():
    assert session_dir_name("../etc") == ".._etc"
    assert session_dir_name("..") == "default"
    assert session_dir_name("") == "default"
    assert session_dir_name("it_abc-12") == "it_abc-12"


def test_use_session_stays_under_root():
    root = root_workdir()
    with use_session_workdir("../../etc/passwd"):
        wd = current_workdir()
    assert wd.resolve().is_relative_to(root)
    assert wd != root


def test_sessions_cannot_see_each_others_files():
    with use_session_workdir("sess_a"):
        assert "已写入" in write.invoke({"path": "only_a.txt", "content": "alpha"})
        assert "alpha" in read.invoke({"path": "only_a.txt"})
    with use_session_workdir("sess_b"):
        assert "已写入" in write.invoke({"path": "only_a.txt", "content": "beta"})
        assert "beta" in read.invoke({"path": "only_a.txt"})
        missing = read.invoke({"path": "from_a_should_not_exist.txt"})
        assert "文件不存在" in missing or "越权" in missing
    with use_session_workdir("sess_a"):
        assert "alpha" in read.invoke({"path": "only_a.txt"})


def test_unbound_uses_root():
    root = root_workdir()
    assert current_workdir() == root
