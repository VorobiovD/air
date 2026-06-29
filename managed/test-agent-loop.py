"""Offline tests for agent_loop's bounded mid-stream retry.

Network-free: a fake client whose stream().get_final_message() raises a real
httpx.RemoteProtocolError (the observed failure: peer closed the connection mid
chunked read). Proves a transient blip recovers, a persistent one gives up
cleanly (no infinite loop), and a real (non-transient) error is NOT retried.
"""

import sys
import types
from pathlib import Path

import httpx
import pytest

_LIB = Path(__file__).resolve().parent.parent / "plugins" / "air" / "lib"
sys.path.insert(0, str(_LIB))

import agent_loop  # noqa: E402


def _client(behaviors):
    """behaviors: list of callables run in order by successive get_final_message()
    calls (the last is reused once exhausted). Each either returns a msg or raises."""
    calls = {"n": 0}

    class _Ctx:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get_final_message(self):
            i = min(calls["n"], len(behaviors) - 1)
            calls["n"] += 1
            return behaviors[i]()

    class _Msgs:
        def stream(self, **kw): return _Ctx()

    return types.SimpleNamespace(messages=_Msgs()), calls


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # Don't actually wait out the backoff in tests.
    monkeypatch.setattr(agent_loop.time, "sleep", lambda *_a: None)


def _drop():
    raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")


def _ok():
    return types.SimpleNamespace(usage=None, content=[], stop_reason="end_turn")


def test_recovers_from_transient_midstream_drop():
    sentinel = _ok()
    client, calls = _client([_drop, _drop, lambda: sentinel])  # 2 blips then success
    out = agent_loop._final_message_with_retry(
        client, log=lambda *_a: None, label="t", model="m", system=[], messages=[])
    assert out is sentinel
    assert calls["n"] == 3  # retried through both blips


def test_gives_up_after_max_attempts_no_infinite_loop():
    client, calls = _client([_drop])  # always drops
    with pytest.raises(httpx.RemoteProtocolError):
        agent_loop._final_message_with_retry(
            client, log=lambda *_a: None, label="t", model="m", system=[], messages=[])
    assert calls["n"] == agent_loop.STREAM_RETRY_ATTEMPTS  # bounded — exactly N tries


def test_non_transient_error_propagates_immediately():
    def _bug():
        raise ValueError("a real error, not a network blip")
    client, calls = _client([_bug])
    with pytest.raises(ValueError):
        agent_loop._final_message_with_retry(
            client, log=lambda *_a: None, label="t", model="m", system=[], messages=[])
    assert calls["n"] == 1  # NOT retried — only transient transport errors are


def test_transient_set_includes_remoteprotocolerror():
    # The observed failure type must be in the retry set (httpx present in this env).
    errs = agent_loop._transient_stream_errors()
    assert issubclass(httpx.RemoteProtocolError, tuple(errs))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
