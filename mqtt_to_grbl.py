import paho.mqtt.client as mqtt
import serial
import time
import sys
import os
import math
from enum import Enum
import yaml

# --- Configuration Loading ---
def load_config():
    """Loads configuration from config.yaml"""
    config_path = os.path.join(os.path.dirname(__file__), 'config.yaml')
    try:
        with open(config_path, 'r') as f:
            config_data = yaml.safe_load(f)
            print(f"Successfully loaded configuration from {config_path}")
            return config_data
    except FileNotFoundError:
        print(f"Error: Configuration file not found at {config_path}")
        sys.exit(1)
    except yaml.YAMLError as e:
        print(f"Error parsing configuration file {config_path}: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"An unexpected error occurred while loading config: {e}")
        sys.exit(1)

config = load_config() # Load configuration at startup

# --- Constants from Config ---
# MQTT Settings
MQTT_BROKER = config['mqtt']['broker']
MQTT_PORT = config['mqtt']['port']
MQTT_TOPIC = config['mqtt']['topic']
MQTT_USERNAME = config['mqtt'].get('username') # Use .get() for optional keys
MQTT_PASSWORD = config['mqtt'].get('password')

# GRBL Settings
SERIAL_PORT = config['grbl']['serial_port'] # Can be None
BAUD_RATE = config['grbl']['baud_rate']
GRBL_BUFFER_SIZE = config['grbl']['buffer_size']

# Text-to-Gcode Settings
GCODE_DIR = config['text_gcode']['gcode_dir']
LINE_LENGTH = config['text_gcode']['line_length']
LINE_SPACING = config['text_gcode']['line_spacing']
PADDING = config['text_gcode']['padding']
PEN_UP_HEIGHT = config['text_gcode']['pen_up_height']
PEN_DOWN_DEPTH = config['text_gcode']['pen_down_depth'] # Might not be directly used with M3/M5
FEED_RATE_Z = config['text_gcode']['feed_rate_z'] # Might not be directly used with M3/M5

# --- Global Variables ---
ser = None # Serial object for GRBL communication, initialized globally
letters = {} # Dictionary to store loaded letter G-code definitions

# --- Text to G-code Classes and Functions (Adapted from https://github.com/Stypox/text-to-gcode) ---
class Instr:
    class Type(Enum):
        move = 0,
        write = 1,

    def __init__(self, *args):
        if len(args) == 1 and type(args[0]) is str: # args must be a data str (a line from .nc file)
            line = args[0].strip() # Remove leading/trailing whitespace
            if not line or line.startswith('(') or line.startswith('%'): # Skip empty lines or common comment/control lines
                # Raise a specific exception or set a flag to indicate this line should be skipped
                raise ValueError("Skipping invalid or comment line") 

            attributes = line.split(' ')
            # G_ X__ Y__ --- Add robust checks
            try:
                # Check if enough parts and if G-code part is long enough
                if len(attributes) >= 3 and len(attributes[0]) >= 2 and attributes[0][0] == 'G':
                    # Check if the type character is '0' or '1' (or others if needed)
                    if attributes[0][1] == '0':
                        self.type = Instr.Type.move
                    elif attributes[0][1] == '1':
                        self.type = Instr.Type.write
                    else:
                         raise ValueError("Unsupported G command type in line") # Skip if not G0 or G1

                    # Check if X and Y parts are long enough and start with X/Y
                    if len(attributes[1]) > 1 and attributes[1][0] == 'X' and \
                       len(attributes[2]) > 1 and attributes[2][0] == 'Y':
                        self.x = float(attributes[1][1:])
                        self.y = float(attributes[2][1:])
                    else:
                        raise ValueError("Invalid X/Y format in line") # Skip if X/Y format wrong
                else:
                    raise ValueError("Line does not match expected G# X# Y# format") # Skip if basic structure wrong
            except (ValueError, IndexError) as e:
                 # If any check fails or conversion error occurs, raise to signal skipping
                 # Use ValueError for semantic issues, IndexError might occur if split result is too short
                 # Re-raise or wrap the exception to be caught in Letter.__init__
                 raise ValueError(f"Skipping line due to parsing error: {e} - Line: '{line}'")

        elif len(args) == 3 and type(args[0]) is Instr.Type and type(args[1]) is float and type(args[2]) is float:
            self.type, self.x, self.y = args
        else:
            raise TypeError("Instr() takes one (str) or three (Instr.Type, float, float) arguments")

    def __repr__(self):
        # Use .value[0] to get the first value of the enum (i.e., integer 0 or 1)
        return "G%d X%.2f Y%.2f" % (self.type.value[0], self.x, self.y)

    def translated(self, x, y):
        return Instr(self.type, self.x + x, self.y + y)

class Letter:
    def __init__(self, *args):
        if len(args) == 1 and type(args[0]) is str:
            self.instructions = []
            skipped_lines = 0 # Counter for skipped lines
            for line in args[0].split('\n'):
                line = line.strip() # Ensure stripped line is used
                if line != "":
                    try:
                        # Attempt to create Instr, might raise ValueError if line should be skipped
                        self.instructions.append(Instr(line)) 
                    except ValueError as e:
                         # Catch the specific error raised by Instr.__init__ for skippable lines
                         # print(f"Debug: Skipping line in Letter init: {e}") # Optional: Debug print
                         skipped_lines += 1 
                         
            # Optional: Print how many lines were skipped for this letter if needed for debugging
            # if skipped_lines > 0:
            #    print(f"Debug: Skipped {skipped_lines} lines while parsing letter data.")

            pointsOnX = [instr.x for instr in self.instructions if hasattr(instr, 'x')] # Safety check
            if pointsOnX:
                # Add check for min/max on empty list
                self.width = max(pointsOnX) - min(pointsOnX) if pointsOnX else 0.0
            else:
                self.width = 0.0 

        elif len(args) == 2 and type(args[0]) is list and type(args[1]) is float:
            self.instructions = args[0]
            self.width = args[1]
        else:
            raise TypeError("Letter() takes one (str) or two (list, float) arguments")

    def __repr__(self):
        return "\n".join([repr(instr) for instr in self.instructions]) + "\n"

    def translated(self, x, y):
        return Letter([instr.translated(x, y) for instr in self.instructions], self.width)

def readLetters(directory):
    local_letters = {
        " ": Letter([], 4.0), # Space definition width
        "\n": Letter([], math.inf) # Newline character handling
    }
    print(f"Reading letter definitions from: {directory}")
    if not os.path.isdir(directory):
        print(f"Error: G-code directory not found: {directory}")
        return None
    try:
        for root, _, filenames in os.walk(directory):
            for filename in filenames:
                # Check if the file ends with .nc (case-insensitive) - CORRECTED EXTENSION
                if filename.lower().endswith('.nc'):
                    try:
                        filepath = os.path.join(root, filename)
                        # Use filename without extension as the character key
                        char_key = os.path.splitext(filename)[0]
                        # Handle potential single character filenames vs multi-character like 'exclamation'
                        # For simplicity, we use the base filename directly.
                        
                        with open(filepath, "r", encoding='utf-8') as file: # Specify encoding
                            data = file.read()
                            if data: # Only add if file is not empty
                                local_letters[char_key] = Letter(data)
                                print(f"  Successfully loaded character: '{char_key}' from {filename}") 
                            else:
                                print(f"Warning: Skipping empty file {filename}")
                    except UnicodeDecodeError:
                        print(f"Error reading file {filename}: 'utf-8' codec can't decode byte, skipping file")
                    except Exception as e:
                        print(f"Error processing file {filename}: {e}, skipping.")
    except Exception as e:
        print(f"Error walking through directory {directory}: {e}")
    
    # Add a check to see if any actual letters were loaded beyond space and newline
    if len(local_letters) <= 2:
        print("Warning: No valid .nc files found or processed in the directory.")
        
    print(f"Finished reading definitions. Total characters loaded (incl. space/newline): {len(local_letters)}") 
    return local_letters

def textToGcode(text, letters, line_length, line_spacing, padding):
    """Converts a string of text into G-code lines using preloaded letter definitions."""
    output = []
    current_x = 0
    current_y = 0
    FEED_RATE_XY = 300 # Default Feed rate for drawing movements (X, Y) -> Will be removed from output

    # Initial position might need adjustment or setup G-code
    # output.append("G90") # Absolute positioning
    # output.append("G21") # Units to mm
    
    lines = text.split('\n')
    first_line = True
    pen_is_down = False # New state variable to track pen state

    for line_text in lines:
        if not first_line:
            # Move down for next line
            print(line_text)
            current_y -= line_spacing
            current_x = 0
            # Pen up before moving to the start of the new line
            if pen_is_down:
                output.append("M05") # Pen Up before new line
                pen_is_down = False
            output.append(f"G0 F1500 X{current_x:.2f} Y{current_y:.2f}") # Move to start of new line
            output.append("M04")
            output.append("M05")
        else:
            first_line = False

        for char in line_text:
            if char in letters:
                letter_data = letters[char]
                if not letter_data.instructions:
                    # Handle space or characters with no drawing instructions (just advance X)
                    current_x += letter_data.width + padding
                    # Ensure pen is up before moving to the next character's potential start
                    if pen_is_down:
                        output.append("M05")
                        pen_is_down = False
                    # We might still need a G0 move here if only width is defined? Assume no for now.
                    continue # Move to the next character

                # Pen down/up logic is now handled inside the loop based on instruction type
                # output.append("M03") # Removed initial M03 before loop
                pen_is_down = False # Reset pen state for each letter initially? No, let it carry over? Let's reset. Assume start with pen up.

                # Apply offset to letter instructions
                for instr in letter_data.instructions:
                    # Calculate absolute coordinates based on current position
                    x = current_x + instr.x
                    y = current_y + instr.y
                    
                    if instr.type == Instr.Type.move: # G0 Move
                        if pen_is_down:
                            output.append("M05 F1500.0") # Pen Up before G0
                            pen_is_down = False
                        output.append(f"G0 F1500.0 X{x:.2f} Y{y:.2f}")

                    elif instr.type == Instr.Type.write: # G1 Move
                        if not pen_is_down:
                            output.append("M03 S150") # Pen Down before G1
                            pen_is_down = True
                        output.append(f"G1 F1500.0 X{x:.2f} Y{y:.2f}") # New G1 without Feed Rate

                # Pen up after finishing all instructions for the character
                if pen_is_down:
                    output.append("M05 F1500.0") # Ensure Pen Up after last instruction
                    pen_is_down = False
                    
                # Update current_x for the next character
                current_x += letter_data.width + padding
                # Move G0 to the starting X of the next char - Pen should be up already
                output.append(f"G0 F1000.0 X{current_x:.2f} Y{current_y:.2f}")

            else:
                print(f"Warning: Character '{char}' not found in definitions. Skipping.")
                # Optionally advance X by a default width or space width?
                # current_x += letters.get(' ', {'width': default_space_width})['width'] + padding 
    
    # Final pen up (just in case)
    # output.append(f"G0 Z{PEN_UP_HEIGHT:.2f}") # Original Final Pen Up
    # We already added M05 after the last character or line break handling.

    return output

# --- End Text to G-code --- 

# --- GRBL Communication --- # (Modified to print G-code)
def send_gcode(command, timeout=2.0):
    """
    发送一行G-code到GRBL串口，并等待回应。如果串口不可用，则打印G-code。
    :param command: 要发送的G-code字符串
    :param timeout: 等待GRBL回应的超时时间（秒）
    :return: (success:bool, response:str)
    """
    global ser
    if ser and ser.is_open:
        try:
            print(f"[GCODE->GRBL] {command}")
            if command == 'M04': #change line
                time.sleep(19)
            ser.write((command + '\n').encode('utf-8'))
            ser.flush()
            start_time = time.time()
            response_buffer = b''
            while True:
                line = ser.readline() # 读到\n为止
                if line:
                    response_buffer += line
                    decoded_line = line.decode('utf-8', errors='ignore').strip()
                    print(f"[GRBL<-] {decoded_line}")
                    time.sleep(1.1)
                    if decoded_line == 'ok' or decoded_line.startswith('error:'):
                        return decoded_line == 'ok', decoded_line
                if time.time() - start_time > timeout:
                    final_response = response_buffer.decode('utf-8', errors='ignore').strip()
                    print(f"[GRBL TIMEOUT] {final_response} {final_response}")
                    return False, final_response
        except Exception as e:
            print(f"[GCODE ERROR] 发送到串口失败: {e}")
            return False, str(e)
    else:
        print(command) # 串口不可用时降级为打印
        return True, "Printed_to_console"

# --- GRBL Initialization --- # (Modified to not actually connect)
def init_grbl():
    """Initializes the serial connection to the GRBL controller and sends initial commands."""
    global ser
    if SERIAL_PORT is None:
        print("SERIAL_PORT not set. Running in GRBL communication bypass mode.")
        return True # Allow running without GRBL connection

    try:
        print(f"Attempting to connect to GRBL on {SERIAL_PORT} at {BAUD_RATE} baud...")
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.1) # Short timeout for initial handshake
        print("Serial port opened. Waiting for GRBL initialization...")
        time.sleep(2) # Wait for GRBL to initialize
        ser.flushInput() # Flush startup messages from GRBL
        print("GRBL controller connected.")

        # --- Send Initialization Sequence --- 
        init_commands = [
            #"$X",                 # Unlock GRBL (Needed after reset or power cycle)
            "G21",                # Set units to millimeters
            "G90",                # Set to absolute positioning
            "G17",                # Select XY plane
            "G94",                # Set feed rate mode to units per minute
            "M05 S1000",
            "M03",           # Turn on spindle/laser (adjust S value as needed)
            "M05",
            "G10 P0 L20 X0",      # set X0
            "G10 P0 L20 Y0",      # set Y0
            "G0 Z5",              # Move to safety height Z=5
            "G0 X0 Y0",           # Move to XY zero
            "G0 Z0"              # Move to Z zero
        ]

        print("Sending GRBL initialization commands...")
        all_init_success = True
        for cmd in init_commands:
            print(f"Sending: {cmd}")
            success, response = send_gcode(cmd, timeout=5.0) # Use longer timeout for init
            if not success:
                print(f"Failed to execute initialization command: {cmd}. Response: {response}")
                all_init_success = False
                break # Stop initialization if one command fails
            else:
                print(f"GRBL response: {response}") # Print success response
                time.sleep(0.1) # Small delay between commands
        
        if not all_init_success:
            print("GRBL initialization failed. Closing port.")
            ser.close()
            ser = None
            return False
        
        print("GRBL initialization sequence successfully sent.")
        return True # Indicate successful initialization

    except serial.SerialException as e:
        print(f"Failed to connect to GRBL on {SERIAL_PORT}: {e}")
        print("Running in GRBL communication bypass mode.")
        ser = None # Ensure ser is None if connection failed
        return True # Allow running without GRBL connection
    except Exception as e:
        print(f"An unexpected error occurred during GRBL initialization: {e}")
        if ser and ser.is_open:
            ser.close()
            print("GRBL serial port closed.")
        ser = None
        return False

# MQTT Callback Functions
def on_connect(client, userdata, flags, reason_code, properties):
    # reason_code 0: Connection successful
    # Note: paho-mqtt v2 callback API passes 5 args
    if reason_code == 0:
        print(f"Successfully connected to MQTT broker with reason code {reason_code}")
        # *** Restore subscription logic ***
        try:
            client.subscribe(MQTT_TOPIC)
            print(f"Subscribed to topic: {MQTT_TOPIC}")
        except Exception as e:
            print(f"Failed to subscribe to topic {MQTT_TOPIC}: {e}")
    else:
        print(f"Failed to connect to MQTT broker with reason code {reason_code}")

def on_disconnect(client, userdata, flags, reason_code, properties):
    # Note: paho-mqtt v2 callback API passes 5 args
    # reason_code is usually 0 unless forced disconnect
    print(f"Disconnected from MQTT broker with reason code {reason_code}")
    # Attempt to reconnect?
    # Note: loop_forever() handles reconnections automatically by default

def on_message(client, userdata, msg):
    global ser, letters # Ensure using global letters
    print(f"\n--- MQTT Message Received ---")
    print(f"Topic: {msg.topic}")
    print(f"Raw Payload: {msg.payload}")
    try:
        text_payload = msg.payload.decode('utf-8')
        print(f"Decoded Text Payload: {text_payload}")
    except UnicodeDecodeError:
        print("Error: Could not decode payload as UTF-8.")
        print("--- End MQTT Message Processing ---")
        return
        
    # Log the state of 'ser' before checking the connection
    print(f"Checking Serial Port: ser={'Exists' if ser else 'None'}, is_open={ser.is_open if ser else 'N/A'}")

    # Check GRBL connection before proceeding
    if not (ser and ser.is_open):
        print("GRBL serial port not available or not open. Attempting to reconnect...")
        if not init_grbl():
             print("Failed to reconnect to GRBL. Skipping message processing for this message.")
             return # Exit if re-init fails (though it shouldn't in bypass mode)

    if not letters: # Check if letters dictionary is populated
        print("Error: Letter definitions are not loaded. Cannot convert text to G-code. Skipping message.")
        return

    # Convert received text to G-code
    print("Converting text to G-code...")
    gcode_output = textToGcode(text_payload, letters, LINE_LENGTH, LINE_SPACING, PADDING)

    if not gcode_output:
        print("G-code conversion resulted in empty output. Skipping sending.")
        print("--- End MQTT Message Processing ---")
        return

    # Send G-code line by line
    gcode_lines = gcode_output
    print(f"Processing {len(gcode_lines)} G-code line(s) generated from text:")
    # print(gcode_lines) # Uncomment to see the generated G-code list
    
    success = True
    for line in gcode_lines:
        line = line.strip()
        if line: # Ignore empty lines
            success, response = send_gcode(line) # Get tuple (success_flag, message)
            if not success:
                print(f"Failed to execute G-code: {line}. Stopping further execution for this message. Response: {response}")
                # success = False # Already set implicitly by send_gcode return
                break # If one line fails, stop sending the rest for this message
    if success:
        print("Finished processing G-code from message.")
        # --- Send Post-Message G-code (Conditional) --- 
        # Only add the extra newline move if the original text didn't end with one
        if not text_payload.endswith('\n'):
            print("Sending post-message G-code (input did not end with newline)...")
            post_cmd = "G1 F500.0 X0.00 Y-8.00" # Move down for next potential line/area
            post_success, post_response = send_gcode(post_cmd)
            time.sleep(6)
            if not post_success:
                print(f"Failed to execute post-message command: {post_cmd}. Response: {post_response}")
            else:
                print(f"Successfully sent post-message command: {post_cmd}")
        else:
            print("Skipping post-message G-code (input ended with newline).")
            
    print("--- End MQTT Message Processing ---")

# Main Program
if __name__ == "__main__":
    # Load letter definitions
    letters = readLetters(GCODE_DIR)
    if letters is None: # Check for None instead of not letters
        print("Failed to load letter definitions or directory not found. Exiting.")
        sys.exit(1)
    if len(letters) <= 2: # Contains only space and newline, no actual characters loaded
        print("Warning: No actual character definitions loaded. Text conversion likely to fail. Check GCODE_DIR and file contents.")
        # Maybe shouldn't exit, allow connection attempt? Or exit here?
        # sys.exit(1) 

    # Initialize GRBL (in bypass mode)
    if not init_grbl():
        print("Exiting due to GRBL initialization failure.") # Theoretically should not be executed anymore
        sys.exit(1)

    # Initialize MQTT Client - using new API version
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        print("MQTT Client initialized with Callback API V2.")
    except Exception as e:
        print(f"Error initializing MQTT client (maybe paho-mqtt version is old?): {e}")
        print("Falling back to default MQTT Client initialization.")
        client = mqtt.Client() # Fallback to old version

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    # Set username and password (usually not required for public brokers)
    # if MQTT_USERNAME and MQTT_PASSWORD:
    #     client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

    # Connect to MQTT Server
    try:
        print(f"Connecting to MQTT broker at {MQTT_BROKER}:{MQTT_PORT}...")
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
    except Exception as e:
        print(f"Error connecting to MQTT broker: {e}")
        if ser and ser.is_open:
           ser.close()
           print("Serial port closed.")
        sys.exit(1)

    # Blocking loop that processes network traffic, dispatches callbacks, and handles reconnecting.
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print("Disconnecting from MQTT and closing GRBL port...")
    finally:
        client.disconnect()
        print("MQTT client disconnected.")
        # if ser and ser.is_open:
        #     ser.close()
        #     print("GRBL serial port closed.") # Remove serial port closing
    print("Exiting program.")
