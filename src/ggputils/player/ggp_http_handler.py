#-------------------------------------------------------------------------
#
# Implementation of an HTTP Handler for a WSGI-based GGP player.  The
# class adheres to the WSGI specification so is a functor that can be
# called with the parameters: environ, start_response.
#
# This handler can deal with both the original GGP protocol and the
# GGP-II protocol for imperfect information games. However (and
# unfortunately) there is no unambiguous way to tell the difference
# between the two protocols. Currently, the only way is to tell the
# difference is to look at the GDL and if it has the "random" role and
# sees predicate then assume it is GDL-II. However, even this is not
# perfect since (I would think) it would still be possible to use a
# GDL-II game description but run a GDL-I game.
#
# The current hack is to have a flag that determines which the GGP
# protocol version. This flag (defaults to GDL-I) must be set at
# creation of the Handler object.
#
# On instantiation the Handler requires a number of callback
# functions. The functions correspond directly to the the GGP
# communications protocol. The prototypes for the callbacks are:
#
# - on_start(timeout, matchid, role, gdl, playclock)
# - on_play(timeout, actions)    - GDL-I
# - on_stop(timeout, actions)    - GDL-I
# - on_play2(timeout, sees)      - GDL-II
# - on_stop2(timeout, sees)      - GDL-II
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
# - The actions in the on_play and on_stop callbacks are a python
#   dictionary of roles to actions. It can be empty, corresponding to
#   the "NIL" actions string that happens with the first PLAY message
#   of a game.
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
#-------------------------------------------------------------------------

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

#-------------------------------------------------------------------------
# Handler does the hard work
#-------------------------------------------------------------------------

class Handler(object):
    #-------------------------------------
    # The GGP gdl protocol versions
    #-------------------------------------
    GGP1 = 1         # GDL I protocol
    GGP2 = 2         # GDL-II protocol

    #-------------------------------------
    # compiled regex for the GGP messages
    #-------------------------------------
    SEARCH_START = r'^\s*\(\s*START'
    SEARCH_PLAY = r'\s*\(\s*PLAY'
    SEARCH_STOP = r'\s*\(\s*STOP'
    SEARCH_INFO = r'\s*\(\s*INFO'
    SEARCH_ABORT = r'\s*\(\s*ABORT'
    SEARCH_PREVIEW = r'\s*\(\s*PREVIEW'
    MATCH_START = r'^\s*\(\s*START\s+([^\s]+)\s+([^\s]+)\s+\((.*)\)\s+(\d+)\s+(\d+)\s*\)\s*$'
    MATCH_PLAY = r'^\s*\(\s*PLAY\s+([^\s]+)\s+(.*)\s*\)\s*$'
    MATCH_STOP = r'^\s*\(\s*STOP\s+([^\s]+)\s+(.*)\s*\)\s*$'
    MATCH_INFO = r'\s*\(\s*INFO\s*\)\s*$'
    MATCH_ABORT = r'\s*\(\s*ABORT\s+([^\s]+)\s*\)\s*$'
    MATCH_PREVIEW = r'\s*\(\s*PREVIEW\s+\((.*)\)\s+(\d+)\s*\)\s*$'
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
    def __init__(self, on_start=None,
                 on_play=None, on_stop=None,
                 on_play2=None, on_stop2=None,
                 on_abort=None,
                 on_info=None, on_preview=None,
                 protocol_version=None, test_mode=False):

        if not protocol_version: protocol_version=Handler.GGP1
        assert protocol_version in [Handler.GGP1, Handler.GGP2],\
            "Unrecognised GDL protocol version {0}".format(protocol_version)

        # Test mode is useful for unit testing individual callback functions
        if not test_mode:
            if (protocol_version == Handler.GGP1) and \
               not (on_start and on_play and on_stop and on_abort):
                raise ValueError(("Must have valid callbacks for: on_start, "
                                  "on_play, on_stop, on_abort"))
            elif (protocol_version == Handler.GGP2) and \
               not (on_start and on_play2 and on_stop2 and on_abort):
                raise ValueError(("Must have valid callbacks for: on_start, "
                                  "on_play2, on_stop2, on_abort"))

        g_logger.info("Running player for GDL version: {0}".format(protocol_version))

        self._protocol_version = protocol_version
        self._on_START = on_start
        self._on_PLAY = on_play
        self._on_STOP = on_stop
        self._on_PLAY2 = on_play2
        self._on_STOP2 = on_stop2
        self._on_ABORT = on_abort
        self._on_INFO = on_info
        self._on_PREVIEW = on_preview

        self._all_conn_queue = Queue()
        self._good_conn_queue = Queue()

        self._uppercase = True

        # Game player state related variables
        self._gdl2_turn = 0
        self._matchid = None
        self._playclock = None
        self._startclock = None
        self._roles = []

    #----------------------------------------------------------------------------
    # Call that adheres to the WSGI application specification. Handles
    # all connections in order and tries to weed out bad
    # ones. Maintains two queues: all-connections and
    # good-connections. Every connection is added to the
    # all-connections queue. It then decides if it is a bad connection
    # in which case it doesn't go on to the good-connection queue. This
    # is done immediately on the handler being called. If it is a good
    # connection then it is placed on the good-connection queue to
    # ensure that only one message is handled at a time and that it is
    # handled in the correct order.
    #
    # This two queue mechanism ensures that bad message can be quickly
    # filtered out while maintaining a clean orderly queue for
    # legitimate messages. Of course, in normal operation we would
    # expect the good queue to only ever contain the current message
    # being handled, but it does mean that even if the player gets
    # behind, the messages will be processed in an orderly way and
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
    # _app_bad is called when the handle is for bad a connection.
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
            # Return an error
            response_headers = _get_response_headers(environ, "")
            start_response('400 Invalid GGP message', response_headers)
            return ""

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
    #
    # BUG NOTE 20141224: I need to revisit this at some point. Firstly, should weed out
    # non-GGP messages here. Also should probably allow mismatched matchids here but
    # ensure that it checks later to make sure there are no problems.
    #---------------------------------------------------------------------------------
    def _is_good_connection(self, environ, timestamp, message):

        # Preview messages are always ok
        if Handler.re_s_PREVIEW.match(message): return True

        # A START message when we are in a game could mean a number of things:
        # 1) either a message has been lost (somehow),
        # 2) The game master is not operating correctly (eg. crashed and restarted),
        # 3) The player (i.e., the callback functions) have not been responded
        #    within the timeout and there may be an end/abort message that is in
        #    the queue waiting to be handled.
        # Whatever the case the best we can do is log an error and let the
        # message through.
        if Handler.re_s_START.match(message):
            if self._matchid is not None:
                g_logger.error(("A new START message has been received before the"
                                "match {0} has ended.").format(self._matchid))
            return True

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

        # It is good
        return True

    #---------------------------------------------------------------------------------
    # Internal functions to format the response message based on the
    # game master using upper or lower case. Don't think it matters
    # for the Dresden game master but does for Stanford.
    # ---------------------------------------------------------------------------------
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

    #----------------------------------------------------------------------
    # handle GGP START message
    #----------------------------------------------------------------------
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

        if self._protocol_version == Handler.GGP2: self._gdl2_turn = 0

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

    #----------------------------------------------------------------------
    # handle GGP PLAY message
    #----------------------------------------------------------------------
    def handle_PLAY(self, timestamp, message):
        match = Handler.re_m_PLAY.match(message)
        if not match:
            raise HTTPErrorResponse(400, "Malformed PLAY message {0}".format(message))
        matchid = match.group(1)
        if self._matchid != matchid:
            self._on_ABORT()
            self._matchid = None
            raise HTTPErrorResponse(400, ("PLAY message has wrong matchid: "
                                          "{0} {1}").format(matchid, self._matchid))

        tmpstr = match.group(2)
        action=None
        actionstr=""

        # GGP 1 and GGP 2 are handled differently
        if self._protocol_version == Handler.GGP1:
            # GDL-I: a list of actions
            if not re.match(r'^\s*\(.*\)\s*$', tmpstr) and \
               not re.match(r'^\s*NIL\s*$', tmpstr, re.I):
                raise HTTPErrorResponse(400, "Malformed PLAY message {0}".format(message))
            actions = parse_actions_sexp(tmpstr)
            if len(actions) != 0 and len(actions) != len(self._roles):
                raise HTTPErrorResponse(400, "Malformed PLAY message {0}".format(message))

            timeout = Timeout(timestamp, self._playclock)
            action = self._on_PLAY(timeout.clone(), dict(zip(self._roles, actions)))
        else:
            # GDL-II: a list of observations
            (turn, action, observations) = _parse_gdl2_playstop_component("PLAY", message, tmpstr)
            timeout = Timeout(timestamp, self._playclock)
            action = self._on_PLAY2(timeout.clone(), action, observations)

            if turn != self._gdl2_turn:
                raise HTTPErrorResponse(400, ("PLAY message has wrong turn number: "
                                          "{0} {1}").format(turn, self._gdl2_turn))
            self._gdl2_turn += 1

        # Handle the return action
        actionstr = "{0}".format(action)

        # Make sure the action is a valid s-expression
        try:
            exp = parse_simple_sexp(actionstr.strip())
        except:
            actionstr = "({0})".format(actionstr)
            g_logger.critical(_fmt(("Invalid action '{0}'. Will try to recover to "
                                    "and send {1}"), action, actionstr))

        remaining = timeout.remaining()
        if remaining <= 0:
            g_logger.error(_fmt("PLAY messsage handler late response by {0}s", remaining))
        else:
            g_logger.info(_fmt("PLAY response with {0}s remaining: {1}", remaining, action))

        # Returns the action as the response
        return actionstr

    #----------------------------------------------------------------------
    # handle GDL STOP message
    #----------------------------------------------------------------------
    def handle_STOP(self, timestamp, message):
        match = Handler.re_m_STOP.match(message)
        if not match:
            raise HTTPErrorResponse(400, "Malformed STOP message {0}".format(message))

        # Make sure the matchid is correct
        matchid = match.group(1)
        if self._matchid != matchid:
            self._on_ABORT()
            self._matchid = None
            raise HTTPErrorResponse(400, ("PLAY message has wrong matchid: "
                                          "{0} {1}").format(matchid, self._matchid))

        # Extract the actions and match to the correct roles
        tmpstr = match.group(2)

        # GGP 1 and GGP 2 are handled differently
        if self._protocol_version == Handler.GGP1:
            # GDL-I: a list of actions
            actions = parse_actions_sexp(tmpstr)
            if len(actions) != len(self._roles):
                raise HTTPErrorResponse(400, "Malformed STOP message {0}".format(message))
            timeout = Timeout(timestamp, self._playclock)
            self._on_STOP(timeout.clone(), dict(zip(self._roles, actions)))
        else:
            # GDL-II: a list of observations
            (turn, action, observations) = _parse_gdl2_playstop_component("STOP", message, tmpstr)
            if turn != self._gdl2_turn:
                raise HTTPErrorResponse(400, ("STOP message has wrong turn number: "
                                          "{0} {1}").format(turn, self._gdl2_turn))
            self._gdl2_turn += 1

            timeout = Timeout(timestamp, self._playclock)
            self._on_STOP2(timeout.clone(), action, observations)

        remaining = timeout.remaining()
        if remaining <= 0:
            g_logger.error(_fmt("STOP messsage handler late response by {0}s", remaining))
        else:
            g_logger.debug(_fmt("STOP response with {0}s remaining", remaining))

        # Now return the DONE response
        return self._response("DONE")

    #----------------------------------------------------------------------
    # handle GGP INFO message
    #----------------------------------------------------------------------
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
        response = self._on_INFO()
        if not response:
            raise ValueError("on_info() callback returned an empty value")
        return self._response(self._on_INFO())

    #----------------------------------------------------------------------
    # handle GGP ABORT message
    #----------------------------------------------------------------------
    def handle_ABORT(self, timestamp, message):
        self._set_case(message, "ABORT")
        match = Handler.re_m_ABORT.match(message)
        if not match:
            raise HTTPErrorResponse(400, "Malformed ABORT message {0}".format(message))
        matchid = match.group(1)
        if self._matchid != matchid:
            self._on_ABORT()
            self._matchid = None
            raise HTTPErrorResponse(400, ("ABORT message has wrong matchid: "
                                          "{0} {1}").format(matchid, self._matchid))

        self._matchid = None
        self._on_ABORT()

        # Stanford test website doesn't match the protocol description at:
        # http://games.stanford.edu/index.php/communication-protocol
        # Test website expects "ABORTED" while description states "DONE"
        return self._response("ABORTED")

    #----------------------------------------------------------------------
    # handle GGP PREVIEW message
    #----------------------------------------------------------------------
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
#        newenv.append(('Access-Control-Allow-Method', 'POST, GET, OPTIONS'))
        newenv.append(('Access-Control-Allow-Method', 'POST'))
        newenv.append(('Allow-Control-Allow-Headers', 'Content-Type'))
        newenv.append(('Access-Control-Allow-Age', str(86400)))
    except:
        pass
    return newenv


#---------------------------------------------------------------------------------
# _get_http_post(environ)
# Checks that it is a valid http post message and returns the content of the message.
# NOTE: should be call only once because the 'wsgi.input' object is a stream object
#       so will be empty once it has been read.
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
# parse part of a GDL-II play/stop message consisting of:
#    "<turn> <lastmove> <observations>"
# Returns a triple of these elements.
# ---------------------------------------------------------------------------------

def _parse_gdl2_playstop_component(mtype, message, component):
    error="Malformed GDL-II {0} message {1}".format(mtype, message)

    # Handle the turn part first
    match = re.match(r'^\s*(\d+)\s+(.*)\s*$', component)
    if not match: raise HTTPErrorResponse(400, error)
    turn=int(match.group(1))
    tmpstr=match.group(2)

    # Parse the remaining <lastmove> <observations> as an sexpression
    exp=parse_simple_sexp("({0})".format(tmpstr))
    if type(exp) == type(''): raise HTTPErrorResponse(400, error)
    if len(exp) != 2: raise HTTPErrorResponse(400, error)
    lastaction = exp_to_sexp(exp[0])
    if lastaction == "NIL": lastaction=None
    if turn == 0 and lastaction: raise HTTPErrorResponse(400, error)
    if type(exp[1]) == type(''):
        if exp[1] != "NIL": raise HTTPErrorResponse(400, error)
        return (turn, lastaction, [])

    observations = []
    for oexp in exp[1]:
        observations.append(exp_to_sexp(oexp))
    return (turn, lastaction, observations)

#---------------------------------------------------------------------------------
# Unescape html the "&lt;" "&gt;" "&amp;"
# _unescape_html(string)
#---------------------------------------------------------------------------------

def _unescape(s):
    s = s.replace("&lt;", "<")
    s = s.replace("&gt;", ">")
    s = s.replace("&amp;", "&") # must be last
    return s
