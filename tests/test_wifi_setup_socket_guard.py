"""Exercise the real ESP setup address guard with deterministic socket/header fakes."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_setup_socket_guard_accepts_mapped_ipv4_without_relaxing_origin(tmp_path):
    compiler = shutil.which("g++") or shutil.which("clang++")
    if not compiler:
        pytest.skip("C++ compiler unavailable")
    assert compiler is not None
    source = (ROOT / "firmware/esp32/main/port_wifi_setup.cpp").read_text()
    start = source.index("bool socket_ipv4(") if "bool socket_ipv4(" in source else source.index("bool local_request(")
    guard = source[start:source.index("}  // namespace", start)]
    cpp = r'''
#include <cassert>
#include <cstring>
#include <cstdint>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#define CONFIG_LWIP_IPV6 1
struct httpd_req_t {};
struct Ip { uint32_t addr=0; };
struct esp_netif_ip_info_t { Ip ip,netmask; };
constexpr int ESP_OK=0;
int ap,sta;
sockaddr_storage local_addr{},peer_addr{};
esp_netif_ip_info_t ap_info,sta_info;
const char* host="192.168.4.1";
const char* origin=nullptr;
void* esp_netif_get_handle_from_ifkey(const char* key) {
  return std::strcmp(key,"WIFI_AP_DEF")==0 ? &ap : &sta;
}
int esp_netif_get_ip_info(void* p,esp_netif_ip_info_t* out) {
  *out=p==&ap ? ap_info : sta_info;return ESP_OK;
}
int httpd_req_to_sockfd(httpd_req_t*) { return 1; }
int fake_name(sockaddr* out,socklen_t* len,const sockaddr_storage& input) {
  socklen_t needed=input.ss_family==AF_INET6 ? sizeof(sockaddr_in6) : sizeof(sockaddr_in);
  std::memcpy(out,&input,*len<needed ? *len : needed);*len=needed;return 0;
}
int fake_getsockname(int,sockaddr* out,socklen_t* n) { return fake_name(out,n,local_addr); }
int fake_getpeername(int,sockaddr* out,socklen_t* n) { return fake_name(out,n,peer_addr); }
#define getsockname fake_getsockname
#define getpeername fake_getpeername
int httpd_req_get_hdr_value_str(httpd_req_t*,const char* name,char* out,size_t size) {
  const char* value=std::strcmp(name,"Host")==0 ? host : origin;
  if (!value || std::strlen(value)>=size) return -1;
  std::strcpy(out,value);return ESP_OK;
}
size_t httpd_req_get_hdr_value_len(httpd_req_t*,const char*) { return origin ? std::strlen(origin) : 0; }
void address(sockaddr_storage& dst,const char* ip,bool mapped) {
  std::memset(&dst,0,sizeof(dst));
  if (mapped) {
    auto* a=reinterpret_cast<sockaddr_in6*>(&dst);a->sin6_family=AF_INET6;
    a->sin6_addr.s6_addr[10]=0xff;a->sin6_addr.s6_addr[11]=0xff;
    auto v=inet_addr(ip);std::memcpy(&a->sin6_addr.s6_addr[12],&v,4);
  } else {
    auto* a=reinterpret_cast<sockaddr_in*>(&dst);a->sin_family=AF_INET;
    a->sin_addr.s_addr=inet_addr(ip);
  }
}
'''
    cpp += guard + r'''
int main() {
  httpd_req_t req;
  ap_info.ip.addr=inet_addr("192.168.4.1");ap_info.netmask.addr=inet_addr("255.255.255.0");
  address(local_addr,"192.168.4.1",false);address(peer_addr,"192.168.4.2",false);
  assert(local_request(&req));
  address(local_addr,"192.168.4.1",true);address(peer_addr,"192.168.4.2",true);
  assert(local_request(&req)); // ESP-IDF dual-stack sockets must accept the AP client.
  address(peer_addr,"192.168.1.2",true);assert(!local_request(&req));
  address(peer_addr,"192.168.4.2",true);
  address(local_addr,"192.168.1.10",true);assert(!local_request(&req));
  address(local_addr,"192.168.4.1",true);
  host="example.com";assert(!local_request(&req));host="192.168.4.1";
  origin="https://evil.example";assert(!local_request(&req));
  origin="http://192.168.4.1";assert(local_request(&req));origin=nullptr;
  sta_info.ip.addr=inet_addr("192.168.4.9");sta_info.netmask.addr=inet_addr("255.255.255.0");
  assert(!local_request(&req));sta_info.ip.addr=0;
  auto* peer=reinterpret_cast<sockaddr_in6*>(&peer_addr);
  inet_pton(AF_INET6,"2001:db8::2",&peer->sin6_addr);assert(!local_request(&req));
}
'''
    file = tmp_path / "setup_socket_guard.cpp"
    file.write_text(cpp)
    binary = tmp_path / "setup_socket_guard"
    subprocess.run([compiler, "-std=c++17", str(file), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
