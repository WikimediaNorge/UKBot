#!/bin/bash
#
# Usage:
# toolforge-jobs run update-venv --command "./update_venv.sh" --image tf-python39 --wait

set -euo pipefail

# activate venv
source www/python/venv/bin/activate

# upgrade pip inside the venv and add support for the wheel package format
pip install -U pip wheel

# install packages
pip install -e . --upgrade
