# SPDX-FileCopyrightText: 2022 Daniel Griswold
#
# SPDX-License-Identifier: MIT

"""

CircuitPython program to monitor the pressure difference in a radon mitigation system vs atmosphere.
See: https://www.health.state.mn.us/communities/environment/air/radon/mitigationsystem.html

It also grabs some air quality and environmental data from miscellaneous sensors.  All data is
published to MQTT.

Secrets should be set in secrets.py

"""

# pylint: disable=wrong-import-order
import board
import time
import json
import digitalio
import busio
import asyncio
import countio
import supervisor
import microcontroller
import ssl
import socketpool
import wifi

import adafruit_minimqtt.adafruit_minimqtt as MQTT
import sdp31
from adafruit_bme280 import basic as adafruit_bme280
import adafruit_ccs811
import foamyguy_nvm_helper as nvm_helper

from secrets import secrets

MQTT_ENVIRONMENT = secrets["mqtt_topic"] + "/environment"
MQTT_SYSTEM = secrets["mqtt_topic"] + "/system"
MQTT_RADON_HEALTH = secrets["mqtt_topic"] + "/radon"
RADON_PRESSURE_THRESHOLD = 75


async def get_sensor_data():
    """Creates dictionary of sensor data"""
    # global sensors

    while True:
        sensors["differential_pressure"] = sdp31.differential_pressure
        sensors["temperature"] = bme280.temperature
        sensors["humidity"] = bme280.relative_humidity
        sensors["pressure"] = bme280.pressure
        sensors["radon_pressure"] = (
            sensors["pressure"] - sensors["differential_pressure"]
        )

        ccs811.set_environmental_data(sensors["humidity"], sensors["temperature"])

        while not ccs811.data_ready:
            await asyncio.sleep(0)
        sensors["eco2"] = ccs811.eco2
        sensors["tvoc"] = ccs811.tvoc
        await asyncio.sleep(0)


async def publish_sensor_data():
    """ once a minute, publish the sensor data to MQTT. """
    while True:
        publish_to_mqtt(MQTT_ENVIRONMENT, json.dumps(sensors))
        await asyncio.sleep(60)


async def get_system_data():
    """Creates dictionary of system information"""
    _data = {}
    while True:
        _data["reset_reason"] = str(microcontroller.cpu.reset_reason)[28:]
        _data["time"] = time.monotonic()
        _data["ip_address"] = wifi.radio.ipv4_address
        _data["board_id"] = board.board_id
        publish_to_mqtt(MQTT_SYSTEM, json.dumps(_data))
        await asyncio.sleep(60)


async def check_radon_health():
    """Compare the differential pressure in the pipe.

    If less than the threshold, turn on the red led and buzzer.
    If greater than the threshold, turn on the green led.

    The state will be published to MQTT."""
    next_update = 0
    while True:
        if sensors["differential_pressure"] < RADON_PRESSURE_THRESHOLD:
            publish_to_mqtt(MQTT_RADON_HEALTH, str('{ "health": "False" }'))
            next_update = 0
        while sensors["differential_pressure"] < RADON_PRESSURE_THRESHOLD:
            green_led.value = False
            red_led.value = True
            if time.monotonic() > buzzer_suppress_time:
                buzzer.value = True
                await asyncio.sleep(1)
                buzzer.value = False
            await asyncio.sleep(1)

        green_led.value = True
        red_led.value = False
        buzzer.value = False

        if time.monotonic() > next_update:
            next_update = time.monotonic() + 60
            publish_to_mqtt(MQTT_RADON_HEALTH, str('{ "health": "True" }'))

        await asyncio.sleep(0)


async def check_buzzer_suppress():
    """ check if the suppress button has been pressed and extend suppression time. """

    global buzzer_suppress_time  # pylint: disable=invalid-name, global-statement
    suppress_button = countio.Counter(board.IO11, pull=digitalio.Pull.UP)
    while True:
        if suppress_button.count >= 1:
            suppress_button.reset()
            buzzer_suppress_time = time.monotonic() + 3600
        await asyncio.sleep(0)


def publish_to_mqtt(topic, message):  # pylint: disable=redefined-outer-name
    """Publish data to mqtt"""
    try:
        mqtt_client.is_connected()
    except MQTT.MMQTTException:
        """MQTT is not connected.  Attempt reconnect and publish."""
        try:
            print("MQTT not connected, retrying.")
            mqtt_client.reconnect()
        except (OSError, MQTT.MMQTTException):
            """Couldn't reconnect and publish."""
            print("Cound not reconnect to MQTT.")
            if supervisor.runtime.serial_connected:
                raise
            microcontroller.reset()
    except OSError:
        """wifi is not connected."""
        print("WiFi not connected")
        if supervisor.runtime.serial_connected:
            raise
        microcontroller.reset()
    finally:
        try:
            mqtt_client.publish(topic, message, retain=True)
        except (OSError, MQTT.MMQTTException):
            if supervisor.runtime.serial_connected:
                raise
            microcontroller.reset()


async def update_ccs811_baseline():
    """ Every day, write the ccs811 baseline to nvm """
    while True:
        await asyncio.sleep(86400)
        config["ccs811_baseline"] = ccs811.baseline
        nvm_helper.save_data(config, test_run=False)


async def main():
    """main function to start other tasks."""
    tasks = []
    tasks.append(asyncio.create_task(get_sensor_data()))
    tasks.append(asyncio.create_task(get_system_data()))
    tasks.append(asyncio.create_task(publish_sensor_data()))
    tasks.append(asyncio.create_task(update_ccs811_baseline()))
    tasks.append(asyncio.create_task(check_radon_health()))
    tasks.append(asyncio.create_task(check_buzzer_suppress()))

    await asyncio.gather(*tasks)


try:
    config = nvm_helper.read_data()
except EOFError:
    config = {"ccs811_baseline": "50260"}

try:
    i2c = busio.I2C(board.SCL, board.SDA)
    bme280 = adafruit_bme280.Adafruit_BME280_I2C(i2c)
    ccs811 = adafruit_ccs811.CCS811(i2c, address=0x5B)
    ccs811.baseline = config["ccs811_baseline"]
    sdp31 = sdp31.SDP31(i2c)
except RuntimeError as e:
    print("Could not initialize sensors: {}".format(e))
    while not i2c.try_lock():
        pass
    i2c.writeto(0x00, bytes([0x06]))
    time.sleep(2)
    if supervisor.runtime.serial_connected:
        raise
    supervisor.reload()


red_led = digitalio.DigitalInOut(board.IO1)
red_led.switch_to_output(False)
green_led = digitalio.DigitalInOut(board.IO18)
green_led.switch_to_output(True)
buzzer = digitalio.DigitalInOut(board.IO33)
buzzer.switch_to_output(False)

# silence_pin = digitalio.DigitalInOut(board.IO11)
# silence_pin.switch_to_input(pull=digitalio.Pull.UP)
# silence_button = Debouncer(silence_pin)
# silence_button.update()

sensors = {}  # pylint: disable=invalid-name
buzzer_suppress_time = 0  # pylint: disable=invalid-name

try:
    print("Connecting to %s..." % secrets["ssid"])
    wifi.radio.connect(secrets["ssid"], secrets["password"])
    print("Connected to %s!" % secrets["ssid"])
    pool = socketpool.SocketPool(wifi.radio)
except (OSError, RuntimeError) as error:
    print("Could not initialize network. {}".format(error))
    raise

try:
    mqtt_client = MQTT.MQTT(
        broker=secrets["mqtt_broker"],
        port=secrets["mqtt_port"],
        username=secrets["mqtt_username"],
        password=secrets["mqtt_password"],
        socket_pool=pool,
        ssl_context=ssl.create_default_context(),
    )

    print("Connecting to %s" % secrets["mqtt_broker"])
    mqtt_client.connect()
except (MQTT.MMQTTException, OSError) as error:
    print("Could not connect to mqtt broker. {}".format(error))
    raise

asyncio.run(main())
