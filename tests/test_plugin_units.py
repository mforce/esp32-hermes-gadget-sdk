"""Hermes-independent plugin modules: text shaping, audio, device store."""

import io
import math
import struct
import wave

import pytest

from hermes_gadget_plugin import audio, textfmt
from hermes_gadget_plugin.store import DeviceStore


# -- text --------------------------------------------------------------------------------

def test_markdown_is_flattened_for_small_screens():
    md = "# Title\n\n**Bold** and *italic* and `code`.\n\n- one\n- two\n\n[link text](https://x.y)\n\n---"
    out = textfmt.for_device(md)
    assert "#" not in out and "**" not in out and "`" not in out and "](" not in out
    assert "Bold and italic and code." in out
    assert "* one" in out and "link text" in out


def test_typography_and_accents_fold_to_ascii_and_emoji_drop():
    out = textfmt.for_device("Café “quoted” — it’s 20°C \U0001F600")
    assert out == "Cafe \"quoted\" - it's 20 degC"


def test_think_blocks_never_reach_the_screen():
    assert textfmt.for_device("<think>secret plan</think>Answer") == "Answer"


def test_code_fences_keep_their_content():
    assert textfmt.for_device("```python\nprint(1)\n```") == "print(1)"


# -- audio -------------------------------------------------------------------------------

def _sine(rate: int, seconds: float, freq: float = 440.0) -> bytes:
    return b"".join(struct.pack("<h", int(10000 * math.sin(2 * math.pi * freq * i / rate)))
                    for i in range(int(rate * seconds)))


@pytest.mark.parametrize("src,dst", [(24000, 16000), (16000, 24000), (22050, 16000), (16000, 16000)])
def test_resampler_output_length_tracks_the_rate_ratio(src, dst):
    pcm = _sine(src, 1.0)
    out = audio.resample(pcm, src, dst)
    assert abs(len(out) // 2 - dst) <= 2


def test_chunked_resampling_equals_one_shot():
    pcm = _sine(24000, 0.5)
    whole = audio.resample(pcm, 24000, 16000)
    r = audio.Resampler(24000, 16000)
    pieces = b"".join(r.process(pcm[i:i + 777]) for i in range(0, len(pcm), 777))  # odd sizes split samples
    assert len(pieces) == len(whole)
    a = struct.unpack(f"<{len(whole) // 2}h", whole)
    b = struct.unpack(f"<{len(pieces) // 2}h", pieces)
    assert max(abs(x - y) for x, y in zip(a, b)) <= 1


def test_stereo_input_is_downmixed():
    mono = _sine(16000, 0.1)
    stereo = b"".join(mono[i:i + 2] * 2 for i in range(0, len(mono), 2))
    assert audio.Resampler(16000, 16000, channels=2).process(stereo) == mono


def test_wav_round_trip_through_decode(tmp_path):
    pcm = _sine(24000, 0.25)
    path = tmp_path / "a.wav"
    path.write_bytes(audio.wav_bytes(pcm, 24000))
    decoded = audio.decode_file(str(path), 16000)
    assert abs(len(decoded) // 2 - 4000) <= 2
    with wave.open(io.BytesIO(audio.wav_bytes(pcm, 24000))) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (24000, 1, 2)


# -- store -------------------------------------------------------------------------------

def test_store_persists_enrollment_and_hides_keys(tmp_path):
    store = DeviceStore(tmp_path)
    store.enroll("hg-0123456789abcdef", b"k" * 32, name="Kitchen", board="b")
    again = DeviceStore(tmp_path)
    assert again.key_for("hg-0123456789abcdef") == b"k" * 32
    listed = again.devices()["hg-0123456789abcdef"]
    assert listed["name"] == "Kitchen" and "key" not in listed
    assert again.forget("hg-0123456789abcdef")
    assert DeviceStore(tmp_path).key_for("hg-0123456789abcdef") is None


def test_pairing_codes_expire(tmp_path):
    store = DeviceStore(tmp_path)
    store.remember_pairing("hg-0123456789abcdef", "ABCD2345", "hermes pairing approve gadget ABCD2345", ttl_s=60)
    assert store.pairing_for("hg-0123456789abcdef")[0] == "ABCD2345"
    store.remember_pairing("hg-0123456789abcdef", "ABCD2345", "cmd", ttl_s=-1)
    assert store.pairing_for("hg-0123456789abcdef") is None


def test_store_changes_from_another_process_are_seen(tmp_path):
    """The gateway keeps its DeviceStore open; `hermes gadget forget` edits the same file from another process."""
    gateway = DeviceStore(tmp_path)
    gateway.enroll("hg-0123456789abcdef", b"k" * 32, name="Kitchen", board="b")

    cli = DeviceStore(tmp_path)  # a second process: fresh instance on the same file
    assert cli.forget("hg-0123456789abcdef")

    gateway.touch("hg-0123456789abcdef", firmware="0.2.0")  # the device reconnects
    assert gateway.key_for("hg-0123456789abcdef") is None, "a forgotten key must not be resurrected by the gateway"
    assert DeviceStore(tmp_path).key_for("hg-0123456789abcdef") is None

    cli.enroll("hg-fedcba9876543210", b"j" * 32)  # and the other way round
    assert gateway.key_for("hg-fedcba9876543210") == b"j" * 32


def test_an_unapproved_device_expires_and_an_approved_one_stays(tmp_path, monkeypatch):
    from hermes_gadget_plugin import store as storemod

    now = {"t": 1_000_000.0}
    monkeypatch.setattr(storemod.time, "time", lambda: now["t"])
    store = DeviceStore(tmp_path)
    store.enroll("hg-0123456789abcdef", b"k" * 32, name="Stranger", address="10.0.0.9")
    store.enroll("hg-fedcba9876543210", b"j" * 32, name="Mine")
    store.confirm("hg-fedcba9876543210")
    assert set(store.pending()) == {"hg-0123456789abcdef"}
    assert store.devices()["hg-0123456789abcdef"]["pending"] is True
    assert store.devices()["hg-fedcba9876543210"]["pending"] is False

    now["t"] += storemod.PENDING_TTL_S - 1
    store.touch("hg-0123456789abcdef")  # still in contact: the clock restarts
    now["t"] += storemod.PENDING_TTL_S - 1
    assert store.key_for("hg-0123456789abcdef") == b"k" * 32

    now["t"] += 2  # no contact for a whole TTL
    assert store.key_for("hg-0123456789abcdef") is None, "an unapproved record is gone after its TTL"
    assert "hg-0123456789abcdef" not in store.devices()
    assert store.key_for("hg-fedcba9876543210") == b"j" * 32, "an approved record stays"


def test_profile_grants_persist_and_survive_forget(tmp_path):
    store = DeviceStore(tmp_path)
    store.enroll("hg-0123456789abcdef", b"k" * 32)
    assert not store.granted("hg-0123456789abcdef")
    store.record_grant("hg-0123456789abcdef", "ops")
    store.forget("hg-0123456789abcdef")  # resets the key; it is not a revocation
    assert DeviceStore(tmp_path).granted("hg-0123456789abcdef")
