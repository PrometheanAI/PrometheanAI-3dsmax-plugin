#! /usr/bin/env python

"""
PrometheanAI startup script for Autodesk 3ds Max
"""

import os
import sys

max_server = None

def update_sys_path():
    script_path = os.path.abspath(os.path.dirname(__file__))
    if script_path not in sys.path:
        sys.path.append(script_path)


def start_promethean_max_server():
    from promethean import promethean_3dsmax_qt_server

    global max_server
    if max_server:
        return
        
    max_server = promethean_3dsmax_qt_server.MaxServer()


update_sys_path()
start_promethean_max_server()
