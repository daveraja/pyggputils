#!/usr/bin/env python

#---------------------------------------------------------------------------------
#
# A sample GGP player that makes NOOP moves. Note: it is not meant to
# be a sensible player, instead it serves as an example of how to use
# the ggputils SimplePlayer API.
#
# (c) 2014 David Rajaratnam
#
#---------------------------------------------------------------------------------

from gevent import monkey; monkey.patch_all()
import argparse
import logging
import re
import time
from ggputils.utils import _fmt
from ggputils.player import SimplePlayer

#---------------------------------------------------------------------------------
# Global variables
#---------------------------------------------------------------------------------
g_logger = logging.getLogger()


#---------------------------------------------------------------------------------
# Callback function for GGP start messages
#---------------------------------------------------------------------------------
def on_start(timeout, matchid, role, gdl, playclock):
    g_logger.info("Received START message...")

#---------------------------------------------------------------------------------
# Callback function to update the state of the system with a set of actions
#---------------------------------------------------------------------------------
def on_update(actions):
    g_logger.info("Updating actions: {0}".format(actions))

#---------------------------------------------------------------------------------
# Callback function to select a move within the timeout
#---------------------------------------------------------------------------------
def on_select(timeout):
    timeout.reduce(1.5)
    g_logger.info("Select: player has {0} seconds to make a move".format(timeout.remaining()))
    time.sleep(timeout.remaining())
    return "NOOP"

#---------------------------------------------------------------------------------
# Callback function to clear the state at the end of a game or on abort.
#---------------------------------------------------------------------------------
def on_clear():
    g_logger.info("Clearing game state")
    
#---------------------------------------------------------------------------------
# log_level(log_string)
#---------------------------------------------------------------------------------
def log_level(log_string):
    if re.match(r'^critical$', log_string): return logging.CRITICAL
    elif re.match(r'^error$', log_string): return logging.ERROR
    elif re.match(r'^warning$', log_string): return logging.WARNING
    elif re.match(r'^info$', log_string): return logging.INFO
    elif re.match(r'^debug$', log_string): return logging.DEBUG
    else:
        raise ValueError("Invalid log level: {0}".format(log_string))
    
#-----------------------------
# main
#-----------------------------
        
def main():
    parser = argparse.ArgumentParser(description="NOOP GGP Player")
    parser.add_argument("--host", default="", 
                        help="listener host")
    parser.add_argument("--port", type=int, default=4001, 
                        help="ggp player port")
    parser.add_argument("--log-level", default="debug", 
                        choices=['critical','error','warning','info','debug'],
                        help="logging level")
    args = parser.parse_args()
    
    # Some some logging
    g_logger.setLevel(log_level(args.log_level))
    g_logger.addHandler(logging.StreamHandler())

    SimplePlayer((args.host, args.port), on_start=on_start, on_update=on_update,
                 on_select=on_select, on_clear=on_clear)
    
if __name__ == '__main__':
    main()
