#!/usr/bin/env python
#-*- coding: utf-8 -*-
# chenwu@espressif.com

import sys, os, re

units = {"B": 1, "KB": 2**10, "MB": 2**20, "GB": 2**30, "TB": 2**40}

def esp_parse_size(size):
    size = size.upper()
    if not re.match(r' ', size):
        size = re.sub(r'([KMGT]?B)', r' \1', size)
    if not re.search(r'([KMGT]?B)', size):
        size += ' B'
    number, unit = [string.strip() for string in size.split()]
    return int(float(number) * units[unit])

def esp_combine_bin():
    pairs, bins = [], []

    # sflag marks explicit target file size or not
    sflag = (len(sys.argv) % 2 == 0)
    indexs = len(sys.argv) - sflag

    # parameters check 
    for i in range(1, indexs, 2):
        try:
            address = int(sys.argv[i], 0)
        except:
            print("\033[31mAddress Error!\033[0m")
            sys.exit(-1)
        try:
            argfile = open(sys.argv[i + 1], 'rb')
            argfile.close()
        except:
            print("\033[31mIndex Error or Open file Error!\033[0m")
            sys.exit(-1)
        pairs.append((address, sys.argv[i + 1]))

    # sort the address and check for overlapping
    last_end_addr = 0
    for address, binfile in sorted(pairs):
        argfile = open(binfile, 'rb')
        argfile.seek(0,2)  # seek to end
        size = argfile.tell()
        argfile.seek(0 ,0)
        argfile.close()
        start_addr = address
        end_addr = address + size
        if start_addr < last_end_addr:
            print("\033[31mAddress Overlap!\033[0m")
            sys.exit(-1)
        bins.append((start_addr, size, binfile))
        last_end_addr = end_addr

    if sflag == 1:
        try:
            target_size = esp_parse_size(sys.argv[indexs])
        except:
            print("\033[31mError size specified!\033[0m")
            sys.exit(-1)
        if target_size < last_end_addr:
            print("\033[31mToo small size specified!\033[0m")
            sys.exit(-1)
    else:
        target_size = last_end_addr

    # combine all small bins into a target.bin
    bin_data = bytearray([0xFF] * target_size)
    for address, binfile in sorted(pairs):
        print('0x%x,%s' % (address, binfile))
        with open(binfile, 'rb') as f:
            data = f.read()
            for i, byte_data in enumerate(data):
                bin_data[address + i] = byte_data

    # write file
    with open("target.bin", 'wb') as f:
        f.write(bin_data)
        print("\033[1;32mgenerate target.bin OK!\033[0m")

def _main():
    esp_combine_bin()

if __name__ == '__main__':
    _main()
