#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Συντόμευση: τρέχει το docs/https/hik_https_check.py (διαγνωστικό HTTPS του καταγραφικού)."""
import os
import runpy

runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "https",
                            "hik_https_check.py"), run_name="__main__")
