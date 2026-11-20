#!/bin/bash
cd /root/robot
exec /root/robot/venv310/bin/python dz.py >> /root/robot/backups/dz-restart-20261113.log 2>&1
