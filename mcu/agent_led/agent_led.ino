// Indicator LED and listening switch for the home agent, over the Bridge.
// Linux calls agent_led(pin, level, fade_ms): level is 0-255 PWM duty
// (Linux applies gamma), and the LED fades there over fade_ms.
// Linux calls agent_switch(pin) to read a switch wired from pin to GND:
// it returns 0 when closed and 1 when open (internal pull-up).

#include <Arduino_RouterBridge.h>

static int pin = -1;
static int level = 0;       // current duty
static int fadeFrom = 0;
static int target = 0;
static int fadeMs = 0;
static unsigned long fadeStart = 0;
static int switchPin = -1;
static bool ledRegistered = false;
static bool switchRegistered = false;
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

int agent_switch(int p) {
    if (p != switchPin) {
        switchPin = p;
        pinMode(switchPin, INPUT_PULLUP);
        delay(5);  // let the pull-up settle before the first read
    }
    return digitalRead(switchPin);
}

bool provideAll() {
    if (!ledRegistered) ledRegistered = Bridge.provide("agent_led", agent_led);
    if (!switchRegistered) switchRegistered = Bridge.provide("agent_switch", agent_switch);
    return ledRegistered && switchRegistered;
}

void setup() {
    // The loader's boot output reaches the router just before us; if our handshake
    // arrives in the same chunk the router discards both and Bridge.begin() waits
    // forever. Let that noise go through on its own first.
    delay(2000);
    Bridge.begin();
    registered = provideAll();
    lastTry = millis();
}

void loop() {
    // At power-on the router may not be listening yet (or boot noise garbles the
    // first message), so keep offering the functions until the router accepts them.
    if (!registered && millis() - lastTry > 2000) {
        registered = provideAll();
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
