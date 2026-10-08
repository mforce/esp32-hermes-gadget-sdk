#include "app_rig.hpp"

using namespace hg_test;

namespace {

// The server's side of a firmware update, as the hub runs it.
struct OtaServer {
  Rig& r;
  std::vector<uint8_t> image;
  std::string sha;
  uint8_t stream = 9;
  uint16_t seq = 0;

  OtaServer(Rig& rig, size_t size) : r(rig), image(size) {
    for (size_t i = 0; i < size; ++i) image[i] = static_cast<uint8_t>(i * 7);
    hg::crypto::Digest d = hg::crypto::sha256(image.data(), image.size());
    sha = hg::crypto::hex(d.data(), d.size());
  }

  std::string key_b64() const { return r.fake.kv.at("device_key"); }

  std::string offer(size_t size = 0) {
    r.fake.sent.clear();
    r.server(R"({"type":"ota.offer","stream":9,"version":"9.9.9","size":)" +
             std::to_string(size ? size : image.size()) + R"(,"sha256":")" + sha + "\"}");
    const Value* ready = r.fake.last("ota.ready");
    return ready ? (*ready)["nonce"].as_string() : std::string();
  }

  void begin(const std::string& nonce, bool wrong_key = false) {
    std::vector<uint8_t> key;
    hg::crypto::base64_decode(key_b64(), key);
    if (wrong_key) key[0] ^= 1;
    std::string mac = hg::proto::ota_mac(key.data(), key.size(), r.app.device_id(), nonce, sha, image.size());
    r.server(R"({"type":"ota.begin","mac":")" + mac + "\"}");
  }

  void chunk(size_t off, size_t len) {
    std::vector<uint8_t> frame(hg::proto::kBinaryHeader + len);
    hg::proto::write_binary_header(frame.data(), hg::proto::Channel::Firmware, stream, seq++);
    std::copy(image.begin() + static_cast<long>(off), image.begin() + static_cast<long>(off + len),
              frame.begin() + hg::proto::kBinaryHeader);
    r.app.on_transport_binary(frame.data(), frame.size());
  }

  void send_all() {
    for (size_t off = 0; off < image.size(); off += 4096) chunk(off, std::min<size_t>(4096, image.size() - off));
  }

  std::string error_code() const {
    const Value* e = r.fake.last("ota.error");
    return e ? (*e)["code"].as_string() : std::string();
  }
};

}  // namespace

TEST("ota: an authorized image streams in, is checked, and the device restarts into it") {
  FakeUpdater upd;
  Rig r;
  r.hal.updater = &upd;
  r.bring_online(true);
  const Value* hello = r.fake.last("hello");
  CHECK(hello && (*hello)["caps"]["ota"]["max_size"].as_int() == 64 * 1024);

  OtaServer s(r, 40000);
  std::string nonce = s.offer();
  CHECK(!nonce.empty());
  s.begin(nonce);
  const Value* ack = r.fake.last("ota.ack");
  CHECK(ack && (*ack)["offset"].as_int() == 0);
  CHECK(upd.open);
  CHECK_EQ(r.app.screen(), hg::Screen::Updating);
  CHECK(r.app.model().detail.find("9.9.9: 0% of 40 KB") != std::string::npos);

  s.send_all();
  ack = r.fake.last("ota.ack");
  CHECK(ack && (*ack)["offset"].as_int() == 40000);  // 16 KB steps, then the end
  CHECK_EQ(r.app.model().detail, std::string("firmware 9.9.9: 100% of 40 KB"));
  r.server(R"({"type":"ota.end"})");
  const Value* done = r.fake.last("ota.done");
  CHECK(done && (*done)["version"].as_string() == "9.9.9");
  CHECK(upd.installed && upd.image == s.image);
  CHECK(!upd.restarted);  // ota.done leaves first
  r.advance(1100);
  CHECK(upd.restarted);
}

TEST("ota: the device refuses images it can't trust or hold") {
  FakeUpdater upd;
  Rig r;
  r.hal.updater = &upd;
  r.bring_online(true);
  OtaServer s(r, 20000);

  s.begin(s.offer(), /*wrong_key=*/true);
  CHECK_EQ(s.error_code(), std::string("unauthorized"));
  CHECK(!upd.open);

  s.offer(100 * 1024);
  CHECK_EQ(s.error_code(), std::string("too_large"));

  r.server(R"({"type":"ota.begin","mac":"x"})");
  CHECK_EQ(s.error_code(), std::string("no_offer"));

  // A wrong SHA-256: the bytes arrive, but don't match what was offered.
  s.begin(s.offer());
  s.image[5] ^= 0xFF;
  s.send_all();
  r.server(R"({"type":"ota.end"})");
  CHECK_EQ(s.error_code(), std::string("checksum"));
  CHECK(!upd.installed);

  // The port rejects the image itself.
  s.image[5] ^= 0xFF;
  s.seq = 0;
  upd.reject_image = true;
  s.begin(s.offer());
  s.send_all();
  r.server(R"({"type":"ota.end"})");
  CHECK_EQ(s.error_code(), std::string("invalid"));
  CHECK(r.fake.last("ota.done") == nullptr);
  CHECK(r.app.model().detail.find("Update failed: not an app image") != std::string::npos);
}

TEST("ota: a missing chunk, a stall or a dropped connection abandons the update") {
  FakeUpdater upd;
  Rig r;
  r.hal.updater = &upd;
  r.bring_online(true);
  OtaServer s(r, 20000);

  s.begin(s.offer());
  s.chunk(0, 4096);
  ++s.seq;  // skip one
  s.chunk(8192, 4096);
  CHECK_EQ(s.error_code(), std::string("sequence"));
  CHECK(!upd.open);
  CHECK(r.app.screen() != hg::Screen::Updating);

  s.seq = 0;
  s.begin(s.offer());
  s.chunk(0, 4096);
  r.advance(31000);
  CHECK_EQ(s.error_code(), std::string("timeout"));
  CHECK(!upd.open);

  s.seq = 0;
  s.begin(s.offer());
  s.chunk(0, 4096);
  r.app.on_transport_closed("gone");
  CHECK(!upd.open);
  CHECK(r.app.status_json().find("\"update\"") == std::string::npos);

  // Devices without an update slot don't advertise one, and say so if offered an image.
  Rig plain;
  plain.bring_online(true);
  const Value* hello = plain.fake.last("hello");
  CHECK(hello && !(*hello)["caps"]["ota"].is_object());
  OtaServer p(plain, 100);
  p.offer();
  CHECK_EQ(p.error_code(), std::string("unsupported"));
}

TEST("ota: a newly installed firmware is kept once it reaches Hermes") {
  FakeUpdater upd;
  upd.pending = true;
  Rig r;
  r.hal.updater = &upd;
  r.app.begin();
  r.app.on_network(true, "test-wifi");
  r.advance(1000);
  r.app.on_transport_open();
  r.server(R"({"type":"challenge","nonce":"bm9uY2U=","enrolled":false})");
  CHECK(!upd.confirmed);  // authenticating isn't enough
  r.server(R"({"type":"welcome","session":"s1","heartbeat_s":20,"paired":false})");
  CHECK(upd.confirmed);
}
