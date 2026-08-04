#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Single-DUT smoke test for at-test.py.

    AT
    AT+CWMODE=1
    AT+CWJAP="688018",""
    AT+GMR

Usage::

    at-test.py -t bin/examples/at_smoke.py -p0 /dev/ttyUSB0 -p1 /dev/ttyUSB1
"""


def run(ctx):
    ctx.at('AT', expect='OK', name='step1')
    ctx.at('AT+CWMODE=1', expect='OK', name='step2')
    ctx.at('AT+CWJAP="688018",""', expect='OK', timeout=10, name='step3')
    ctx.at('AT+GMR', expect='OK', name='step4')
