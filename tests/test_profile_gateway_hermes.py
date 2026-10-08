"""Profile routing and owner binding on real Hermes classes."""
from __future__ import annotations

import json
import types
import pytest
from conftest import requires_hermes

pytestmark = [requires_hermes, pytest.mark.usefixtures("registered_platform")]


def test_the_roster_lists_the_profiles_this_gateway_serves(hermes_home, gadget):
    ids = [p["id"] for p in gadget.adapter.profiles()]
    assert ids == ["default", "ops"]  # parked 'old' is not served


def test_old_firmware_gets_no_roster_and_an_untouched_source(hermes_home, gadget):
    import os
    from hermes_gadget_plugin import protocol

    key = os.urandom(32)
    gadget.authorized.add(protocol.device_id_for_key(key))
    with _RawDevice(gadget.url, key, {}) as device:
        welcome = device.welcome
        device.ws.send(json.dumps({"type": "text", "id": "m1", "text": "hi"}))
        while True:
            msg = json.loads(device.ws.recv(timeout=10))
            if msg.get("type") == "reply" and msg.get("text") == "echo: hi":
                break
    assert "profile" not in welcome and "profiles" not in welcome
    event = next(e for e in reversed(gadget.events) if e.text == "hi")
    assert event.source.profile is None


def test_grants_are_scoped_serialized_and_refusable(hermes_home, gadget, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from gateway.pairing import PairingStore
    from hermes_gadget_plugin.adapter import GadgetAdapter

    (hermes_home / ".env").write_text("GADGET_ALLOWED_USERS=hg-0000000000000000\n")
    (hermes_home / "profiles" / "ops" / ".env").write_text("GADGET_ALLOWED_USERS=hg-1111111111111111\n")
    devices = [f"hg-{i:016x}" for i in range(2, 6)]
    with ThreadPoolExecutor(4) as pool:
        assert all(pool.map(lambda d: GadgetAdapter._grant(d, d, "ops"), devices))
    store = PairingStore(profile="ops")
    assert all(store.is_approved("gadget", d) for d in devices)  # no grant lost to a concurrent rewrite
    assert "hg-0000000000000000" in (hermes_home / ".env").read_text()
    assert not any(d in (hermes_home / ".env").read_text() for d in devices)  # default's allowlist untouched
    monkeypatch.setattr(PairingStore, "generate_code", lambda self, *a, **kw: None)  # rate limited / locked out
    assert GadgetAdapter._grant("hg-ffffffffffffffff", "x", "ops") is False


DEVICE = "hg-cccccccccccccccc"


def _routing_rig(device):
    """The real gateway resolvers around one gadget adapter (upstream's test_multiplex_transport_matrix.py rig)."""
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.pairing import PairingStore
    from gateway.profile_routing import parse_profile_routes
    from gateway.run import GatewayRunner
    from hermes_gadget_plugin.adapter import GadgetAdapter

    gadget = Platform("gadget")
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.config.platforms = {gadget: PlatformConfig(enabled=True, extra={})}
    # A static route a user may have configured before device selection existed.
    runner.config.profile_routes = parse_profile_routes(
        [{"name": "old-route", "platform": "gadget", "profile": "ops", "user_id": device}])
    runner.pairing_store, runner.pairing_stores = PairingStore(profile="default"), {}
    runner._primary_profile_name = "default"
    adapter = GadgetAdapter(PlatformConfig(enabled=True, extra={"port": 0, "host": "127.0.0.1"}))
    adapter.gateway_runner = runner
    runner.adapters, runner._profile_adapters = {gadget: adapter}, {"ops": {}}
    return runner, adapter


@pytest.fixture
def routing_rig(hermes_home):
    return (*_routing_rig(DEVICE), hermes_home)


def _session(profile, capable=True):
    return types.SimpleNamespace(device_id=DEVICE, name="Kitchen", profile=profile,
                                 caps={"profiles": True} if capable else {})


def test_a_device_on_ops_runs_in_the_ops_home_and_replies_through_the_gadget_adapter(routing_rig):
    from gateway.run import _profile_runtime_scope
    from gateway.session_identity import resolve_identity
    from hermes_constants import get_hermes_home

    runner, adapter, home = routing_rig
    source = adapter._source(_session("ops"))
    identity = resolve_identity(source, runner=runner, adapter=adapter)
    assert (identity.transport_profile, identity.runtime_profile) == ("default", "ops")
    assert identity.runtime_home == home / "profiles" / "ops"
    assert identity.authorization_home == home
    assert runner._session_key_for_source(source).startswith("agent:ops:gadget:dm:")
    assert runner._delivery_adapter_for(source) is adapter
    with _profile_runtime_scope(identity.runtime_home):
        assert get_hermes_home() == home / "profiles" / "ops"


def test_a_capable_device_on_default_beats_a_static_route(routing_rig):
    from gateway.session_identity import resolve_identity

    runner, adapter, home = routing_rig
    identity = resolve_identity(adapter._source(_session("default")), runner=runner, adapter=adapter)
    assert identity.runtime_profile == "default" and identity.runtime_home == home
    legacy = resolve_identity(adapter._source(_session("default", capable=False)), runner=runner, adapter=adapter)
    assert legacy.runtime_profile == "ops"  # legacy devices keep today's routing


def test_owner_check_ignores_routes_and_ops_only_approval(routing_rig):
    # Real Hermes callbacks: the adapter's routed one trusts an ops-only approval through the static
    # route; the default-bound owner check the plugin uses for every profile decision does not.
    from gateway.config import Platform
    from gateway.pairing import PairingStore
    from gateway.run import _profile_runtime_scope

    runner, adapter, home = routing_rig
    ops = PairingStore(profile="ops")
    runner.pairing_stores["ops"] = ops  # Hermes picks per-profile stores from this map
    with _profile_runtime_scope(home / "profiles" / "ops"):  # as _grant does: never touch default's allowlist
        ops.approve_code("gadget", ops.generate_code("gadget", DEVICE, "Kitchen"))
    routed = runner._make_adapter_auth_check(Platform("gadget"))
    owner = runner._make_adapter_auth_check(Platform("gadget"), profile_name="default")
    assert routed(DEVICE, "dm", DEVICE) is True
    assert owner(DEVICE, "dm", DEVICE) is False
    adapter._owner_check = owner
    assert adapter._owner_trusts(DEVICE) is False
    # Approved in default: trusted by the owner check whatever the route says, so its first switch can happen.
    default = runner.pairing_store
    default.approve_code("gadget", default.generate_code("gadget", DEVICE, "Kitchen"))
    assert owner(DEVICE, "dm", DEVICE) is True
    capable = types.SimpleNamespace(device_id=DEVICE, caps={"profiles": True})
    assert adapter._verdict(capable) is True


def test_an_unknown_profile_falls_back_to_the_launch_home(routing_rig):
    # Why the plugin refuses unknown profiles itself: Hermes only warns.
    from gateway.session_identity import resolve_identity

    runner, adapter, home = routing_rig
    identity = resolve_identity(adapter._source(_session("nosuch")), runner=runner, adapter=adapter)
    assert identity.runtime_home == home


class _RawDevice:
    """A device speaking the handshake by hand, so a test chooses exactly what it claims in hello."""

    def __init__(self, url, key, caps, profile=None):
        import base64

        from websockets.sync.client import connect
        from hermes_gadget_plugin import protocol

        device = protocol.device_id_for_key(key)
        self.ws = connect(url, subprotocols=["hermes-gadget.v1"])
        self.ws.send(json.dumps({"type": "hello", "proto": 1, "device_id": device, "name": "Kitchen",
                                 "board": "raw", "firmware": "0.1.0", "caps": caps,
                                 **({"profile": profile} if profile else {})}))
        challenge = json.loads(self.ws.recv(timeout=5))
        auth = ({"mac": protocol.auth_mac(key, device, challenge["nonce"])} if challenge["enrolled"]
                else {"key": base64.b64encode(key).decode()})
        self.ws.send(json.dumps({"type": "auth", **auth}))
        self.welcome = json.loads(self.ws.recv(timeout=5))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.ws.close()


@pytest.fixture
def live_rig(hermes_home, loop_thread, tmp_path, monkeypatch):
    """The adapter listening, behind Hermes's real authorization: its callbacks and per-profile
    pairing stores, with an old static route sending the device to ops."""
    import gateway.config as gateway_config
    import plugins.plugin_storage as storage
    from gateway.config import Platform
    from gateway.pairing import PairingStore
    from hermes_gadget_plugin import protocol

    monkeypatch.setattr(storage, "plugin_data_dir", lambda name: tmp_path / "plugin-data" / name)
    monkeypatch.setattr(gateway_config, "persist_home_channel", lambda home, **kw: None)
    monkeypatch.delenv("GADGET_HOME_CHANNEL", raising=False)
    key = bytes(range(1, 33))
    device = protocol.device_id_for_key(key)
    runner, adapter = _routing_rig(device)
    runner.pairing_stores["ops"] = PairingStore(profile="ops")
    adapter.set_authorization_check(runner._make_adapter_auth_check(Platform("gadget")))
    events = []

    async def handler(event):
        events.append(event)

    adapter.set_message_handler(handler)
    assert loop_thread.run(adapter.connect())
    url = f"ws://127.0.0.1:{adapter.hub.bound_port}/gadget"

    def admits(source):
        """Hermes's own ingress decision: canonicalize, then authorize."""
        return runner._canonicalize(source, primary_home=hermes_home) is not None and \
            runner._is_user_authorized_for_source(source)

    def approve_default():
        store = runner.pairing_store
        store.approve_code("gadget", store.generate_code("gadget", device, "Kitchen"))

    yield types.SimpleNamespace(
        runner=runner, adapter=adapter, device=device, events=events, run=loop_thread.run, admits=admits,
        approve_default=approve_default, connect=lambda caps, profile=None: _RawDevice(url, key, caps, profile))
    loop_thread.run(adapter.cancel_background_tasks())
    loop_thread.run(adapter.disconnect())


def _admitted(rig, wait_s=1.5):
    """Events Hermes would run, after giving dispatch a moment to deliver them."""
    import time as _time

    deadline = _time.monotonic() + wait_s
    while _time.monotonic() < deadline:
        if any(rig.admits(e.source) for e in rig.events):
            break
        _time.sleep(0.05)
    return [(e.text, e.source.profile) for e in rig.events if rig.admits(e.source)]


def test_dropping_the_profile_flag_does_not_reuse_a_plugin_grant(live_rig):
    rig = live_rig
    rig.approve_default()
    with rig.connect({"profiles": True}) as device:
        assert device.welcome["paired"] is True
        session = rig.adapter.hub.get(rig.device)
        assert rig.run(rig.adapter.select_profile(session, "ops")) is None  # the plugin approves it in ops
    rig.runner.pairing_store.revoke("gadget", rig.device)
    with rig.connect({}) as device:  # older firmware, same key, the old ops route still configured
        assert device.welcome["paired"] is False
        session = rig.adapter.hub.get(rig.device)
        rig.run(rig.adapter.on_text(session, "m1", "hi"))
        assert _admitted(rig) == []


def test_a_replaced_connection_cannot_send_voice_after_revocation(live_rig, monkeypatch):
    import asyncio
    import threading

    import hermes_gadget_plugin.adapter as adapter_module

    rig = live_rig
    rig.approve_default()
    release = threading.Event()

    async def slow_cache(data, ext):
        await asyncio.to_thread(release.wait, 10)  # the upload is still being written
        return "/tmp/utterance.wav"

    monkeypatch.setattr(adapter_module, "cache_audio_from_bytes_async", slow_cache)
    with rig.connect({"profiles": True}):
        old = rig.adapter.hub.get(rig.device)
        assert rig.run(rig.adapter.select_profile(old, "ops")) is None

        async def speak():
            return old.spawn(rig.adapter.on_utterance(old, "u1", b"RIFF", 1.0))

        upload = rig.run(speak())
    rig.runner.pairing_store.revoke("gadget", rig.device)
    with rig.connect({"profiles": True}) as device:  # the device is back, now refused
        assert device.welcome["paired"] is False
        release.set()
        rig.run(asyncio.wait_for(upload, 10))
        assert _admitted(rig) == []


def test_a_device_is_bound_to_the_default_profile_before_any_grant_is_visible(live_rig, monkeypatch):
    import asyncio
    import threading

    from hermes_gadget_plugin.adapter import GadgetAdapter

    rig = live_rig
    rig.approve_default()
    granted, release = threading.Event(), threading.Event()
    grant = GadgetAdapter._grant

    def paused_grant(*args):
        result = grant(*args)  # the ops approval is on disk from here
        granted.set()
        release.wait(10)
        return result

    monkeypatch.setattr(GadgetAdapter, "_grant", staticmethod(paused_grant))
    with rig.connect({"profiles": True}):
        session = rig.adapter.hub.get(rig.device)

        async def start():
            return asyncio.ensure_future(rig.adapter.select_profile(session, "ops"))

        switch = rig.run(start())
        assert granted.wait(10)
        rig.runner.pairing_store.revoke("gadget", rig.device)
        with rig.connect({}) as legacy:  # older firmware, same key, the old ops route still configured
            assert legacy.welcome["paired"] is False
            rig.run(rig.adapter.on_text(rig.adapter.hub.get(rig.device), "m1", "grant-gap"))
            assert _admitted(rig) == []
            release.set()
            assert rig.run(asyncio.wait_for(switch, 10)) == "unpaired"


def test_a_switch_that_cannot_bind_the_device_mints_nothing(live_rig, monkeypatch):
    from gateway.pairing import PairingStore

    rig = live_rig
    rig.approve_default()

    def fail(device_id):
        raise OSError("disk full")

    monkeypatch.setattr(rig.adapter._store, "bind_to_owner", fail)
    with rig.connect({"profiles": True}):
        session = rig.adapter.hub.get(rig.device)
        assert rig.run(rig.adapter.select_profile(session, "ops")) == "unpaired"
        assert session.profile == "default"
    assert not PairingStore(profile="ops").is_approved("gadget", rig.device)


def test_forget_from_a_stale_store_keeps_the_device_bound(live_rig):
    from hermes_gadget_plugin.store import DeviceStore

    rig = live_rig
    rig.approve_default()
    directory = rig.adapter._store.path.parent
    with rig.connect({"profiles": True}):
        session = rig.adapter.hub.get(rig.device)
        cli = DeviceStore(directory)  # 'hermes gadget forget' in another process, loaded before the switch
        assert rig.run(rig.adapter.select_profile(session, "ops")) is None
    assert cli.forget(rig.device)  # rewrites devices.json from its older snapshot
    rig.adapter._store = DeviceStore(directory)  # as after a gateway restart
    rig.runner.pairing_store.revoke("gadget", rig.device)
    with rig.connect({}) as legacy:  # re-enrolls the same key, no profile flag, old ops route
        assert legacy.welcome["paired"] is False
        rig.run(rig.adapter.on_text(rig.adapter.hub.get(rig.device), "m1", "after-forget"))
        assert _admitted(rig) == []


@pytest.mark.parametrize("hermes", ["no factory", "old factory signature"])
def test_a_hermes_without_the_owner_check_refuses_instead_of_dropping_the_device(live_rig, monkeypatch, hermes):
    rig = live_rig
    rig.approve_default()
    if hermes == "no factory":
        owner = next(c for c in type(rig.runner).__mro__ if "_make_adapter_auth_check" in c.__dict__)
        monkeypatch.delattr(owner, "_make_adapter_auth_check")
    else:
        monkeypatch.setattr(rig.runner, "_make_adapter_auth_check", lambda platform: None)
    with rig.connect({"profiles": True}, profile="ops") as device:
        assert device.welcome["type"] == "welcome"
        assert device.welcome["paired"] is False and device.welcome["profile"] == "default"
        assert rig.adapter.hub.get(rig.device) is not None  # still connected
    assert not rig.runner.pairing_stores["ops"].is_approved("gadget", rig.device)
    assert all(e.text == "/status" and e.source.profile == "default" for e in rig.events)  # the pairing trigger only


def test_a_hermes_without_profile_dirs_refuses_the_switch_and_keeps_the_device(live_rig, monkeypatch):
    import hermes_cli.profiles as profiles

    rig = live_rig
    rig.approve_default()
    with rig.connect({"profiles": True}) as device:
        assert device.welcome["paired"] is True
        monkeypatch.delattr(profiles, "get_profile_dir")
        device.ws.send(json.dumps({"type": "profile.select", "profile": "ops"}))
        while (reply := json.loads(device.ws.recv(timeout=5)))["type"] != "profile":
            pass
        assert reply == {"type": "profile", "profile": "default", "error": "unpaired"}
        assert rig.adapter.hub.get(rig.device) is not None  # still connected
    assert not rig.runner.pairing_stores["ops"].is_approved("gadget", rig.device)
    assert all(e.text == "/status" and e.source.profile == "default" for e in rig.events)  # the pairing trigger only
