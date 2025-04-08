# encoding=utf-8
from __future__ import unicode_literals
import sys
import locale
import gettext
import yaml
import os
import psutil
import pkg_resources
import logging

logger = logging.getLogger(__name__)

STATE_NORMAL = 'normal'
STATE_ENDING = 'ending'
STATE_CLOSING = 'closing'

def i18n(*args):
    """Returns a string that substs Module:UKB's getMessage function on-wiki"""
    if len(args) == 0:
        raise ValueError('At least one argument (message key) must be given')
    return '{{subst:#invoke:UKB|getMessage|%s}}' % '|'.join(str(arg) for arg in args)

def fetch_parsed_i18n(*args, site=None):
    """
	Fetch and return the contents of an i18n message. This should only used in
	two cases:
	 - 1. The string will not be used on-wiki
	 - 2. The string will be used in edit summaries
	For all cases where text is output to wiki, the i18n() function should be used instead.
	"""
    if len(args) == 0:
        raise ValueError('At least one argument (message key) must be given')

    return site.api('parse', text=i18n(args), pst=1, onlypst=1)['parse']['text']['*']

logfile = sys.stdout
def log(msg, newline = True):
    if newline:
        msg = msg + '\n'
    logfile.write(msg.encode('utf-8'))
    logfile.flush()

process = psutil.Process(os.getpid())

def get_mem_usage():
    """ Returns memory usage in MBs """
    return process.memory_info().rss / 1024.**2

class InvalidContestPage(Exception):
    """Raised when wikitext input is not on the expected form, so we don't find what we're looking for"""

    def __init__(self, msg):
        self.msg = msg

