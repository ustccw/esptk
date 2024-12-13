#!/usr/bin/env python
#-*- coding: utf-8 -*-
# chenwu@espressif.com

import os
import sys
import argparse
import subprocess
import glob

esp_toolchain_prefix = {
    "esp32": "xtensa-esp32-elf",
    "esp32c2": "riscv32-esp-elf",
    "esp32c3": "riscv32-esp-elf",
    "esp32c5": "riscv32-esp-elf",
    "esp32c6": "riscv32-esp-elf",
    "esp32c61": "riscv32-esp-elf",
    "esp32s2": "xtensa-esp32s2-elf",
    "esp32s3": "xtensa-esp32s3-elf",
    "esp32h2": "riscv32-esp-elf",
    # "esp32h4": "riscv32-esp-elf",
    # "esp32s5": "xtensa-esp32s5-elf",
    "esp32p4": "riscv32-esp-elf",
    "esp8266": "xtensa-lx106-elf",
}

# {IDF_TARGET_TOOLCHAIN_PREFIX}-addr2line -pfiaC -e build/PROJECT.elf ADDRESS

esp_target_list = list(esp_toolchain_prefix.keys())

def ESP_LOGI(x):
    print("\033[32m{}\033[0m".format(x))

def ESP_LOGE(x):
    print("\033[31m{}\033[0m".format(x))

def ESP_LOGW(x):
    print("\033[33m{}\033[0m".format(x))


def esp_idf_get_toolchain(addr2line_tool):
    home_dir = os.path.expanduser('~')
    espressif_dir = os.path.join(home_dir, '.espressif', 'tools')
    addr2line_files = glob.glob(os.path.join(espressif_dir, '**', f'{addr2line_tool}'), recursive=True)

    if not addr2line_files:
        raise Exception(f'No {addr2line_tool} found in {espressif_dir}')
    else:
        latest_file = max(addr2line_files, key=os.path.getmtime)
        return os.path.abspath(latest_file)

def esp_dump_addr_to_symbols(args):
    addr2line_file = f'{esp_toolchain_prefix.get(args.chip)}-addr2line'
    addr2line_path = esp_idf_get_toolchain(addr2line_file)
    cmd = [addr2line_path, '-pfiaC', '-e', args.elf_file] + args.addr_list
    ret = ''
    try:
        translation = subprocess.check_output(cmd)
        ret = translation.decode()
    except OSError:
        pass
    return ret

class AddrDumpPairAction(argparse.Action):
    def __init__(self, option_strings, dest, nargs="+", **kwargs):
        super(AddrDumpPairAction, self).__init__(
            option_strings, dest, nargs, **kwargs
        )

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)

def main(argv=None, esp=None):
    parser = argparse.ArgumentParser(description="A simple dump script for the Espressif SoCs.", prog='esp-dump.py')
    parser.add_argument('elf_file', help='the path of your ELF file')
    parser.add_argument('addr_list', metavar="<address>:<address>/<address>", help='the list of the coredump address', action=AddrDumpPairAction)
    parser.add_argument('--chip', choices=esp_target_list, default='esp32c3', help='Chip type (default: esp32c3)')
    args = parser.parse_args()

    if not os.path.exists(args.elf_file):
        raise Exception('File does not exist: {}'.format(args.elf_file))

    try:
        ret = esp_dump_addr_to_symbols(args)
        ESP_LOGW(ret)
    finally:
        # do final cleanup
        pass

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        ESP_LOGE("{}".format(e))
        sys.exit(1)
