#pragma once
#include <Arduino.h>

// Transform one rtl_433-format JSON line into the Zasder Weather
// /ingest/custom payload shape and POST it. backendUrl is the bare
// base ("https://weather.example.com"); we append /ingest/custom.
// Silently skips the POST if either backendUrl or ingestToken is
// empty (lets the board run decode-only for bench testing).
void zasder_post(const char *rtl433Json,
                 const String &backendUrl,
                 const String &ingestToken);
