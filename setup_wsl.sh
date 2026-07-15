#!/bin/bash
set -ex
cd /mnt/c/Users/saiki/OneDrive/Documents/Desktop/sdn-link-failure
rm -rf venv310
python3.10 -m venv --without-pip venv310
curl -sS https://bootstrap.pypa.io/get-pip.py | venv310/bin/python
source venv310/bin/activate
pip install setuptools==57.5.0 wheel
pip install -r requirements.txt
