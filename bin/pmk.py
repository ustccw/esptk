#!/usr/bin/env python
#-*- coding: utf-8 -*-
# chenwu@espressif.com

import hmac
import hashlib

def calculate_pmk(ssid, password):
    ssid_bytes = ssid.encode('utf-8')
    password_bytes = password.encode('utf-8')
    
    pmk = hashlib.pbkdf2_hmac('sha1', password_bytes, ssid_bytes, 4096, 32)
    
    return pmk

ssid = input("Please input ssid: ")
password = input("Please input password: ")

pmk = calculate_pmk(ssid, password)

pmk_hex = pmk.hex()
print("PMK: ", pmk_hex)

