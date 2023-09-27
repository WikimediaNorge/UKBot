# encoding=utf-8
# vim: fenc=utf-8 et sw=4 ts=4 sts=4 ai
import weakref
import re
import urllib
from collections import OrderedDict
from datetime import datetime
import logging
import pytz
from mwtemplates import TemplateEditor
from mwtextextractor import get_body_text
from .common import _

logger = logging.getLogger(__name__)


class Revision(object):

    def __init__(self, article, revid, **kwargs):
        """
        A revision is uniquely identified by its revision id and its site

        Arguments:
          - article: (Article) article object reference
          - revid: (int) revision id
        """
        self.article = weakref.ref(article)
        self.errors = []

        self.revid = revid
        self.size = -1
        self.text = ''
        self.point_deductions = []

        self.parentid = 0
        self.parentsize = 0
        self.parenttext = ''
        self.username = ''
        self.parsedcomment = None
        self.saved = False  # Saved in local DB
        self.dirty = False  #
        self._te_text = None  # Loaded as needed
        self._te_parenttext = None  # Loaded as needed

        for k, v in kwargs.items():
            if k == 'timestamp':
                self.timestamp = int(v)
            elif k == 'parentid':
                self.parentid = int(v)
            elif k == 'size':
                self.size = int(v)
            elif k == 'parentsize':
                self.parentsize = int(v)
            elif k == 'username':
                self.username = v[0].upper() + v[1:]
            elif k == 'parsedcomment':
                self.parsedcomment = v
            elif k == 'text':
                if v is not None:
                    self.text = v
            elif k == 'parenttext':
                if v is not None:
                    self.parenttext = v
            else:
                raise Exception('add_revision got unknown argument %s' % k)

        for pd in self.article().user().point_deductions:
            if pd['revid'] == self.revid and self.article().site().match_prefix(pd['site']):
                self.add_point_deduction(pd['points'], pd['reason'])

    def __repr__(self):
        return "Revision(%s of %s:%s)" % (self.revid, self.article().site().key, self.article().name)

    def __str__(self):
        return self.__repr__()

    def __hash__(self):
        return hash(self.__repr__())

    @property
    def utc(self):
        return pytz.utc.localize(datetime.fromtimestamp(self.timestamp))

    @property
    def wiki_tz(self):
        return self.utc.astimezone(self.article().user().contest().wiki_tz)

    def te_text(self):
        if self._te_text is None:
            self._te_text = TemplateEditor(re.sub('<nowiki ?/>', '', self.text))
        return self._te_text

    def te_parenttext(self):
        if self._te_parenttext is None:
            self._te_parenttext = TemplateEditor(re.sub('<nowiki ?/>', '', self.parenttext))
        return self._te_parenttext

    @property
    def bytes(self):
        return self.size - self.parentsize

    @property
    def words(self):
        if self.article().site().host == 'www.wikidata.org':
            # Don't do wordcount for wikidata
            return 0
        try:
            return self._wordcount
        except:
            pass

        mt1 = get_body_text(re.sub('<nowiki ?/>', '', self.text))
        mt0 = get_body_text(re.sub('<nowiki ?/>', '', self.parenttext))

        if self.article().site().key == 'ja.wikipedia.org':
            words1 = len(mt1) / 3.0
            words0 = len(mt0) / 3.0
        elif self.article().site().key == 'zh.wikipedia.org':
            words1 = len(mt1) / 2.0
            words0 = len(mt0) / 2.0
        else:
            words1 = len(mt1.split())
            words0 = len(mt0.split())

        charcount = len(mt1) - len(mt0)
        self._wordcount = words1 - words0

        logger.debug('Wordcount: Revision %s@%s: %+d bytes, %+d characters, %+d words',
                     self.revid, self.article().site().key, self.bytes, charcount, self._wordcount)

        if not self.new and words0 == 0 and self._wordcount > 1:
            w = _('Revision [//%(host)s/w/index.php?diff=prev&oldid=%(revid)s %(revid)s]: The word count difference might be wrong, because no words were found in the parent revision (%(parentid)s) of size %(size)d, possibly due to unclosed tags or templates in that revision.') % {
                'host': self.article().site().host,
                'revid': self.revid,
                'parentid': self.parentid,
                'size': len(self.parenttext)
            }
            logger.warning(w)
            self.errors.append(w)

        elif self._wordcount > 10 and self._wordcount > self.bytes:
            w = _('Revision [//%(host)s/w/index.php?diff=prev&oldid=%(revid)s %(revid)s]: The word count difference might be wrong, because the word count increase (%(words)d) is larger than the byte increase (%(bytes)d). Wrong word counts can occur for invalid wiki text.') % {
                'host': self.article().site().host,
                'revid': self.revid,
                'words': self._wordcount,
                'bytes': self.bytes
            }
            logger.warning(w)
            self.errors.append(w)

        #s = _('A problem encountered with revision %(revid)d may have influenced the word count for this revision: <nowiki>%(problems)s</nowiki> ')
        #s = _('Et problem med revisjon %d kan ha påvirket ordtellingen for denne: <nowiki>%s</nowiki> ')
        del mt1
        del mt0
        # except DanmicholoParseError as e:
        #     log("!!!>> FAIL: %s @ %d" % (self.article().name, self.revid))
        #     self._wordcount = 0
        #     #raise
        return self._wordcount

    @property
    def new(self):
        return self.parentid == 0 or (self.parentredirect and not self.redirect)

    @property
    def redirect(self):
        return bool(self.article().site().redirect_regexp.match(self.text))

    @property
    def parentredirect(self):
        return bool(self.article().site().redirect_regexp.match(self.parenttext))

    def get_link(self, homesite):
        """ returns a link to revision """
        q = OrderedDict([('oldid', self.revid)])
        if not self.new:
            q['diff'] = 'prev'
        if self.article().site().host == homesite.host:
            host_prefix = '{{SERVER}}'
        else:
            host_prefix = '//' + self.article().site().host
        return host_prefix + self.article().site().site['script'] + '?' + urllib.parse.urlencode(q)

    def get_parent_link(self):
        """ returns a link to parent revision """
        q = OrderedDict([('oldid', self.parentid)])
        return '//' + self.article().site().host + self.article().site().site['script'] + '?' + urllib.parse.urlencode(q)

    def add_point_deduction(self, points, reason):
        logger.info('Revision %s: Removing %d points for reason: %s', self.revid, points, reason)
        self.point_deductions.append([points, reason])
