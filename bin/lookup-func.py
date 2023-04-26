#!/usr/bin/env python
#-*- coding: utf-8 -*-
# chenwu@espressif.com

import os, sys, subprocess

extension = '.a'

def ESP_LOGI(x):
    print('\033[32m{}\033[0m'.format(x))

def ESP_LOGIB(x):
    print('\033[1;32m{}\033[0m'.format(x))

def ESP_LOGE(x):
    print('\033[31m{}\033[0m'.format(x))

if len(sys.argv) != 2:
    ESP_LOGE('Usage: python lookup-func.py <func_name>')
    sys.exit(1)
else:
    function_name = sys.argv[1]

def search_files_with_extension(extension):
    files = []
    for dirpath, dirnames, filenames in os.walk('.'):
        for filename in filenames:
            if filename.endswith(extension) and 'build' not in dirpath:
                files.append(os.path.join(dirpath, filename))
    return files

def search_function_in_a_files(files, function_name):
    ret = []
    for file in files:
        try:
            cmd = 'nm {} | grep {} | grep " T "'.format(file, function_name)
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if result.returncode == 0:
                ret.append((file, result.stdout))
        except subprocess.CalledProcessError as e:
            ESP_LOGE(f"Error occurred while processing file: {file}, error message: {e.output}")
    return ret

if __name__ == '__main__':
    ESP_LOGIB(f"Searching for '{function_name}' in '{extension}' files in current directory and its subdirectories...")
    a_files = search_files_with_extension(extension)
    ret = search_function_in_a_files(a_files, function_name)

    if len(ret) == 0:
        ESP_LOGE(f"Cannot find '{function_name}' in '{extension}' files in current directory and its subdirectories.")
    else:
        ESP_LOGI(f"Found '{function_name}' in '{extension}' files in current directory and its subdirectories:")
        for r in ret:
            print(f"Found '{function_name}' in file: {r[0]} ----> {r[1]}", end='')
