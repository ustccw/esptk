#!/usr/bin/env python
#-*- coding: utf-8 -*-
# chenwu@espressif.com

import sys
import time
import subprocess
import datetime

if len(sys.argv) < 2:
    print("Usage: python scheduler.py <python_script.py> [<arg1>] [<arg2>] ...")
    sys.exit(1)

python_script = sys.argv[1]
args = sys.argv[2:]

while True:
    command = ["python", python_script] + args
    print("[{}] Running '{}' ...".format(datetime.datetime.now(), " ".join(command)))
    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as e:
        print(f"\033[31mError: {e}\033[0m")
        sys.exit(1)
    time.sleep(60)
