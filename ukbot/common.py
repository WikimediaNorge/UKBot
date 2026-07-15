# encoding=utf-8
from __future__ import unicode_literals
import sys
import locale
import gettext
import os
import psutil
import logging

logger = logging.getLogger(__name__)

# LOCALE_PATH = pkg_resources.resource_filename('ukbot', 'locale/')
LOCALE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "locale")

logger.info('Locale path: %s', LOCALE_PATH)

STATE_NORMAL = 'normal'
STATE_ENDING = 'ending'
STATE_CLOSING = 'closing'


# Singleton
class Localization:
    class __Localization:
        def __init__(self):
            self.t = lambda x: x
            self._ = lambda x: x

        def init(self, cl):
            '''prepare i18n'''
            if not isinstance(cl, list):
                cl = [cl]
                # ['nb_NO.UTF-8', 'nb_NO.utf8', 'no_NO']:
            for loc in cl:
                try:
                    # print "Trying (", loc.encode('utf-8'), 'utf-8',")"                    locale.setlocale(locale.LC_ALL, (loc, 'utf-8'))
                    logger.info('Using locale %s', loc)
                    # logger.info('Locale set to %s' % loc)
                    break
                except locale.Error:
                    try:
                        locstr = loc + '.UTF-8'
                        # print "Trying",locstr
                        locale.setlocale(locale.LC_ALL, locstr)
                        logger.info('Using locale %s', loc)
                        break
                    except locale.Error:
                        pass

            lang, charset = locale.getlocale()
            if lang is None:
                raise Exception('Failed to set locale!')

            t = gettext.translation('messages', LOCALE_PATH, fallback=True, languages=[lang])

            self.t = t
            self._ = t.gettext

    instance = None

    def __init__(self):
        if not Localization.instance:
            Localization.instance = Localization.__Localization()
        # else:
        #    Localization.instance.val = arg

    def __getattr__(self, name):
        return getattr(self.instance, name)


localization = Localization()


def ngettext(*args, **kwargs):
    try:
        return localization.t.ngettext(*args, **kwargs)
    except AttributeError:
        # During tests, Localization might not be initialized
        return args[0]


def _(*args, **kwargs):
    return localization._(*args, **kwargs)


logfile = sys.stdout


def log(msg, newline=True):
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

    def __str__(self):
        return self.msg

    def __repr__(self):
        return 'InvalidContestPage(%r)' % self.msg
