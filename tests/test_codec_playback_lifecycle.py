"""Compile actual speaker methods with deterministic FreeRTOS/codec fakes.

These tests cover task interleavings, not physical codec/PA timing or I2S wiring.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_v2_short_tone_survives_pending_abort_flush(tmp_path):
    compiler = shutil.which("g++") or shutil.which("clang++")
    if not compiler:
        pytest.skip("C++ compiler unavailable")
    assert compiler is not None
    source = (ROOT / "firmware/esp32/main/port_codec.cpp").read_text()
    methods = source[source.index("bool CodecSpeaker::begin(uint32_t sample_rate)"):]
    methods = methods[:methods.rindex("}  // namespace hgp")]
    # Run the real worker body a finite number of iterations under a deterministic scheduler.
    assert methods.count("for (;;) {") == 1
    methods = methods.replace("for (;;) {", "for (int step = 0; step < worker_steps; ++step) {")
    harness = r'''
#include <atomic>
#include <cassert>
#include <cstdint>
#include <cstring>
#include <deque>
#include <mutex>
#include <vector>
#include "ws185.hpp"
#define ESP_LOGW(...) ((void)0)
#define pdMS_TO_TICKS(ms) (ms)
using gpio_num_t = int;
using esp_codec_dev_handle_t = void*;
struct Buffer { std::deque<uint8_t> bytes; };
using StreamBufferHandle_t = Buffer*;
int worker_steps = 12, pa_level = 0, clears = 0;
std::vector<uint8_t> played;
int xStreamBufferReset(Buffer* b) { b->bytes.clear(); ++clears; return 1; }
size_t xStreamBufferSend(Buffer* b, const void* p, size_t n, int) {
  auto* s = static_cast<const uint8_t*>(p);
  b->bytes.insert(b->bytes.end(), s, s + n); return n;
}
size_t xStreamBufferReceive(Buffer* b, void* p, size_t n, int) {
  size_t count = n < b->bytes.size() ? n : b->bytes.size();
  auto* out = static_cast<uint8_t*>(p);
  for (size_t i=0; i<count; ++i) { out[i]=b->bytes.front(); b->bytes.pop_front(); }
  return count;
}
size_t xStreamBufferBytesAvailable(Buffer* b) { return b->bytes.size(); }
void gpio_set_level(int, int level) { pa_level=level; }
void vTaskDelay(int) {}
int esp_codec_dev_set_out_mute(void*, bool) { return 0; }
int esp_codec_dev_set_out_vol(void*, int) { return 0; }
int esp_codec_dev_write(void*, const void* p, int n) {
  auto* s=static_cast<const uint8_t*>(p); played.insert(played.end(),s,s+n);return 0;
}
namespace hgp {
const char* TAG="test";
constexpr size_t kSpeakerChunk=512;
struct CodecAudio { static constexpr uint32_t kRate=16000; };
class CodecSpeaker {
public:
  bool begin(uint32_t); void write(const int16_t*,size_t); void end(); void abort();
  bool busy() const; void set_volume(uint8_t); static void task(void*);
  void* dev_=reinterpret_cast<void*>(1); bool stereo32_=true;
  int32_t staging[1024]={}; int32_t* stereo_=staging; int pa_=15;
  std::mutex pa_lock_; uint32_t pa_generation_=0;
  Buffer storage; Buffer* buffer_=&storage;
  std::atomic<bool> open_{false},draining_{false},flush_{false};
};
'''
    harness += methods + r'''
} // namespace hgp
int main() {
  hgp::CodecSpeaker s;
  std::vector<int16_t> tone(4000,900);
  assert(s.begin(16000));
  s.write(tone.data(),tone.size());
  s.end();
  // Worst case: app queues the entire short tone before the worker gets CPU.
  hgp::CodecSpeaker::task(&s);
  assert(played.size()==tone.size()*2*sizeof(int32_t));
  assert(!s.busy());
  assert(pa_level==0);
  // Cancellation must discard old queued samples, not a subsequent reply.
  played.clear();
  assert(s.begin(16000)); s.write(tone.data(),tone.size());
  s.abort(); assert(pa_level==0);
  hgp::CodecSpeaker::task(&s); assert(played.empty());
  assert(s.begin(16000)); s.write(tone.data(),tone.size()); s.end();
  hgp::CodecSpeaker::task(&s);
  assert(played.size()==tone.size()*2*sizeof(int32_t));
  assert(!s.busy());
}
'''
    cpp = tmp_path / "speaker_lifecycle.cpp"
    cpp.write_text(harness)
    binary = tmp_path / "speaker_lifecycle"
    subprocess.run([compiler, "-std=c++17", "-pthread", "-I", str(ROOT / "firmware/drivers"),
                    str(cpp), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
