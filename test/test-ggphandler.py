#!/usr/bin/env python

import unittest
from wsgiref.util import setup_testing_defaults
import StringIO
import string
import logging

from ggputils.player.ggp_http_handler import Handler

#---------------------------------------------------------------------------------
# Global variables
#---------------------------------------------------------------------------------
g_logger = logging.getLogger()


class Player(object):
    def __init__(self):
        pass
    
    def on_start(self, timeout, matchid, role, gdl, playclock):
        pass

    def on_play(self, timeout, actions):
        pass

    def on_stop(self, timeout, actions):
        pass

    def on_abort(self):
        pass

    def on_info(self):
        pass

    def on_preview(self, timeout, gdl):
        pass
    

#---------------------------------------------------------------------------------
# Useful helper functions
#---------------------------------------------------------------------------------
def make_handler(on_start=None, on_play=None, on_stop=None,
                 on_abort=None, on_info=None, on_preview=None):
    return Handler(on_start=on_start, on_play=on_play, on_stop=on_stop,
                   on_abort=on_abort, on_info=on_info,
                   on_preview=on_preview, test_mode=True)
    
def make_environ(data):
    environ = { 'REQUEST_METHOD': 'POST',
                'wsgi.input': StringIO.StringIO(data),
                'CONTENT_LENGTH' : str(len(data)) }    
    setup_testing_defaults(environ)
    return environ

#---------------------------------------------------------------------------------
# Unit test class
#---------------------------------------------------------------------------------
class GGPHandlerTest(unittest.TestCase):       

    #------------------------------------------
    # Useful start_response callbacks
    #------------------------------------------
    def start_response_status_ok(self, status, headers):
        self.assertEqual(status, "200 OK")

    def start_response_status_not_ok(self, status, headers):
        self.assertNotEqual(status, "200 OK")
#        print "Status: {0}".format(status)

    def start_response_print(self, status, headers):
        print "Status: {0}, headers: {1}".format(status,headers)

    #------------------------------------------
    # Test non-GGP message
    #------------------------------------------
    def test_non_ggp_message(self):
        handler = make_handler()

        # Test unknown post message
        environ = make_environ("(BLAH)")
        body = handler(environ, self.start_response_status_not_ok)
        self.assertFalse(body)

    #------------------------------------------
    # Test GGP START message
    #------------------------------------------
    def test_start_message(self):

        # A start message callback
        def on_start(timeout, matchid, role, gdl, playclock):
            self.assertEqual(role, "robot")
            self.assertEqual(gdl, "(role robot) (other gdl)")
            self.assertEqual(playclock, int(5))
        
        handler = make_handler(on_start=on_start)

        # A bad start message
        environ = make_environ("(START incomplete)")
        body = handler(environ, self.start_response_status_not_ok)
        self.assertFalse(body)

        # A good start message (note: using upper case
        environ = make_environ("(START test3_#s robot ((role robot) (other gdl)) 10 5)")
        body = handler(environ, self.start_response_status_ok)
        self.assertEqual(body, "READY")

    #------------------------------------------
    # Test GGP ABORT message
    #------------------------------------------
    def test_abort_message(self):

        # Note python 2.X doesn't do well with nested functions and
        # variable scoping, so use a class instead.
        class TMP(object):
            def __init__(self):
                self._called = False

            # An abort message callback
            def on_abort(self):
                self._called = True

        # A dummy start message callback 
        def on_start(timeout, matchid, role, gdl, playclock):
            pass

        tmp = TMP()
        handler = make_handler(on_start=on_start, on_abort=tmp.on_abort)

        # First test an abort message called when not within a game
        # This should return an error
        environ = make_environ("(ABORT someid)")
        body = handler(environ, self.start_response_status_not_ok)
        self.assertFalse(body)

        # Now test after a START message
        environ = make_environ("(START testmatch1 robot ((role robot) (other gdl)) 10 5)")
        body = handler(environ, self.start_response_status_ok)

        environ = make_environ("(ABORT testmatch1)")
        body = handler(environ, self.start_response_status_ok)
        self.assertTrue(tmp._called)
                
    #------------------------------------------
    # Test GGP PLAY message
    #------------------------------------------
    def test_play_message(self):

        # Note python 2.X doesn't do well with nested functions and
        # variable scoping, so use a class instead.
        class TMP(object):
            def __init__(self):
                self._called = False
                self._timeout = None
                self._actions = None

            # An abort message callback
            def on_play(self, timeout, actions):
                self._timeout = timeout
                self._actions = actions
                self._called = True

        # A dummy start message callback 
        def on_start(timeout, matchid, role, gdl, playclock):
            pass

        tmp = TMP()
        handler = make_handler(on_start=on_start, on_play=tmp.on_play)

        # Test after a START message
        environ = make_environ("(START testmatch1 robot ((role robot) (other gdl)) 10 5)")
        body = handler(environ, self.start_response_status_ok)

        # Test the NIL message
        environ = make_environ("(PLAY testmatch1 NIL)")
        body = handler(environ, self.start_response_status_ok)
        self.assertTrue(tmp._called)

        # Test the NIL message
        tmp._called = False
        environ = make_environ("(PLAY testmatch1 ((a move )))")
        body = handler(environ, self.start_response_status_ok)
        self.assertTrue(tmp._called)
 
    #------------------------------------------
    # Test GGP STOP message
    #------------------------------------------
    def test_stop_message(self):

        # Note python 2.X doesn't do well with nested functions and
        # variable scoping, so use a class instead.
        class TMP(object):
            def __init__(self):
                self._called = False
                self._timeout = None
                self._actions = None

            # An abort message callback
            def on_stop(self, timeout, actions):
                self._timeout = timeout
                self._actions = actions
                self._called = True

        # A dummy start message callback 
        def on_start(timeout, matchid, role, gdl, playclock):
            pass

        tmp = TMP()
        handler = make_handler(on_start=on_start, on_stop=tmp.on_stop)

        # Test after a START message
        environ = make_environ("(START testmatch1 robot ((role robot) (other gdl)) 10 5)")
        body = handler(environ, self.start_response_status_ok)

        # Not sure if a STOP message with NIL actions is allowed. For the moment say no.
        environ = make_environ("(STOP testmatch1 NIL)")
        body = handler(environ, self.start_response_status_not_ok)

        # Test the NIL message
        tmp._called = False
        environ = make_environ("(STOP testmatch1 ((a move )))")
        body = handler(environ, self.start_response_status_ok)
        self.assertTrue(tmp._called)
                   

    #------------------------------------------
    # Test GGP INFO message
    #------------------------------------------
    def test_default_info_message(self):
        handler = make_handler()

        # Test upper case 
        environ = make_environ("(INFO)")
        body = handler(environ, self.start_response_status_ok)
        self.assertEqual(body, "AVAILABLE")

        # Test lower case 
        environ = make_environ("(info)")
        body = handler(environ, self.start_response_status_ok)
        self.assertEqual(body, "available")

    #------------------------------------------
    # Test GGP PREVIEW message
    #------------------------------------------
    def test_preview_message(self):
        environ = make_environ("(PREVIEW ((role robot) (other gdl)) 10)")
    
        # First test the default behaviour
        handler = make_handler()
        body = handler(environ, self.start_response_status_ok)
        self.assertEqual(body, "DONE")

        # Now test that a preview callback works
        def on_preview(timeout, gdl):
            self.assertFalse(timeout.has_expired())
            self.assertEqual(gdl, "(role robot) (other gdl)")
        
        environ = make_environ("(PREVIEW ((role robot) (other gdl)) 10)")
        handler = make_handler(on_preview=on_preview)
        body = handler(environ, self.start_response_status_ok)
        self.assertEqual(body, "DONE")

        
#-----------------------------
# main
#-----------------------------
        
def main():
    g_logger.setLevel(logging.DEBUG)
    g_logger.addHandler(logging.StreamHandler())
    
    unittest.main()
    
if __name__ == '__main__':
    main()

