#pragma once
#include <WebSocketsClient.h>
#include "utils/utils.h"

// Use the same IDF CA bundle as HTTP bootstrap; never downgrade WSS to an
// unverified TLS connection after discovering a verified HTTPS configuration.
extern const uint8_t kServerCaBundleStart[] asm("_binary_x509_crt_bundle_start");
extern const uint8_t kServerCaBundleEnd[] asm("_binary_x509_crt_bundle_end");
inline void server_begin_wss(WebSocketsClient& client,const WsProto& target,const char* path) {
  client.beginSslWithBundle(target.host,target.port,path,kServerCaBundleStart,
                           kServerCaBundleEnd-kServerCaBundleStart);
}
