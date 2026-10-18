const uint8_t STEP_BUTTON_PIN = 2;
const unsigned long DEBOUNCE_MS = 40;

int lastReading = HIGH;
int stableState = HIGH;
unsigned long lastChangeAt = 0;

void setup() {
  pinMode(STEP_BUTTON_PIN, INPUT_PULLUP);
  Serial.begin(9600);
}

void loop() {
  const int reading = digitalRead(STEP_BUTTON_PIN);
  if (reading != lastReading) {
    lastReading = reading;
    lastChangeAt = millis();
  }

  if (millis() - lastChangeAt >= DEBOUNCE_MS && reading != stableState) {
    stableState = reading;
    if (stableState == LOW) {
      Serial.println("NEXT");
    }
  }
}
