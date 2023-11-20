#!/usr/bin/env python
#-*- coding: utf-8 -*-
# chenwu@espressif.com

import os
import sys
import argparse

AT_FIRMWARES = {
    "ESP32-C2": {
        "local": False,
        "firmwares": [
            {"ESP32-C2-4MB (v3.3.0.0)": "esp32c2/ESP32-C2-4MB-V3.3.0.0.zip"},
            {"ESP32-C2-2MB (v3.3.0.0)": "esp32c2/ESP32-C2-2MB-V3.3.0.0.zip"},
            {"ESP32-C2-4MB (v3.1.0.0)": "esp32c2/ESP32-C2-4MB-V3.1.0.0.zip"},
            {"ESP32-C2-2MB (v3.1.0.0)": "esp32c2/ESP32-C2-2MB-V3.1.0.0.zip"},
            {"ESP32-C2-4MB (v3.0.0.0)": "esp32c2/ESP32-C2-4MB-V3.0.0.0.zip"},
            {"ESP32-C2-4MB (v3.0.0.0)": "esp32c2/ESP32-C2-4MB-V3.0.0.0.zip"},
        ],
    },

    "ESP32-C3": {
        "local": False,
        "firmwares": [
            {"ESP32-C3-MINI-1 (v3.3.0.0)": "esp32c3/ESP32-C3-MINI-1-AT-V3.3.0.0.zip"},
            {"ESP32-C3-MINI-1 (v3.2.0.0)": "esp32c3/ESP32-C3-MINI-1-AT-V3.2.0.0.zip"},
            {"ESP32-C3-MINI-1 (v2.4.2.0)": "esp32c3/ESP32-C3-MINI-1-AT-V2.4.2.0.zip"},
            {"ESP32-C3-MINI-1 (v2.4.1.0)": "esp32c3/ESP32-C3-MINI-1-AT-V2.4.1.0.zip"},
            {"ESP32-C3-MINI-1 (v2.4.0.0)": "esp32c3/ESP32-C3-MINI-1-AT-V2.4.0.0.zip"},
            {"ESP32-C3-MINI-1 (v2.3.0.0)": "esp32c3/ESP32-C3-MINI-1-AT-V2.3.0.0.zip"},
            {"ESP32-C3-MINI-1 (v2.2.0.0)": "esp32c3/ESP32-C3-MINI-1-AT-V2.2.0.0.zip"},
        ],
    },

    "ESP32-C6": {
        "local": False,
        "firmwares": [
            {"ESP32-C6-4MB (v4.0.0.0)": "esp32c6/ESP32-C6-4MB-AT-V4.0.0.0.zip"},
        ],
    },

    "ESP32": {
        "local": False,
        "firmwares": [
            # ESP32-WROOM-32
            {"ESP32-WROOM-32 (v3.3.0.0)": "esp32/ESP32-WROOM-32/ESP32-WROOM-32-AT-V3.3.0.0.zip"},
            {"ESP32-WROOM-32 (v3.2.0.0)": "esp32/ESP32-WROOM-32/ESP32-WROOM-32-AT-V3.2.0.0.zip"},
            {"ESP32-WROOM-32 (v2.4.3.0)": "esp32/ESP32-WROOM-32/ESP32-WROOM-32-AT-V2.4.3.0.zip"},
            {"ESP32-WROOM-32 (v2.4.0.0)": "esp32/ESP32-WROOM-32/ESP32-WROOM-32-AT-V2.4.0.0.zip"},
            {"ESP32-WROOM-32 (v2.2.0.0)": "esp32/ESP32-WROOM-32/ESP32-WROOM-32-AT-V2.2.0.0.zip"},
            {"ESP32-WROOM-32 (v2.1.0.0)": "esp32/ESP32-WROOM-32/ESP32-WROOM-32-AT-V2.1.0.0.zip"},
            {"ESP32-WROOM-32 (v2.0.0.0)": "esp32/ESP32-WROOM-32/ESP32-WROOM-32-AT-V2.0.0.0.zip"},
            {"ESP32-WROOM-32 (v1.1.2.0)": "esp32/ESP32-WROOM-32/ESP32-WROOM-32-AT-V1.1.2.0.zip"},
            {"ESP32-WROOM-32 (v1.1.1.0)": "esp32/ESP32-WROOM-32/ESP32-WROOM-32-AT-V1.1.1.0.zip"},
            {"ESP32-WROOM-32 (v1.1.0.0)": "esp32/ESP32-WROOM-32/ESP32-WROOM-32-AT-V1.1.0.0.zip"},
            {"ESP32-WROOM-32 (v1.0.0.0)": "esp32/ESP32-WROOM-32/ESP32-WROOM-32-AT-V1.0.0.0.zip"},
            {"ESP32-WROOM-32 (v0.10.0.0)": "esp32/ESP32-WROOM-32/ESP32-WROOM-32-AT-V0.10.0.0.zip"},
            # ESP32-WROVER-32
            {"ESP32-WROVER-32 (v2.4.3.0)": "esp32/ESP32-WROVER-32/ESP32-WROVER-32-AT-V2.4.3.0.zip"},
            {"ESP32-WROVER-32 (v2.4.0.0)": "esp32/ESP32-WROVER-32/ESP32-WROVER-32-AT-V2.4.0.0.zip"},
            {"ESP32-WROVER-32 (v2.2.0.0)": "esp32/ESP32-WROVER-32/ESP32-WROVER-32-AT-V2.2.0.0.zip"},
            {"ESP32-WROVER-32 (v2.1.0.0)": "esp32/ESP32-WROVER-32/ESP32-WROVER-32-AT-V2.1.0.0.zip"},
            {"ESP32-WROVER-32 (v2.0.0.0)": "esp32/ESP32-WROVER-32/ESP32-WROVER-32-AT-V2.0.0.0.zip"},
            {"ESP32-WROVER-32 (v0.10.0.0)": "esp32/ESP32-WROVER-32/ESP32-WROVER-32-AT-V0.10.0.0.zip"},
            # ESP32-SOLO
            {"ESP32-SOLO (v3.3.0.0)": "esp32/ESP32-SOLO/ESP32-SOLO-AT-V3.3.0.0.zip"},
            {"ESP32-SOLO (v3.2.0.0)": "esp32/ESP32-SOLO/ESP32-SOLO-AT-V3.2.0.0.zip"},
            {"ESP32-SOLO (v2.4.3.0)": "esp32/ESP32-SOLO/ESP32-SOLO-AT-V2.4.3.0.zip"},
            {"ESP32-SOLO (v2.4.0.0)": "esp32/ESP32-SOLO/ESP32-SOLO-AT-V2.4.0.0.zip"},
            {"ESP32-SOLO (v2.2.0.0)": "esp32/ESP32-SOLO/ESP32-SOLO-AT-V2.2.0.0.zip"},
            {"ESP32-SOLO (v2.1.0.0)": "esp32/ESP32-SOLO/ESP32-SOLO-AT-V2.1.0.0.zip"},
            {"ESP32-SOLO (v2.0.0.0)": "esp32/ESP32-SOLO/ESP32-SOLO-AT-V2.0.0.0.zip"},
            # ESP32-PICO-D4
            {"ESP32-PICO-D4 (v3.3.0.0)": "esp32/ESP32-PICO-D4/ESP32-PICO-D4-AT-V3.3.0.0.zip"},
            {"ESP32-PICO-D4 (v3.2.0.0)": "esp32/ESP32-PICO-D4/ESP32-PICO-D4-AT-V3.2.0.0.zip"},
            {"ESP32-PICO-D4 (v2.4.3.0)": "esp32/ESP32-PICO-D4/ESP32-PICO-D4-AT-V2.4.3.0.zip"},
            {"ESP32-PICO-D4 (v2.4.0.0)": "esp32/ESP32-PICO-D4/ESP32-PICO-D4-AT-V2.4.0.0.zip"},
            {"ESP32-PICO-D4 (v2.2.0.0)": "esp32/ESP32-PICO-D4/ESP32-PICO-D4-AT-V2.2.0.0.zip"},
            {"ESP32-PICO-D4 (v2.1.0.0)": "esp32/ESP32-PICO-D4/ESP32-PICO-D4-AT-V2.1.0.0.zip"},
            {"ESP32-PICO-D4 (v2.0.0.0)": "esp32/ESP32-PICO-D4/ESP32-PICO-D4-AT-V2.0.0.0.zip"},
            # ESP32-MINI-1
            {"ESP32-MINI-1 (v3.3.0.0)": "esp32/ESP32-MINI-1/ESP32-MINI-1-AT-V3.3.0.0.zip"},
            {"ESP32-MINI-1 (v3.2.0.0)": "esp32/ESP32-MINI-1/ESP32-MINI-1-AT-V3.2.0.0.zip"},
            {"ESP32-MINI-1 (v2.4.3.0)": "esp32/ESP32-MINI-1/ESP32-MINI-1-AT-V2.4.3.0.zip"},
            {"ESP32-MINI-1 (v2.4.0.0)": "esp32/ESP32-MINI-1/ESP32-MINI-1-AT-V2.4.0.0.zip"},
            {"ESP32-MINI-1 (v2.2.0.0)": "esp32/ESP32-MINI-1/ESP32-MINI-1-AT-V2.2.0.0.zip"},
        ],
    },

    "ESP8266": {
        "local": False,
        "firmwares": [
            {"ESP-WROOM-02 (v2.2.1.0)": "esp8266/ESP-WROOM-02-AT-V2.2.1.0.zip"},
            {"ESP-WROOM-02 (v2.2.0.0)": "esp8266/ESP-WROOM-02-AT-V2.2.0.0.zip"},
            {"ESP-WROOM-02N (v2.2.0.0)": "esp8266/ESP-WROOM-02N-AT-V2.2.0.0.zip"},
            {"ESP-WROOM-02 (v2.1.0.0)": "esp8266/ESP-WROOM-02-AT-V2.1.0.0.zip"},
            {"ESP-WROOM-02 (v2.0.0.0)": "esp8266/ESP-WROOM-02-AT-V2.0.0.0.zip"},
        ],
    },

    "ESP8266 (NONOS)": {
        "local": False,
        "firmwares": [
            {"ESP-WROOM-02 (v1.7.5.0)": "nonos/esp8266_at_bin_v1.7.5.zip"},
            {"ESP-WROOM-02 (v1.7.4.0)": "nonos/esp8266_at_bin_v1.7.4.zip"},
            {"ESP-WROOM-02 (v1.7.3.0)": "nonos/esp8266_at_bin_v1.7.3.zip"},
            {"ESP-WROOM-02 (v1.7.2.0)": "nonos/esp8266_at_bin_v1.7.2.zip"},
            {"ESP-WROOM-02 (v1.7.1.0)": "nonos/esp8266_at_bin_v1.7.1.zip"},
            {"ESP-WROOM-02 (v1.7.0.0)": "nonos/esp8266_at_bin_v1.7.0.zip"},
            {"ESP-WROOM-02 (v1.6.2.0)": "nonos/esp8266_at_bin_v1.6.2.zip"},
            {"ESP-WROOM-02 (v1.6.1.0)": "nonos/esp8266_at_bin_v1.6.1.zip"},
            {"ESP-WROOM-02 (v1.6.0.0)": "nonos/esp8266_at_bin_v1.6.0.zip"},
            {"ESP-WROOM-02 (v1.5.1.0)": "nonos/esp8266_at_bin_v1.5.1.zip"},
        ],
    },

    "ESP32-S2": {
        "local": True,
        "path_dir": "~/esp/at/release-related/esp-at-dl",
        "firmwares": [
            {"ESP32-S2-SOLO (v2.1.0.0)": "esp32s2/ESP32-S2-SOLO/ESP32-S2-SOLO-AT-V2.1.0.0.zip"},
            {"ESP32-S2-WROVER (v2.1.0.0)": "esp32s2/ESP32-S2-WROVER/ESP32-S2-WROVER-AT-V2.1.0.0.zip"},
            {"ESP32-S2-WROOM (v2.1.0.0)": "esp32s2/ESP32-S2-WROOM/ESP32-S2-WROOM-AT-V2.1.0.0.zip"},
            {"ESP32-S2-MINI (v2.1.0.0)": "esp32s2/ESP32-S2-MINI/ESP32-S2-MINI-AT-V2.1.0.0.zip"},
        ],
    },

    "ESP8266 (QCLOUD)": {
        "local": False,
        "firmwares": [
            {"ESP8266-AT QCLOUD (v2.2.0)": "qcloud/qcloud_at_esp8266_v2.2.0.zip"},
            {"ESP8266-AT QCLOUD (v2.1.1)": "qcloud/qcloud_at_esp8266_v2.1.1.zip"},
        ],
    }
}

def ESP_LOGI(x):
    print("\033[32m{}\033[0m".format(x))

def ESP_LOGE(x):
    print("\033[31m{}\033[0m".format(x))

def ESP_LOGB(x):
    print("\033[1m{}\033[0m".format(x))

def ESP_LOGN(x):
    print("{}".format(x))

def ESP_LOGB0(x):
    print("\033[1m{}\033[0m".format(x), end='')

def at_choose_firmware(list_all):
    for index, key in enumerate(AT_FIRMWARES):
        ESP_LOGI("{}: {}".format(index, key))

    ESP_LOGB0("Select the released firmware you would like to download by index (default 0): ")
    chip_id = input().strip()
    
    if chip_id == '':
        chip_id = 0

    for index, key in enumerate(AT_FIRMWARES):
        if index == int(chip_id):
            firmware_list = AT_FIRMWARES[key]['firmwares']
            for module_id, key in enumerate(firmware_list):
                for module_name, module_url in key.items():
                    ESP_LOGI("{}: {}".format(module_id, module_name))
                # print('id:{} firmware list:{}'.format(module_id, firmware_list))


    return None

def esp_show_help():
    ESP_LOGB("NAME\r\n  at-released.py: Download the released firmware of esp-at project from AT Binary Lists\r\n")
    ESP_LOGN("  at-released.py: just download the firmware from a short binary list")
    ESP_LOGN("  at-released.py -a: just download the firmware from a long binary list")
    ESP_LOGN("  at-released.py [x]: download the firmware from a short binary list and write flash by /dev/ttyUSBx")
    ESP_LOGN("  at-released.py [x] -a: download the firmware from a long binary list and write flash by /dev/ttyUSBx")

def main():

    argv = sys.argv[1:]

    if '-h' in argv or '--help' in argv:
        esp_show_help()
        sys.exit(0)

    list_all = False
    download_usb = None

    if len(argv) == 0:
        pass
    elif len(argv) == 1:
        if argv[0] == '-a':
            list_all = True
        else:
            download_usb = '/dev/ttyUSB' + argv[0]
    elif len(argv) == 2:
        list_all = True
        download_usb = '/dev/ttyUSB' + argv[0]
    elif len(argv) > 2:
        ESP_LOGE("Too many arguments")
        esp_show_help()
        sys.exit(1)

    if download_usb and not os.path.exists(download_usb) :
        ESP_LOGE("The device {} does not exist".format(download_usb))
        sys.exit(1)

    result = at_choose_firmware(list_all)


class FatalError(RuntimeError):
    """
    Wrapper class for runtime errors that aren't caused by internal bugs, but by
    ESP-AT responses or input content.
    """
    def __init__(self, message):
        RuntimeError.__init__(self, message)

    @staticmethod
    def WithResult(message, result):
        """
        Return a fatal error object that appends the hex values of
        'result' as a string formatted argument.
        """
        message += " (result was {})".format(hexify(result))
        return FatalError(message)

def _main():
    try:
        main()
    except FatalError as e:
        ESP_LOGE("A fatal error occurred: {}".format(e))
        sys.exit(2)
    except Exception as e:
        ESP_LOGE("A system error occurred: {}".format(e))

if __name__ == '__main__':
    _main()
