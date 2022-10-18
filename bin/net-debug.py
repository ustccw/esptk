#!/usr/bin/env python
#-*- coding: utf-8 -*-
# chenwu@espressif.com

import sys, os

def ESP_LOGI(x):
    print('\033[32m{}\033[0m'.format(x))

def ESP_LOGIB(x):
    print('\033[1;32m{}\033[0m'.format(x))

def ESP_LOGE(x):
    print('\033[31m{}\033[0m'.format(x))

def ESP_LOGB(x):
    print('\033[1m{}\033[0m'.format(x))

def ESP_LOGN(x):
    print('{}'.format(x))

def print_help():
    ESP_LOGB('Usage: enter ESP-IDF directory and run this script "python {}".\r\n'.format(sys.argv[0]))
    ESP_LOGN('  This is a simple tool to help debuging TCP and above protocol.')
    ESP_LOGN('  It will print all TCP Tx and TCP Rx packets information, which includes tcp len, tcp seq, tcp ack, src port, dest port, and tcp flags.')

# Actually, this file is applied to all netif layer
wlanif_c_file = 'wlanif.c'
# In cause of dupilcate patch
duplicate_check_pattern = 'lwip_print_tcp_info'

# Find the debug function entry, and add this print function
dbg_func_pos_pattern = 'In this function, the hardware should be initialized.'
dbg_func_pos_offset = 8
esp_lwip_tcp_dbg_func = """
#include "esp_log.h"
void lwip_print_tcp_info(void *buf, bool is_send)
{
  if (buf) {
      struct pbuf* p = (struct pbuf*)buf;
      if (p->tot_len < 50) {
          return;
      }
      uint32_t i;
      bool tcp_flag = false;
      i = *((unsigned char*)p->payload + 12);
      if (i == 0x08) { /*ipv4*/
          i = *((unsigned char*)p->payload + 13);
          if (i == 0) {
              i = *((unsigned char*)p->payload + 23);
              if (i == 0x06) { /*tcp*/
                  i = *((unsigned char*)p->payload + 16);
                  i <<= 8;
                  i += *((unsigned char*)p->payload + 17);
                  tcp_flag = true;
              }
          }
      }
      if (tcp_flag) {
          if (i >= 40) { /*tcp data*/
              uint32_t len, seq, ack, srcport, destport, flags;
              len = i;
              i = *((unsigned char*)p->payload + 38);
              i <<= 8;
              i += *((unsigned char*)p->payload + 39);
              i <<= 8;
              i += *((unsigned char*)p->payload + 40);
              i <<= 8;
              i += *((unsigned char*)p->payload + 41);
              seq = i;
              i = *((unsigned char*)p->payload + 42);
              i <<= 8;
              i += *((unsigned char*)p->payload + 43);
              i <<= 8;
              i += *((unsigned char*)p->payload + 44);
              i <<= 8;
              i += *((unsigned char*)p->payload + 45);
              ack = i;
              i = *((unsigned char*)p->payload + 34);
              i <<= 8;
              i += *((unsigned char*)p->payload + 35);
              srcport = i;
              i = *((unsigned char*)p->payload + 36);
              i <<= 8;
              i += *((unsigned char*)p->payload + 37);
              destport = i;
              flags = *((unsigned char *)p->payload + 47);

              if (is_send) {
                  ESP_LOGI("TEST", "@@ WiFi Tx TCP - L:%u, S:%u, A:%u, SP:%u, DP:%u, F:%x", len, seq, ack, srcport, destport, flags);
              } else {
                  ESP_LOGI("TEST", "@@ WiFi Rx TCP - L:%u, S:%u, A:%u, SP:%u, DP:%u, F:%x", len, seq, ack, srcport, destport, flags);
              }
          }
      }
  }
}
"""

# Find the TCP Tx entry, and add this Rx print info
dbg_tx_caller_pos_pattern = 'if(q->next == NULL) {'
esp_lwip_tcp_debug_tx = """
    lwip_print_tcp_info(p, true);
"""

# Find the TCP Rx entry, and add this Rx print info
dbg_rx_caller_pos_pattern = '/* full packet send to tcpip_thread to process */'
esp_lwip_tcp_debug_rx = """
    lwip_print_tcp_info(p, false);
"""

def findfile(start, name):
    for relpath, dirs, files in os.walk(start):
        if name in files:
            full_path = os.path.join(start, relpath, name)
            return os.path.normpath(os.path.abspath(full_path))
    return None

def main():
    if len(sys.argv) == 2 and (sys.argv[1] == "-h" or sys.argv[1] == "--help"):
        print_help()
        sys.exit(0)

    wlanif_abspath = findfile('.', wlanif_c_file)
    if not wlanif_abspath:
        raise Exception('No {} found in {}'.format(wlanif_c_file, os.path.realpath('.')))

    with open(wlanif_abspath, 'rt+') as f:
        data = f.read()

        if data.find(duplicate_check_pattern) >= 0:
            ESP_LOGI("Already applied this patch in {}".format(wlanif_abspath))
            sys.exit(0)

        # add definition of lwip_print_tcp_info()
        pos = data.find(dbg_func_pos_pattern) - dbg_func_pos_offset
        data = data[:pos] + esp_lwip_tcp_dbg_func + data[pos:]

        # add tx caller of lwip_print_tcp_info()
        pos = data.find(dbg_tx_caller_pos_pattern)
        data = data[:pos] + esp_lwip_tcp_debug_tx + '\n    '  + data[pos:]

        # add rx caller of lwip_print_tcp_info()
        pos = data.find(dbg_rx_caller_pos_pattern)
        data = data[:pos] + esp_lwip_tcp_debug_rx + '\n    ' + data[pos:]

        f.seek(0)
        f.write(data)
    ESP_LOGIB('Patch applied done! ----> {}'.format(wlanif_abspath))

if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        ESP_LOGE('{}'.format(e))
        print_help()
        sys.exit(-1)
