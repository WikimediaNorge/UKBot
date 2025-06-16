#!/bin/bash

# Set some UTF-8 locale to get the UTF-8 in logs and filenames.
export LANG=en_US.utf8
export LC_ALL=en_US.utf8

CONTEST=$1
JOB_ID=$(cat /proc/sys/kernel/random/uuid)

projectdir=/data/project/ukbot
logfile=logs/${CONTEST}.upload.log

echo "-----------------------------------------------------------------"
cd "${projectdir}"
echo "$(date) : Starting '$CONTEST' job ($JOB_ID) on $HOSTNAME" > $logfile
START=$(date +%s)

. www/python/venv/bin/activate

# set -o pipefail  # doesn't work when run through the task schedueler
stdbuf -oL -eL ukbot config/config.${CONTEST}.yml --job_id "$JOB_ID" --action "uploadplot" >> $logfile 2>&1
status="${PIPESTATUS[0]}"

echo "$(date) : Job $JOB_NAME ($JOB_ID) on $HOSTNAME finished with exit code $status" >> $logfile
