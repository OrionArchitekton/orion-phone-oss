"""S3: two agents want the same phone. BUSY names the holder; expired leases are taken over and audited."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from orion_phone.lease import LeaseStore


def test_second_holder_gets_busy_and_cannot_act(phone_env):
    assert phone_env.run("lease", "a", "--holder", "agent-1")[0] == 0
    code, out = phone_env.run("lease", "a", "--holder", "agent-2")
    assert (code, out["code"]) == (3, "BUSY")
    assert out["data"]["holder"] == "agent-1" and out["data"]["expires_at"]
    code, _ = phone_env.run("tap", "a", "1", "1", "--holder", "agent-2")
    assert code == 4
    assert not any(c.startswith("input tap") for c in phone_env.shell_calls())


def test_busy_is_answered_without_touching_the_phone(phone_env):
    code, first = phone_env.run("lease", "a", "--holder", "agent-1")
    assert code == 0
    phone_env.remove_device("emulator-5554")  # the phone dies while agent-1 holds it
    calls_before = phone_env.calls()
    code, out = phone_env.run("lease", "a", "--holder", "agent-2")
    assert (code, out["code"]) == (3, "BUSY")  # the holder is the answer, not PHONE_DOWN
    assert out["data"]["holder"] == "agent-1"
    assert out["data"]["expires_at"] == first["data"]["expires_at"]
    assert phone_env.calls() == calls_before  # a refused claim makes no adb call, not even a liveness probe


def test_leasing_again_extends_your_own_lease(phone_env):
    _, first = phone_env.run("lease", "a", "--holder", "agent-1")
    time.sleep(1.1)
    code, second = phone_env.run("lease", "a", "--holder", "agent-1")
    assert code == 0
    assert second["data"]["expires_at"] > first["data"]["expires_at"]
    assert "took_over_from" not in second["data"]  # extending your own lease is not a takeover


def test_expired_lease_is_taken_over_and_audited(short_lease_env):
    env = short_lease_env
    assert env.run("lease", "a", "--holder", "agent-1")[0] == 0
    time.sleep(1.5)
    code, out = env.run("lease", "a", "--holder", "agent-2")
    assert (code, out["data"]["took_over_from"]) == (0, "agent-1")
    record = env.audit("a")[-1]
    assert (record["holder"], record["took_over_from"]) == ("agent-2", "agent-1")


def test_expired_holder_can_no_longer_act(short_lease_env):
    env = short_lease_env
    env.run("lease", "a", "--holder", "agent-1")
    time.sleep(1.5)
    code, out = env.run("tap", "a", "1", "1", "--holder", "agent-1")
    assert (code, out["code"]) == (4, "NOT_HOLDER")
    assert "expired" in out["message"]


def test_concurrent_cli_claims_have_exactly_one_winner(phone_env):
    for _ in range(5):
        procs = [phone_env.popen("lease", "a", "--holder", f"agent-{i}") for i in range(8)]
        for proc in procs:
            proc.communicate(timeout=60)
        codes = [proc.returncode for proc in procs]
        assert sorted(codes) == [0] + [3] * 7, codes
        winner = codes.index(0)
        assert phone_env.run("release", "a", "--holder", f"agent-{winner}")[0] == 0


# An action in flight on a phone keeps a new lease from beginning: the previous holder's lease can lapse while its last
# action is still running, and a second agent must not be handed the phone while that action is on it. A refusal does not
# wait: a lease someone else holds is BUSY at once.


def test_busy_is_answered_at_once_while_an_action_is_in_flight(phone_env):
    assert phone_env.run("lease", "a", "--holder", "agent-1")[0] == 0
    with phone_env.holding_the_action_lock("a"):  # agent-1's action is on the phone
        code, out = phone_env.run("lease", "a", "--holder", "agent-2", timeout=10)
    assert (code, out["code"]) == (3, "BUSY")
    assert out["data"]["holder"] == "agent-1"


@pytest.mark.parametrize("lapsed", [None, "agent-1"], ids=["a free phone", "a lapsed lease"])
def test_a_lease_does_not_answer_until_the_action_in_flight_is_over(phone_env, lapsed):
    if lapsed:  # agent-1's lease lapsed while its last action was still running
        LeaseStore(Path(phone_env.env["ORION_PHONE_STATE"])).acquire("a", lapsed, -1)
    proc = None
    try:
        with phone_env.holding_the_action_lock("a"):  # an action is still on the phone
            proc = phone_env.popen("lease", "a", "--holder", "agent-2")
            with pytest.raises(subprocess.TimeoutExpired):
                proc.wait(timeout=2)  # long past the time the CLI takes to answer when nothing is in its way
        out, err = proc.communicate(timeout=30)  # the action is over: now it answers
        assert proc.returncode == 0, (out, err)
        data = json.loads(out)["data"]
        assert data["holder"] == "agent-2" and data.get("took_over_from") == lapsed, data
        record = phone_env.audit("a")[-1]
        assert (record["holder"], record.get("took_over_from")) == ("agent-2", lapsed), record
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.communicate()
