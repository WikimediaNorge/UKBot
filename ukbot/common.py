# encoding=utf-8
from __future__ import unicode_literals
import sys
import yaml
import json
import re
import os
import psutil
import pkg_resources
import logging

logger = logging.getLogger(__name__)

STATE_NORMAL = 'normal'
STATE_ENDING = 'ending'
STATE_CLOSING = 'closing'

class Localization:
    class __Localization:
        def __init__(self):
            self.messages = lambda x: x
            self.site = lambda x: x

        def init(self, homesite):
            messages = homesite.api(
                'parse',
                text='{{subst:#invoke:UKB/sandkasse|getAllI18n}}', # FIXME: De-sandbox
                pst=1,
                onlypst=1
            )['parse']['text']['*']
            messages = json.loads(messages)

            self.messages = messages
            self.site = homesite

    instance = None
    def __init__(self):
        if not Localization.instance:
            Localization.instance = Localization.__Localization()

    def __getattr__(self, name):
        return getattr(self.instance, name)

localization = Localization()

def i18n(*args):
    """
    Returns a string from the i18n file saved in Commons.

    TODO: Make more resilient in case key is not present in the file (and other things?)
    """
    logger.debug('Arguments to i18n(): %s' % '|'.join([str(x) for x in args]))
    if len(args) == 0:
        raise ValueError('At least one argument (message key) must be given')
    if args[0] == 'bot-day':
        logger.debug('Messages json: ' + json.dumps(localization.messages))
    message = localization.messages[args[0]]
    for i in range(1, len(args)):
        message = message.replace('$' + str(i), str(args[i]))
    # Add subst: in front of some magic words, so they don't appear in wikitext
    replacements = ['PLURAL', 'GENDER', 'GRAMMAR', '#time']
    message = re.sub(re.compile('\{\{(' + '|'.join(replacements) + r'):'), '{{subst:\\1:', message)
    return message

def fetch_parsed_i18n(*args):
    """
	Fetch and return the contents of an i18n message. This should only used in
	two cases:
	 - 1. The string will not be used in a MediaWiki environment
	 - 2. The string will be used in edit summaries
	For all cases where text is output to wiki, the i18n() function should be used instead.
	"""
    logger.debug('Arguments to fetch_parsed_i18n(): %s', '|'.join([str(x) for x in args]))
    if len(args) == 0:
        raise ValueError('At least one argument (message key) must be given')
    if len(args) == 1:
        return i18n(*args)
    return localization.site.api('parse', text=i18n(*args), pst=1, onlypst=1)['parse']['text']['*']

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

