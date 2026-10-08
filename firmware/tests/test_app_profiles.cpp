#include "app_rig.hpp"

using namespace hg_test;

TEST("profiles: hello offers switching; welcome brings the roster") {
  Rig r;
  r.app.begin();
  r.app.on_network(true, "wifi");
  r.advance(1000);
  r.app.on_transport_open();
  const Value* hello = r.fake.last("hello");
  CHECK(hello != nullptr);
  if (!hello) return;
  CHECK((*hello)["caps"]["profiles"].as_bool());
  CHECK(!hello->has("profile"));  // default is implied
  r.server(R"({"type":"challenge","nonce":"bm9uY2U=","enrolled":false})");
  r.server(R"({"type":"welcome","session":"s1","heartbeat_s":20,"paired":true,"profile":"ops",)"
           R"("profiles":[{"id":"default","name":"Hermes"},{"id":"ops","name":"Ops"}]})");
  CHECK_EQ(r.app.agent_profile(), std::string("ops"));
  CHECK_EQ(r.app.agent_profile_name(), std::string("Ops"));
  CHECK(r.app.status_json().find("\"profile\":\"ops\"") != std::string::npos);
  CHECK_EQ(r.fake.kv["profile"], std::string("ops"));
}

TEST("profiles: settings ask Hermes for the next profile and keep what it confirms") {
  Rig r;
  r.bring_online_with_profiles();
  CHECK(r.app.open_settings());
  for (int i = 0; i < 3; ++i) r.app.console("cancel");  // Volume -> Brightness -> Talk mode -> Profile
  CHECK_EQ(r.app.model().detail, std::string("Hermes profile"));
  CHECK(r.app.model().body.find("Hermes") != std::string::npos);
  r.app.console("talk");
  r.app.console("release");
  const Value* select = r.fake.last("profile.select");
  CHECK(select != nullptr);
  if (select) CHECK_EQ((*select)["profile"].as_string(), std::string("ops"));
  CHECK(r.fake.kv.count("profile") == 0);  // nothing changes until Hermes confirms
  r.server(R"({"type":"profile","profile":"ops"})");
  CHECK_EQ(r.fake.kv["profile"], std::string("ops"));
  CHECK(r.app.model().body.find("Ops") != std::string::npos);
  r.server(R"({"type":"profile","profile":"ops","error":"busy"})");  // a refusal keeps the current one
  CHECK_EQ(r.app.agent_profile(), std::string("ops"));

  Rig reboot;
  reboot.fake.kv = r.fake.kv;
  reboot.app.begin();
  reboot.app.on_network(true, "wifi");
  reboot.advance(1000);
  reboot.app.on_transport_open();
  const Value* hello = reboot.fake.last("hello");
  CHECK(hello != nullptr);
  if (hello) CHECK_EQ((*hello)["profile"].as_string(), std::string("ops"));
}

TEST("profiles: without a roster the settings menu has no profile entry") {
  Rig r;
  r.bring_online(true);
  CHECK(r.app.open_settings());
  for (int i = 0; i < 3; ++i) r.app.console("cancel");
  CHECK_EQ(r.app.model().detail, std::string("Microphone check"));
  CHECK(!r.app.next_agent_profile());
}

TEST("profiles: no switching while a turn runs or before pairing") {
  Rig r;
  r.bring_online_with_profiles();
  r.server(R"({"type":"turn.start","turn":"t"})");
  CHECK(!r.app.next_agent_profile());
  Rig unpaired;  // a roster, but not approved yet: only the pairing guard can refuse
  unpaired.app.begin();
  unpaired.app.on_network(true, "wifi");
  unpaired.advance(1000);
  unpaired.app.on_transport_open();
  unpaired.server(R"({"type":"challenge","nonce":"bm9uY2U=","enrolled":false})");
  unpaired.server(R"({"type":"welcome","session":"s1","heartbeat_s":20,"paired":false,"profile":"default",)"
                  R"("profiles":[{"id":"default","name":"Hermes"},{"id":"ops","name":"Ops"}]})");
  CHECK(!unpaired.app.next_agent_profile());
}

TEST("profiles: a stored profile Hermes no longer offers is dropped; the console cannot set one") {
  Rig r;
  r.fake.kv["profile"] = "gone";
  r.app.begin();
  r.app.on_network(true, "wifi");
  r.advance(1000);
  r.app.on_transport_open();
  const Value* hello = r.fake.last("hello");
  CHECK(hello != nullptr);
  if (hello) CHECK_EQ((*hello)["profile"].as_string(), std::string("gone"));
  r.server(R"({"type":"challenge","nonce":"bm9uY2U=","enrolled":false})");
  r.server(R"({"type":"welcome","session":"s1","heartbeat_s":20,"paired":true,"profile":"default",)"
           R"("profiles":[{"id":"default","name":"Hermes"},{"id":"ops","name":"Ops"}]})");
  CHECK_EQ(r.app.agent_profile(), std::string("default"));
  CHECK(r.fake.kv.count("profile") == 0);
  CHECK_EQ(r.app.console("set profile ops"), std::string("@error unknown key"));
  r.server(R"({"type":"profile","profile":"default","error":"busy"})");
  CHECK(r.app.model().detail.find("busy") != std::string::npos);  // refusals are visible on Ready
  r.server(R"({"type":"profile","profile":"ops"})");
  CHECK_EQ(r.fake.kv["profile"], std::string("ops"));
  r.app.console("factory-reset");
  CHECK(r.fake.kv.count("profile") == 0);  // fails without the explicit erase
}

namespace {
// First panel point that lands on the profile chip, or {-1, -1}.
std::pair<int, int> chip_point(Rig& r, int w, int h) {
  for (int y = 0; y < h; y += 2)
    for (int x = 0; x < w; x += 2)
      if (r.app.profile_hit(x, y)) return {x, y};
  return {-1, -1};
}
}  // namespace

TEST("touch: holding the profile chip for a second switches profile; it never starts talking") {
  Rig r(Rig::touch_profile());
  r.bring_online_with_profiles();
  CHECK_EQ(r.app.model().profile, std::string("Hermes"));
  auto [x, y] = chip_point(r, 320, 240);
  CHECK(x >= 0);
  // The settings target keeps a usable part of the top bar beside the chip.
  bool settings_left = false;
  for (int sx = 0; sx < 320; sx += 2) settings_left |= r.app.settings_title_hit(sx, 4) && !r.app.profile_hit(sx, 4);
  CHECK(settings_left);
  hg::TouchGestures touch(r.app);
  touch.update(true, x, y, r.fake.clock);
  r.advance(500);
  touch.tick(r.fake.clock);
  CHECK(r.fake.last("profile.select") == nullptr);
  CHECK(!r.fake.mic_on);
  r.advance(600);
  touch.tick(r.fake.clock);
  CHECK(r.fake.last("profile.select") != nullptr);
  touch.update(false, 0, 0, r.fake.clock);
  CHECK(!r.fake.mic_on);
  CHECK(r.fake.last("audio.start") == nullptr);
}

TEST("touch: the profile chip shows only on the Ready screen, and only with two or more profiles") {
  Rig r(Rig::touch_profile());
  r.bring_online(true);
  CHECK(r.app.model().profile.empty());
  CHECK_EQ(chip_point(r, 320, 240).first, -1);
  Rig busy(Rig::touch_profile());
  busy.bring_online_with_profiles();
  auto [x, y] = chip_point(busy, 320, 240);
  busy.server(R"({"type":"turn.start","turn":"t"})");  // Ready -> Thinking clears the chip
  CHECK(busy.app.model().profile.empty());
  CHECK_EQ(chip_point(busy, 320, 240).first, -1);
  CHECK(!busy.app.profile_hit(x, y));  // where it was is the title bar again (the settings target)
  busy.server(R"({"type":"turn.end","turn":"t"})");
  busy.app.open_settings();
  CHECK(busy.app.model().profile.empty());
  busy.app.close_settings();
  busy.advance(30000);  // past the reply's dwell, back on the hero Ready screen
  CHECK_EQ(busy.app.model().profile, std::string("Hermes"));
}

TEST("touch: a long profile name is cut to fit and its whole chip stays on the panel") {
  for (int round = 0; round < 2; ++round) {
    Rig r(Rig::touch_profile());
    const int size = round ? 466 : 320;
    if (round) r.fake.make_round(466);
    r.app.begin();
    r.app.on_network(true, "wifi");
    r.advance(1000);
    r.app.on_transport_open();
    r.server(R"({"type":"challenge","nonce":"bm9uY2U=","enrolled":false})");
    r.server(R"({"type":"welcome","session":"s1","heartbeat_s":20,"paired":true,"profile":"default",)"
             R"("profiles":[{"id":"default","name":"ABCDEFGHIJKLMNOPQRSTUVWX"},{"id":"ops","name":"Ops"}]})");
    int x0 = size, y0 = size, x1 = -1, y1 = -1;
    for (int y = 0; y < (round ? size : 240); ++y)
      for (int x = 0; x < size; ++x)
        if (r.app.profile_hit(x, y)) {
          x0 = std::min(x0, x); y0 = std::min(y0, y); x1 = std::max(x1, x); y1 = std::max(y1, y);
        }
    CHECK(x1 >= 0);
    CHECK(x1 - x0 + 1 <= size / 3 + 1);  // at most a third of the bar
    if (round) {
      const int c = size / 2;
      for (auto [cx, cy] : {std::pair{x0, y0}, {x1, y0}, {x0, y1}, {x1, y1}})
        CHECK((cx - c) * (cx - c) + (cy - c) * (cy - c) < c * c);  // every corner inside the circle
    }
  }
}

TEST("touch: the first touch on the profile chip of a sleeping display only wakes it") {
  Rig r(Rig::touch_profile());
  r.fake.backlight = true;
  r.bring_online_with_profiles();
  auto [x, y] = chip_point(r, 320, 240);
  CHECK(x >= 0);
  r.app.console("set screen_timeout 30");
  r.advance(31000);  // asleep, and still inside the heartbeat window
  CHECK(r.app.status_json().find("\"display_sleeping\":true") != std::string::npos);
  hg::TouchGestures touch(r.app);
  touch.update(true, x, y, r.fake.clock);
  r.advance(1100);
  touch.tick(r.fake.clock);
  touch.update(false, 0, 0, r.fake.clock);
  CHECK(r.fake.last("profile.select") == nullptr);
  CHECK(r.app.status_json().find("\"display_sleeping\":false") != std::string::npos);
}

TEST("touch: the profile chip and the settings target never share a point, and each hold does its own thing") {
  for (int round = 0; round < 2; ++round) {
    const int w = round ? 466 : 320, h = round ? 466 : 240;
    Rig r(Rig::touch_profile());
    if (round) r.fake.make_round(w);
    r.bring_online_with_profiles();
    std::pair<int, int> chip{-1, -1}, settings{-1, -1};
    int shared = 0;
    for (int y = 0; y < h; ++y)
      for (int x = 0; x < w; ++x) {
        const bool on_chip = r.app.profile_hit(x, y), on_settings = r.app.settings_title_hit(x, y);
        shared += on_chip && on_settings;
        if (on_chip && chip.first < 0) chip = {x, y};
        if (on_settings && settings.first < 0) settings = {x, y};
      }
    CHECK_EQ(shared, 0);
    CHECK(chip.first >= 0);
    CHECK(settings.first >= 0);
    hg::TouchGestures touch(r.app);
    touch.update(true, settings.first, settings.second, r.fake.clock);
    r.advance(1100);
    touch.tick(r.fake.clock);
    touch.update(false, 0, 0, r.fake.clock);
    CHECK(r.app.settings_open());
    CHECK(r.fake.last("profile.select") == nullptr);
    r.app.close_settings();
    touch.update(true, chip.first, chip.second, r.fake.clock);
    r.advance(1100);
    touch.tick(r.fake.clock);
    touch.update(false, 0, 0, r.fake.clock);
    CHECK(r.fake.last("profile.select") != nullptr);
    CHECK(!r.app.settings_open());
  }
}

namespace {
// What a fresh render of `m` paints on a round panel `size` pixels across.
std::vector<uint16_t> render_round(int size, const hg::UiModel& m) {
  FakeHal panel;
  panel.make_round(size);
  hg::Ui ui(panel);
  ui.render(m);
  return panel.fb;
}
}  // namespace

TEST("ui: on round panels the profile chip stays in the bottom band, clear of the SETTINGS target") {
  for (int size : {360, 466}) {
    FakeHal panel;
    panel.make_round(size);
    hg::Ui ui(panel);
    hg::UiModel m;
    m.screen = hg::Screen::Ready;
    m.link = hg::Link::Online;
    m.hero = true;
    m.headline = "Hi, I'm Hermes";
    m.profile = "default";
    const int bottom = (size - ui.area().height) / 2 + ui.area().height - ui.layout().bottom_h;
    int chip = 0, shared = 0, above = 0;
    for (int y = 0; y < size; ++y)
      for (int x = 0; x < size; ++x)
        if (ui.profile_hit(m, x, y)) {  // the raw geometry, before the app takes the chip out of the target
          ++chip;
          shared += ui.title_hit(x, y);
          above += y < bottom;
        }
    CHECK(chip > 0);
    CHECK_EQ(shared, 0);
    CHECK_EQ(above, 0);
    for (bool hold : {true, false}) {
      m.settings_hold = hold;
      hg::UiModel plain = m;
      plain.profile.clear();
      const std::vector<uint16_t> with = render_round(size, m), without = render_round(size, plain);
      int painted = 0, painted_above = 0;
      for (size_t i = 0; i < with.size(); ++i)
        if (with[i] != without[i]) {
          ++painted;
          painted_above += static_cast<int>(i) / size < bottom;
        }
      CHECK(painted > 0);
      CHECK_EQ(painted_above, 0);
    }
  }
}

TEST("touch: a profile hold that ends after Wi-Fi setup opens does not switch") {
  Rig r(Rig::touch_profile());
  r.app.on_wifi_setup = [] { return "Temporary setup network"; };
  r.bring_online_with_profiles();
  auto [x, y] = chip_point(r, 320, 240);
  CHECK(x >= 0);
  hg::TouchGestures touch(r.app);
  touch.update(true, x, y, r.fake.clock);
  r.advance(200);
  CHECK(r.app.start_wifi_setup());
  r.advance(900);
  touch.tick(r.fake.clock);
  touch.update(false, 0, 0, r.fake.clock);
  CHECK(r.fake.last("profile.select") == nullptr);
  CHECK(!r.app.next_agent_profile());
}
