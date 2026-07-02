"""Tests for rdev watch: reconnect policy + watch_remote loop."""

import subprocess
from unittest import mock

from my_toolbox.rdev import watch
from my_toolbox.rdev.watch import ReconnectPolicy, watch_remote


def _completed(rc: int) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["ssh"], returncode=rc)


def _run_sequence(rcs, policy=None):
    """Drive watch_remote through a scripted sequence of ssh exit codes.

    Returns (final_rc, number_of_ssh_sessions). time.sleep is stubbed out so
    retries are instant.
    """
    calls = iter(rcs)
    run = mock.Mock(side_effect=lambda argv: _completed(next(calls)))
    with mock.patch.object(watch.subprocess, "run", run), mock.patch.object(
        watch.time, "sleep"
    ):
        rc = watch_remote("host-1", "nvitop", policy=policy)
    return rc, run.call_count


class TestReconnectPolicy:
    def test_long_session_resets_rapid_counter(self):
        p = ReconnectPolicy(min_session_secs=10, max_rapid_failures=2)
        assert p.keep_retrying(1.0)  # rapid #1
        assert p.keep_retrying(60.0)  # long session -> counter reset
        assert p.keep_retrying(1.0)  # rapid #1 again

    def test_consecutive_rapid_failures_stop(self):
        p = ReconnectPolicy(min_session_secs=10, max_rapid_failures=3)
        assert p.keep_retrying(1.0)
        assert p.keep_retrying(1.0)
        assert not p.keep_retrying(1.0)


class TestWatchRemote:
    def test_clean_quit_stops_immediately(self):
        rc, sessions = _run_sequence([0])
        assert rc == 0
        assert sessions == 1

    def test_remote_ctrl_c_stops(self):
        rc, sessions = _run_sequence([130])
        assert rc == 130
        assert sessions == 1

    def test_command_not_found_does_not_retry(self):
        rc, sessions = _run_sequence([127])
        assert rc == 127
        assert sessions == 1

    def test_drop_then_clean_quit_reconnects_once(self):
        rc, sessions = _run_sequence([255, 0])
        assert rc == 0
        assert sessions == 2

    def test_persistent_rapid_failures_give_up(self):
        # All sessions die instantly (elapsed ~0 < min_session_secs), so the
        # loop stops after max_rapid_failures sessions.
        policy = ReconnectPolicy(max_rapid_failures=3)
        rc, sessions = _run_sequence([255] * 10, policy=policy)
        assert rc == 255
        assert sessions == 3

    def test_local_ctrl_c_during_connect_stops(self):
        run = mock.Mock(side_effect=KeyboardInterrupt)
        with mock.patch.object(watch.subprocess, "run", run):
            rc = watch_remote("host-1", "nvitop")
        assert rc == 130
