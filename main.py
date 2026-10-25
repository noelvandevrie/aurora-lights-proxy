import os
import json
import math
import socket
import threading
import traceback
import uuid
from dotenv import load_dotenv
import time
import signal
import requests
import socketio
import logging
import paho.mqtt.client as mqtt

load_dotenv()

logging.basicConfig(
    format='[%(asctime)s] %(levelname)-8s %(message)s',
    level=os.environ.get('LOG_LEVEL', 'INFO'),
    datefmt='%Y-%m-%d %H:%M:%S')

# Global settings, see the "Live source (Aurora)" section of the logo's PROTOCOL.md
packet_size = 512                   # one DMX universe
fixture_count = 9                   # a 9×2 grid: B .. dot4 on row 0 (x0–x8), the tree on row 1 at x4
fixture_channels = 6                # R, G, B, (unused), dimmer, strobe
tree_fixture = 4                    # the tree's fixture; the name's fixtures are the others, in order
tree_segments = 3                   # the tree fixture's colour goes to crown, trunk and roots
min_frame_interval = 1 / 40         # at most ~40 frames per second
resend_interval = 1.0               # the logo falls back to its scene after 5 s without frames
aurora_timeout = 15 * 60.0          # stop sending when Aurora sends no DMX packets this long (stuck Aurora)
topic_live = 'boom-logo/live'
topic_status = 'boom-logo/bridge/status'
# End global settings

start_address = int(os.environ.get('DMX_START_ADDRESS', '1'))
channel_count = fixture_count * fixture_channels
if start_address < 1 or start_address - 1 + channel_count > packet_size:
    raise ValueError(f"DMX_START_ADDRESS {start_address}: {channel_count} channels must fit in one universe")

mqtt_tls = os.environ.get('MQTT_TLS', 'false').lower() in ('1', 'true', 'yes')
mqtt_host = os.environ['MQTT_HOST']
mqtt_port = int(os.environ.get('MQTT_PORT', '8883' if mqtt_tls else '1883'))


# Stop thread when necessary
running = True
def stop_execution(signum, frame):
    global running
    running = False


# When receiving an EXIT signal, try to stop the execution flow
signal.signal(signal.SIGINT, stop_execution)
signal.signal(signal.SIGTERM, stop_execution)

sio = socketio.Client(logger=True)

status_thread = None
status_thread_running = False
start_time = 0
latency_ms = 0

# The latest frame from Aurora (None while Aurora is not connected), picked up by the sender thread
latest_frame = None
latest_frame_at = 0.0
frame_lock = threading.Lock()
frame_changed = threading.Event()
aurora_connected = False

# Logging variables to track the incoming packets
last_log = time.monotonic()
packets_since = 0
frames_since = 0
auth_cookie = ''

def get_headers():
    global auth_cookie
    return {'cookie': 'connect.sid=' + auth_cookie}

def parse_array(arr, desired_length):
    int_arr = [max(min(int(x), 255), 0) for x in arr]

    current_length = len(int_arr)

    if current_length >= desired_length:
        # No need to pad, return the original array
        return int_arr
    else:
        # Calculate the number of zeros to pad
        num_zeros = desired_length - current_length

        # Create a new array with zeros and concatenate it with the original array
        padded_array = int_arr + [0] * num_zeros

        return padded_array


def build_frame(channels):
    """The live frame for one DMX packet: R, G, B per segment times its dimmer, then the strobe byte."""
    base = start_address - 1
    colors = []
    strobing = False
    for n in range(fixture_count):
        r, g, b, _, dimmer, strobe = channels[base + n * fixture_channels:base + (n + 1) * fixture_channels]
        colors.append(bytes(min(255, int(c * dimmer / 255 + 0.5)) for c in (r, g, b)))
        strobing = strobing or strobe == 255
    # The tree sits in the middle of the grid but comes after the name in the frame
    frame = bytearray(b''.join(colors[:tree_fixture] + colors[tree_fixture + 1:]) + colors[tree_fixture] * tree_segments)
    frame.append(255 if strobing else 0)
    return bytes(frame)


def format_channels(channels):
    """The logo's channels for the debug log: RRGGBB/dimmer/strobe per fixture."""
    base = start_address - 1
    fixtures = []
    for n in range(fixture_count):
        r, g, b, _, dimmer, strobe = channels[base + n * fixture_channels:base + (n + 1) * fixture_channels]
        fixtures.append(f"{r:02x}{g:02x}{b:02x}/{dimmer}/{strobe}")
    return ' | '.join(fixtures)


def set_frame(frame):
    global latest_frame, latest_frame_at
    with frame_lock:
        changed = frame != latest_frame
        latest_frame = frame
        latest_frame_at = time.monotonic()
    if changed:
        frame_changed.set()


def send_frames():
    """Publish frames when they change, at most ~40 per second, and resend the last one every second."""
    global frames_since, latest_frame
    sent = None
    sent_at = 0.0
    resend_at = 0.0
    while running:
        changed = frame_changed.wait(max(0.0, resend_at - time.monotonic()))
        frame_changed.clear()
        # Rate limit; a burst collapses into its last frame, so the logo ends on the right colours
        wait = sent_at + min_frame_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        with frame_lock:
            # Resending a frozen frame forever would freeze the logo; stop, so it falls back to its scene
            if latest_frame is not None and time.monotonic() - latest_frame_at > aurora_timeout:
                logging.warning(f"No DMX packets from Aurora for {aurora_timeout / 60:.0f} min, stopping frames")
                latest_frame = None
            frame = latest_frame
        if frame is None or not mqtt_client.is_connected():
            resend_at = time.monotonic() + resend_interval
            continue
        if changed and frame == sent:
            resend_at = sent_at + resend_interval
            continue
        mqtt_client.publish(topic_live, frame, qos=0, retain=False)
        sent = frame
        sent_at = time.monotonic()
        resend_at = sent_at + resend_interval
        frames_since += 1


def publish_status():
    payload = json.dumps({'online': True, 'aurora_connected': aurora_connected})
    mqtt_client.publish(topic_status, payload, qos=0, retain=True)


def set_aurora_connected(connected):
    global aurora_connected
    if connected == aurora_connected:
        return
    aurora_connected = connected
    if not connected:
        # Stop sending, so the logo falls back to its scene
        set_frame(None)
    publish_status()


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code.is_failure:
        logging.error(f"Could not connect to MQTT broker: {reason_code}")
        return
    logging.info(f"Connected to MQTT broker {mqtt_host}:{mqtt_port}")
    publish_status()


def on_connect_fail(client, userdata):
    logging.warning(f"Could not reach MQTT broker {mqtt_host}:{mqtt_port}, retrying...")


def on_disconnect(client, userdata, flags, reason_code, properties):
    logging.warning(f"Disconnected from MQTT broker: {reason_code}")


def on_socket_open(client, userdata, sock):
    # Send each small frame right away instead of waiting for the broker's ACK of the previous one
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)


mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"aurora-live-{uuid.uuid4().hex[:8]}")
mqtt_client.username_pw_set(os.environ.get('MQTT_USERNAME', 'bridge'), os.environ['MQTT_PASSWORD'])
mqtt_client.will_set(topic_status, json.dumps({'online': False}), qos=0, retain=True)
if mqtt_tls:
    mqtt_client.tls_set()
mqtt_client.reconnect_delay_set(min_delay=1, max_delay=30)
mqtt_client.on_connect = on_connect
mqtt_client.on_connect_fail = on_connect_fail
mqtt_client.on_disconnect = on_disconnect
mqtt_client.on_socket_open = on_socket_open

def send_status_updates():
    global status_thread_running, sio, latency_ms

    while status_thread_running:
        uptime_seconds = int(time.time() - start_time)
        system_timestamp = math.floor(time.time_ns() / 1000000)
        last_send_time = system_timestamp

        def status_update_callback():
            global latency_ms
            callback_time = time.time_ns() / 1000000
            rtt = callback_time - last_send_time
            latency_ms = int(rtt / 2)
            logging.info(f"Latency: {latency_ms} ms")

        sio.emit('status:update', {
            'uptimeSeconds': uptime_seconds,
            'systemTimestamp': system_timestamp,
            'latencyMilliseconds': latency_ms,
        }, callback=status_update_callback)

        time.sleep(5)

def create_status_loop():
    global status_thread, status_thread_running
    status_thread_running = True
    status_thread = threading.Thread(target=send_status_updates)
    status_thread.daemon = True
    status_thread.start()

def stop_status_loop():
    global status_thread, status_thread_running
    if status_thread is None:
        return
    status_thread_running = False
    status_thread.join(timeout=1.0)
    status_thread = None

def main():
    global sio, running, start_time, auth_cookie

    start_time = time.time()

    url = os.environ['URL'] + '/api/auth/key'
    result = requests.post(url, {'key': os.environ['API_KEY']})

    if result.status_code != 200:
        body = result.json()
        raise Exception("Could not authenticate with core: [HTTP {}]: {}".format(
            result.status_code,
            body['details'] if body['details'] else body['message']),
        )

    auth_cookie = result.cookies.get('connect.sid')

    # Initialize SocketIO
    sio.connect(os.environ['URL'], headers=get_headers, namespaces=['/', '/lights'])

    logging.info('Connected')

    while running:
        time.sleep(0.5)

    sio.disconnect()

@sio.event(namespace='/lights')
def dmx_packet(packet):
    global packets_since, frames_since, last_log

    channels = parse_array(packet, packet_size)
    set_frame(build_frame(channels))
    packets_since = packets_since + 1
    if logging.getLogger().isEnabledFor(logging.DEBUG):
        logging.debug(f"DMX: {format_channels(channels)}")

    # Logging/debugging time
    now = time.monotonic()
    if now - last_log > 1:
        logging.debug(f"Received {packets_since:02} DMX packets, sent {frames_since:02} frames since last log "
                      f"(last frame: {latest_frame.hex() if latest_frame else None})")

        # Reset logging variables
        packets_since = 0
        frames_since = 0
        last_log = now

@sio.event
def disconnect():
    set_aurora_connected(False)
    stop_status_loop()

@sio.event
def connect():
    set_aurora_connected(True)
    create_status_loop()

if __name__ == '__main__':
    mqtt_client.connect_async(mqtt_host, mqtt_port, keepalive=15)
    mqtt_client.loop_start()
    threading.Thread(target=send_frames, daemon=True).start()

    while running:
        try:
            main()
        except Exception as e:
            set_aurora_connected(False)
            logging.error(traceback.format_exc())
            logging.info('Something went wrong. Retrying in 5 seconds...')
            for _ in range(5):
                if running:
                    time.sleep(1)

    # Clean shutdown: tell the website the bridge is gone, the LWT only covers a lost connection
    if mqtt_client.is_connected():
        mqtt_client.publish(topic_status, json.dumps({'online': False}), qos=0, retain=True).wait_for_publish(2)
    mqtt_client.disconnect()
    mqtt_client.loop_stop()
