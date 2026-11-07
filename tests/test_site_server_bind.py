"""site_server start reaps only our own listener and yields a healthy port."""

from __future__ import annotations

import errno
import importlib.util
import os
import signal
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "scripts" / "site_server.py"


def _load_site():
    spec = importlib.util.spec_from_file_location("site_server_bind_under_test", SITE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["site_server_bind_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _listen_row(port: int, inode: str, ip_hex: str = "0100007F") -> str:
    return f" 0: {ip_hex}:{port:04X} 00000000:0000 0A 00000000:00000000 00:00000000 00000000 1000 0 {inode}"


def test_parse_listen_inodes_matches_port_and_bind():
    mod = _load_site()
    text = "\n".join(
        [
            "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode",
            _listen_row(8794, "42"),
            _listen_row(80, "7", ip_hex="00000000"),
            _listen_row(8794, "99", ip_hex="0100A8C0"),  # 192.168.0.1, not our bind
        ]
    )
    assert mod.parse_listen_inodes(text, 8794, "127.0.0.1") == {"42"}
    assert mod._ipv4_from_proc_hex("0100007F") == "127.0.0.1"


def test_is_our_site_server_requires_this_repo(tmp_path: Path):
    mod = _load_site()
    repo = tmp_path / "TradingAgents"
    (repo / "scripts").mkdir(parents=True)
    script = repo / "scripts" / "site_server.py"
    script.write_text("# x\n", encoding="utf-8")
    proc = tmp_path / "proc"
    ours = proc / "4242"
    (ours / "fd").mkdir(parents=True)
    (ours / "cmdline").write_bytes(b".venv/bin/python\0scripts/site_server.py\0")
    os.symlink(repo, ours / "cwd")
    other = proc / "7"
    (other / "fd").mkdir(parents=True)
    (other / "cmdline").write_bytes(b"nginx\0")
    os.symlink(tmp_path, other / "cwd")

    assert mod.is_our_site_server(4242, root=repo, proc_root=proc) is True
    assert mod.is_our_site_server(7, root=repo, proc_root=proc) is False
    assert mod.is_our_site_server(1, root=repo, proc_root=proc) is False


def test_reap_signals_only_our_listener(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    mod = _load_site()
    repo = tmp_path / "TradingAgents"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "site_server.py").write_text("# x\n", encoding="utf-8")
    proc = tmp_path / "proc"
    header = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode"
    (proc / "net").mkdir(parents=True)
    (proc / "net" / "tcp").write_text(
        "\n".join([header, _listen_row(8794, "42"), _listen_row(8794, "77", ip_hex="0100A8C0")]),
        encoding="utf-8",
    )
    (proc / "net" / "tcp6").write_text(header + "\n", encoding="utf-8")

    def add_pid(pid: int, cmd: bytes, inode: str, cwd: Path):
        entry = proc / str(pid)
        (entry / "fd").mkdir(parents=True)
        (entry / "cmdline").write_bytes(cmd)
        os.symlink(cwd, entry / "cwd")
        (entry / "fd" / "4").symlink_to(f"socket:[{inode}]")

    add_pid(4242, b".venv/bin/python\0scripts/site_server.py\0", "42", repo)
    add_pid(99, b"/usr/sbin/nginx\0", "77", tmp_path)

    monkeypatch.setattr(mod, "ROOT", repo)
    signaled: list[tuple[int, int]] = []

    def kill(pid, sig):
        signaled.append((pid, sig))
        if sig == signal.SIGTERM:
            (proc / str(pid)).rename(proc / f"dead-{pid}")

    reaped = mod.reap_own_site_listener(
        "127.0.0.1", 8794, proc_root=proc, kill=kill, sleep=lambda _s: None, wait_s=1
    )
    assert reaped == [4242]
    assert signaled == [(4242, signal.SIGTERM)]
    assert not any(pid == 99 for pid, _sig in signaled)


def test_bind_exits_0_when_healthz_already_200(monkeypatch: pytest.MonkeyPatch):
    mod = _load_site()
    monkeypatch.setattr(mod, "reap_own_site_listener", lambda *_a, **_k: [])
    monkeypatch.setattr(mod, "healthz_ok", lambda *_a, **_k: True)

    class Busy:
        def __init__(self, *_a, **_k):
            raise OSError(errno.EADDRINUSE, "Address already in use")

    monkeypatch.setattr(mod, "ThreadingHTTPServer", Busy)
    with pytest.raises(SystemExit) as caught:
        mod.bind_site_server()
    assert caught.value.code == 0


def test_bind_reraises_when_healthz_is_down(monkeypatch: pytest.MonkeyPatch):
    mod = _load_site()
    monkeypatch.setattr(mod, "reap_own_site_listener", lambda *_a, **_k: [])
    monkeypatch.setattr(mod, "healthz_ok", lambda *_a, **_k: False)

    class Busy:
        def __init__(self, *_a, **_k):
            raise OSError(errno.EADDRINUSE, "Address already in use")

    monkeypatch.setattr(mod, "ThreadingHTTPServer", Busy)
    with pytest.raises(OSError) as caught:
        mod.bind_site_server()
    assert caught.value.errno == errno.EADDRINUSE
