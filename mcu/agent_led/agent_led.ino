// Indicator LED for the home agent, driven from Linux over the Bridge.
// Linux calls agent_led(pin, level, fade_ms): level is 0-255 PWM duty
// (Linux applies gamma), and the LED fades there over fade_ms.

#include <Arduino_RouterBridge.h>

static int pin = -1;
static int level = 0;       // current duty
static int fadeFrom = 0;
static int target = 0;
static int fadeMs = 0;
static unsigned long fadeStart = 0;
static bool registered = false;
static unsigned long lastTry = 0;

void agent_led(int p, int lvl, int ms) {
    if (p != pin) {
        if (pin >= 0) analogWrite(pin, 0);  // release the old pin
        pin = p;
        pinMode(pin, OUTPUT);
        level = 0;
        analogWrite(pin, 0);
    }
    fadeFrom = level;
    target = constrain(lvl, 0, 255);
    fadeMs = max(ms, 0);
    fadeStart = millis();
}

void setup() {
    // The loader's boot output reaches the router just before us; if our handshake
    // arrives in the same chunk the router discards both and Bridge.begin() waits
    // forever. Let that noise go through on its own first.
    delay(2000);
    Bridge.begin();
    registered = Bridge.provide("agent_led", agent_led);
    lastTry = millis();
}

void loop() {
    // At power-on the router may not be listening yet (or boot noise garbles the
    // first message), so keep offering the function until the router accepts it.
    if (!registered && millis() - lastTry > 2000) {
        registered = Bridge.provide("agent_led", agent_led);
        lastTry = millis();
    }
    if (pin < 0 || level == target) return;
    unsigned long t = millis() - fadeStart;
    int v = (fadeMs == 0 || t >= (unsigned long)fadeMs)
        ? target
        : fadeFrom + (long)(target - fadeFrom) * (long)t / fadeMs;
    if (v != level) {
        level = v;
        analogWrite(pin, level);
    }
}
