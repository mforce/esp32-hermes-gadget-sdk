// Original adapter based on Waveshare Rev2.0 wiring and register facts.
#pragma once
#include <cstddef>
#include <cstdint>
#include <functional>
#include <utility>

namespace hg {
// EXIO1/2 in the demo are one-based: TCA9554 P0 touch, P1 LCD.
// Configure only those two outputs; never alter SD/RTC/expansion pins.
class Ws185Resets {
 public:
  using Read = std::function<bool(uint8_t, uint8_t&)>;
  using Write = std::function<bool(uint8_t, uint8_t)>;
  using Delay = std::function<void(uint32_t)>;
  Ws185Resets(Read read, Write write, Delay delay)
      : read_(std::move(read)), write_(std::move(write)), delay_(std::move(delay)) {}
  bool begin() {
    uint8_t output = 0, config = 0;
    if (!read_(1, output) || !read_(3, config)) return false;
    // Set the latch low before making either pin an output (no high glitch).
    if (!write_(1, output & ~uint8_t(3)) || !write_(3, config & ~uint8_t(3))) return false;
    delay_(10);
    if (!write_(1, output | uint8_t(3))) return false;
    delay_(50);
    return true;
  }
 private:
  Read read_;
  Write write_;
  Delay delay_;
};

// CST816 report at register 0x02: count, X high/low, Y high/low.
class Ws185TouchFault {
 public:
  void valid() { failures_ = 0; }
  bool release_due(bool held) {
    if (failures_ < 5) ++failures_;
    return held && failures_ >= 5;
  }
 private:
  uint8_t failures_ = 0;
};

inline bool ws185_touch(const uint8_t* data, bool& down, int16_t& x, int16_t& y) {
  down = (data[0] & 0x0f) != 0;
  x = ((data[1] & 0x0f) << 8) | data[2];
  y = ((data[3] & 0x0f) << 8) | data[4];
  return !down || (x < 360 && y < 360);
}

// Factory I2S frame is two 32-bit slots containing four signed 16-bit ADC
// samples: R M N M. Use both microphones, not the analog playback reference
// (R) or unused channel (N). The SDK transports mono PCM16. No AEC DSP here.
inline void ws185_mono(const int16_t* rmnm, int16_t* mono, size_t frames) {
  for (size_t i = 0; i < frames; ++i)
    mono[i] = static_cast<int16_t>((static_cast<int32_t>(rmnm[4 * i + 1]) + rmnm[4 * i + 3]) / 2);
}
inline void ws185_stereo32(const int16_t* mono, int32_t* stereo, size_t frames) {
  for (size_t i = 0; i < frames; ++i)
    stereo[2 * i] = stereo[2 * i + 1] = static_cast<int32_t>(mono[i]) * 65536;
}
}  // namespace hg
