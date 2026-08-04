#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Two-DUT SoftAP TCP echo for at-test.py.

AT1 (log=/dev/ttyUSB0, cmd=/dev/ttyUSB1): SoftAP + TCP server
AT2 (log=/dev/ttyUSB2, cmd=/dev/ttyUSB3): Station + TCP client

Flow::

    AT1 SoftAP + CIPSERVER
    AT2 CWJAP SoftAP + CIPSTART TCP client
    AT2 sends ``hello, esp!``
    AT1 receives, echoes back
    AT2 receives echo  ->  PASS

DEVICES (module-level dict)
---------------------------
Declare named DUTs here. Empty ``{}`` only names the DUT; ports usually come
from CLI ``--dut``. Optional keys per DUT::

    DEVICES = {
        'AT1': {
            'log': '/dev/ttyUSB0',       # or 'log_port'
            'cmd': '/dev/ttyUSB1',       # or 'cmd_port'
            'log_baudrate': 74880,       # optional; default -b / 115200
            'cmd_baudrate': 115200,
        },
        'AT2': {
            'log': '/dev/ttyUSB2',
            'cmd': '/dev/ttyUSB3',
        },
    }

CLI ``--dut NAME=log,cmd`` overrides ports for that DUT.

Usage::

    at-test.py -t bin/examples/at_multi_dut.py \\
      --dut AT1=/dev/ttyUSB0,/dev/ttyUSB1 \\
      --dut AT2=/dev/ttyUSB2,/dev/ttyUSB3
"""

import re

AP_SSID = 'softap-688018'
AP_PASS = '12345678'
AP_IP = '192.168.4.1'
TCP_PORT = 8080
PAYLOAD = b'hello, esp!'

DEVICES = {
    'AT1': {},
    'AT2': {},
}


def run(ctx):
    ap = ctx.dut('AT1')
    sta = ctx.dut('AT2')
    text = PAYLOAD.decode()
    n = len(PAYLOAD)

    # AT1: SoftAP + TCP server
    ap.at('AT', expect='OK', name='step1')
    ap.at('AT+CWMODE=2', expect='OK', name='step2')
    ap.at('AT+CIPMUX=1', expect='OK', name='step3')
    ap.at(f'AT+CWSAP="{AP_SSID}","{AP_PASS}",5,3', expect='OK', name='step4')
    ap.at(f'AT+CIPSERVER=1,{TCP_PORT}', expect='OK', name='step5')

    # AT2: Station joins SoftAP
    sta.at('AT', expect='OK', name='step6')
    sta.at('AT+CWMODE=1', expect='OK', name='step7')
    sta.at(
        f'AT+CWJAP="{AP_SSID}","{AP_PASS}"',
        expect='OK',
        timeout=20,
        name='step8',
    )

    # TCP connect (client -> server)
    mark_conn = ap.mark()
    sta.at(
        f'AT+CIPSTART="TCP","{AP_IP}",{TCP_PORT}',
        expect='OK',
        timeout=10,
        name='step9',
    )
    ap.expect(
        re.compile(r'\d+,CONNECT'),
        expect_port='cmd',
        after=mark_conn,
        timeout=5,
        name='step10',
    )

    # Client sends payload
    mark_ipd = ap.mark()
    sta.at(f'AT+CIPSEND={n}', expect='>', name='step11')
    sta.send_raw(PAYLOAD, expect='SEND OK', name='step12')
    ap.expect(
        re.compile(rf'\+IPD,\d+,{n}:{re.escape(text)}'),
        expect_port='cmd',
        after=mark_ipd,
        timeout=5,
        name='step13',
    )

    # Server echoes payload back on link 0
    mark_echo = sta.mark()
    ap.at(f'AT+CIPSEND=0,{n}', expect='>', name='step14')
    ap.send_raw(PAYLOAD, expect='SEND OK', name='step15')
    sta.expect(
        re.compile(rf'\+IPD,{n}:{re.escape(text)}'),
        expect_port='cmd',
        after=mark_echo,
        timeout=5,
        name='step16',
    )
