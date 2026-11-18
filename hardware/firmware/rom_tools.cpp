#include "rom_tools.h"
#include "rom_control.h"
#include "display.h"
#include "speaker.h"
#include "ws_transport.h"
#include "utils/utils.h"
#include <ArduinoJson.h>
#include <cstring>
#include <cctype>
#include "esp_heap_caps.h"
namespace {
struct CachedResult {char id[33]{};uint32_t hash=0;bool pending=false;char json[256]{};};
static CachedResult s_cache[8];
static portMUX_TYPE s_cache_lock=portMUX_INITIALIZER_UNLOCKED;
static unsigned s_next_slot=0;
uint32_t fingerprint(const uint8_t* data,size_t size) {
  uint32_t hash=2166136261u;for(size_t i=0;i<size;++i) hash=(hash^data[i])*16777619u;return hash;
}
}
void rom_tools_report(const char* id,bool ok,const char* status,const char* error,int volume) {
  JsonDocument out;out["type"]="rom_result";out["id"]=id;out["ok"]=ok;out["status"]=status;
  if(error) out["error"]=error;if(volume>=0) out["volume"]=volume;
  if(strcmp(status,"displayed")==0){out["width"]=DESKBOT_DRAW_W;out["height"]=DESKBOT_DRAW_H;}
  char json[256];serializeJson(out,json,sizeof(json));
  portENTER_CRITICAL(&s_cache_lock);
  for(auto& item:s_cache) if(strcmp(item.id,id)==0){strlcpy(item.json,json,sizeof(item.json));item.pending=false;break;}
  portEXIT_CRITICAL(&s_cache_lock);ws_transport_enqueue_state(json);
}
bool rom_tools_handle_frame(uint8_t* data,size_t length) {
  PackedFrame frame;
  if(!parse_packed_frame(data,length,frame) || strcmp(frame.doc["type"]|"","rom_command")!=0) return false;
  const char* id=frame.doc["id"]|"";if(strlen(id)!=32) return true;
  for(size_t i=0;i<32;++i) if(!isxdigit((unsigned char)id[i])) return true;
  const uint32_t hash=fingerprint(data,length);
  char previous[256]{};bool duplicate=false,mismatch=false;int slot=-1;
  portENTER_CRITICAL(&s_cache_lock);
  for(unsigned i=0;i<8;++i){
    if(strcmp(s_cache[i].id,id)==0){duplicate=true;mismatch=s_cache[i].hash!=hash;strlcpy(previous,s_cache[i].json,sizeof(previous));break;}
  }
  if(!duplicate) for(unsigned offset=0;offset<8;++offset){
    const unsigned i=(s_next_slot+offset)%8;if(!s_cache[i].pending){slot=(int)i;s_next_slot=(i+1)%8;break;}
  }
  if(!duplicate && slot>=0){s_cache[slot]=CachedResult{};strlcpy(s_cache[slot].id,id,sizeof(s_cache[slot].id));s_cache[slot].hash=hash;s_cache[slot].pending=true;}
  portEXIT_CRITICAL(&s_cache_lock);
  if(duplicate){if(mismatch) rom_tools_report(id,false,"error","command_id_reused");else if(previous[0]) ws_transport_enqueue_state(previous);return true;}
  if(slot<0){rom_tools_report(id,false,"error","device_busy");return true;}
  const char* op=frame.doc["op"]|"";JsonObjectConst args=frame.doc["arguments"].as<JsonObjectConst>();
  if(strcmp(op,"volume")==0 && frame.bin_len==0){
    const char* action=args["action"]|"";int value=args["value"]|0,target=0;rom_control::VolumeAction mode;
    if(strcmp(action,"get")==0) mode=rom_control::VolumeAction::Get;
    else if(strcmp(action,"set")==0) mode=rom_control::VolumeAction::Set;
    else if(strcmp(action,"adjust")==0) mode=rom_control::VolumeAction::Adjust;
    else{rom_tools_report(id,false,"error","invalid_volume_action");return true;}
    if((mode!=rom_control::VolumeAction::Get && args["value"].isNull()) ||
       (!args["value"].isNull() && (!args["value"].is<int>() || args["value"].is<bool>())) || !rom_control::volume_target(speaker_get_volume(),value,mode,target)){
      rom_tools_report(id,false,"error","invalid_volume_value");return true;
    }
    speaker_set_volume(target);rom_tools_report(id,true,"completed",nullptr,speaker_get_volume());return true;
  }
  if(strcmp(op,"stop_image")==0 && frame.bin_len==0){display_stop_image();rom_tools_report(id,true,"completed");return true;}
  if(strcmp(op,"image")==0){
    const unsigned w=args["width"]|0u,h=args["height"]|0u,ms=args["duration_ms"]|0u;
    if(!rom_control::image_size(w,h,frame.bin_len,DESKBOT_DRAW_W,DESKBOT_DRAW_H) || (frame.doc["next_bin_len"]|0u)!=(unsigned)frame.bin_len || ms<1000 || ms>30000){
      rom_tools_report(id,false,"error","invalid_image");return true;
    }
    auto* pixels=(uint16_t*)heap_caps_malloc(frame.bin_len,MALLOC_CAP_SPIRAM);
    if(!pixels){rom_tools_report(id,false,"error","image_allocation_failed");return true;}
    for(size_t i=0;i<(size_t)w*h;++i) pixels[i]=(uint16_t)frame.bin[2*i] | ((uint16_t)frame.bin[2*i+1]<<8);
    if(!display_submit_image_owned(pixels,ms,id)) rom_tools_report(id,false,"error","display_queue_full");return true;
  }
  rom_tools_report(id,false,"error","unsupported_command");return true;
}
