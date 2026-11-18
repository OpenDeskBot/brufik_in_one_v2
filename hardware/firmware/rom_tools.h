#pragma once
#include <stddef.h>
#include <stdint.h>
bool rom_tools_handle_frame(uint8_t* data,size_t length);
void rom_tools_report(const char* id,bool ok,const char* status,const char* error=nullptr,int volume=-1);
