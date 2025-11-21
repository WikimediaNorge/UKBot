# encoding=utf-8
# vim: fenc=utf-8 et sw=4 ts=4 sts=4 ai
import time
import weakref
from collections import OrderedDict
from datetime import datetime
import pytz
import logging

from more_itertools import first, last
import numpy as np

from .revision import Revision

logger = logging.getLogger(__name__)


class Article(object):

    def __init__(self, site, user, name, ns):
        """
        An article is uniquely identified by its name and its site
        """
        self.site = weakref.ref(site)
        self.user = weakref.ref(user)
        self.ns = str(ns)
        self.name = name
        self.disqualified = False
        self._created_at = None

        self.revisions = OrderedDict()
        self.errors = []

    def __eq__(self, other):
        if self.site() == other.site() and self.name == other.name:
            return True
        else:
            return False

    def __repr__(self):
        return "Article(%s, %s, %s)" % (self.site().key, self.name, self.user().name)

    def __str__(self):
        return self.__repr__()

    def __hash__(self):
        return hash(self.__repr__())

    @property
    def created_at(self):
        if self._created_at is None:
            sql = self.user().contest().sql
            cur = sql.cursor()
            res = self.site().pages[self.name].revisions(prop='timestamp', limit=1, dir='newer')
            ts = next(res)['timestamp']
            self._created_at = pytz.utc.localize(datetime.fromtimestamp(time.mktime(ts)))

            cur.execute(
                'INSERT INTO articles (site, name, created_at) VALUES (%s, %s, %s)',
                [self.site().key, self.key, self._created_at.strftime('%Y-%m-%d %H:%M:%S')]
            )
            sql.commit()
        return self._created_at

    @property
    def key(self):
        return '%s:%s' % (self.site().key, self.name)

    @property
    def firstrev(self):
        return self.revisions[first(self.revisions)]
    
    @property
    def lastrev(self):
        return self.revisions[last(self.revisions)]
    

    @property
    def redirect(self):
        return self.lastrev.redirect

    @property
    def new(self):
        # Deprecated, compare created_at with contest start date instead!
        return self.firstrev.new

    @property
    def new_non_redirect(self):
        # Deprecated, compare created_at with contest start date instead!
        firstrev = self.revisions[first(self.revisions)]
        return firstrev.new and not firstrev.redirect

    def add_revision(self, revid, **kwargs):
        rev = Revision(self, revid, **kwargs)
        self.revisions[revid] = rev
        self.user().revisions[revid] = rev
        return rev

    def link(self):
        # Create a link to the page, including a site prefix if the site is not the homesite.
        return self.site().link_to(self)

    @property
    def bytes(self):
        return np.sum([rev.bytes for rev in self.revisions.values()])

    @property
    def words(self):
        """
        Returns the total number of words added to this Article. The number
        will never be negative, but words removed in one revision will
        contribute negatively to the sum.
        """
        return np.max([0, np.sum([rev.words for rev in self.revisions.values()])])
