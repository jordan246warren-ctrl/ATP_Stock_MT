#!/bin/bash
cd "$(dirname "$0")"
python3 -m streamlit run app.py --server.address 127.0.0.1 --browser.gatherUsageStats false
