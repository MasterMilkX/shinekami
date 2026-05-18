#!/usr/bin/env bash
if pkill -f "python3 shimeji.py"; then
    echo "All Shimeji dismissed."
else
    echo "No Shimeji running."
fi
