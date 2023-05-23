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
    ESP_LOGN('  This is a simple tool to help debuging TCP/UDP/ICMP and above protocol.')
    ESP_LOGN('  (Attention: this script does not support IP fragment and IPv6 till now.)')
    ESP_LOGB('\r\n  TCP:')
    ESP_LOGN('    It will print all outgoing (TCP Tx) and incoming (TCP Rx) packets information, which includes ip total len, tcp data len, tcp seq, tcp ack, src port, dest port, and tcp flags.')
    ESP_LOGN('    TCP flags is the accumulation of CWR:128, ECN:64, Urgent:32, Ack:16, Push:8, Reset:4, SYN:2, FIN:1.')
    ESP_LOGB('\r\n  UDP:')
    ESP_LOGN('    It will print all outgoing (UDP Tx) and incoming (UDP Rx) packets information which src port or dst port is 1234.')
    ESP_LOGN('    (you can specify any other port or all ports by modifying s_udp_port variable of file: {}, which in ESP-IDF)'.format(wlanif_c_file))
    ESP_LOGN('    packets information includes ip total len, src port, dest port, and udp data len.')
    ESP_LOGB('\r\n  ICMP:')
    ESP_LOGN('    It will print all outgoing (ICMP Tx) and incoming (ICMP Rx) packets information which icmp type is echo-request (8) or echo-reply (0),')
    ESP_LOGN('    packets information includes ip total len, icmp type, id, seq, and icmp data len.')

# Actually, this file is applied to all netif layer
wlanif_c_file = 'wlanif.c'
# In cause of dupilcate patch
duplicate_check_pattern = 'lwip_print_pkt_info'

# Find the debug function entry, and add this print function
dbg_func_pos_pattern = 'In this function, the hardware should be initialized.'
dbg_func_pos_offset = 8
esp_lwip_tcp_dbg_func = """
#include <stdio.h>
static uint16_t s_tcp_port = 0;     /* watch all tcp link */
static uint16_t s_udp_port = 1234;  /* watch one udp link which port == 1234 in case of udp rx flooding */
/* only support tcp, udp, icmp now. attention: don't support ip fragment now. */
void lwip_print_pkt_info(void *buf, bool is_send)
{
    if (!buf) {
        return;
    }
    // length check
    struct pbuf *p = (struct pbuf *)buf;
    if (p->tot_len <= 34) { /* no a valid IPv4 header */
        return;
    }

    // network protocol check
    enum {
        NET_PROTO_IPV4 = 0x0800,
        NET_PROTO_IPV6 = 0x86DD,
        NET_PROTO_ARP  = 0x0806,
    };
    uint16_t net_proto;
    /* p->payload consists of sequential: dst mac(6B) + src mac(6B) + proto type (2B) + IP4 header (20B+) + etc */
    net_proto = *((uint8_t *)p->payload + 12);
    net_proto <<= 8;
    net_proto += *((uint8_t *)p->payload + 13);
    if (net_proto != NET_PROTO_IPV4) { /* only support IPv4+ protocol now */
        return;
    }

    /* IPv4+ protocol check */
    enum {
        IP4_PROTO_ICMP = 1,
        IP4_PROTO_IGMP = 2,
        IP4_PROTO_TCP  = 6, /* SSL, HTTP, FTP, SMTP,TELNET, SSH */
        IP4_PROTO_UDP = 17, /* DNS, SNTP, TFTP, SNMP */
    };
    uint8_t *ip4 = (uint8_t *)p->payload + 14;
    uint8_t ip_proto = *(ip4 + 9);
    uint16_t ip_tlen = *(ip4 + 2);
    ip_tlen <<= 8;
    ip_tlen += *(ip4 + 3);
    uint16_t ip_hlen = (*ip4 & 0x0F) * 4;
    uint16_t ip_dlen = ip_tlen - ip_hlen;

    // print key information for ip protocols
    switch (ip_proto) {
    case IP4_PROTO_TCP: {
        if (ip_dlen < 20) {
            return;
        }
        uint8_t *tcp = ip4 + ip_hlen;
        uint16_t src_port = *tcp; src_port <<= 8; src_port += *(tcp + 1);
        uint16_t dst_port = *(tcp + 2); dst_port <<= 8; dst_port += *(tcp + 3);
        uint32_t seq = *(tcp + 4); seq <<= 8; seq += *(tcp + 5); seq <<= 8; seq += *(tcp + 6); seq <<= 8; seq += *(tcp + 7);
        uint32_t ack = *(tcp + 8); ack <<= 8; ack += *(tcp + 9); ack <<= 8; ack += *(tcp + 10); ack <<= 8; ack += *(tcp + 11);
        uint8_t tcp_hlen = ((*(tcp + 12) & 0xF0) >> 4) * 4;
        uint8_t flags = *(tcp + 13);
        uint16_t tcp_dlen = ip_dlen - tcp_hlen;
        if (s_tcp_port != 0 && src_port != s_tcp_port && dst_port != s_tcp_port) {
            return;
        }
        if (is_send) {
            printf("@@ WiFi Tx TCP - IPL:%u, S:%u, A:%u, SP:%u, DP:%u, F:0x%x, TDL:%u\\n", ip_tlen, seq, ack, src_port, dst_port, flags, tcp_dlen);
        } else {
            printf("@@ WiFi Rx TCP - IPL:%u, S:%u, A:%u, SP:%u, DP:%u, F:0x%x, TDL:%u\\n", ip_tlen, seq, ack, src_port, dst_port, flags, tcp_dlen);
        }
    }
        break;

    case IP4_PROTO_UDP: {
        if (ip_dlen < 8) {
            return;
        }
        uint8_t *udp = ip4 + ip_hlen;
        uint16_t src_port = *udp; src_port <<= 8; src_port += *(udp + 1);
        uint16_t dst_port = *(udp + 2); dst_port <<= 8; dst_port += *(udp + 3);
        uint16_t udp_dlen = ip_dlen - 8; /* fixed length: 8 bytes for udp header */
        if (s_udp_port != 0 && src_port != s_udp_port && dst_port != s_udp_port) {
            return;
        }
        if (is_send) {
            printf("@@ WiFi Tx UDP - IPL:%u, SP:%u, DP:%u, UDL:%u\\n", ip_tlen, src_port, dst_port, udp_dlen);
        } else {
            printf("@@ WiFi Rx UDP - IPL:%u, SP:%u, DP:%u, UDL:%u\\n", ip_tlen, src_port, dst_port, udp_dlen);
        }
    }
        break;

    case IP4_PROTO_ICMP: {
        if (ip_dlen < 8) {
            return;
        }
        uint8_t *icmp = ip4 + ip_hlen;
        uint8_t type = *icmp;
        uint8_t code = *(icmp + 1);
        bool valid_type = (type == 0 || type == 8); /* icmp echo-request or echo-reply */
        bool valid_code = (code == 0);
        uint16_t icmp_dlen = ip_dlen - 8; /* fixed length: 8 bytes for icmp echo-request or echo-reply */
        if (!valid_type || !valid_code) {
            return;
        }
        uint16_t id = *(icmp + 4); id <<= 8; id += *(icmp + 5);
        uint16_t seq = *(icmp + 6); seq <<= 8; seq += *(icmp + 7);
        if (is_send) {
            printf("@@ WiFi Tx ICMP - %s, IPL:%u, ID:0x%x, S:%u PDL:%u\\n", (type == 8) ? "Echo" : "Echo Reply", ip_tlen, id, seq, icmp_dlen);
        } else {
            printf("@@ WiFi Rx ICMP - %s, IPL:%u, ID:0x%x, S:%u PDL:%u\\n", (type == 8) ? "Echo" : "Echo Reply", ip_tlen, id, seq, icmp_dlen);
        }
    }
        break;

    default:
        return;
    }
}
"""

# Find the TCP Tx entry, and add this Rx print info
dbg_tx_caller_pos_pattern_esp_idf = 'if(q->next == NULL) {'
dbg_tx_caller_pos_pattern_rtos = 'if (!netif_is_up(netif)) {'
esp_lwip_tcp_debug_tx = """
    lwip_print_pkt_info(p, true);
"""

# Find the TCP Rx entry, and add this Rx print info
dbg_rx_caller_pos_pattern = '/* full packet send to tcpip_thread to process */'
esp_lwip_tcp_debug_rx = """
    lwip_print_pkt_info(p, false);
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

        # add definition of lwip_print_pkt_info()
        pos = data.find(dbg_func_pos_pattern) - dbg_func_pos_offset
        data = data[:pos] + esp_lwip_tcp_dbg_func + data[pos:]

        # add tx caller of lwip_print_pkt_info()
        pos = data.find(dbg_tx_caller_pos_pattern_esp_idf)
        if pos < 0:
            pos = data.find(dbg_tx_caller_pos_pattern_rtos)
            if pos < 0:
                raise Exception('No Tx caller entry found.')
        data = data[:pos] + esp_lwip_tcp_debug_tx + '\n    '  + data[pos:]

        # add rx caller of lwip_print_pkt_info()
        pos = data.find(dbg_rx_caller_pos_pattern)
        if pos < 0:
            raise Exception('No Rx caller entry found.')
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
