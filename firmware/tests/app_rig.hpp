#pragma once
// Drives hg::App through a fake HAL the way a Hermes gateway would.
#include <deque>
#include <map>
#include <string>
#include <vector>

#include "check.hpp"
#include "hg/app.hpp"
#include "hg/crypto.hpp"
#include "hg/protocol.hpp"
#include "hg/setup.hpp"
#include "hg/touch.hpp"

using hg::json::Value;

namespace hg_test {

struct FakeHal : hg::Display, hg::AudioIn, hg::AudioOut, hg::Transport, hg::Storage, hg::System {
  // System
  uint32_t clock = 1000;
  uint32_t now_ms() override { return clock; }
  void random_bytes(uint8_t* out, size_t len) override {
    for (size_t i = 0; i < len; ++i) out[i] = static_cast<uint8_t>(i);
  }
  void log(hg::LogLevel, std::string_view) override {}

  // Transport
  std::string url;
  int connects = 0, closes = 0;
  std::vector<Value> sent;
  std::vector<std::vector<uint8_t>> sent_binary;
  void connect(const std::string& u, const std::string&) override {
    url = u;
    ++connects;
  }
  bool send_text(std::string_view text) override {
    Value v;
    if (hg::json::parse(text, v)) sent.push_back(v);
    return true;
  }
  bool send_binary(const uint8_t* data, size_t len) override {
    sent_binary.emplace_back(data, data + len);
    return true;
  }
  void close() override { ++closes; }

  // Storage
  std::map<std::string, std::string> kv;
  bool storage_full = false;  // every set() fails, as a worn or misconfigured flash would
  std::optional<std::string> get(std::string_view key) override {
    auto it = kv.find(std::string(key));
    if (it == kv.end()) return std::nullopt;
    return it->second;
  }
  bool set(std::string_view key, std::string_view value) override {
    if (storage_full) return false;
    kv[std::string(key)] = std::string(value);
    return true;
  }
  void erase(std::string_view key) override { kv.erase(std::string(key)); }

  // Display
  int width = 320, height = 240;
  bool round = false;
  int corner_inset = 0;
  bool backlight = false;
  int brightness = 0, volume = 0;
  std::vector<uint16_t> fb = std::vector<uint16_t>(320 * 240, 0);
  int flushes = 0;
  int flushed_rows = 0;
  hg::DisplayInfo info() const override {
    hg::DisplayInfo d;
    d.width = static_cast<uint16_t>(width);
    d.height = static_cast<uint16_t>(height);
    d.round = round;
    d.corner_inset = static_cast<uint8_t>(corner_inset);
    d.has_backlight = backlight;
    return d;
  }
  void make_round(int diameter) {
    width = height = diameter;
    round = true;
    fb.assign(static_cast<size_t>(diameter * diameter), 0);
  }
  uint16_t* framebuffer() override { return fb.data(); }
  void set_backlight(uint8_t percent) override { brightness = percent; }
  void flush(uint16_t y0, uint16_t y1) override {
    ++flushes;
    flushed_rows += y1 - y0;
  }

  // Mic
  bool mic_on = false;
  bool start(uint32_t) override { return mic_on = true; }
  void stop() override { mic_on = false; }

  // Speaker
  bool spk_open = false;
  size_t spk_samples = 0;
  bool begin(uint32_t) override { return spk_open = true; }
  void write(const int16_t*, size_t n) override { spk_samples += n; }
  void end() override { spk_open = false; }
  void abort() override { spk_open = false; }
  bool busy() const override { return spk_open; }
  void set_volume(uint8_t percent) override { volume = percent; }

  hg::Hal hal() {
    hg::Hal h;
    h.system = this;
    h.transport = this;
    h.storage = this;
    h.display = this;
    h.mic = this;
    h.speaker = this;
    return h;
  }

  const Value* last(const std::string& type) const {
    for (auto it = sent.rbegin(); it != sent.rend(); ++it)
      if ((*it)["type"].as_string() == type) return &*it;
    return nullptr;
  }
};

struct FakeUpdater : hg::Updater {
  size_t slot = 64 * 1024;
  std::vector<uint8_t> image;
  bool open = false, installed = false, restarted = false, pending = false, confirmed = false;
  bool reject_image = false;
  size_t capacity() const override { return slot; }
  bool begin(size_t, std::string&) override {
    image.clear();
    return open = true;
  }
  bool write(const uint8_t* data, size_t len, std::string&) override {
    image.insert(image.end(), data, data + len);
    return open;
  }
  bool finish(std::string& error) override {
    open = false;
    if (reject_image) error = "not an app image";
    return installed = !reject_image;
  }
  void abort() override { open = false; }
  void restart() override { restarted = true; }
  bool pending_verify() const override { return pending; }
  void confirm() override {
    pending = false;
    confirmed = true;
  }
};

struct Rig {
  FakeHal fake;
  hg::Hal hal = fake.hal();
  hg::App app;

  explicit Rig(const std::string& server = "ws://hermes.local:8765/gadget") : app(hal, profile(server)) {}
  explicit Rig(hg::DeviceProfile p) : app(hal, std::move(p)) {}

  static hg::DeviceProfile touch_profile() {
    hg::DeviceProfile p = profile("ws://hermes.local:8765/gadget");
    p.touch_screen = true;
    p.cancel_label = "Swipe down";
    p.extra_settings = {"touch_cancel"};
    return p;
  }

  static hg::DeviceProfile profile(const std::string& server) {
    hg::DeviceProfile p;
    p.board = "test-board";
    p.firmware = "1.2.3";
    p.default_server_url = server;
    return p;
  }

  void advance(uint32_t ms) {
    for (uint32_t t = 0; t < ms; t += 10) {
      fake.clock += 10;
      app.tick();
    }
  }

  void server(const std::string& json) { app.on_transport_text(json); }

  // Boot, connect, authenticate and (optionally) get approved.
  void bring_online(bool paired) {
    app.begin();
    app.on_network(true, "test-wifi");
    advance(1000);
    app.on_transport_open();
    server(R"({"type":"challenge","nonce":"bm9uY2U=","enrolled":false})");
    server(std::string(R"({"type":"welcome","session":"s1","heartbeat_s":20,"paired":)") +
           (paired ? "true" : "false") + "}");
  }

  // Like bring_online(true), against a server that offers two Hermes profiles.
  void bring_online_with_profiles(const std::string& current = "default") {
    app.begin();
    app.on_network(true, "test-wifi");
    advance(1000);
    app.on_transport_open();
    server(R"({"type":"challenge","nonce":"bm9uY2U=","enrolled":false})");
    server(R"({"type":"welcome","session":"s1","heartbeat_s":20,"paired":true,"profile":")" + current +
           R"(","profiles":[{"id":"default","name":"Hermes"},{"id":"ops","name":"Ops"}]})");
  }
};

}  // namespace
