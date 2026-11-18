#pragma once
#include "pb_model.h"

// Only bootstrap installs a package, before any network/media consumer starts.
// The immutable document owns all strings until reboot; selected frames are copied.
bool face_package_install(const uint8_t* bytes, size_t length, const char* expected_version = nullptr);
bool face_package_validate(const uint8_t* bytes, size_t length, const char* expected_version);
const char* face_package_version();
const char* face_package_mouth(bool open);
JsonObjectConst face_package_idle();
bool face_package_load(const char* id, bool mouth, uint32_t requested_ms, int frame,
                       pb_anim_frame*& out, size_t& count, uint32_t& duration, const char*& error);
