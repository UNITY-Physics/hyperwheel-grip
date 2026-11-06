"""Every project's key is checked when the service starts and every 12 hours, without an upload."""

from __future__ import annotations

from conftest import PROJECT

from hyperwheel.dashboard import snapshot
from hyperwheel.service import Service


def snap(env):
    from hyperwheel.dashboard import HEARTBEAT
    env.store.set_cursor(HEARTBEAT, int(env.clock()))
    return snapshot(env.store, env.site, env.routing, now=env.clock(), free_bytes=lambda p: 50 * 1024 ** 3)


def test_keys_are_checked_when_the_service_starts_without_any_upload(env):
    from hyperwheel.keys import Credentials
    from hyperwheel.uploader import Uploader
    svc = Service(env.site, env.routing, orthanc=lambda: env.orthanc, keys=env.keys, clock=env.clock,
                  synced=lambda: True, internet_probe=lambda routing: True)
    up = env.uploader()
    svc.upload_round(up)  # nothing to upload
    assert env.store.key_status(PROJECT)[0] == "working"
    assert snap(env)["keys"] == [{"project": PROJECT, "status": "Working", "ok": True}]
    tokens = env.fake.token_requests
    svc.upload_round(up)  # not again within 12 hours
    assert env.fake.token_requests == tokens

    env.keys.write(PROJECT, Credentials("site-a-sandbox-client", "revoked-secret"))  # the key stops working
    env.advance(12 * 3600)
    svc.upload_round(up)
    assert env.store.key_status(PROJECT)[0] == "rejected"
    assert any(f.code == "KEY_NOT_WORKING" for f in env.store.open_flags(project=PROJECT))


def test_key_not_checked_while_the_clock_is_not_set(env):
    svc = Service(env.site, env.routing, orthanc=lambda: env.orthanc, keys=env.keys, clock=env.clock,
                  synced=lambda: False, internet_probe=lambda routing: True)
    svc.upload_round(env.uploader())
    assert env.store.key_status(PROJECT) is None and env.fake.token_requests == 0
