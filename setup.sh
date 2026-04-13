#!/bin/bash
set -e

echo "Setting up Tosca Job Agent..."

sudo apt update -y
sudo apt install -y python3 python3-pip python3-venv

cd "$(dirname "$0")"

python3 -m venv .venv
source .venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt

python -m playwright install chromium

mkdir -p data
touch data/application_log.json

echo "Setup complete."
echo "Now add your .env file if not already present."
