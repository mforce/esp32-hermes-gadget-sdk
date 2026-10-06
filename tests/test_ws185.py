"""V2 integration: compile the actual board map and exercise its native UI profile.

Register/PCM conversion tests live in firmware/tests/test_ws185.cpp; these
checks do not claim a working physical display, codec, or echo cancellation.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import requires_sim
from hermes_gadget.sim.runner import BOARDS

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "firmware/esp32/main"


def test_v2_actual_board_config_compiles_and_preserves_pins(tmp_path):
    compiler = shutil.which("g++") or shutil.which("clang++")
    if compiler is None:
        pytest.skip("C++ compiler unavailable")
    (tmp_path / "sdkconfig.h").write_text("#define CONFIG_HG_BOARD_WS_ESP32S3_TOUCH_LCD_185C_V2 1\n")
    source = tmp_path / "board_check.cpp"
    source.write_text(r'''
#include <cassert>
#include <cstring>
#include "board.hpp"
int main() {
  const auto& b = hgp::board_config();
  assert(std::strcmp(b.name, "waveshare-esp32-s3-touch-lcd-1.85c-v2") == 0);
  assert(b.lcd.enabled && b.lcd.controller == hgp::LcdController::St77916);
  assert(b.lcd.width == 360 && b.lcd.height == 360 && b.lcd.round);
  assert(b.lcd.sclk == 40 && b.lcd.cs == 21 && b.lcd.mosi == 46);
  assert(b.lcd.d1 == 45 && b.lcd.d2 == 42 && b.lcd.d3 == 41);
  assert(b.lcd.rst == -1 && b.lcd.backlight == 5 && b.tca9554_resets);
  assert(!b.lcd.swap_xy && !b.lcd.mirror_x && !b.lcd.mirror_y && b.lcd.invert);
  assert(b.i2c.sda == 11 && b.i2c.scl == 10 && b.i2c.hz == 400000);
  assert(b.codec.enabled && b.codec.stereo32 && b.codec.mclk == 2);
  assert(b.codec.bclk == 48 && b.codec.ws == 38 && b.codec.dout == 47);
  assert(b.codec.din == 39 && b.codec.pa == 15);
  assert(b.touch.enabled && b.touch.controller == hgp::TouchController::Cst816);
  assert(b.touch.addr == 0x15 && b.touch.width == 360 && b.touch.height == 360);
  assert(!b.touch.mirror_x && !b.touch.mirror_y && b.touch.rst == -1);
  assert(b.buttons.talk == 0 && b.buttons.cancel == -1);
  assert(!b.axp2101 && !b.latch_power.enabled && !b.pwr_key.enabled);
}
''')
    binary = tmp_path / "board_check"
    subprocess.run([compiler, "-std=c++17", "-I", str(tmp_path), "-I", str(MAIN),
                    str(source), str(MAIN / "board.cpp"), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True)


def test_v2_profile_metadata_and_ci_release_discovery():
    from test_firmware_tools import package_release
    env = "esp32s3-touch-lcd-185c-v2"
    board_dir = "waveshare-esp32-s3-touch-lcd-1.85c-v2"
    assert package_release.environments(package_release.PROJECT_DIR)[env] == board_dir
    meta = json.loads((ROOT / "firmware/esp32/boards" / board_dir / "board.json").read_text())
    assert meta["ready_made"] and "experimental" in meta["title"]
    assert "Rev2.0 only" in meta["summary"] and "no software AEC" in meta["summary"]
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "from tools.package_release import environments" in ci
    assert 'list(environments(Path(".")))' in ci
    assert "fromJSON(needs.boards.outputs.boards)" in ci
    release = (ROOT / ".github/workflows/release.yml").read_text()
    assert "pio run" in release and "package_release.py --all" in release
    defaults = (ROOT / "firmware/esp32/boards" / board_dir / "sdkconfig.defaults").read_text()
    for value in ["CONFIG_ESPTOOLPY_FLASHSIZE_16MB=y", "CONFIG_SPIRAM_MODE_OCT=y",
                  "CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y", "CONFIG_HG_BOARD_WS_ESP32S3_TOUCH_LCD_185C_V2=y"]:
        assert value in defaults


@requires_sim
def test_v2_round_profile_tap_yes_swipe_cancel_and_boot_talk(devserver, make_sim):
    board = BOARDS["sim-360x360-round"]
    assert (board.width, board.height, board.round, board.touch, board.scroll_buttons) == (360, 360, True, True, False)
    hub, brain, url = devserver()
    sim = make_sim(url, board=board.name)
    assert sim.wait_screen("ready", timeout=10)
    hello = next(m for m in sim.sent if m["type"] == "hello")
    assert hello["caps"]["display"]["width"] == 360
    assert hello["caps"]["display"]["shape"] == "round"
    assert "touch" in hello["caps"]["inputs"]
    # Circular glass hides the rectangular corners.
    assert sim.rgb888()[:3] == bytes(3)
    sim.press("talk")
    assert sim.wait_screen("listening", timeout=2)
    sim.touch(True, 180, 80)
    sim.touch(True, 180, 220)
    sim.touch(False)
    assert sim.wait_screen("ready", timeout=2)
    sim.release("talk")
    sim.device.transport_text(json.dumps({"type": "prompt", "id": "v2-q", "text": "Continue?"}))
    assert sim.device.screen() == "prompt"
    sim.run_for(0.7)
    sim.touch(True, 180, 180)
    sim.run_for(0.05)
    sim.touch(False)
    assert sim.wait_for(lambda: any(m.get("type") == "prompt.reply" and m.get("id") == "v2-q"
                                  and m.get("answer") == "yes" for m in sim.sent), timeout=2)
    assert sim.device.screen() != "listening"
