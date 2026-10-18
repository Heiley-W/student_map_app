# Connecting a physical Arduino

Pythagoras guided navigation can advance one instruction at a time from an Arduino button. The browser listens for a newline-terminated `NEXT` message over USB serial. The large **DEMO · Next step** button remains available if the Arduino is not connected.

This is a prototype connection, not a certified accessibility or safety device. Test the route, device, and instructions with intended users before relying on it for navigation.

## What you need

- An Arduino board with USB serial support, such as an Uno or Leonardo.
- A USB data cable connected to your computer.
- A normally-open momentary push button and two jumper wires.
- Arduino IDE.
- A desktop version of Chrome or Edge with Web Serial support.

## Wire the button

Connect one terminal of the button to digital pin **2** and the other terminal to **GND**:

| Button terminal | Arduino |
| --- | --- |
| One side | Digital pin 2 |
| Other side | GND |

The sketch enables the internal pull-up resistor, so no external resistor or 5V connection is needed. With the board unplugged, place the button across the center gap of a breadboard if applicable. Avoid shorting 5V to GND.

## Upload the Arduino sketch

1. Open `arduino/pythagoras_step_signal.ino` in Arduino IDE.
2. Connect the Arduino by USB.
3. In **Tools → Board** and **Tools → Port**, select your board and its serial port.
4. Click **Upload** and wait for the IDE to report completion.
5. Close Serial Monitor and Serial Plotter if either is open; the browser needs exclusive access to the port.

The sketch uses **9600 baud**, debounces the button, and sends `NEXT` plus a newline once for each press.

## Connect it to Pythagoras

1. Start the backend and frontend using the commands in the [README](README.md#run-the-integrated-prototype).
2. Open the frontend URL in a regular Chrome or Edge tab. USB serial is available on `localhost` or an HTTPS site; an embedded preview may not expose serial devices.
3. Choose a start and destination, then select **Find route**.
4. Turn on **Visually impaired guided mode**.
5. In the large top-right panel, click **Connect Arduino via USB**.
6. In the browser's device picker, choose the Arduino's USB serial port and approve access.
7. Press the physical button once to advance one instruction. The map marker and floor update with the instruction. Use **DEMO · Next step** as a manual fallback.
8. When finished, click **Disconnect Arduino** before using the board in another application.

## Troubleshooting

- **No USB connection button:** use a recent desktop Chrome or Edge release and open Pythagoras directly at `http://localhost:5500/web.html?api=http://localhost:8001` or over HTTPS.
- **Port missing from the picker:** reconnect the USB cable, confirm it supports data, check the selected board/port in Arduino IDE, and close Serial Monitor/Plotter. Some boards need a USB driver.
- **Port is busy:** close any other application using the Arduino serial port, then disconnect and reconnect it in Pythagoras.
- **Button does nothing:** check the pin 2/GND wiring; upload the sketch again; verify it is set to 9600 baud; then disconnect and reconnect the device. The browser must be allowed to read from the selected port.
- **Testing without hardware:** guided mode remains usable with **DEMO · Next step**. It advances the same instruction sequence as the Arduino.

The browser integration reads `NEXT` lines from the serial port; it does not send commands to the board. The browser needs a user gesture to request access to a port, and each page session must be granted access by the user.
