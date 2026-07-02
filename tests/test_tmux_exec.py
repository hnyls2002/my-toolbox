"""Tests for rdev tmux-exec launch-string building and dispatch."""

import subprocess
from unittest import mock

from my_toolbox.rdev import container
from my_toolbox.rdev.container import (
    _build_tmux_launch,
    tmux_exec_direct,
    tmux_exec_in_container,
)


class TestBuildTmuxLaunch:
    def test_subshell_wraps_whole_command(self):
        # The redirect must apply to the whole command, not just its last
        # simple command -- so `a && b` both land in the log.
        s = _build_tmux_launch("a && b", "sess", "/tmp/l.log", False)
        assert "( a && b ) > /tmp/l.log 2>&1" in s

    def test_detached_and_verified(self):
        s = _build_tmux_launch("cmd", "sess", "/tmp/l.log", False)
        assert "new-session -d -s sess" in s
        assert s.endswith("&& tmux has-session -t sess")

    def test_replace_kills_first(self):
        s = _build_tmux_launch("cmd", "sess", "/tmp/l.log", True)
        assert s.startswith("tmux kill-session -t sess 2>/dev/null; ")

    def test_quotes_special_chars(self):
        # A session name / log with spaces must be shell-quoted.
        s = _build_tmux_launch("cmd", "my sess", "/tmp/a b.log", False)
        assert "'my sess'" in s
        assert "'/tmp/a b.log'" in s


class TestTmuxExecDispatch:
    def _run(self, target, *args, **kwargs):
        run = mock.Mock(return_value=subprocess.CompletedProcess(args=[], returncode=0))
        with mock.patch.object(container.subprocess, "run", run):
            rc = target(*args, **kwargs)
        return rc, run.call_args.args[0]

    def test_direct_uses_plain_ssh_and_rx_tmux_pick(self):
        rc, argv = self._run(
            tmux_exec_direct, "host-1", "cmd", session="s", log="/tmp/l", replace=False
        )
        assert rc == 0
        assert argv[0] == "ssh" and argv[1] == "host-1"
        # devbox prefers rx's injected tmux, falling back to system tmux
        assert "/opt/radixark/bin/tmux" in argv[2]
        assert "$T new-session -d -s s" in argv[2]

    def test_container_wraps_in_docker_exec(self):
        rc, argv = self._run(
            tmux_exec_in_container,
            "host-1",
            "ctr",
            "cmd",
            session="s",
            log="/tmp/l",
            replace=False,
        )
        assert rc == 0
        assert argv[0] == "ssh" and argv[1] == "host-1"
        assert argv[2].startswith("docker exec ctr bash -c ")
        assert "tmux new-session -d -s s" in argv[2]
