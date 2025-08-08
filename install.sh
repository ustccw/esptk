#!/bin/bash
# chenwu@espressif.com
set -e

REPO_PATH="$(dirname "$(realpath "${BASH_SOURCE[0]}")")"
source $REPO_PATH/bin/esptk-export


ESP_LOGI "Updating apt package index..."
sudo apt update

ESP_LOGI "Installing system dependencies..."
sudo apt install -y git net-tools wget unzip xclip jq

ESP_LOGI "Installing Python dependencies..."
pip install -r $REPO_PATH/requirements.txt || ESP_LOGE "Failed to install Python dependencies"

ESP_LOGIB "\r\nAll done! You can now run the tools located in $REPO_PATH/bin/"
