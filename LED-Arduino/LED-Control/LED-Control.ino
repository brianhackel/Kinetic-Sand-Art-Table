
/*
  Designed for DIY MAchines Kinetic Sand Project and based on code originally written by Keith Lord 
  
You will need to install the WS2812FX library which can be found here: https://github.com/kitesurfer1404/WS2812FX

  LICENSE

  The MIT License (MIT)

  Portions Copyright (c) 2017  Keith Lord 

  Permission is hereby granted, free of charge, to any person obtaining a copy
  of this software and associated documentation files (the "Software"), to deal
  in the Software without restriction, including without limitation the rights
  to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
  copies of the Software, and to permit persons to whom the Software is
  furnished to do so, subject to the following conditions:

  The above copyright notice and this permission notice shall be included in
  all copies or substantial portions of the Software.

  THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
  IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
  FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
  AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
  LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
  OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
  THE SOFTWARE.
*/

#include <WS2812FX.h>

#define LED_PIN    6  // digital pin used to drive the LED strip
#define LED_COUNT 70  // number of LEDs on the strip - if you make a larger table with more LED's then adjust this number

/*
  Serial protocol (115200 baud, one command per line, terminated by \n; \r is ignored)

    P <n>       select pattern 0..10
    B <n>       brightness 0..255 (limited to maximumBrightnesAllowed)
    S <n>       speed in ms per step (lower = faster)
    C <rrggbb>  color as hex, until the next P command
    ?           report current state

  Every command is answered with OK or ERR <reason>; "?" answers with a STATE line instead of OK.
  The sketch sends READY once at boot.
*/

const int maximumBrightnesAllowed = 128;  // set this value the maximum brightness you want to allow the LEDs to reach (0 -> 255)
const uint16_t minSpeed = 2;
const uint16_t maxSpeed = 10000;

struct Pattern {
  uint8_t mode;
  uint32_t color;
};

const Pattern patterns[] = {
  {FX_MODE_CHASE_RAINBOW,      WHITE},   // 0
  {FX_MODE_FADE,               WHITE},   // 1
  {FX_MODE_COLOR_WIPE,         WHITE},   // 2
  {FX_MODE_COLOR_WIPE_RANDOM,  CYAN},    // 3
  {FX_MODE_RAINBOW,            RED},     // 4
  {FX_MODE_RUNNING_LIGHTS,     WHITE},   // 5
  {FX_MODE_SPARKLE,            WHITE},   // 6
  {FX_MODE_COLOR_SWEEP_RANDOM, WHITE},   // 7
  {FX_MODE_LARSON_SCANNER,     RED},     // 8
  {FX_MODE_FIRE_FLICKER_SOFT,  ORANGE},  // 9
  {FX_MODE_STATIC,             WHITE},   // 10
};
const int patternCount = sizeof(patterns) / sizeof(patterns[0]);

int currentPattern = 0;
int currentBrightness = 64;
uint16_t currentSpeed = 1000;
uint32_t currentColor = WHITE;

const byte lineSize = 24;
char lineBuffer[lineSize];
byte lineLength = 0;
bool lineOverflow = false;

WS2812FX tableSurfaceLEDs = WS2812FX(LED_COUNT, LED_PIN, NEO_GRB + NEO_KHZ800);

void setup() {
  Serial.begin(115200);

  tableSurfaceLEDs.init();
  tableSurfaceLEDs.setBrightness(currentBrightness);
  tableSurfaceLEDs.setSpeed(currentSpeed);
  setPattern(0);

  Serial.println(F("READY"));
}

void setPattern(int n) {
  currentPattern = n;
  currentColor = patterns[n].color;
  tableSurfaceLEDs.setMode(patterns[n].mode);
  tableSurfaceLEDs.setColor(currentColor);
  tableSurfaceLEDs.start();
}

void printState() {
  Serial.print(F("STATE P="));
  Serial.print(currentPattern);
  Serial.print(F(" B="));
  Serial.print(currentBrightness);
  Serial.print(F(" S="));
  Serial.print(currentSpeed);
  Serial.print(F(" C="));
  char hex[7];
  snprintf(hex, sizeof(hex), "%06lX", (unsigned long)currentColor);
  Serial.println(hex);
}

// Parses a decimal integer argument; returns false if it is missing or malformed.
bool parseNumber(const char *arg, long &value) {
  char *end;
  value = strtol(arg, &end, 10);
  return end != arg && *end == '\0';
}

void handleCommand(char *line) {
  char cmd = line[0];
  char *arg = line + 1;
  while (*arg == ' ') arg++;

  if (cmd == '?') {
    printState();
    return;
  }

  if (cmd == 'C') {
    char *end;
    unsigned long rgb = strtoul(arg, &end, 16);
    if (end == arg || *end != '\0' || strlen(arg) != 6) {
      Serial.println(F("ERR bad color"));
      return;
    }
    currentColor = rgb;
    tableSurfaceLEDs.setColor(currentColor);
    Serial.println(F("OK"));
    return;
  }

  long n;
  if ((cmd != 'P' && cmd != 'B' && cmd != 'S') || !parseNumber(arg, n)) {
    Serial.println(F("ERR bad command"));
    return;
  }

  switch (cmd) {
    case 'P':
      if (n < 0 || n >= patternCount) {
        Serial.println(F("ERR pattern out of range"));
        return;
      }
      setPattern(n);
      break;
    case 'B':
      currentBrightness = constrain(n, 0, maximumBrightnesAllowed);
      tableSurfaceLEDs.setBrightness(currentBrightness);
      break;
    case 'S':
      currentSpeed = constrain(n, minSpeed, maxSpeed);
      tableSurfaceLEDs.setSpeed(currentSpeed);
      break;
  }
  Serial.println(F("OK"));
}

// Collects serial bytes into lineBuffer without blocking; dispatches each complete line.
void readSerial() {
  while (Serial.available() > 0) {
    char c = Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      if (lineOverflow) {
        Serial.println(F("ERR line too long"));
      } else if (lineLength > 0) {
        lineBuffer[lineLength] = '\0';
        handleCommand(lineBuffer);
      }
      lineLength = 0;
      lineOverflow = false;
    } else if (lineLength < lineSize - 1) {
      lineBuffer[lineLength++] = c;
    } else {
      lineOverflow = true;
    }
  }
}

void loop() {
  tableSurfaceLEDs.service();
  readSerial();
}
