#---------------------------------------------------------------------------------
# (c) 2013-2014 David Rajaratnam
#---------------------------------------------------------------------------------

#--------------------------------------------------------------------------
# Useful small utility functions and classes that are too small to be
# defined on their own but useful across may modules.
#--------------------------------------------------------------------------

import time
import re
import copy

#--------------------------------------------------------------------------
#
# - BracesMessage is take from:
#   http://mail.python.org/pipermail/python-ideas/2011-February/009144.html
#
# This makes logging more efficient when used with the python logging functions
# because it won't be evaluated unless the log level is triggered. For example:
#
#     g_logger.warning(_fmt("Some error code {0}", errorcode))
#
# will only generate the output string when the logger tries to print it.
#
#--------------------------------------------------------------------------

class BraceMessage(object):
    def __init__(self, fmt, *args, **kwargs):
        self._fmt = fmt
        self._args = args
        self._kwargs = kwargs

    def __str__(self):
        return self._fmt.format(*self._args, **self._kwargs)

_fmt = BraceMessage

#--------------------------------------------------------------------------------------
# Encapsulates a timeout from some timepoint (in seconds). It takes an
# initial timestamp and a duration which is the time given for a
# response.  This matches the GGP protocol where the player is given a
# start or playclock in which to respond.
#
# The timeout can be extended or retracted. retraction is useful for
# providing a buffer in which to respond.
# --------------------------------------------------------------------------------------

class Timeout(object):
    def __init__(self, timestamp, response_duration):
        self._decision_time = timestamp + response_duration

    def has_expired(self):
        return float(self._decision_time - time.time()) <= 0.0

    def remaining(self):
        remainder = float(self._decision_time - time.time())
        if remainder < 0.0: return 0.0
        return remainder

    def extend(self, duration):
        self._decision_time += duration

    def reduce(self, duration):
        self._decision_time -= duration

    def clone(self):
        return copy.copy(self)

#--------------------------------------------------------------------------------------
# Generate an integer timeout (in seconds) from some timepoint. It requires a
# decision_time which is the point in time when the decision must be made.
# The number returned is always >= 0, so that if it happens that decision was
# required 10 seconds ago then the best we can do is a 0 timeout.
#
# _timeout(decision_time, now=time.time())
#
# If called with only one parameter then it returns the number of seconds from the
# now till the decision_time.
#
# NOTE: return value is an integer since this is what is required for the current
# qp protocol for talking to subplayers. This may change in the future.
#
#---------------------------------------------------------------------------------------
#def timeout(decision_time, now=-1.0):
#    if now <= 0.0: now = time.time()
#    timeout = float(decision_time - now)
#    if timeout < 0.0: timeout=0.0
#    return timeout



#--------------------------------------------------------------------------------------
# Handle simple s-expressions.
# Adapted from: http://rosettacode.org/wiki/S-Expressions#Python
#--------------------------------------------------------------------------------------

#--------------------------------------------------------------------------------------
# Parse simple s-expressions into lists of lists.
# Its not a full s-expression parser. Doesn't support escaping and does handle
# quoted strings. So ("quoted string") returns the list ['"quoted', 'string"'].
# Also everything is treated as text as we don't care about distinguishing numbers
# integers or floats from text.
#--------------------------------------------------------------------------------------

_term_regex = r'''(?mx)
    \s*(?:
        (?P<brackl>\()|
        (?P<brackr>\))|
        (?P<s>[^(^)\s]+)
       )'''

_dbg=False
def parse_simple_sexp(sexp):
    stack = []
    out = []

    if re.match(r'^\s*$', sexp): raise ValueError("An empty string is not a valid s-expression")
    if _dbg: print("%-6s %-14s %-44s %-s" % tuple("term value out stack".split()))
    for termtypes in re.finditer(_term_regex, sexp):
        term, value = [(t,v) for t,v in termtypes.groupdict().items() if v][0]
        if _dbg: print("%-7s %-14s %-44r %-r" % (term, value, out, stack))
        if   term == 'brackl':
            stack.append(out)
            out = []
        elif term == 'brackr':
            if not stack:
                raise ValueError("Bad bracket nesting in s-expression: \"{0}\"".format(sexp))
            tmpout, out = out, stack.pop(-1)
            out.append(tmpout)
        elif term == 's':
            out.append(value)
        else:
            raise NotImplementedError("Error: %r" % (term, value))

    # Make sure the stack is now empty
    if stack:
        raise ValueError("Bad bracket nesting in s-expression: \"{0}\"".format(sexp))
    return out[0]

#--------------------------------------------------------------------------------------
# Convert an sexpression to a string.
#--------------------------------------------------------------------------------------
def exp_to_sexp(exp):
    out = ''
    if type(exp) == type([]):
        out += '(' + ' '.join(exp_to_sexp(x) for x in exp) + ')'
    elif type(exp) == type('') and re.search(r'[\s()]', exp):
        raise ValueError(("Cannot be converted to an s-expression as a "
                          "text element contains spaces or '(' or ')'"))
    else:
        out += '{0}'.format(exp)
    return out


#-----------------------------------------------------------------------
# Split an actionstr into its component actions.
#-----------------------------------------------------------------------
def parse_actions_sexp(sexp):
    exp = parse_simple_sexp(sexp)
    if type(exp) == type(''):
        if re.match(r'^NIL$', exp, re.IGNORECASE): return []
        return [exp_to_sexp(exp)]
#        raise ValueError("{0} is not a sequence of actions".format(sexp))
    actions = []
    for aexp in exp:
        actions.append(exp_to_sexp(aexp))
    return actions

def actions_to_sexp(actions):
    if hasattr(actions, '__iter__'):
        if not actions: return "NIL"
        if len(actions) == 1: return "{0}".format(actions[0])
        return "({0})".format(" ".join(actions))
    raise ValueError("{0} is not a sequence of actions".format(actions))


#-----------------------------------------------------------------------
# Split action values sexpression into a list of action value pairs.
# eg., "((NOOP 50) ((MARK 3 4) 60))" => [("NOOP", 50), ("(MARK 3 4)", 60)]
#
# Note: An empty list of action values is legal: "()" => []
#-----------------------------------------------------------------------
def parse_actionvalues_sexp(sexp):
    exp = parse_simple_sexp(sexp)
    if type(exp) == type(''):
        raise ValueError("Invalid action values list")
    avs = []
    for av_exp in exp:
        if type(av_exp) == type(''):
            raise ValueError("Invalid action value pair")
        (aexp, vexp) = av_exp
        if type(vexp) != type(''):
            raise ValueError("Invalid value")
        avs.append((exp_to_sexp(aexp), int(vexp)))
    return avs

def actionvalues_to_sexp(avs):
    if not hasattr(avs, '__iter__'):
        raise ValueError("{0} is not a sequence of action value pairs".format(avs))
    avstrs = []
    for (action, value) in avs:
        avstrs.append("({0} {1})".format(action, value))
    return "({0})".format(" ".join(avstrs))
