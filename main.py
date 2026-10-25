import os
import json
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
fixture_count = 9                   # B .. dot4, then the tree (crown + trunk + roots) as one fixture
fixture_channels = 6                # R, G, B, (unused), dimmer, strobe
tree_segments = 3                   # the last fixture's colour goes to crown, trunk and roots
min_frame_interval = 1 / 40         # at most ~40 frames per second
resend_interval = 1.0               # the logo falls back to its scene after 5 s without frames
topic_live = 'boom-logo/live'
topic_status = 'boom-logo/bridge/status'
# End global settings

start_address = int(os.environ.get('DMX_START_ADDRESS', '1'))
control_fixture = os.environ.get('CONTROL_FIXTURE', 'false').lower() in ('1', 'true', 'yes')
channel_count = fixture_count * fixture_channels + (4 if control_fixture else 0)
if start_address < 1 or start_address - 1 + channel_count > packet_size:
    raise ValueError(f"DMX_START_ADDRESS {start_address}: {channel_count} channels must fit in one universe")

mqtt_tls = os.environ.get('MQTT_TLS', 'false').lower() in ('1', 'true', 'yes')
mqtt_host = os.environ['MQTT_HOST']
mqtt_port = int(os.environ.get('MQTT_PORT', '8883' if mqtt_tls else '1883'))


# Stop thread when necessary
running = True
# SocketIO exception
crashed = False
def stop_execution(signum, frame):
    global running
    running = False


# When receiving an EXIT signal, try to stop the execution flow
signal.signal(signal.SIGINT, stop_execution)
signal.signal(signal.SIGTERM, stop_execution)

# The latest frame from Aurora (None while Aurora is not connected), picked up by the sender thread
latest_frame = None
frame_lock = threading.Lock()
frame_changed = threading.Event()
aurora_connected = False

# Beat counter state: the counter goes up each time the beat channel rises past 128
beat_counter = 0
last_beat_channel = None

# Logging variables to track the incoming packets
last_log = time.monotonic()
packets_since = 0
frames_since = 0


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
    """The live frame for one DMX packet: R, G, B per segment times its dimmer, then the controls."""
    global beat_counter, last_beat_channel

    base = start_address - 1
    frame = bytearray()
    strobing = False
    for n in range(fixture_count):
        r, g, b, _, dimmer, strobe = channels[base + n * fixture_channels:base + (n + 1) * fixture_channels]
        color = bytes(min(255, int(c * dimmer / 255 + 0.5)) for c in (r, g, b))
        frame += color * (tree_segments if n == fixture_count - 1 else 1)
        strobing = strobing or strobe == 255

    if not control_fixture:
        frame.append(255 if strobing else 0)
        return bytes(frame)

    variant, beat, texture, hit = channels[base + fixture_count * fixture_channels:base + channel_count]
    frame.append((variant or 255) if strobing else 0)
    # A counter instead of the channel itself, so a short beat survives the rate limit
    if last_beat_channel is not None and last_beat_channel < 128 <= beat:
        beat_counter = (beat_counter + 1) % 256
    last_beat_channel = beat
    frame += bytes((beat_counter, texture, hit))
    return bytes(frame)


def set_frame(frame):
    global latest_frame
    with frame_lock:
        changed = frame != latest_frame
        latest_frame = frame
    if changed:
        frame_changed.set()


def send_frames():
    """Publish frames when they change, at most ~40 per second, and resend the last one every second."""
    global frames_since
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


def main_thread():
    global running, crashed

    crashed = False

    url = os.environ['URL'] + '/api/auth/key'
    result = requests.post(url, {'key': os.environ['API_KEY']})

    if result.status_code != 200:
        body = result.json()
        raise Exception("Could not authenticate with core: [HTTP {}]: {}".format(
            result.status_code,
            body['details'] if body['details'] else body['message']),
        )

    cookie = result.cookies.get('connect.sid')

    sio = socketio.Client()

    @sio.event(namespace='/lights')
    @sio.event(namespace='/')
    def dmx_packet(packet):
        global packets_since, frames_since, last_log

        set_frame(build_frame(parse_array(packet, packet_size)))
        packets_since = packets_since + 1

        # Logging/debugging time
        now = time.monotonic()
        if now - last_log > 1:
            logging.debug(f"Received {packets_since:02} DMX packets, sent {frames_since:02} frames since last log "
                          f"(last frame: {latest_frame.hex() if latest_frame else None})")

            # Reset logging variables
            packets_since = 0
            frames_since = 0
            last_log = now

    @sio.event(namespace='/')
    @sio.event(namespace='/lights')
    def disconnect(*args):
        global crashed
        crashed = True

    sio.connect(os.environ['URL'], headers={'cookie': 'connect.sid=' + cookie},
                namespaces=['/', '/lights'])
    set_aurora_connected(True)
    logging.info('Start listening...')

    while running and not crashed:
        time.sleep(1)

    set_aurora_connected(False)
    sio.disconnect()
    if crashed and running:
        raise Exception('Socket disconnect')


mqtt_client.connect_async(mqtt_host, mqtt_port, keepalive=15)
mqtt_client.loop_start()
threading.Thread(target=send_frames, daemon=True).start()

while running:
    try:
        logging.info('Connecting...')
        main_thread()
    except Exception as e:
        set_aurora_connected(False)
        logging.error(traceback.format_exc())
        logging.info('Something went wrong. Try again...')
        for _ in range(5):
            if running:
                time.sleep(1)

# Clean shutdown: tell the website the bridge is gone, the LWT only covers a lost connection
if mqtt_client.is_connected():
    mqtt_client.publish(topic_status, json.dumps({'online': False}), qos=0, retain=True).wait_for_publish(2)
mqtt_client.disconnect()
mqtt_client.loop_stop()
