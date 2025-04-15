# MQTT Text-to-Gcode for GRBL (Supports Serial/Bypass Mode)

This project receives text messages via MQTT, converts them into G-code, and controls a GRBL-based CNC/laser device through the serial port.
All major parameters are configured via `config.yaml`, so you can easily switch MQTT broker, serial port, G-code library, etc., without modifying the code.

---

## Requirements
- Python 3.x
- pip
- (Recommended) Use a virtual environment (`.venv`)
- Dependencies:
  ```bash
  pip install -r requirements.txt
  # requirements.txt includes paho-mqtt, pyserial, PyYAML
  ```

## Quick Start
1. **Clone this repository**
2. **Prepare the G-code library**
   - Download the [text-to-gcode project](https://github.com/Stypox/text-to-gcode/tree/master)
   - Place its `ascii_gcode` folder in the root directory of this project, or set the `gcode_dir` path in `config.yaml` to your own library location
3. **Edit the configuration file** `config.yaml`
   - MQTT section: Fill in your MQTT broker address, port, and topic
   - GRBL section:
     - Set `serial_port` to your GRBL device's port, e.g. `/dev/tty.usbmodemXXXX` (Mac/Linux) or `COM3` (Windows)
     - If set to `null` or commented out, the script runs in bypass mode (G-code is only printed, not sent to serial)
   - `gcode_dir`: Path to your G-code library (supports both relative and absolute paths)

4. **Run the program**
   ```bash
   # Recommended: use virtual environment
   .venv/bin/python mqtt_to_grbl.py
   ```

5. **Send messages using an MQTT client**
   - Connect to the MQTT broker you configured, and publish plain text messages to the `topic` defined in `config.yaml`
   - The program will automatically convert the received message to G-code and send it to GRBL (or print it in bypass mode)

---

## Configuration Details (`config.yaml`)
```yaml
mqtt:
  broker: "test.mosquitto.org"
  port: 1883
  topic: "fablab/chaihuo/machine/text"
  # username/password optional

grbl:
  serial_port: /dev/tty.usbmodemXXXX # Your GRBL serial port. Set to null for bypass mode
  baud_rate: 115200
  buffer_size: 128
  init_commands:
    - "$X"  # Unlock GRBL
    - "G21" # Set to millimeter units
    ...
  post_message_command: "G1 X0.00 Y-8.00" # Optional

text_gcode:
  gcode_dir: "ascii_gcode" # G-code library directory
  ...
```

---

## Troubleshooting
- **Serial port busy/unavailable**
  - Error `Resource busy`: Close all serial monitor programs (such as Arduino IDE, screen, UGS, etc.), use `lsof | grep tty.usbmodem` to check for occupying processes
- **No response/timeout from GRBL**
  - Check that the GRBL device is powered on, baud rate is correct, and the cable is good
  - Some boards require pressing the reset button or clearing limit alarms
  - You can manually test with `screen /dev/tty.usbmodemXXXX 115200`
- **G-code not actually sent**
  - Check the `serial_port` setting in `config.yaml`
  - In bypass mode, G-code will only be printed, not sent to serial
- **MQTT communication issues**
  - Check broker address, port, topic settings, and network connectivity

---

## Advanced Usage
- You can customize the GRBL initialization G-code sequence (for different devices such as laser or engraver)
- Supports custom G-code libraries: just replace the contents of the `gcode_dir` directory

---

## Acknowledgements
- The G-code library and core text-to-G-code logic are adapted from [Stypox/text-to-gcode](https://github.com/Stypox/text-to-gcode/tree/master)

---

If you have any questions, feel free to open an issue or contact the author!
