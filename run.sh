#!/bin/bash
# Start Trading Desk at http://127.0.0.1:5057
cd "$(dirname "$0")"
exec .venv/bin/python app.py
