#---------------------------------------------------------------------------------
#
# API for GGP Players.
#
# (c) 2014 David Rajaratnam
#
#---------------------------------------------------------------------------------

import logging
import operator
from .ggp_http_handler import Handler
from ggputils.utils import *
from gevent.wsgi import *

g_logger = logging.getLogger(__name__)

#---------------------------------------------------------------------------------
# RawPlayer provides the base of a WSGI server based GGP player. It
# starts an HTTP server on the given port and listens for connections
# from the game master. It is a thin layer that requires the same
# callback functions as the Handler class.
#
# See the comments in the Handler class for more details, but the
# prototypes for the callbacks are:
#
# - on_start(timeout, matchid, role, gdl, playclock)
# - on_play(timeout, actionstr)
# - on_stop(timeout, actionstr)
# - on_abort()
# - on_info() - optional
# - on_preview(timeout, gdl) -optional
#
# Note: The timeout is a ggputils.util.Timeout object. It is
# calculated from a timestamp taken when the GGP message has been
# received with the addition of the start/play/preview clock.  This
# timeout should be reduced (using the reduce() call) to allow for
# some buffer in responding to the game master. Unfortunately, we
# can't do better than this without some modifications to the GGP
# protocol itself.
#
# Example usage:
# 
#    ggputils.player.RawPlayer(('', 4001), on_start=XXX, on_play=XXX,
#                              on_stop=XXX, on_abort=XXX)
#
# ---------------------------------------------------------------------------------

class RawPlayer(WSGIServer):
    def __init__(self, address, on_start, on_play, on_stop, on_abort,
                 on_info=None, on_preview=None):
        self._handler = Handler(on_start=on_start, on_play=on_play,
                                on_stop=on_stop, on_abort=on_abort,
                                on_info=on_info, on_preview=on_preview)
        super(RawPlayer, self).__init__(address,self._handler)
        self.serve_forever()

#---------------------------------------------------------------------------------
#
# Player provides a cleaner API for a GGP player. Abdallah Saffidine
# has argued strongly that a game playing API should separate the
# updating of moves from the reasoning about move selection. This is
# also closer to the interfaces of other automated game challenges.
#
# Prototypes for the callbacks:
#
# - on_start(timeout, matchid, role, gdl, playclock)
# - on_update(actions)
# - on_select(timeout)
# - on_clear()
# - on_info() - optional
# - on_preview(timeout, gdl) - optional
#
# Note the timeout
# ---------------------------------------------------------------------------------


class SimplePlayer(object):
    def __init__(self, address, on_start, on_update, on_select,
                 on_clear, on_info=None, on_preview=None):
        self._on_update=on_update
        self._on_select=on_select
        self._on_clear=on_clear
        self._on_preview=on_preview
        self._player = RawPlayer(address,
                                on_start=on_start,
                                on_play=self._on_ggp_play,
                                on_stop=self._on_ggp_stop,
                                on_abort=on_clear,
                                on_info=on_info,
                                on_preview=on_preview)

    #-----------------------------------------------------------------------------
    # The callbacks for the GGP comms
    #-----------------------------------------------------------------------------        
    def _on_ggp_play(self, timeout, actions):
        # The Handler should guarantee that the match ids match.    
        if actions != {}: self._on_update(actions)
        return self._on_select(timeout)

    def _on_ggp_stop(self, timeout, actions):
        if actions != {}: self._on_update(actions)
        self._on_clear()
