#!/usr/bin/env python
#-*- coding: utf-8 -*-
# chenwu@espressif.com

import sys, subprocess

https_way = 'https://'
https_port = ':6688'

ssh_way = 'ssh://git@'
ssh_port = ':27227'

def ESP_LOGI(x):
    print('\033[32m{}\033[0m'.format(x))

def ESP_LOGE(x):
    print('\033[31m{}\033[0m'.format(x))

def ESP_LOGN(x):
    print('{}'.format(x))

# Get current remote URL
try:
    remote_url = subprocess.check_output(['git', 'remote', 'get-url', 'origin'], text=True).strip()
except Exception as e:
    ESP_LOGE(f'Error: {e}')
    sys.exit(1)

# Check if remote URL starts with "https" and contains port "6688"
if remote_url.startswith(https_way) and https_port in remote_url:
    # Replace "https" with "ssh" and replace port "6688" with "27227"
    new_remote_url = remote_url.replace(https_way, ssh_way).replace(https_port, ssh_port)
    subprocess.run(['git', 'remote', 'set-url', 'origin', new_remote_url])
    ESP_LOGI(f'Remote URL updated to ----> {new_remote_url}')

# Check if remote URL starts with "ssh" and contains port "27227"
elif remote_url.startswith(ssh_way) and ssh_port in remote_url:
    # Replace "ssh" with "https" and replace port "27227" with "6688"
    new_remote_url = remote_url.replace(ssh_way, https_way).replace(ssh_port, https_port)
    subprocess.run(['git', 'remote', 'set-url', 'origin', new_remote_url])
    ESP_LOGI(f'Remote URL updated to ----> {new_remote_url}')

else:
    ESP_LOGN('No changes made to the remote URL.')
