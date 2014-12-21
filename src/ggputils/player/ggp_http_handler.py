#---------------------------------------------------------------------------------
#
# Implementation of an HTTP Handler for a WSGI-based GGP player.  The
# class adheres to the WSGI specification so is a functor that can be
# called with the parameters: environ, start_response.
#
# On instantiation the Handler requires a number of callback
# functions. The functions correspond directly to the the GGP
# communications protocol. The prototypes for the callbacks are:
#
# - on_start(timeout, matchid, role, gdl, playclock)
# - on_play(timeout, actions)
# - on_stop(timeout, actions)
# - on_abort()
# - on_info()
# - on_preview(timeout, gdl)
#
# Things to note:
#
# - on_info/on_preview are optional. The Player implements sensible
#   default behaviour for the INFO messages if no callback is
#   provided. By default the Player but does nothing with the PREVIEW
#   messages.
#
# - All callbacks have a timeout object (see
#   ggputils.util.Timeout). This timeout object is based on a
#   timestamp taken as soon as the message arrives. Note: I don't know
#   enough about HTTP to be sure but from what I can tell the two GGP
#   game masters (Stanford/Tiltyard and Dresden) do not provide any
#   timestamp information in the HTTP message that is sent to the
#   players. Hence the timestamp in the timeout does NOT represent the
#   time since the message was sent but the time the handler received
#   it. This seems to be a weakness in the GGP protocol as it has no
#   mechanism for allowing for things like network lag. Hence a the
#   timeout should be reduced by a healthy margin to make sure that
#   the player responds in time.
#
# - The actions in the on_play and on_stop callbacks are a dict of
#   roles to actions. It can be empty, corresponding to the "NIL"
#   actions string that happens with the first PLAY message of a game.
#
# The handler tries to be robust in how it handles requests. It adapts
# to the variations between the Stanford game server and the Dresden
# game controller. It also tries to respond appropriately to out of
# turn messages and other spurious connections.  Finally, it tries not
# to drop valid connections and instead maintains a queue of valid
# connections which it handles sequentially. Note: of course under
# normal operations there should be only one request at a time so the
# queue should be empty.
#
#
# (c) 2014 David Rajaratnam
#---------------------------------------------------------------------------------

import time
import re
import logging
from ggputils.utils import *
from ggputils.utils import _fmt
from cgi import escape
from gevent.lock import *
from gevent.queue import *
from gevent.event import *

g_logger = logging.getLogger(__name__)

#---------------------------------------------------------------------------------
# Handler does the hard work
#---------------------------------------------------------------------------------


class Handler(object):

    #-------------------------------------
    # compiled regex for the GGP messages
    #-------------------------------------
    SEARCH_START = r'^\s*\(\s*START'
    SEARCH_PLAY = r'\s*\(\s*PLAY'
    SEARCH_STOP = r'\s*\(\s*STOP'
    SEARCH_INFO = r'\s*\(\s*INFO'
    SEARCH_ABORT = r'\s*\(\s*ABORT'
    SEARCH_PREVIEW = r'\s*\(\s*PREVIEW'
    MATCH_START = r'^\s*\(\s*START\s+([^\s]+)\s+(\w+)\s+\((.*)\)\s+(\d+)\s+(\d+)\s*\)\s*$'
    MATCH_PLAY = r'^\s*\(\s*PLAY\s+([^\s]+)\s+(.*)\s*\)\s*$'
    MATCH_STOP = r'^\s*\(\s*STOP\s+([^\s]+)\s+(.*)\s*\)\s*$'
    MATCH_INFO = r'\s*\(\s*INFO\s*\)\s*$'
    MATCH_ABORT = r'\s*\(\s*ABORT\s+([^\s]+)\s*\)\s*$'
    MATCH_PREVIEW = r'\s*\(\s*PREVIEW\s+(.*)\s+(\d+)\s*\)\s*$'
    MATCH_SPS_MATCHID = r'\s*\(\s*(START|PLAY|STOP)\s+([^\s]+)\s+.*\)\s*$'

    re_s_START = re.compile(SEARCH_START, re.IGNORECASE)
    re_s_PLAY = re.compile(SEARCH_PLAY, re.IGNORECASE)
    re_s_STOP = re.compile(SEARCH_STOP, re.IGNORECASE)
    re_s_INFO = re.compile(SEARCH_INFO, re.IGNORECASE)
    re_s_ABORT = re.compile(SEARCH_ABORT, re.IGNORECASE)
    re_s_PREVIEW = re.compile(SEARCH_PREVIEW, re.IGNORECASE)
    re_m_START = re.compile(MATCH_START, re.IGNORECASE | re.DOTALL)
    re_m_PLAY = re.compile(MATCH_PLAY, re.IGNORECASE | re.DOTALL)
    re_m_STOP = re.compile(MATCH_STOP, re.IGNORECASE | re.DOTALL)
    re_m_INFO = re.compile(MATCH_INFO, re.IGNORECASE | re.DOTALL)
    re_m_ABORT = re.compile(MATCH_ABORT, re.IGNORECASE | re.DOTALL)
    re_m_PREVIEW = re.compile(MATCH_PREVIEW, re.IGNORECASE | re.DOTALL)
    re_m_SPS_MATCHID = re.compile(MATCH_SPS_MATCHID, re.IGNORECASE | re.DOTALL)

    re_m_GDL_ROLE = re.compile("role", re.IGNORECASE)

    #---------------------------------------------------------------------------------
    # Constructor takes callbacks for the different GGP message types.
    # INFO and PREVIEW callbacks are optional with the following default behaviours:
    # - PREVIEW: does nothing except responds with "DONE"
    # - INFO: if not in a game then responds with "AVAILABLE", or "BUSY" otherwise.
    #---------------------------------------------------------------------------------
    def __init__(self, on_start, on_play, on_stop, on_abort, on_info=None, on_preview=None):
        self._on_START = on_start
        self._on_PLAY = on_play
        self._on_STOP = on_stop
        self._on_INFO = on_info
        self._on_ABORT = on_abort
        self._on_PREVIEW = on_preview

        self._all_conn_queue = Queue()
        self._good_conn_queue = Queue()

        self._uppercase = True

        # Game player state related variables
        self._matchid = None
        self._playclock = None
        self._startclock = None
        self._roles = []

    #----------------------------------------------------------------------------
    # Call that adheres to the WSGI application specification. Handles
    # all connections in order and tries to weed out bad
    # ones. Maintains two queues: all connections and good
    # connections. Every connection is added to the all connections
    # queue. It then decides if it is a bad connection in which case
    # it doesn't go on the good connection queue. If it is a good
    # connection then it is placed on the good connection queue to to
    # ensure that only one message is handled at a time and that it is
    # handled in the correct order.
    #
    # This two queue mechanism ensures that bad message can be quickly
    # filtered out while maintaining a clean orderly queue for
    # legitimate messages. Of course, in normal operation we would
    # expect the good queue to only ever contain the current message
    # being handled, but it does mean that even if the player does get
    # behind the messages will be processed in an orderly way and
    # there is the possibility of catching up.
    # ----------------------------------------------------------------------------
    def __call__(self, environ, start_response):

        # Timestamp  as early as possible
        timestamp = time.time()

        # NOTE: _get_http_post(environ) can only be called once.
        try:
#            post_message = escape(_get_http_post(environ))
            post_message = _get_http_post(environ)
        except:
            return self._app_bad(environ, start_response)

        # Handle one connection at a time in order by creating an event
        # adding it to the queue and then waiting for that event to be called.
        myevent = AsyncResult()
        self._all_conn_queue.put(myevent)

        # If I'm not the head of the all queue then wait till I'm called
        if self._all_conn_queue.peek() != myevent: myevent.wait()
        mygood = self._is_good_connection(environ, timestamp, post_message)

        # If I'm not bad then add myself to the good connection queue
        if mygood:
            myevent = AsyncResult()
            self._good_conn_queue.put(myevent)

        # remove myself from the all queue and call up the next one
        self._all_conn_queue.get()
        if not self._all_conn_queue.empty(): self._all_conn_queue.peek().set()

        # If I'm not good then the journey ends here
        if not mygood: return self._app_bad(environ, start_response)

        # If I'm not the head of the good queue then wait till I'm called
        if self._good_conn_queue.peek() != myevent: myevent.wait()

        result = self._app_normal(environ, start_response, timestamp, post_message)

        # remove myself from the good queue and call up the next one
        self._good_conn_queue.get()
        if not self._good_conn_queue.empty(): self._good_conn_queue.peek().set()

#        g_logger.debug(_fmt("Handled message in: {0}s", time.time() - timestamp))
        return result

    #---------------------------------------------------------------------------------
    # Internal functions to handle messages
    # _app_normal is for normal operation.
    # _app_bad is called when the handle for bad connections.
    #---------------------------------------------------------------------------------
    def _app_normal(self, environ, start_response, timestamp, post_message):
        try:
            response_body = self._handle_POST(timestamp, post_message)

            response_headers = _get_response_headers(environ, response_body)

            start_response('200 OK', response_headers)
            return response_body

        except HTTPErrorResponse as er:
            g_logger.info(_fmt("HTTPErrorResponse: {0}", er))
            response_headers = _get_response_headers(environ, "")
            start_response(str(er), response_headers)
            return ""
        except Exception as e:
            g_logger.error(_fmt("Unknown Exception: {0}", e))
            response_headers = _get_response_headers(environ, "")
            start_response('500 Internal Server Error', response_headers)
            raise
            return ""

    def _app_bad(self, environ, start_response):
        try:
            # Simply return BUSY
            if self._uppercase: response_body = "BUSY"
            response_body = "busy"

            response_headers = _get_response_headers(environ, response_body)
            start_response('200 OK', response_headers)
            return response_body

        except Exception as e:
            g_logger.error(_fmt("Unknown Exception: {0}", e))
            response_headers = _get_response_headers(environ, "")
            start_response('500 Internal Server Error', response_headers)
            return ""

    #---------------------------------------------------------------------------------
    # Internal functions to decide if the caller is a good or bad connection:
    # - GGP is only interested in POST messages.
    # - ABORT should always be let through.
    # - If a START/PLAY/STOP message has not been responded to within its timeout.
    #   then we let the message through. Otherwise its not our fault so assume bad.
    #---------------------------------------------------------------------------------
    def _is_good_connection(self, environ, timestamp, message):

        # Preview messages are always ok
        if Handler.re_s_PREVIEW.match(message): return True

        # A START message when we're already in a game is bad
#        if Handler.re_s_START.match(message):
#            if not self._matchid is None: return False
#            else: return True

# NOTE: 20140603 - Changing behaviour of on receiving a START message
# during a running game. I have been assuming that the game controller
# is working properly so should NEVER to dodgy things like crashing
# part way through a game (or if it does then it is ok that the qp
# controller needs to be restarted). However, for debugging purposes,
# Abdallah has been simply stopping the game controller part way
# through a game. I'm not sure I want the qp controller to be tolerant
# to this because it might open us up to problems down the track. But
# in any case for the moment I'm changing this to let it work.

        if Handler.re_s_START.match(message): return True




        # Non-START game messages (those with matchids) are ok only if
        # they match the current matchid.
        match = Handler.re_m_SPS_MATCHID.match(message)
        matchid = None
        if match:
            matchid = match.group(2)
        else:
            match = Handler.re_m_ABORT.match(message)
            if match:
                matchid = match.group(1)
        if matchid:
            if matchid == self._matchid: return True
            else: return False

            # If the good connection queue is empty then pass it on
#            if self._good_conn_queue.empty(): return True

            # I guess everything else is ok
        return True

    #---------------------------------------------------------------------------------
    # Internal functions to format the response message based on the game master
    # using upper or lower case. Don't think it matters for Dresden game master but
    # does Stanford.
    #---------------------------------------------------------------------------------
    def _response(self, response):
        if self._uppercase: return response.upper()
        return response.lower()

    #---------------------------------------------------------------------------------
    # Internal functions - handle the different types of GGP messages
    #---------------------------------------------------------------------------------
    def _handle_POST(self, timestamp, message):
        logstr = message
        if len(logstr) > 40: logstr = logstr[:50] + "..."
        g_logger.info("Game Master message: {0}".format(logstr))
        if Handler.re_s_START.match(message):
            return self.handle_START(timestamp, message)
        elif Handler.re_s_PLAY.match(message):
            return self.handle_PLAY(timestamp, message)
        elif Handler.re_s_STOP.match(message):
            return self.handle_STOP(timestamp, message)
        elif Handler.re_s_INFO.match(message):
            return self.handle_INFO(timestamp, message)
        elif Handler.re_s_ABORT.match(message):
            return self.handle_ABORT(timestamp, message)
        elif Handler.re_s_PREVIEW.match(message):
            return self.handle_PREVIEW(timestamp, message)
        else:
            raise HTTPErrorResponse(400, "Invalid GGP message: {0}".format(message))

    def handle_START(self, timestamp, message):
        self._set_case(message, "START")
        match = Handler.re_m_START.match(message)
        if not match:
            raise HTTPErrorResponse(400, "Malformed START message {0}".format(message))
        self._matchid = match.group(1)
        role = match.group(2)
        gdl = match.group(3)
        self._startclock = int(match.group(4))
        self._playclock = int(match.group(5))

        # Hack: need to process the GDL to extract the order of roles as they appear
        # in the GDL file so that we can get around the brokeness of the PLAY/STOP
        # messages, which require a player to know the order of roles to match
        # to the correct actions.
        try:
            self._roles_in_correct_order(gdl)
        except Exception as e:
            g_logger.error(_fmt("GDL error. Will ignore this game: {0}", e))
            self._matchid = None
            return
        
        timeout = Timeout(timestamp, self._startclock)
        self._on_START(timeout.clone(), self._matchid, role, gdl, self._playclock)
        remaining = timeout.remaining()
        if  remaining <= 0:
            g_logger.error(_fmt("START messsage handler late response by {0}s", remaining))
        else:
            g_logger.debug(_fmt("START response with {0}s remaining", remaining))

        # Now return the READY response
        return self._response("READY")


    def handle_PLAY(self, timestamp, message):
        match = Handler.re_m_PLAY.match(message)
        if not match:
            raise HTTPErrorResponse(400, "Malformed PLAY message {0}".format(message))
        matchid = match.group(1)
        if self._matchid != matchid:
            self._on_ABORT()
            self._matchid = None
            raise HTTPErrorResponse(400, "PLAY message has wrong matchid: {0} {1}".format(matchid, self._matchid))

        actionstr = match.group(2)
        if not re.match(r'^\s*\(.*\)\s*$', actionstr) and not re.match(r'^\s*NIL\s*$', actionstr, re.I):
            raise HTTPErrorResponse(400, "Malformed PLAY message {0}".format(message))
        actions = parse_actions_sexp(actionstr)
        if len(actions) != 0 and len(actions) != len(self._roles):
            raise HTTPErrorResponse(400, "Malformed PLAY message {0}".format(message))

        timeout = Timeout(timestamp, self._playclock)
        action = self._on_PLAY(timeout.clone(), dict(zip(self._roles, actions)))
        actionstr = "{0}".format(action)

        # Make sure the action is a valid s-expression
        try:
            exp = parse_simple_sexp(actionstr.strip())
        except:
            actionstr = "({0})".format(actionstr)
            g_logger.critical(_fmt("Invalid action '{0}'. Will try to recover to and send {1}", action, actionstr))
            
        remaining = timeout.remaining()
        if remaining <= 0:
            g_logger.error(_fmt("PLAY messsage handler late response by {0}s", remaining))
        else:
            g_logger.info(_fmt("PLAY response with {0}s remaining: {1}", remaining, action))

        # Returns the action as the response
        return actionstr

    def handle_STOP(self, timestamp, message):
        match = Handler.re_m_STOP.match(message)
        if not match:
            raise HTTPErrorResponse(400, "Malformed STOP message {0}".format(message))

        # Make sure the matchid is correct
        matchid = match.group(1)
        if self._matchid != matchid:
            self._on_ABORT()
            self._matchid = None
            raise HTTPErrorResponse(400, "PLAY message has wrong matchid: {0} {1}".format(matchid, self._matchid))

        # Extract the actions and match to the correct roles
        actionstr = match.group(2)
        actions = parse_actions_sexp(actionstr)
        if len(actions) != len(self._roles):
            raise HTTPErrorResponse(400, "Malformed PLAY message {0}".format(message))

        timeout = Timeout(timestamp, self._playclock)
        self._on_STOP(timeout.clone(), dict(zip(self._roles, actions)))

        remaining = timeout.remaining()
        if remaining <= 0:
            g_logger.error(_fmt("STOP messsage handler late response by {0}s", remaining))
        else:
            g_logger.debug(_fmt("STOP response with {0}s remaining", remaining))

        # Now return the DONE response
        return self._response("DONE")

    def handle_INFO(self, timestamp, message):
        self._set_case(message, "INFO")
        match = Handler.re_m_INFO.match(message)
        if not match:
            raise HTTPErrorResponse(400, "Malformed INFO message {0}".format(message))

        # If no INFO callback provide a sensible default
        if not self._on_INFO:
            if self._matchid: return self._response("BUSY")
            return self._response("AVAILABLE")

        # Use the user-provided callback
        return self._response(self._on_INFO())

    def handle_ABORT(self, timestamp, message):
        match = Handler.re_m_ABORT.match(message)
        if not match:
            raise HTTPErrorResponse(400, "Malformed ABORT message {0}".format(message))
        matchid = match.group(1)
        if self._matchid != matchid:
            self._on_ABORT()
            self._matchid = None
            raise HTTPErrorResponse(400, "ABORT message has wrong matchid: {0} {1}".format(matchid, self._matchid))

        self._matchid = None
        self._on_ABORT()

        # Stanford test website doesn't match the protocol description at:
        # http://games.stanford.edu/index.php/communication-protocol
        # Test website expects "ABORTED" while description states "DONE"
        return self._response("ABORTED")

    def handle_PREVIEW(self, timestamp, message):
        self._set_case(message, "PREVIEW")
        match = Handler.re_m_PREVIEW.match(message)
        if not match:
            raise HTTPErrorResponse(400, "Malformed PREVIEW message {0}".format(message))
        gdl = match.group(1)
        previewclock = int(match.group(2))
        timeout = Timeout(timestamp, previewclock)
        if self._on_PREVIEW: self._on_PREVIEW(timeout, gdl)
        return self._response("DONE")


    #---------------------------------------------------------------------------------
    # Internal functions - work out the case for talking to the game server
    #---------------------------------------------------------------------------------
    def _set_case(self, message, command="START"):
        uc = r'^\s*\(\s*{0}'.format(command.upper())
        lc = r'^\s*\(\s*{0}'.format(command.lower())

        if re.match(uc, message): self._uppercase = True
        elif re.match(lc, message): self._uppercase = False
        else:
            g_logger.warning(("Cannot determine case used by game server, "
                              "so defaulting to uppercase responses"))
            self._uppercase = True


    #---------------------------------------------------------------------------------
    # Maintain a list of roles in the same order as it appears in the GDL.
    # _roles_in_correct_order(self, gdl)
    #---------------------------------------------------------------------------------
    def _roles_in_correct_order(self, gdl):
        self._roles = []
        exp = parse_simple_sexp("({0})".format(gdl))
        for pexp in exp:
            if type(pexp) == type([]) and len(pexp) == 2:
                if self.re_m_GDL_ROLE.match(pexp[0]):
                    self._roles.append(pexp[1])
        if not self._roles: raise ValueError("Invalid GDL has no roles")


#---------------------------------------------------------------------------------
# User callable functions
#---------------------------------------------------------------------------------

#---------------------------------------------------------------------------------
# Internal support functions and classes
#---------------------------------------------------------------------------------

class HTTPErrorResponse(Exception):
    def __init__(self, status, message):
        Exception.__init__(self, message)
        self.status = status
        self.message = message
    def __str__(self):
        return "{0} {1}".format(self.status, self.message)

#---------------------------------------------------------------------------------
# _get_response_headers(environ_dict, response_body)
# Returns a sensible reponse header. Input is the original evironment
# dictionary and the response_body (used for calculating the context-length).
# Output a list of tuples of (variable, value) pairs.
#---------------------------------------------------------------------------------
def _get_response_headers(environ, response_body):
    newenv = []
    try:
        # Adjust the content type header to match the game controller
        if 'CONTENT_TYPE' in environ:
            newenv.append(('Content-Type', environ.get('CONTENT_TYPE')))
        else:
            newenv.append(('Content-Type', 'text/acl'))

        # Now the other headers
        newenv.append(('Content-Length', str(len(response_body))))
        newenv.append(('Access-Control-Allow-Origin', '*'))
        newenv.append(('Access-Control-Allow-Method', 'POST, GET, OPTIONS'))
        newenv.append(('Allow-Control-Allow-Headers', 'Content-Type'))
        newenv.append(('Access-Control-Allow-Age', str(86400)))
    except:
        pass
    return newenv


#---------------------------------------------------------------------------------
# _get_http_post(environ)
# Checks that it is a valid http post message and returns the context of the message.
#---------------------------------------------------------------------------------
def _get_http_post(environ):
    try:
        if environ.get('REQUEST_METHOD') != "POST":
            raise HTTPErrorResponse(405, 'Non-POST method not supported')
        request_body_size = int(environ.get('CONTENT_LENGTH'))
        if request_body_size <= 5:
            raise HTTPErrorResponse(400, 'Message content too short to be meaningful')
        return environ['wsgi.input'].read(request_body_size)
    except HTTPErrorResponse:
        raise
    except Exception as e:
        g_logger.warning(_fmt("HTTP POST exception: {0}", e))
        raise HTTPErrorResponse(400, 'Invalid content')


#---------------------------------------------------------------------------------
# Unescape html the "&lt;" "&gt;" "&amp;"
# _unescape_html(string)
#---------------------------------------------------------------------------------

def _unescape(s):
    s = s.replace("&lt;", "<")
    s = s.replace("&gt;", ">")
    s = s.replace("&amp;", "&") # must be last
    return s
