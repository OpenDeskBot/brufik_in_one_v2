"""Run production credential storage and connection order against fake NVS/radio."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("c++"), reason="native compiler unavailable")
def test_saved_wifi_moves_preserve_passwords_and_control_connection_order(tmp_path):
    firmware = Path(__file__).resolve().parents[1] / "firmware"
    headers = {
        "Arduino.h": r'''
#pragma once
#include <cstdint>
#include <cstdio>
#include <string>
class String : public std::string {
 public:
  using std::string::string;
  String(const std::string& s):std::string(s){}
  void trim(){auto a=find_first_not_of(" \r\n\t");
    if(a==npos){clear();return;} auto b=find_last_not_of(" \r\n\t");
    *this=substr(a,b-a+1);}
};
struct FakeSerial {
  template<class... A> void printf(const char*, A...){}
  void println(const char*){} void print(const char*){}
};
inline FakeSerial Serial;
inline void delay(unsigned long){}
''',
        "Preferences.h": r'''
#pragma once
#include "Arduino.h"
#include <map>
class Preferences {
  using Values=std::map<std::string,String>;
  inline static std::map<std::string,Values> store;
  std::string ns;
 public:
  bool begin(const char* name,bool){ns=name;return true;}
  void end(){}
  bool isKey(const char* key){return store[ns].count(key);}
  String getString(const char* key,const char* fallback=""){
    auto i=store[ns].find(key);return i==store[ns].end()?String(fallback):i->second;}
  size_t putString(const char* key,const String& value){store[ns][key]=value;return value.size();}
  unsigned getUInt(const char* key,unsigned fallback=0){
    return isKey(key)?std::stoul(store[ns][key]):fallback;}
  uint8_t getUChar(const char* key,uint8_t fallback=0){return getUInt(key,fallback);}
  size_t putUInt(const char* key,unsigned value){store[ns][key]=std::to_string(value);return 4;}
  size_t putUChar(const char* key,uint8_t value){return putUInt(key,value);}
  bool remove(const char* key){return store[ns].erase(key);}
  void clear(){store[ns].clear();}
};
''',
        "esp_random.h": "#pragma once\n#include <cstdint>\ninline uint32_t esp_random(){return 1234;}\n",
        "esp_wifi.h": r'''
#pragma once
using esp_err_t=int;
constexpr int ESP_OK=0, WIFI_COUNTRY_POLICY_MANUAL=1, WIFI_PS_NONE=0;
struct wifi_country_t {char cc[3];int schan,nchan,max_tx_power,policy;};
inline int esp_wifi_set_country(const wifi_country_t*){return ESP_OK;}
inline void esp_wifi_set_ps(int){}
''',
        "wifi_provision_internal.h": r'''
#pragma once
#include "Arduino.h"
#include <vector>
enum wl_status_t {WL_IDLE_STATUS,WL_NO_SSID_AVAIL,WL_SCAN_COMPLETED,WL_CONNECTED,
                  WL_CONNECT_FAILED,WL_CONNECTION_LOST,WL_DISCONNECTED};
constexpr int WIFI_STA=1,WIFI_POWER_19_5dBm=78,kWifiMaxReconnectAttempts=40;
struct IPAddress {
  IPAddress(int=0,int=0,int=0,int=0){}
  bool operator!=(const IPAddress&)const{return true;}
  String toString()const{return "192.168.1.2";}
};
struct FakeWifi {
  struct Network {String ssid;int rssi,channel;};
  struct Attempt {String ssid,password;int channel;};
  std::vector<Network> scan;
  std::vector<Attempt> attempts;
  String success,current;
  void persistent(bool){} void setAutoReconnect(bool){} void setSleep(bool){}
  void setTxPower(int){} void mode(int){}
  void disconnect(bool,bool){current.clear();}
  int scanNetworks(){return scan.size();} void scanDelete(){}
  String SSID(int i)const{return scan[i].ssid;} String SSID()const{return current;}
  int RSSI(int i)const{return scan[i].rssi;} int RSSI()const{return -40;}
  int channel(int i)const{return scan[i].channel;}
  void begin(const char* ssid,const char* password,int channel=0){
    attempts.push_back({ssid,password,channel});current=ssid;}
  wl_status_t status()const{return current==success&&!success.empty()?WL_CONNECTED:WL_CONNECT_FAILED;}
  IPAddress localIP()const{return {};}
};
inline FakeWifi WiFi;
extern String g_wifi_ssid,g_wifi_password;
extern bool g_wifi_was_up;
void wifi_apply_runtime_keepalive();
inline void wifi_register_event_handlers_once(){}
''',
        "boot_guide.h": r'''
#pragma once
inline void boot_guide_wifi_result(bool,const char*,const char*){}
inline void boot_guide_wifi_connecting(const char*){}
inline void boot_guide_wifi_on_connected(const char*,const char*){}
''',
        "deskbot_config.h": '#pragma once\n#define WIFI_DEFAULT_SSID "compiled-default"\n#define WIFI_DEFAULT_PASSWORD "default-pass"\n',
    }
    for name, content in headers.items():
        (tmp_path / name).write_text(content)
    # Keep the production translation unit intact; only hardware includes are replaced.
    shutil.copyfile(firmware / "wifi_sta.cpp", tmp_path / "wifi_sta.cpp")
    source = tmp_path / "check.cpp"
    source.write_text(r'''
#include "wifi_provision_internal.h"
#include "utils/nvs_config_utils.h"
#include <cassert>
String g_wifi_ssid,g_wifi_password;
bool g_wifi_was_up=false;
bool wifi_connect_saved_then_default(const char*,int);
void check_order(const char* first,const char* second,const char* third){
  NvsWifiCredential saved[NVS_MAX_SAVED_WIFI];
  assert(nvs_wifi_list(saved,NVS_MAX_SAVED_WIFI)==3);
  assert(saved[0].ssid==first && saved[1].ssid==second && saved[2].ssid==third);
  for(int i=0;i<3;i++) {
    if(saved[i].ssid=="Weak")assert(saved[i].password=="weak-pass");
    if(saved[i].ssid=="Hidden")assert(saved[i].password=="hidden-pass");
    if(saved[i].ssid=="Strong")assert(saved[i].password=="strong-pass");
  }
}
int main(){
  nvs_wifi_clear();
  assert(!nvs_wifi_move("missing",1));
  assert(nvs_wifi_upsert("Strong","strong-pass"));
  assert(nvs_wifi_upsert("Weak","weak-pass"));
  assert(nvs_wifi_upsert("Hidden","hidden-pass"));
  check_order("Hidden","Weak","Strong");
  assert(nvs_wifi_move("Hidden",1));
  check_order("Weak","Hidden","Strong");
  assert(!nvs_wifi_move("Weak",-1));
  assert(!nvs_wifi_move("Strong",1));
  assert(!nvs_wifi_move("missing",-1));
  assert(!nvs_wifi_move("Weak",0));
  assert(!nvs_wifi_move("Weak",2));
  assert(!nvs_wifi_move(nullptr,1));
  check_order("Weak","Hidden","Strong");

  // Scan/RSSI order cannot override saved priority. Within one SSID use its best AP.
  WiFi.scan={{"Strong",-30,1},{"Weak",-88,6},{"Weak",-45,11}};
  WiFi.success="Weak";
  assert(wifi_connect_saved_then_default("test",1));
  assert(WiFi.attempts.size()==1 && WiFi.attempts[0].ssid=="Weak");
  assert(WiFi.attempts[0].channel==11 && WiFi.attempts[0].password=="weak-pass");

  // Failed higher priorities fall through in order, including an unscanned hidden SSID.
  WiFi.attempts.clear();WiFi.success="Strong";
  assert(wifi_connect_saved_then_default("test",1));
  assert(WiFi.attempts.size()==3);
  assert(WiFi.attempts[0].ssid=="Weak" && WiFi.attempts[1].ssid=="Hidden");
  assert(WiFi.attempts[1].channel==0 && WiFi.attempts[1].password=="hidden-pass");
  assert(WiFi.attempts[2].ssid=="Strong");

  assert(nvs_wifi_move("Strong",-1));
  assert(nvs_wifi_move("Strong",-1));
  check_order("Strong","Weak","Hidden");
  WiFi.attempts.clear();
  assert(wifi_connect_saved_then_default("test",1));
  assert(WiFi.attempts.size()==1 && WiFi.attempts[0].ssid=="Strong");
  check_order("Strong","Weak","Hidden");

  // Compile-time default stays last; full failure restores the user's first preference.
  WiFi.attempts.clear();WiFi.success.clear();
  assert(!wifi_connect_saved_then_default("test",1));
  assert(WiFi.attempts.size()==4 && WiFi.attempts.back().ssid=="compiled-default");
  assert(g_wifi_ssid=="Strong" && g_wifi_password=="strong-pass");
  assert(nvs_wifi_delete("Weak"));
  NvsWifiCredential saved[NVS_MAX_SAVED_WIFI];
  assert(nvs_wifi_list(saved,NVS_MAX_SAVED_WIFI)==2);
  assert(saved[0].ssid=="Strong" && saved[1].ssid=="Hidden");
  assert(nvs_wifi_upsert("Hidden","new-hidden-pass"));
  assert(nvs_wifi_list(saved,NVS_MAX_SAVED_WIFI)==2);
  assert(saved[0].ssid=="Hidden" && saved[0].password=="new-hidden-pass");
  assert(saved[1].ssid=="Strong" && saved[1].password=="strong-pass");

  // Empty edits keep credentials, explicit open-network edits clear them without reordering.
  assert(nvs_wifi_update_password("Strong","changed-pass"));
  assert(nvs_wifi_list(saved,NVS_MAX_SAVED_WIFI)==2);
  assert(saved[0].ssid=="Hidden" && saved[1].ssid=="Strong");
  assert(saved[1].password=="changed-pass");
  assert(nvs_wifi_update_password("Strong",""));
  assert(nvs_wifi_update_password("Strong",nullptr));
  assert(nvs_wifi_list(saved,NVS_MAX_SAVED_WIFI)==2 && saved[1].password=="changed-pass");
  assert(!nvs_wifi_update_password("missing","password"));
  assert(!nvs_wifi_update_password(nullptr,"password"));
  assert(nvs_wifi_update_password("Strong","ignored",true));
  assert(nvs_wifi_list(saved,NVS_MAX_SAVED_WIFI)==2 && saved[1].password.empty());
  assert(saved[0].ssid=="Hidden" && saved[1].ssid=="Strong");
  assert(nvs_wifi_upsert("Hidden",""));
  assert(nvs_wifi_list(saved,NVS_MAX_SAVED_WIFI)==2 && saved[0].password=="new-hidden-pass");
  assert(nvs_wifi_upsert("Hidden","",false));
  assert(nvs_wifi_list(saved,NVS_MAX_SAVED_WIFI)==2 && saved[0].password.empty());
  assert(nvs_wifi_upsert("New open network",""));
  assert(nvs_wifi_list(saved,NVS_MAX_SAVED_WIFI)==3 && saved[0].password.empty());

  // Even when random output repeats, reset must generate a different persistent identity.
  const auto old_suffix=nvs_get_device_suffix();
  const auto new_suffix=nvs_reset_device_suffix();
  assert(new_suffix!=old_suffix && new_suffix==nvs_get_device_suffix());
  assert(new_suffix>=1000 && new_suffix<=9999);
}
''')
    executable = tmp_path / "check"
    subprocess.run(
        [
            "c++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
            "-I", str(tmp_path), "-I", str(firmware),
            str(source), str(tmp_path / "wifi_sta.cpp"),
            str(firmware / "utils/nvs_config_utils.cpp"), "-o", str(executable),
        ],
        check=True,
    )
    subprocess.run([str(executable)], check=True)
