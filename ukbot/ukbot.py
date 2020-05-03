# encoding=utf-8
# vim: fenc=utf-8 et sw=4 ts=4 sts=4 ai
import time

runstart_s = time.time()
print('Loading')

import sys
import logging
import matplotlib
import pydash
import weakref
import numpy as np
import calendar
from datetime import datetime
from datetime import time as dt_time
import pytz
from isoweek import Week  # Sort-of necessary until datetime supports %V, see http://bugs.python.org/issue12006
                          # and See http://stackoverflow.com/questions/5882405/get-date-from-iso-week-number-in-python
import re
import json
import os
from collections import OrderedDict
import urllib
import argparse
import codecs
import mwclient
import mwtemplates
from mwtemplates import TemplateEditor
import locale
import rollbar
import platform
from dotenv import load_dotenv
import pymysql
from retry import retry
from more_itertools import first

from .contributions import UserContributions
from .rules import NewPageRule, ByteRule, WordRule, RefRule, ImageRule, TemplateRemovalRule
from .common import get_mem_usage, Localization, _
from .rules import rule_classes
from .filters import *
from .db import db_conn, result_iterator
from .util import cleanup_input, load_config, unix_time
from .site import Site, WildcardPage
from .article import Article

# ----------------------------------------------------------

STATE_NORMAL ='normal'
STATE_ENDING = 'ending'
STATE_CLOSING = 'closing'

matplotlib.use('svg')

if sys.version_info < (3, 4):
    print('Requires Python >= 3.4')
    sys.exit(1)

# ----------------------------------------------------------
# Setup root logger


class AppFilter(logging.Filter):

    @staticmethod
    def format_as_mins_and_secs(msecs):
        secs = msecs / 1000.
        mins = int(secs / 60.)
        secs = int(secs % 60.)
        return '%3.f:%02.f' % (mins, secs)

    def filter(self, record):
        record.mem_usage = '%.0f' % (get_mem_usage(),)
        record.relative_time = AppFilter.format_as_mins_and_secs(record.relativeCreated)
        return True


logging.getLogger('requests').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)
logging.getLogger('requests_oauthlib').setLevel(logging.WARNING)
logging.getLogger('oauthlib').setLevel(logging.WARNING)
logging.getLogger('mwtemplates').setLevel(logging.INFO)

logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
syslog = logging.StreamHandler()
logger.addHandler(syslog)
syslog.setLevel(logging.INFO)
formatter = logging.Formatter('[%(relative_time)s] {%(mem_usage)s MB} %(name)-20s %(levelname)s : %(message)s')
syslog.setFormatter(formatter)
syslog.addFilter(AppFilter())

# ----------------------------------------------------------

load_dotenv()


def sum_stats_by(values, key=None, user=None):
    the_sum = 0
    for value in values:
        if key is not None and key != value['key']:
            continue
        if user is not None and user != value['user']:
            continue
        the_sum += value['value']
    return the_sum


class User(object):

    def __init__(self, username, contest):
        self.name = username
        self.articles = OrderedDict()
        self.revisions = OrderedDict()
        self.contest = weakref.ref(contest)
        self.suspended_since = None
        self.contributions = UserContributions(self, contest.config)
        self.disqualified_articles = []
        self.point_deductions = []

    def __repr__(self):
        return "<User %s>" % self.name

    def sort_contribs(self):

        # sort revisions by revision id
        for article in self.articles.values():
            article.revisions = OrderedDict(sorted(article.revisions.items(), key=lambda x: x[0]))   # sort by key (revision id)

        # sort articles by first revision id
        self.articles = OrderedDict(sorted(self.articles.items(), key=lambda x: first(x[1].revisions)))

    def add_article_if_necessary(self, site, article_title, ns):
        article_key = site.key + ':' + article_title

        if not article_key in self.articles:
            self.articles[article_key] = Article(site, self, article_title, ns)
            if article_key in self.disqualified_articles:
                self.articles[article_key].disqualified = True

        return self.articles[article_key]

    def add_contribs_from_wiki(self, site, start, end, fulltext=False, **kwargs):
        """
        Populates self.articles with entries from the API.

            site      : mwclient.client.Site object
            start     : datetime object with timezone Europe/Oslo
            end       : datetime object with timezone Europe/Oslo
            fulltext  : get revision fulltexts
        """

        # logger.info('Reading contributions from %s', site.host)

        apilim = 50
        if 'bot' in site.rights:
            apilim = site.api_limit         # API limit, should be 500

        site_key = site.host

        ts_start = start.astimezone(pytz.utc).strftime('%FT%TZ')
        ts_end = end.astimezone(pytz.utc).strftime('%FT%TZ')

        # 1) Fetch user contributions

        args = {}
        if 'namespace' in kwargs:
            args['namespace'] = kwargs['namespace']
            logger.debug('Limiting to namespaces: %s', args['namespace'])

        new_revisions = []
        # stored_revisions = set(copy(self.revisions.keys()))
        stored_revisions = set([rev.revid for rev in self.revisions.values() if rev.article().site() == site])
        current_revisions = set()
        t0 = time.time()
        t1 = time.time()
        tnr = 0
        n_articles = len(self.articles)
        for c in site.usercontributions(self.name, ts_start, ts_end, 'newer', prop='ids|title|timestamp|comment', **args):
            tnr += 1

            dt1 = time.time() - t1
            if dt1 > 10:
                dt0 = time.time() - t0
                t1 = time.time()
                logger.info('Found %d new revisions from API so far (%.0f secs elapsed)',
                            len(new_revisions), dt0)

            if 'comment' in c:
                article_comment = c['comment']

                ignore = False
                for pattern in self.contest().config['ignore']:
                    if re.search(pattern, article_comment):
                        ignore = True
                        logger.info('Ignoring revision %d of %s:%s because it matched /%s/', c['revid'], site_key, c['title'], pattern)
                        break

                if not ignore:
                    rev_id = c['revid']
                    article_title = c['title']
                    article_key = site_key + ':' + article_title
                    current_revisions.add(rev_id)

                    if rev_id in self.revisions:
                        # We check self.revisions instead of article.revisions, because the revision may
                        # already belong to "another article" (another title) if the article has been moved

                        if self.revisions[rev_id].article().name != article_title:
                            rev = self.revisions[rev_id]
                            logger.info('Moving revision %d from "%s" to "%s"', rev_id, rev.article().name, article_title)
                            article = self.add_article_if_necessary(site, article_title, c['ns'])
                            rev.article().revisions.pop(rev_id)  # remove from old article
                            article.revisions[rev_id] = rev    # add to new article
                            rev.article = weakref.ref(article)              # and update reference

                    else:

                        article = self.add_article_if_necessary(site, article_title, c['ns'])
                        rev = article.add_revision(rev_id, timestamp=time.mktime(c['timestamp']), username=self.name)
                        rev.saved = False  # New revision that should be stored in DB
                        new_revisions.append(rev)

        # Check if revisions have been deleted
        logger.info('Site: %s, stored revisions: %d, current revisions: %d', site.key, len(stored_revisions), len(current_revisions))
        deleted_revisions = stored_revisions.difference(current_revisions)
        for deleted_revision in deleted_revisions:
            rev = self.revisions[deleted_revision]
            logger.info('Removing deleted revision %s from %s.', rev.revid, rev.article().name)
            del rev.article().revisions[deleted_revision]
            del self.revisions[deleted_revision]

        # If revisions were moved from one article to another, and the redirect was not created by the same user,
        # some articles may now have zero revisions. We should drop them
        to_drop = set()
        for article_key, article in self.articles.items():
            if len(article.revisions) == 0:
                to_drop.add(article_key)
        for article_key in to_drop:
            logger.debug('Dropping article "%s" due to zero remaining revisions', article_key)
            del self.articles[article_key]

        # Always sort after we've added contribs
        new_articles = len(self.articles) - n_articles
        self.sort_contribs()
        # if len(new_revisions) > 0 or new_articles > 0:
        dt = time.time() - t0
        t0 = time.time()
        logger.info('Checked %d contributions, found %d new revisions and %d new articles from %s in %.2f secs',
                    tnr, len(new_revisions), new_articles, site.host, dt)

        # 2) Check if pages are redirects (this information can not be cached, because other users may make the page a redirect)
        #    If we fail to notice a redirect, the contributions to the page will be double-counted, so lets check

        #titles = [a.name for a in self.articles.values() if a.site.key == site_key]
        #for s0 in range(0, len(titles), apilim):
        #    ids = '|'.join(titles[s0:s0+apilim])
        #    for page in site.api('query', prop = 'info', titles = ids)['query']['pages'].values():
        #        article_key = site_key + ':' + page['title']
        #        self.articles[article_key].redirect = ('redirect' in page.keys())

        # 3) Fetch info about the new revisions: diff size, possibly content

        props = 'ids|size|parsedcomment'
        if fulltext:
            props += '|content'
        revids = [str(r.revid) for r in new_revisions]
        parentids = set()
        revs = set()

        cur_apilim = apilim

        rev_count = len(revids)
        while len(revids) > 0:
            ids = '|'.join(revids[:cur_apilim])
            logger.info('Fetching revisions %d-%d of %d', len(revs) + 1, min([len(revs) + cur_apilim, len(revids)]), rev_count)
            res = site.api('query', prop='revisions', rvprop=props, revids=ids, rvslots='main', uselang='nb')
            if pydash.get(res, 'warnings.result.*') is not None:
                # We ran into Manual:$wgAPIMaxResultSize, try reducing
                logger.warning('We ran into wgAPIMaxResultSize, reducing the batch size from %d to %d', cur_apilim, round(cur_apilim / 2))
                cur_apilim = round(cur_apilim / 2)
                continue

            for page in res['query']['pages'].values():
                article_key = site_key + ':' + page['title']
                for apirev in page['revisions']:
                    rev = self.articles[article_key].revisions[apirev['revid']]
                    rev.parentid = apirev['parentid']
                    rev.size = apirev['size']
                    rev.parsedcomment = apirev['parsedcomment']
                    content = pydash.get(apirev, 'slots.main.*')
                    if content is not None:
                        rev.text = content
                        rev.dirty = True
                    if not rev.new:
                        parentids.add(rev.parentid)
                    revs.add(apirev['revid'])
            revids = revids[cur_apilim:]

        dt = time.time() - t0
        t0 = time.time()
        if len(revs) > 0:
            logger.info('Checked %d revisions, found %d parent revisions in %.2f secs',
                        len(revs), len(parentids), dt)

        if len(revs) != len(new_revisions):
            raise Exception('Expected %d revisions, but got %d' % (len(new_revisions), len(revs)))

        # 4) Fetch info about the parent revisions: diff size, possibly content

        props = 'ids|size'
        if fulltext:
            props += '|content'
        nr = 0

        parentids = [str(i) for i in parentids]
        rev_count = len(parentids)
        while len(parentids) > 0:
            ids = '|'.join(parentids[:cur_apilim])
            logger.info('Fetching revisions %d-%d of %d', nr + 1, min([nr + cur_apilim, len(parentids)]), rev_count)
            res = site.api('query', prop='revisions', rvprop=props, revids=ids, rvslots='main', uselang='nb')
            if pydash.get(res, 'warnings.result.*') is not None:
                # We ran into Manual:$wgAPIMaxResultSize, try reducing
                logger.warning('We ran into wgAPIMaxResultSize, reducing the batch size from %d to %d', cur_apilim, round(cur_apilim / 2))
                cur_apilim = round(cur_apilim / 2)
                continue

            for page in res['query']['pages'].values():
                article_key = site_key + ':' + page['title']
                # In the case of a merge, the new title (article_key) might not be part of the user's 
                # contribution list (self.articles), so we need to check:
                if article_key in self.articles:
                    article = self.articles[article_key]
                    for apirev in page.get('revisions', []):
                        nr += 1
                        parentid = apirev['revid']
                        found = False
                        for revid, rev in article.revisions.items():
                            if rev.parentid == parentid:
                                found = True
                                break
                        if found:
                            rev.parentsize = apirev['size']
                            content = pydash.get(apirev, 'slots.main.*')
                            if content is None:
                                logger.warning('Did not get revision text for %s', article.name)
                            else:
                                rev.parenttext = content
                                logger.debug('Got revision text for %s: %d bytes', article.name, len(rev.parenttext))
                        else:
                            rev.parenttext = ''  # New page
            parentids = parentids[cur_apilim:]

        if nr > 0:
            dt = time.time() - t0
            logger.info('Checked %d parent revisions in %.2f secs', nr, dt)

    def backfill_article_creation_dates(self, sql):
        cur = sql.cursor()

        logger.debug('Reading and backfilling article creation dates')

        # Group articles by site
        articles_by_site = {}
        for article in self.articles.values():
            if article.site() not in articles_by_site:
                articles_by_site[article.site()] = {}
            articles_by_site[article.site()][article.name] = article

        for site, articles in articles_by_site.items():
            article_keys = list(articles.keys())
            cur.execute(
                'SELECT name, created_at FROM articles WHERE site=%s AND name IN (' + ','.join(['%s' for x in range(len(article_keys))]) + ')',
                [site.name] + article_keys
            )
            for row in result_iterator(cur):
                article = articles_by_site[site][row[0]]
                article._created_at = pytz.utc.localize(row[1])

            # n = 0
            # for article_key, article in articles.items():
            #     if article.created_at is None:
            #         res = site.pages[article.name].revisions(prop='timestamp', limit=1, dir='newer')
            #         ts = article.created_at = next(res)['timestamp']
            #         ts = time.strftime('%Y-%m-%d %H:%M:%S', ts)
            #         # datetime.fromtimestamp(rev.timestamp).strftime('%F %T')
            #         cur.execute(
            #             'INSERT INTO articles (site, name, created_at) VALUES (%s, %s, %s)',
            #             [site.name, article_key, ts]
            #         )
            #         n += 1
            # sql.commit()
            # if n > 0:
            #     logger.debug('Backfilled %d article creation dates from %s', n, site.name)

        cur.close()

    def save_contribs_to_db(self, sql):
        """ Save self.articles to DB so it can be read by add_contribs_from_db """

        cur = sql.cursor()

        contribs_query_params = []
        fulltexts_query_params = []

        for article_key, article in self.articles.items():
            site_key = article.site().key

            for revid, rev in article.revisions.items():
                ts = datetime.fromtimestamp(rev.timestamp).strftime('%F %T')

                # Save revision if not already saved
                if not rev.saved:
                    contribs_query_params.append((revid, site_key, rev.parentid, self.name, article.name, ts, rev.size, rev.parentsize, rev.parsedcomment, article.ns))
                    rev.saved = True

                if rev.dirty:
                    # Save revision text if we have it and if not already saved
                    fulltexts_query_params.append((revid, site_key, rev.text))
                    fulltexts_query_params.append((rev.parentid, site_key, rev.parenttext))

        # Insert all revisions
        chunk_size = 1000
        for n in range(0, len(contribs_query_params), chunk_size):
            data = contribs_query_params[n:n+chunk_size]
            # logger.info('Adding %d contributions to database', len(data))

            t0 = time.time()
            cur.executemany("""
                insert into contribs (revid, site, parentid, user, page, timestamp, size, parentsize, parsedcomment, ns)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, data
            )
            dt = time.time() - t0
            logger.info('Added %d contributions to database in %.2f secs', len(data), dt)

        chunk_size = 100
        for n in range(0, len(fulltexts_query_params), chunk_size):
            data = fulltexts_query_params[n:n+chunk_size]
            # logger.info('Adding %d fulltexts to database', len(data))
            t0 = time.time()

            cur.executemany("""
                insert into fulltexts (revid, site, revtxt)
                values (%s,%s,%s)
                on duplicate key update revtxt=values(revtxt);
                """, data
            )

            dt = time.time() - t0
            logger.info('Added %d fulltexts to database in %.2f secs', len(data), dt)

        sql.commit()
        cur.close()

    def backfill_text(self, sql, site, rev):
        parentid = None
        props = 'ids|size|content'
        res = site.api('query', prop='revisions', rvprop=props, rvslots='main', revids='{}|{}'.format(rev.revid, rev.parentid))['query']
        if res.get('pages') is None:
            logger.info('Failed to get revision %d, revision deleted?', rev.revid)
            return

        for page in res['pages'].values():
            for apirev in page['revisions']:
                if apirev['revid'] == rev.revid:
                    content = pydash.get(apirev, 'slots.main.*')
                    if content is None:
                        logger.warning('No revision text available!')
                    else:
                        rev.text = content
                elif apirev['revid'] == rev.parentid:
                    content = pydash.get(apirev, 'slots.main.*')
                    if content is None:
                        logger.warning('No parent revision text available!')
                    else:
                        rev.parenttext = content

        cur = sql.cursor()

        # Save revision text if we have it and if not already saved
        cur.execute('SELECT revid FROM fulltexts WHERE revid=%s AND site=%s', [rev.revid, site.key])
        if len(rev.text) > 0 and len(cur.fetchall()) == 0:
            cur.execute('INSERT INTO fulltexts (revid, site, revtxt) VALUES (%s,%s,%s)', (rev.revid, site.key, rev.text))
            sql.commit()

        # Save parent revision text if we have it and if not already saved
        if parentid is not None:
            logger.debug('Storing parenttext %d , revid %s ', len(rev.parenttext), rev.parentid)
            cur.execute('SELECT revid FROM fulltexts WHERE revid=%s AND site=%s', [rev.parentid, site.key])
            if len(rev.parenttext) > 0 and len(cur.fetchall()) == 0:
                cur.execute('INSERT INTO fulltexts (revid, site, revtxt) VALUES (%s,%s,%s)', (rev.parentid, site.key, rev.parenttext))
                sql.commit()

        cur.close()

    @retry(pymysql.err.OperationalError, tries=3, delay=30)
    def add_contribs_from_db(self, sql, start, end, sites):
        """
        Populates self.articles with entries from MySQL DB

            sql       : SQL Connection object
            start : datetime object
            end   : datetime object
            sites : list of sites
        """
        # logger.info('Reading user contributions from database')

        cur = sql.cursor()
        ts_start = start.astimezone(pytz.utc).strftime('%F %T')
        ts_end = end.astimezone(pytz.utc).strftime('%F %T')
        nrevs = 0
        narts = 0
        t0 = time.time()
        cur.execute(u"""
            SELECT
                c.revid, c.site, c.parentid, c.page, c.timestamp, c.size, c.parentsize, c.parsedcomment, c.ns,
                ft.revtxt,
                ft2.revtxt
            FROM contribs AS c
            LEFT JOIN fulltexts AS ft ON ft.revid = c.revid AND ft.site = c.site
            LEFT JOIN fulltexts AS ft2 ON ft2.revid = c.parentid AND ft2.site = c.site
            WHERE c.user = %s
            AND c.timestamp >= %s AND c.timestamp <= %s
            """,
            (self.name, ts_start, ts_end)
        )
        for row in result_iterator(cur):

            rev_id, site_key, parent_id, article_title, ts, size, parentsize, parsedcomment, ns, rev_text, parent_rev_txt = row
            article_key = site_key + ':' + article_title

            ts = unix_time(pytz.utc.localize(ts))

            if site_key not in sites:
                # Contribution from a wiki which is not part of this contest config
                continue

            # Add article if not present
            if not article_key in self.articles:
                narts += 1
                self.articles[article_key] = Article(sites[site_key], self, article_title, ns)
                if article_key in self.disqualified_articles:
                    self.articles[article_key].disqualified = True
            article = self.articles[article_key]

            # Add revision if not present
            if not rev_id in self.revisions:
                nrevs += 1
                article.add_revision(rev_id, timestamp=ts, parentid=parent_id, size=size, parentsize=parentsize,
                    username=self.name, parsedcomment=parsedcomment, text=rev_text, parenttext=parent_rev_txt)
            rev = self.revisions[rev_id]
            rev.saved = True

            # Add revision text
            if rev_text is None or rev_text == '':
                logger.debug('Article: %s, text missing %s, backfilling', article.name, rev_id)
                self.backfill_text(sql, sites[site_key], rev)

            # Add parent revision text
            if not rev.new:
                if parent_rev_txt is None or parent_rev_txt == '':
                    logger.debug('Article: %s, parent text missing: %s,  backfilling', article.name, parent_id)
                    self.backfill_text(sql, sites[site_key], rev)

        cur.close()

        # Always sort after we've added contribs
        self.sort_contribs()

        # if nrevs > 0 or narts > 0:
        dt = time.time() - t0
        logger.info('Read %d revisions, %d pages from database in %.2f secs', nrevs, narts, dt)

    def filter(self, filters):

        logger.info('Filtering user contributions')
        n0 = len(self.articles)
        t0 = time.time()

        def apply_filters(articles, filters, depth):
            if isinstance(filters, list):
                # Apply filters in serial (AND)
                res = copy(articles)
                logger.debug('%s Intersection of %d filters (AND):', '>' * depth, len(filters))
                for f in filters:
                    res = apply_filters(res, f, depth + 1)
                return res

            elif isinstance(filters, tuple):
                # Apply filters in parallel (OR)
                if len(filters) == 0:  # Interpret empty tuple as "filtering nothing" rather than "filter everything"
                    return articles
                res = OrderedDict()
                logger.debug('%s Union of %d filters (OR):', '>' * depth, len(filters))
                for f in filters:
                    for o in apply_filters(articles, f, depth + 1):
                        if o not in res:
                            res[o] = articles[o]
                return res
            else:
                # Apply single filter
                logger.debug('%s Applying %s filter', '>' * depth, type(filters).__name__)
                return filters.filter(articles)

        logger.debug('Before filtering : %d articles',
                     len(self.articles))

        self.articles = apply_filters(self.articles, filters, 1)
        logger.debug('After filtering : %d articles',
                     len(self.articles))

        # We should re-sort afterwards since not all filters preserve the order (notably the CatFilter)
        self.sort_contribs()

        dt = time.time() - t0
        logger.info('%d of %d pages remain after filtering. Filtering took %.2f secs', len(self.articles), n0, dt)
        for a in self.articles.keys():
            logger.debug(' - %s', a)

    def count_article_stats_per_site(self, key, fn):
        keyed = {}
        for article in self.articles.values():
            keyed[article.site().key] = keyed.get(article.site().key, 0) + int(fn(article))
        return [{'user': self.name, 'site': k, 'key': key, 'value': v} for k, v in keyed.items()]

    def count_bytes_per_site(self):
        return self.count_article_stats_per_site('bytes', lambda a: a.bytes)

    def count_words_per_site(self):
        return self.count_article_stats_per_site('words', lambda a: a.words)

    def count_pages_per_site(self):
        return self.count_article_stats_per_site('pages', lambda a: 1 if not a.redirect else 0)

    def count_newpages_per_site(self):
        return self.count_article_stats_per_site('newpages', lambda a: 1 if a.new_non_redirect else 0)

    def analyze(self, rules):
        x = []
        y = []
        utc = pytz.utc

        # loop over articles
        for article in self.articles.values():
            # if self.contest().verbose:
            #     logger.info(article_key)
            # else:
            #     logger.info('.', newline=False)
            # log(article_key)

            # loop over revisions
            for revid, rev in article.revisions.items():

                # loop over rules
                for rule in rules:
                    for contribution in rule.test(rev):
                        self.contributions.add(contribution)

                if not article.disqualified:
                    dt = pytz.utc.localize(datetime.fromtimestamp(rev.timestamp))
                    if self.suspended_since is None or dt < self.suspended_since:
                        contributions = self.contributions.get(revision=rev)
                        points = sum([contribution.points for contribution in contributions])

                        if points > 0:
                            # logger.debug('%s: %d: %s', self.name, rev.revid, points)
                            ts = float(unix_time(utc.localize(datetime.fromtimestamp(rev.timestamp)).astimezone(
                                self.contest().wiki_tz
                            )))
                            x.append(ts)
                            y.append(float(points))

                            # logger.debug('    %d : %d ', revid, points)
            logger.debug('[[%s]] Sum: %.1f points', article.name,
                         self.contributions.get_article_points(article=article))

        x = np.array(x)
        y = np.array(y)

        o = np.argsort(x)
        x = x[o]
        y = y[o]
        #pl = np.array(pl, dtype=float)
        #pl.sort(axis = 0)
        y2 = np.array([np.sum(y[:q + 1]) for q in range(len(y))])
        self.plotdata = np.column_stack((x, y2))
        #np.savetxt('user-%s'%self.name, np.column_stack((x,y,y2)))

    def format_result(self):
        logger.debug('Formatting results for user %s', self.name)
        entries = []


        ## WIP

        # <<<<<<< HEAD
        #                         dt = utc.localize(datetime.fromtimestamp(rev.timestamp))
        #                         dt_str = dt.astimezone(self.contest().wiki_tz).strftime(_('%d.%m, %H:%M'))
        #                         out = '[%s %s]: %s' % (rev.get_link(), dt_str, descr)
        #                         if self.suspended_since is not None and dt > self.suspended_since:
        #                             out = '<s>' + out + '</s>'
        #                         if len(rev.errors) > 0:
        #                             out = '[[File:Ambox warning yellow.svg|12px|%s]] ' % (', '.join(rev.errors)) + out
        #                         revs.append(out)

        #                 titletxt = ''
        #                 try:
        #                     cat_path = [x.split(':')[-1] for x in article.cat_path]
        #                     titletxt = ' : '.join(cat_path) + '<br />'
        #                 except AttributeError:
        #                     pass
        #                 titletxt += '<br />'.join(revs)
        #                 # if len(article.point_deductions) > 0:
        #                 #     pds = []
        #                 #     for points, reason in article.point_deductions:
        #                 #         pds.append('%.f p: %s' % (-points, reason))
        #                 #     titletxt += '<div style="border-top:1px solid #CCC">\'\'' + _('Notes') + ':\'\'<br />%s</div>' % '<br />'.join(pds)

        #                 titletxt += '<div style="border-top:1px solid #CCC">' + _('Total {{formatnum:%(bytecount)d}} bytes, %(wordcount)d words') % {'bytecount': article.bytes, 'wordcount': article.words} + '</div>'

        #                 p = '%.1f p' % brutto
        #                 if brutto != netto:
        #                     p = '<s>' + p + '</s> '
        #                     if netto != 0.:
        #                         p += '%.1f p' % netto

        #                 out = '[[%s|%s]]' % (article.link(), article.name)
        #                 if article_key in self.disqualified_articles:
        #                     out = '[[File:Qsicon Achtung.png|14px]] <s>' + out + '</s>'
        #                     titletxt += '<div style="border-top:1px solid red; background:#ffcccc;">' + _('<strong>Note:</strong> The contributions to this article are currently disqualified.') + '</div>'
        #                 elif brutto != netto:
        #                     out = '[[File:Qsicon Achtung.png|14px]] ' + out
        #                     #titletxt += '<div style="border-top:1px solid red; background:#ffcccc;"><strong>Merk:</strong> En eller flere revisjoner er ikke talt med fordi de ble gjort mens brukeren var suspendert. Hvis suspenderingen oppheves vil bidragene telle med.</div>'
        #                 if article.new:
        #                     out += ' ' + _('<abbr class="newpage" title="New page">N</abbr>')
        #                 out += ' (<abbr class="uk-ap">%s</abbr>)' % p

        #                 out = '# ' + out
        #                 out += '<div class="uk-ap-title" style="font-size: smaller; color:#888; line-height:100%;">' + titletxt + '</div>'

        #                 entries.append(out)
        #                 logger.debug('    %s: %.f / %.f points', article_key, netto, brutto)
        # =======
        # >>>>>>> WIP

        ros = '{awards}'

        suspended = ''
        if self.suspended_since is not None:
            suspended = ', ' + _('suspended since') + ' %s' % self.suspended_since.strftime(_('%A, %H:%M'))
        userprefix = self.contest().sites.homesite.namespaces[2]
        out = '=== %s [[%s:%s|%s]] (%.f p%s) ===\n' % (ros, userprefix, self.name, self.name,
                                                       self.contributions.sum(), suspended)
        if len(entries) == 0:
            out += "''" + _('No qualifying contributions registered yet') + "''"
        else:
            out += '%s, {{formatnum:%.2f}} kB\n' % (_('articles') % {'articlecount' : len(entries)}, self.bytes / 1000.)
        if len(entries) > 10:
            out += _('{{Kolonner}}\n')
        out += '\n'.join(entries)
        out += '\n\n'

        return out


class FilterTemplate(object):

    def __init__(self, template, translations, sites):
        self.template = template
        self.sites = sites
        self.named_params_raw_values = {
            cleanup_input(k): v.value
            for k, v in template.parameters.items()
        }
        self.named_params = {
            k: cleanup_input(v)
            for k, v in self.named_params_raw_values.items()
        }
        self.anon_params = [
            cleanup_input(v) if v is not None else None
            for v in template.get_anonymous_parameters()
        ]
        self.translations = translations

        def get_type(value):
            for k, v in translations['params'].items():
                if v.get('name') == value:
                    return k
            raise InvalidContestPage(_('The filter name "%s" was not understood') % value)

        self.type = get_type(self.anon_params[1].lower())

    def get_localized_name(self, name):
        return pydash.get(
            self.translations,
            'params.%s.params.%s' % (self.type, name),
            'params._all.params.%s' % (name)
        )

    def has_param(self, name):
        return self.template.has_param(self.get_localized_name(name))

    def get_param(self, name, default=None):
        return self.named_params.get(self.get_localized_name(name), default)

    def get_raw_param(self, name, default=None):
        return self.named_params_raw_values.get(self.get_localized_name(name), default)

    def make(self, contest):
        filter_cls = {
            'new': NewPageFilter,
            'existing': ExistingPageFilter,
            'template': TemplateFilter,
            'bytes': ByteFilter,
            'category': CatFilter,
            'sparql': SparqlFilter,
            'backlink': BackLinkFilter,
            'forwardlink': ForwardLinkFilter,
            'namespace': NamespaceFilter,
            'pages': PageFilter,
        }[self.type]

        return filter_cls.make(contest=contest, tpl=self, cfg=self.translations['params'][self.type])


class Contest(object):

    def __init__(self, page, state, sites, sql, config, wiki_tz, server_tz, project_dir, job_id):
        """
            page: mwclient.Page object
            sites: list
            sql: mysql Connection object
        """
        logger.info('Initializing contest [[%s]], state: %s', page.name, state)
        self.page = page
        self.state = state
        self.name = self.page.name
        self.config = config
        self.project_dir = project_dir
        self.job_id = job_id
        txt = page.text()

        self.sql = sql
        self.wiki_tz = wiki_tz
        self.server_tz = server_tz

        self.sites = sites
        self.users = [User(n, self) for n in self.extract_userlist(txt)]
        self.rules, self.filters = self.extract_rules(txt, self.config.get('catignore', ''))

        logger.info("- %d participants", len(self.users))
        # logger.info("- %d filter(s):" % len(self.filters))
        # for filter in self.filters:
        #     logger.info("  - %s" % filter.__class__.__name__)

        logger.info("%d rule(s)" % len(self.rules))
        for rule in self.rules:
            logger.info("  - %s" % rule.__class__.__name__)

        logger.info('- Open from %s to %s',
                    self.start.strftime('%F %T'),
                    self.end.strftime('%F %T'))

        # if self.startweek == self.endweek:
        #     logger.info(' - Week %d', self.startweek)
        # else:
        #     logger.info(' - Week %d–%d', self.startweek, self.endweek)

    def extract_userlist(self, txt):
        lst = []
        m = re.search(r'==\s*%s\s*==' % self.config['contestPages']['participantsSection'], txt)
        if not m:
            raise InvalidContestPage(_("Couldn't find the list of participants!"))
        deltakerliste = txt[m.end():]
        m = re.search('==[^=]+==', deltakerliste)
        if not m:
            raise InvalidContestPage('Fant ingen overskrift etter deltakerlisten!')
        deltakerliste = deltakerliste[:m.start()]
        for d in deltakerliste.split('\n'):
            q = re.search(r'\[\[(?:[^|\]]+):([^|\]]+)', d)
            if q:
                lst.append(q.group(1))
        return lst

    def extract_rules(self, txt, catignore_page=''):
        rules = []

        # Syntax is compatible with https://stackoverflow.com/questions/6875361/using-lepl-to-parse-a-boolean-search-query
        # In the future we could also support a boolean string input from the criterion template.        
        filters = [
            ()
        ]

        config = self.config

        rulecfg = config['templates']['rule']

        dp = TemplateEditor(txt)

        if config['templates']['rule']['name'] not in dp.templates:
            raise InvalidContestPage(_('There are no point rules defined for this contest. Point rules are defined by {{tl|%(template)s}}.') % {'template': config['templates']['rule']['name']})

        #if not 'ukens konkurranse kriterium' in dp.templates.keys():
        #    raise InvalidContestPage('Denne konkurransen har ingen bidragskriterier. Kriterier defineres med {{tl|ukens konkurranse kriterium}}.')


        ######################## Read infobox ########################

        infobox = parse_infobox(txt, self.sites.homesite.namespaces[2], self.config, self.wiki_tz)
        self.start = infobox['start_time']
        self.end = infobox['end_time']

        # args = {'week': commonargs['week'], 'year': commonargs['year'], 'start': ibcfg['start'], 'end': ibcfg['end'], 'template': ibcfg['name']}
        # raise InvalidContestPage(_('Did not find %(week)s+%(year)s or %(start)s+%(end)s in {{tl|%(templates)s}}.') % args)

        self.year = self.start.isocalendar()[0]
        self.startweek = self.start.isocalendar()[1]
        self.endweek = self.end.isocalendar()[1]
        self.month = self.start.month

        self.ledere = infobox['organizers']
        if len(self.ledere) == 0:
            logger.warning('Found no organizers in {{tl|%s}}.', infobox['name'])

        self.prices = infobox['awards']
        self.prices.sort(key=lambda x: x[2], reverse=True)

        ######################## Read filters ########################

        nfilters = 0
        # print dp.templates.keys()
        filter_template_config = config['templates']['filters']
        if filter_template_config['name'] in dp.templates:
            for template in dp.templates[filter_template_config['name']]:
                filter_tpl = FilterTemplate(template, filter_template_config, self.sites)

                if filter_tpl.type in ['new', 'existing', 'namespace']:
                    op = 'AND'
                else:
                    op = 'OR'

                try:
                    filter_inst = filter_tpl.make(self)
                except RuntimeError as exp:
                    raise InvalidContestPage(
                        _('Could not parse {{tlx|%(template)s|%(firstarg)s}} template: %(err)s') 
                        % {
                            'template': filter_template_config['name'],
                            'firstarg': filter_tpl.anon_params[1],
                            'err': str(exp)
                        }
                    )

                nfilters += 1
                if op == 'OR':
                    # Append filter to the last tuple in the filters list
                    filters[-1] = filters[-1] + (filter_inst,)
                else:
                    # Prepend filter to the filters list
                    filters.insert(0, filter_inst)

        ######################## Read rules ########################

        rule_classes_map = {
            rulecfg[rule_cls.rule_name]: rule_cls for rule_cls in rule_classes
        }

        nrules = 0
        for rule_template in dp.templates[rulecfg['name']]:
            nrules += 1
            rule_name = rule_template.parameters[1].value.lower()
            try:
                rule_cls = rule_classes_map[rule_name]
            except:
                raise InvalidContestPage(
                    _('Unkown argument given to {{tl|%(template)s}}: %(argument)s')
                    % {'template': rulecfg['name'], 'argument': rule_name}
                )

            rule = rule_cls(self.sites, rule_template.parameters, rulecfg)
            rules.append(rule)

        ####################### Check if contest is in DB yet ##################

        cur = self.sql.cursor()
        now = datetime.now()
        cur.execute('UPDATE contests SET start_date=%s, end_date=%s, update_date=%s, last_job_id=%s WHERE site=%s AND name=%s', [
            self.start.strftime('%F %T'),
            self.end.strftime('%F %T'),
            now.strftime('%F %T'),
            self.job_id,
            self.sites.homesite.key,
            self.name,
        ])
        self.sql.commit()
        cur.close()

        ######################## Read disqualifications ########################

        sucfg = self.config['templates']['suspended']
        if sucfg['name'] in dp.templates:
            for template in dp.templates[sucfg['name']]:
                uname = cleanup_input(template.parameters[1].value)
                try:
                    sdate = self.wiki_tz.localize(datetime.strptime(cleanup_input(template.parameters[2].value), '%Y-%m-%d %H:%M'))
                except ValueError:
                    raise InvalidContestPage(_("Couldn't parse the date given to the {{tl|%(template)s}} template.") % sucfg['name'])

                #print 'Suspendert bruker:',uname,sdate
                ufound = False
                for u in self.users:
                    if u.name == uname:
                        #print " > funnet"
                        u.suspended_since = sdate
                        ufound = True
                if not ufound:
                    pass
                    # TODO: logging.warning
                    #raise InvalidContestPage('Fant ikke brukeren %s gitt til {{tl|UK bruker suspendert}}-malen.' % uname)

        dicfg = self.config['templates']['disqualified']
        if dicfg['name'] in dp.templates:
            logger.info('Disqualified contributions:')
            for template in dp.templates[dicfg['name']]:
                uname = cleanup_input(template.parameters[1].value)
                anon = template.get_anonymous_parameters()
                uname = anon[1]
                if not template.has_param('s'):
                    for article_name in anon[2:]:
                        page = self.sites.resolve_page(article_name)
                        article_key = page.site.key + ':' + page.name

                        ufound = False
                        for u in self.users:
                            if u.name == uname:
                                logger.info('- [%s] %s', uname, article_key)
                                u.disqualified_articles.append(article_key)
                                ufound = True
                        if not ufound:
                            raise InvalidContestPage(_('Could not find the user %(user)s given to the {{tl|%(template)s}} template.') % {'user': uname, 'template': dicfg['name']})

        pocfg = self.config['templates']['penalty']
        if pocfg['name'] in dp.templates:
            for templ in dp.templates[pocfg['name']]:
                uname = cleanup_input(templ.parameters[1].value)
                revid = int(cleanup_input(templ.parameters[2].value))
                site_key = ''
                if 'site' in templ.parameters:
                    site_key = cleanup_input(templ.parameters['site'].value)

                site = self.sites.from_prefix(site_key)
                if site is None:
                    raise InvalidContestPage(_('Failed to parse the %(template)s template: Did not find a site matching the site prefix %(prefix)s') % {
                        'template': pocfg['name'],
                        'prefix': site_key,
                    })

                points = float(cleanup_input(templ.parameters[3].value).replace(',', '.'))
                reason = cleanup_input(templ.parameters[4].value)
                ufound = False
                logger.info('Point deduction: %d points to "%s" for revision %s:%s. Reason: %s', points, uname, site.key, revid, reason)
                for u in self.users:
                    if u.name == uname:
                        u.point_deductions.append({
                            'site': site.key,
                            'revid': revid,
                            'points': points,
                            'reason': reason,
                        })
                        ufound = True
                if not ufound:
                    raise InvalidContestPage(_("Couldn't find the user %(user)s given to the {{tl|%(template)s}} template.") % {
                        'user': uname,
                        'template': pocfg['name'],
                    })

        pocfg = self.config['templates']['bonus']
        if pocfg['name'] in dp.templates:
            for templ in dp.templates[pocfg['name']]:
                uname = cleanup_input(templ.parameters[1].value)
                revid = int(cleanup_input(templ.parameters[2].value))
                site_key = ''
                if 'site' in templ.parameters:
                    site_key = cleanup_input(templ.parameters['site'].value)

                site = None
                for s in self.sites.sites.values():
                    if s.match_prefix(site_key):
                        site = s
                        break

                if site is None:
                    raise InvalidContestPage(_('Failed to parse the %(template)s template: Did not find a site matching the site prefix %(prefix)s') % {
                        'template': pocfg['name'],
                        'prefix': site_key,
                    })

                points = float(cleanup_input(templ.parameters[3].value).replace(',', '.'))
                reason = cleanup_input(templ.parameters[4].value)
                ufound = False
                logger.info('Point addition: %d points to %s for revision %s:%s. Reason: %s', points, uname, site.key, revid, reason)
                for u in self.users:
                    if u.name == uname:
                        u.point_deductions.append({
                            'site': site.key,
                            'revid': revid,
                            'points': -points,
                            'reason': reason
                        })
                        ufound = True
                if not ufound:
                    raise InvalidContestPage(_("Couldn't find the user %(user)s given to the {{tl|%(template)s}} template.") % {
                        'user': uname,
                        'template': pocfg['name'],
                    })

        return rules, filters

    def prepare_plotdata(self, results):
        if 'plot' not in self.config:
            return

        plotdata = []
        for result in results:
            tmp = {'name': result['name'], 'values': []}
            for point in result['plotdata']:
                tmp['values'].append({'x': point[0], 'y': point[1]})
            plotdata.append(tmp)

        if 'datafile' in self.config['plot']:
            filename = os.path.join(self.project_dir, self.config['plot']['datafile'] % {'year': self.year, 'week': self.startweek, 'month': self.month})
            with open(filename, 'w') as fp:
                json.dump(plotdata, fp)

        return plotdata

    def plot(self, plotdata):
        if 'plot' not in self.config:
            return
        import matplotlib.pyplot as plt

        w = 20 / 2.54
        goldenratio = 1.61803399
        h = w / goldenratio
        fig = plt.figure(figsize=(w, h))

        ax = fig.add_subplot(1, 1, 1, frame_on=True)
        # ax.grid(True, which='major', color='gray', alpha=0.5)
        fig.subplots_adjust(left=0.10, bottom=0.09, right=0.65, top=0.94)

        t0 = float(unix_time(self.start))

        datediff = self.end.date() - self.start.date()  # Compare just dates to avoid issues with daylight saving time
        ndays = datediff.days + 1

        xt = t0 + np.arange(ndays + 1) * 86400
        xt_mid = t0 + 43200 + np.arange(ndays) * 86400

        now = float(unix_time(self.server_tz.localize(datetime.now()).astimezone(pytz.utc)))

        yall = []
        cnt = 0

        for result in plotdata:
            x = [t['x'] for t in result['values']]
            y = [t['y'] for t in result['values']]

            if len(x) > 0:
                cnt += 1
                yall.extend(y)
                x.insert(0, xt[0])
                y.insert(0, 0)
                if now < xt[-1]:
                    x.append(now)
                    y.append(y[-1])
                else:
                    x.append(xt[-1])
                    y.append(y[-1])
                l = ax.plot(x, y, linewidth=1.2, label=result['name'])  # markerfacecolor='#FF8C00', markeredgecolor='#888888', label = u['name'])
                c = l[0].get_color()
                #ax.plot(x[1:-1], y[1:-1], marker='.', markersize=4, markerfacecolor=c, markeredgecolor=c, linewidth=0., alpha=0.5)  # markerfacecolor='#FF8C00', markeredgecolor='#888888', label = u['name'])
                if cnt >= 15:
                    break

        if now < xt[-1]:   # showing vertical line indicating when the plot was updated
            ax.axvline(now, color='black', alpha=0.5)

        abday = [calendar.day_abbr[x] for x in [0, 1, 2, 3, 4, 5, 6]]

        x_ticks_major_size = 5
        x_ticks_minor_size = 0

        if ndays == 7:
            # Tick marker every midnight
            ax.set_xticks(xt, minor=False)
            ax.set_xticklabels([], minor=False)

            # Tick labels at the middle of every day
            ax.set_xticks(xt_mid, minor=True)
            ax.set_xticklabels(abday, minor=True)
        elif ndays == 14:
            # Tick marker every midnight
            ax.set_xticks(xt, minor=False)
            ax.set_xticklabels([], minor=False)

            # Tick labels at the middle of every day
            ax.set_xticks(xt_mid, minor=True)
            ax.set_xticklabels([abday[0], '', abday[2], '', abday[4], '', abday[6], '', abday[1], '', abday[3], '', abday[5], ''], minor=True)
        elif ndays > 14:

            # Tick marker every week
            x_ticks_major_labels = np.arange(0, ndays + 1, 7)
            x_ticks_major = t0 + x_ticks_major_labels * 86400
            ax.set_xticks(x_ticks_major, minor=False)
            ax.set_xticklabels(x_ticks_major_labels, minor=False)

            # Tick every day
            x_ticks_minor = t0 + np.arange(ndays + 1) * 86400
            ax.set_xticks(x_ticks_minor, minor=True)
            x_ticks_minor_size = 3

            # ax.set_xticklabels(['1', '', '', '', '5', '', '', '', '', '10', '', '', '', '', '15', '', '', '', '', '20', '', '', '', '', '25', '', '', '', '', '30'], minor=True)
        # elif ndays == 31:
        #     ax.set_xticklabels(['1', '', '', '', '5', '', '', '', '', '10', '', '', '', '', '15', '', '', '', '', '20', '', '', '', '', '25', '', '', '', '', '', '31'], minor=True)



        for i in range(1, ndays, 2):
            ax.axvspan(xt[i], xt[i + 1], facecolor='#000099', linewidth=0., alpha=0.03)

        for i in range(0, ndays, 2):
            ax.axvspan(xt[i], xt[i + 1], facecolor='#000099', linewidth=0., alpha=0.07)

        for line in ax.xaxis.get_ticklines(minor=False):
            line.set_markersize(x_ticks_major_size)

        for line in ax.xaxis.get_ticklines(minor=True):
            line.set_markersize(x_ticks_minor_size)

        for line in ax.yaxis.get_ticklines(minor=False):
            line.set_markersize(x_ticks_major_size)

        if len(yall) > 0:
            ax.set_xlim(t0, xt[-1])
            ax.set_ylim(0, 1.05 * np.max(yall))

            ax.set_xlabel(_('Day'))
            ax.set_ylabel(_('Points'))

            now = self.server_tz.localize(datetime.now())
            now2 = now.astimezone(self.wiki_tz).strftime(_('%e. %B %Y, %H:%M'))
            ax_title = _('Updated %(date)s')

            #print ax_title.encode('utf-8')
            #print now2.encode('utf-8')
            ax_title = ax_title % {'date': now2}
            ax.set_title(ax_title)

            plt.legend()
            ax = plt.gca()
            ax.legend(
                # ncol = 4, loc = 3, bbox_to_anchor = (0., 1.02, 1., .102), mode = "expand", borderaxespad = 0.
                loc=2, bbox_to_anchor=(1.0, 1.0), borderaxespad=0., frameon=0.
            )
            figname = os.path.join(self.project_dir, self.config['plot']['figname'] % {'year': self.year, 'week': self.startweek, 'month': self.month})
            plt.savefig(figname, dpi=200)
            logger.info('Wrote plot: %s', figname)

    def format_msg(self, template_name, awards):
        template = self.config['award_messages'][template_name]
        arg_yes = self.config['templates']['commonargs'][True]
        arg_endweek = self.config['templates']['commonargs']['week2']
        args = {
            'year': str(self.year),
            'week': str(self.startweek),
            'month': str(self.month),
            'awards': '|'.join(['%s=%s' % (award, arg_yes) for award in awards]),
        }
        if self.startweek != self.endweek:
            args['week'] += '|%s=%s' % (arg_endweek, self.endweek)

        return template % args

    def format_heading(self):
        if self.config.get('contest_type') == 'weekly':
            if self.startweek == self.endweek:
                return _('Weekly contest for week %(week)d') % {'week': self.startweek}
            else:
                return _('Weekly contest for week %(startweek)d–%(endweek)d') % {'startweek': self.startweek, 'endweek': self.endweek}
        else:
            return self.config.get('name') % {'month': self.month, 'year': self.year}

    def deliver_message(self, username, topic, body, sig='~~~~'):
        logger.info('Delivering message to %s', username)

        prefix = self.sites.homesite.namespaces[3]
        prefixed = prefix + ':' + username

        res = self.sites.homesite.api(action='query', prop='flowinfo', titles=prefixed)
        pageinfo = list(res['query']['pages'].values())[0]
        flow_enabled = 'missing' not in pageinfo and 'enabled' in pageinfo['flowinfo']['flow']

        pagename = '%s:%s' % (prefix, username)

        if flow_enabled:
            token = self.sites.homesite.get_token('csrf')
            self.sites.homesite.api(action='flow',
                              submodule='new-topic',
                              page=pagename,
                              nttopic=topic,
                              ntcontent=body,
                              ntformat='wikitext',
                              token=token)

        else:
            page = self.sites.homesite.pages[pagename]
            page.save(text=body + ' ' + sig, bot=False, section='new', summary=topic)

    def deliver_prices(self, results, simulate=False):
        config = self.config
        heading = self.format_heading()

        cur = self.sql.cursor()
        cur.execute('SELECT contest_id FROM contests WHERE site=%s AND name=%s', [self.sites.homesite.key, self.name])
        contest_id = cur.fetchall()[0][0]

        logger.info('Delivering prices for contest %d' % (contest_id,))

        # self.sql.commit()
        # cur.close()

        for i, result in enumerate(results):
            prices = []

            if i == 0:
                # Contest winenr
                for price in self.prices:
                    # Is there's a special winner's prize?
                    if price[1] == 'winner':
                        prices.append(price[0])

            # Append the first point limit price, if any
            for price in self.prices:
                if price[1] == 'pointlimit' and result['points'] >= price[2]:
                    prices.append(price[0])
                    break

            if len(prices) == 0:
                logger.info('No price for %s', result['name'])
                continue

            now = self.server_tz.localize(datetime.now())
            now_l = now.astimezone(self.wiki_tz)
            dateargs = {
                'year': now_l.year,
                'week': now_l.isocalendar()[1],
                'month': now_l.month,
            }
            userprefix = self.sites.homesite.namespaces[2]

            tpl = 'winner_template' if i == 0 else 'participant_template'
            msg = self.format_msg(tpl, prices) + '\n'
            msg += self.config['award_messages']['reminder_msg'] % {
                'url': self.config['pages']['default'] % dateargs,
                **dateargs,
            }
            sig = _('Regards') + ' ' + ', '.join(['[[%s:%s|%s]]' % (userprefix, s, s) for s in self.ledere]) + ' ' + _('and') + ' ~~~~'

            if not simulate:
                cur.execute('SELECT prize_id FROM prizes WHERE contest_id=%s AND site=%s AND user=%s', [contest_id, self.sites.homesite.key, result['name']])
                rows = cur.fetchall()
                if len(rows) == 0:
                    self.deliver_message(result['name'], heading, msg, sig)
                    cur.execute('INSERT INTO prizes (contest_id, site, user, timestamp) VALUES (%s,%s,%s, NOW())', [contest_id, self.sites.homesite.key, result['name']])
                    self.sql.commit()

    def deliver_ended_contest_notification(self):
        if 'awardstatus' not in self.config:
            return

        heading = self.format_heading()
        args = {
            'prefix': self.sites.homesite.site['server'] + self.sites.homesite.site['script'],
            'page': self.config['awardstatus']['pagename'],
            'title': urllib.parse.quote(self.config['awardstatus']['send'])
        }
        link = '%(prefix)s?title=%(page)s&action=edit&section=new&preload=%(page)s/Preload&preloadtitle=%(title)s' % args
        usertalkprefix = self.sites.homesite.namespaces[3]
        awards = []
        for key, award in self.config['awards'].items():
            if 'organizer' in award:
                awards.append(key)
        if len(awards) == 0:
            raise Exception('No organizer award found in config')
        for u in self.ledere:
            mld = self.format_msg('organizer_template', awards)
            mld += _('Now you must check if the results look ok. If there are error messages at the bottom of the [[%(page)s|contest page]], you should check that the related contributions have been awarded the correct number of points. Also check if there are comments or complaints on the discussion page. If everything looks fine, [%(link)s click here] (and save) to indicate that I can send out the awards at first occasion.') % {'page': self.name, 'link': link}
            sig = _('Thanks, ~~~~')

            logger.info('Delivering notification about ended contenst to the contest organizers')
            self.deliver_message(u, heading, mld, sig)

    def deliver_receipt_to_leaders(self):
        heading = self.format_heading()
        usertalkprefix = self.sites.homesite.namespaces[3]

        args = {'prefix': self.sites.homesite.site['server'] + self.sites.homesite.site['script'], 'page': 'Special:Contributions'}
        link = '%(prefix)s?title=%(page)s&contribs=user&target=UKBot&namespace=3' % args
        mld = '\n:' + _('Awards have been [%(link)s sent out].') % {'link': link}
        for u in self.ledere:
            page = self.sites.homesite.pages['%s:%s' % (usertalkprefix, u)]
            logger.info('Leverer kvittering til %s', page.name)

            # Find section number
            txt = page.text()
            sections = [s.strip() for s in re.findall(r'^[\s]*==([^=]+)==', txt, flags=re.M)]
            try:
                csection = sections.index(heading) + 1
            except ValueError:
                logger.error('Fant ikke "%s" i "%s', heading, page.name)
                return

            # Append text to section
            txt = page.text(section=csection)
            page.save(appendtext=mld, bot=False, summary='== ' + heading + ' ==')

    def delete_contribs_from_db(self):
        cur = self.sql.cursor()
        cur2 = self.sql.cursor()
        ts_start = self.start.astimezone(pytz.utc).strftime('%F %T')
        ts_end = self.end.astimezone(pytz.utc).strftime('%F %T')
        ndel = 0
        cur.execute(u"SELECT site,revid,parentid FROM contribs WHERE timestamp >= %s AND timestamp <= %s", (ts_start, ts_end))
        for row in result_iterator(cur):
            cur2.execute(u"DELETE FROM fulltexts WHERE site=%s AND revid=%s", [row[0], row[1]])
            ndel += cur2.rowcount
            cur2.execute(u"DELETE FROM fulltexts WHERE site=%s AND revid=%s", [row[0], row[2]])
            ndel += cur2.rowcount

        cur.execute('SELECT COUNT(*) FROM fulltexts')
        nremain = cur.fetchone()[0]
        logger.info('Cleaned %d rows from fulltexts-table. %d rows remain', ndel, nremain)

        cur.execute(u"""DELETE FROM contribs WHERE timestamp >= %s AND timestamp <= %s""", (ts_start, ts_end))
        ndel = cur.rowcount
        cur.execute('SELECT COUNT(*) FROM contribs')
        nremain = cur.fetchone()[0]
        logger.info('Cleaned %d rows from contribs-table. %d rows remain', ndel, nremain)

        cur.close()
        cur2.close()
        self.sql.commit()

    def deliver_warnings(self, simulate=False):
        """
        Inform users about problems with their contribution(s)
        """
        usertalkprefix = self.sites.homesite.namespaces[3]
        cur = self.sql.cursor()
        for u in self.users:
            msgs = []
            if u.suspended_since is not None:
                d = [self.sites.homesite.key, self.name, u.name, 'suspension', '']
                cur.execute('SELECT id FROM notifications WHERE site=%s AND contest=%s AND user=%s AND class=%s AND args=%s', d)
                if len(cur.fetchall()) == 0:
                    msgs.append('Du er inntil videre suspendert fra konkurransen med virkning fra %s. Dette innebærer at dine bidrag gjort etter dette tidspunkt ikke teller i konkurransen, men alle bidrag blir registrert og skulle suspenderingen oppheves i løpet av konkurranseperioden vil også bidrag gjort i suspenderingsperioden telle med. Vi oppfordrer deg derfor til å arbeide med problemene som førte til suspenderingen slik at den kan oppheves.' % u.suspended_since.strftime(_('%e. %B %Y, %H:%M')))
                    if not simulate:
                        cur.execute('INSERT INTO notifications (site, contest, user, class, args) VALUES (%s,%s,%s,%s,%s)', d)
            discs = []
            for article_key, article in u.articles.items():
                if article.disqualified:
                    d = [self.sites.homesite.key, self.name, u.name, 'disqualified', article_key]
                    cur.execute('SELECT id FROM notifications WHERE site=%s AND contest=%s AND user=%s AND class=%s AND args=%s', d)
                    if len(cur.fetchall()) == 0:
                        discs.append('[[:%s|%s]]' % (article_key, article.name))
                        if not simulate:
                            cur.execute('INSERT INTO notifications (site, contest, user, class, args) VALUES (%s,%s,%s,%s,%s)', d)
            if len(discs) > 0:
                if len(discs) == 1:
                    s = discs[0]
                else:
                    s = ', '.join(discs[:-1]) + ' og ' + discs[-1]
                msgs.append('Bidragene dine til %s er diskvalifisert fra konkurransen. En diskvalifisering kan oppheves hvis du selv ordner opp i problemet som førte til diskvalifiseringen. Hvis andre brukere ordner opp i problemet er det ikke sikkert at den vil kunne oppheves.' % s)

            if len(msgs) > 0:
                if self.startweek == self.endweek:
                    heading = '== Viktig informasjon angående Ukens konkurranse uke %d ==' % self.startweek
                else:
                    heading = '== Viktig informasjon angående Ukens konkurranse uke %d–%d ==' % (self.startweek, self.endweek)
                #msg = 'Arrangøren av denne [[%(pagename)s|ukens konkurranse]] har registrert problemer ved noen av dine bidrag:
                #så langt. Det er dessverre registrert problemer med enkelte av dine bidrag som medfører at vi er nødt til å informere deg om følgende:\n' % { 'pagename': self.name }

                msg = ''.join(['* %s\n' % m for m in msgs])
                msg += 'Denne meldingen er generert fra anmerkninger gjort av konkurransearrangør på [[%(pagename)s|konkurransesiden]]. Du finner mer informasjon på konkurransesiden og/eller tilhørende diskusjonsside. Så lenge konkurransen ikke er avsluttet, kan problemer løses i løpet av konkurransen. Om du ønsker det, kan du fjerne denne meldingen når du har lest den. ~~~~' % {'pagename': self.name}

                #print '------------------------------',u.name
                #print msg
                #print '------------------------------'

                page = self.sites.homesite.pages['%s:%s' % (usertalkprefix, u.name)]
                logger.info('Leverer advarsel til %s', page.name)
                if simulate:
                    logger.info(msg)
                else:
                    page.save(text=msg, bot=False, section='new', summary=heading)
            self.sql.commit()

    def run(self, simulate=False, output=''):
        config = self.config

        if not self.page.exists:
            logger.error('Contest page [[%s]] does not exist! Exiting', self.page.name)
            return

        # Loop over users

        narticles = 0

        stats = []

        # extraargs = {'namespace': 0}
        extraargs = {}
        # host_filter = None
        # for f in self.filters:
        #     if isinstance(f, NamespaceFilter):
        #         extraargs['namespace'] = '|'.join(f.namespaces)
        #         host_filter = f.site

        article_errors = {}
        results = []

        while True:
            if len(self.users) == 0:
                break
            user = self.users.pop()

            logger.info('=== User:%s ===', user.name)

            # First read contributions from db
            user.add_contribs_from_db(self.sql, self.start, self.end, self.sites.sites)

            # Then fill in new contributions from wiki
            for site in self.sites.sites.values():

                # if host_filter is None or site.host == host_filter:
                user.add_contribs_from_wiki(site, self.start, self.end, fulltext=True, **extraargs)

            # And update db
            user.save_contribs_to_db(self.sql)

            user.backfill_article_creation_dates(self.sql)

            try:

                # Filter out relevant articles
                user.filter(self.filters)

                # And calculate points
                logger.info('Calculating points')
                tp0 = time.time()
                user.analyze(self.rules)
                tp1 = time.time()
                logger.info('%s: %.f points (calculated in %.1f secs)', user.name,
                            user.contributions.sum(), tp1 - tp0)

                stats.extend(user.count_bytes_per_site())
                stats.extend(user.count_words_per_site())
                stats.extend(user.count_pages_per_site())
                stats.extend(user.count_newpages_per_site())

                tp2 = time.time()
                logger.info('Wordcount done in %.1f secs', tp2 - tp1)

                for article in user.articles.values():
                    k = article.link()
                    if len(article.errors) > 0:
                        article_errors[k] = article.errors
                    for rev in article.revisions.values():
                        if len(rev.errors) > 0:
                            if k in article_errors:
                                article_errors[k].extend(rev.errors)
                            else:
                                article_errors[k] = rev.errors

                results.append({
                    'name': user.name,
                    'points': user.contributions.sum(),
                    'result': user.contributions.format(homesite=self.sites.homesite),
                    'plotdata': user.plotdata,
                })

            except InvalidContestPage as e:
                err = "\n* '''%s'''" % e.msg
                out = '\n{{%s | error | %s }}' % (config['templates']['botinfo'], err)
                if simulate:
                    logger.error(out)
                else:
                    self.page.save('dummy', summary=_('UKBot encountered a problem'), appendtext=out)
                raise

            del user

        # Sort users by points

        logger.info('Sorting contributions and preparing contest page')

        results.sort(key=lambda x: x['points'], reverse=True)

        # Make outpage

        out = ''
        #out += '[[File:Nowp Ukens konkurranse %s.svg|thumb|400px|Resultater (oppdateres normalt hver natt i halv ett-tiden, viser kun de ti med høyest poengsum)]]\n' % self.start.strftime('%Y-%W')

        summary_tpl = None
        if 'status' in config['templates']:

            summary_tpl_args = ['|pages=%d' % sum_stats_by(stats, key='pages')]

            trn = 0
            for rule in self.rules:
                if isinstance(rule, NewPageRule):
                    summary_tpl_args.append('%s=%d' % (rule.key, sum_stats_by(stats, key='newpages')))
                elif isinstance(rule, ByteRule):
                    nbytes = sum_stats_by(stats, key='bytes')
                    if nbytes >= 10000:
                        summary_tpl_args.append('kilo%s=%.f' % (rule.key, nbytes / 1000.))
                    else:
                        summary_tpl_args.append('%s=%d' % (rule.key, nbytes))
                elif isinstance(rule, WordRule):
                    summary_tpl_args.append('%s=%d' % (rule.key, sum_stats_by(stats, key='words')))
                elif isinstance(rule, RefRule):
                    summary_tpl_args.append('%s=%d' % (rule.key, rule.totalsources))
                # elif isinstance(rule, RefSectionFiRule):
                #     summary_tpl_args.append('|%s=%d' % (rule.key, rule.total)
                elif isinstance(rule, ImageRule):
                    summary_tpl_args.append('%s=%d' % (rule.key, rule.total))
                elif isinstance(rule, TemplateRemovalRule):
                    for tpl in rule.templates:
                        trn += 1
                        summary_tpl_args.append('%(key)s%(idx)d=%(tpl)s' % {'key': rule.key, 'idx': trn, 'tpl': tpl['name']})
                        summary_tpl_args.append('%(key)s%(idx)dn=%(cnt)d' % {'key': rule.key, 'idx': trn, 'cnt': tpl['total']})

            summary_tpl = '{{%s|%s}}' % (config['templates']['status'], '|'.join(summary_tpl_args))

        now = self.server_tz.localize(datetime.now())
        if self.state == STATE_ENDING:
            # Konkurransen er nå avsluttet – takk til alle som deltok! Rosetter vil bli delt ut så snart konkurransearrangøren(e) har sjekket resultatene.
            out += "''" + _('This contest is closed – thanks to everyone who participated! Awards will be sent out as soon as the contest organizer has checked the results.') + "''\n\n"
        elif self.state == STATE_CLOSING:
            out += "''" + _('This contest is closed – thanks to everyone who participated!') + "''\n\n"
        else:
            oargs = {
                'lastupdate': now.astimezone(self.wiki_tz).strftime(_('%e. %B %Y, %H:%M')),
                'startdate': self.start.strftime(_('%e. %B %Y, %H:%M')),
                'enddate': self.end.strftime(_('%e. %B %Y, %H:%M'))
            }
            out += "''" + _('Last updated %(lastupdate)s. The contest is open from %(startdate)s to %(enddate)s.') % oargs + "''\n\n"

        for i, result in enumerate(results):
            awards = ''
            if self.state == STATE_CLOSING:
                if i == 0:
                    for price in self.prices:
                        if price[1] == 'winner':
                            awards += '[[File:%s|20px]] ' % config['awards'][price[0]]['file']
                            break
                for price in self.prices:
                    if price[1] == 'pointlimit' and result['points'] >= price[2]:
                        awards += '[[File:%s|20px]] ' % config['awards'][price[0]]['file']
                        break
            out += result['result'].replace('{awards}', awards)

        errors = []
        for art, err in article_errors.items():
            if len(err) > 8:
                err = err[:8]
                err.append('(...)')
            errors.append('\n* ' + _('UKBot encountered the following problems with the page [[%s]]') % art + ''.join(['\n** %s' % e for e in err]))

        for site in self.sites.sites.values():
            for error in site.errors:
                errors.append('\n* %s' % error)

        if len(errors) == 0:
            out += '{{%s | ok | %s }}' % (config['templates']['botinfo'], now.astimezone(self.wiki_tz).strftime('%F %T'))
        else:
            out += '{{%s | 1=note | 2=%s | 3=%s }}' % (config['templates']['botinfo'], now.astimezone(self.wiki_tz).strftime('%F %T'), ''.join(errors))

        out += '\n' + config['contestPages']['footer'] % {'year': self.year} + '\n'

        ib = config['templates']['infobox']

        if not simulate:
            txt = self.page.text()
            tp = TemplateEditor(txt)

            if summary_tpl is not None:
                tp.templates[ib['name']][0].parameters[ib['status']] = summary_tpl
            txt = tp.wikitext()
            secstart = -1
            secend = -1

            # Check if <!-- Begin:ResultsSection --> exists first
            try:
                trs1 = next(re.finditer(r'<!--\s*Begin:ResultsSection\s*-->', txt, re.I))
                trs2 = next(re.finditer(r'<!--\s*End:ResultsSection\s*-->', txt, re.I))
                secstart = trs1.end()
                secend = trs2.start()

            except StopIteration:
                if 'resultsSection' not in config['contestPages']:
                    raise InvalidContestPage(_('Results markers %(start_marker)s and %(end_marker)s not found') % {
                        'start_marker': '<!-- Begin:ResultsSection -->', 
                        'end_marker': '<!-- End:ResultsSection -->',
                    })
                for s in re.finditer(r'^[\s]*==([^=]+)==[\s]*\n', txt, flags=re.M):
                    if s.group(1).strip() == config['contestPages']['resultsSection']:
                        secstart = s.end()
                    elif secstart != -1:
                        secend = s.start()
                        break
            if secstart == -1:
                raise InvalidContestPage(_('No "%(section_name)s" section found.') % {
                    'section_name': config['contestPages']['resultsSection'], 
                })
            if secend == -1:
                txt = txt[:secstart] + out
            else:
                txt = txt[:secstart] + out + txt[secend:]

            logger.info('Updating wiki')
            if self.state == STATE_ENDING:
                self.page.save(txt, summary=_('Updating with final results, the contest is now closed.'))
            elif self.state == STATE_CLOSING:
                self.page.save(txt, summary=_('Checking results and handing out awards'))
            else:
                self.page.save(txt, summary=_('Updating'))

        if output != '':
            logger.info("Writing output to file")
            f = codecs.open(output, 'w', 'utf-8')
            f.write(out)
            f.close()

        if self.state == STATE_ENDING:
            logger.info('Ending contest')
            if not simulate:
                if 'awardstatus' in config:
                    aws = config['awardstatus']
                    page = self.sites.homesite.pages[aws['pagename']]
                    page.save(text=aws['wait'], summary=aws['wait'], bot=True)

                cur = self.sql.cursor()
                cur.execute('UPDATE contests SET ended=1 WHERE site=%s AND name=%s', [self.sites.homesite.key, self.name])
                self.sql.commit()
                count = cur.rowcount
                cur.close()

                if count == 0:
                    logger.info('Leader notifications have already been delivered')
                else:
                    self.deliver_ended_contest_notification()

        if self.state == STATE_CLOSING:
            logger.info('Delivering prices')

            self.deliver_prices(results, simulate)

            cur = self.sql.cursor()

            # Aggregate stats
            stats_agg = {}
            for stat in stats:
                if stat['key'] not in stats_agg:
                    stats_agg[stat['key']] = {}
                if stat['site'] not in stats_agg[stat['key']]:
                    stats_agg[stat['key']][stat['site']] = 0
                stats_agg[stat['key']][stat['site']] += stat['value']

            if not simulate:

                # Store stats
                for result in results:
                    cur.execute(
                        'INSERT INTO users (site, contest, user, points, bytes, pages, newpages) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                        [
                            self.sites.homesite.key,
                            self.name,
                            result['name'],
                            result['points'],
                            sum_stats_by(stats, user=result['name'], key='bytes'),
                            sum_stats_by(stats, user=result['name'], key='pages'),
                            sum_stats_by(stats, user=result['name'], key='newpages'),
                        ]
                    )

                    for dimension, values in stats_agg.items():
                        for contribsite, value in values.items():
                            cur.execute(
                                'INSERT INTO stats (contestsite, contest, contribsite, dimension, value) VALUES (%s,%s,%s,%s,%s)',
                                [
                                    self.sites.homesite.key,
                                    self.name,
                                    contribsite,
                                    dimension,
                                    value,
                                ]
                            )
                cur.execute(
                    'UPDATE contests SET closed=1 WHERE site=%s AND name=%s',
                    [self.sites.homesite.key, self.name]
                )
                self.sql.commit()

            cur.close()

            aws = config['awardstatus']
            page = self.sites.homesite.pages[aws['pagename']]
            page.save(text=aws['sent'], summary=aws['sent'], bot=True)

            # if not simulate:
            #
            # Skip for now: not Flow compatible
            #     self.deliver_receipt_to_leaders()

            logger.info('Cleaning database')
            if not simulate:
                self.delete_contribs_from_db()

        # Notify users about issues

        # self.deliver_warnings(simulate=simulate)

        # Update Wikipedia:Portal/Oppslagstavle

        if 'noticeboard' in config:
            boardname = config['noticeboard']['name']
            boardtpl = config['noticeboard']['template']
            commonargs = config['templates']['commonargs']
            tplname = boardtpl['name']
            oppslagstavle = self.sites.homesite.pages[boardname]
            txt = oppslagstavle.text()

            dp = TemplateEditor(txt)
            ntempl = len(dp.templates[tplname])
            if ntempl != 1:
                raise Exception('Feil: Fant %d %s-maler i %s' % (ntempl, tplname, boardname))

            tpl = dp.templates[tplname][0]
            now2 = now.astimezone(self.wiki_tz)
            if int(tpl.parameters['uke']) != int(now2.strftime('%V')):
                logger.info('Updating noticeboard: %s', boardname)
                tpllist = config['templates']['contestlist']
                commonargs = config['templates']['commonargs']
                tema = self.sites.homesite.api('parse', text='{{subst:%s|%s=%s}}' % (tpllist['name'], commonargs['week'], now2.strftime('%Y-%V')), pst=1, onlypst=1)['parse']['text']['*']
                tpl.parameters[1] = tema
                tpl.parameters[boardtpl['date']] = now2.strftime('%e. %h')
                tpl.parameters[commonargs['year']] = now2.isocalendar()[0]
                tpl.parameters[commonargs['week']] = now2.isocalendar()[1]
                txt2 = dp.wikitext()
                if txt != txt2:
                    if not simulate:
                        oppslagstavle.save(txt2, summary=_('The weekly contest is: %(link)s') % {'link': tema})

        # Make a nice plot

        if 'plot' in config:
            plotdata = self.prepare_plotdata(results)
            self.plot(plotdata)

            if self.state == STATE_ENDING:
                self.uploadplot(simulate)

    def uploadplot(self, simulate=False, output=''):
        if not self.page.exists:
            logger.error('Contest page [[%s]] does not exist! Exiting', self.page.name)
            return

        if not 'plot' in self.config:
            return

        figname = self.config['plot']['figname'] % {
            'year': self.year,
            'week': self.startweek,
            'month': self.month,
        }
        remote_filename = os.path.basename(figname).replace(' ', '_')
        local_filename = os.path.join(self.project_dir, figname)

        if not os.path.isfile(local_filename):
            logger.error('File "%s" was not found', local_filename)
            sys.exit(1)

        weeks = '%d' % self.startweek
        if self.startweek != self.endweek:
            weeks += '-%s' % self.endweek

        pagetext = self.config['plot']['description'] % {
            'pagename': self.name,
            'week': weeks,
            'year': self.year,
            'month': self.month,
            'start': self.start.strftime('%F')
        }

        logger.info('Uploading: %s', figname)
        commons = mwclient.Site('commons.wikimedia.org',
                                consumer_token=os.getenv('MW_CONSUMER_TOKEN'),
                                consumer_secret=os.getenv('MW_CONSUMER_SECRET'),
                                access_token=os.getenv('MW_ACCESS_TOKEN'),
                                access_secret=os.getenv('MW_ACCESS_SECRET'))
        file_page = commons.pages['File:' + remote_filename]

        if simulate:
            return

        with open(local_filename.encode('utf-8'), 'rb') as file_buf:
            if not file_page.exists:
                logger.info('Adding plot')
                res = commons.upload(file_buf, remote_filename,
                                     comment='Bot: Uploading new plot',
                                     description=pagetext,
                                     ignore=True)
                logger.info(res)
            else:
                logger.info('Updating plot')
                res = commons.upload(file_buf, remote_filename,
                                     comment='Bot: Updating plot',
                                     ignore=True)
                logger.info(res)


def award_delivery_confirmed(site, config, page_title):
    status_page = site.pages[config['pagename']]
    confirmation_message = config['send']

    if status_page.exists:
        lastrev = status_page.revisions(prop='user|comment|content', slots='main').next()
        if lastrev['comment'].find('/* %s */' % confirmation_message) == -1 and lastrev['slots']['main']['*'].find(confirmation_message) == -1:
            logger.info('Contest [[%s]] is to be closed, but award delivery has not been confirmed yet', page_title)
        else:
            logger.info('Will close contest [[%s]], award delivery has been confirmed', page_title)
            return True


def parse_infobox(page_text, userprefix, config, wiki_tz):
    infobox_cfg = config['templates']['infobox']
    common_cfg = config['templates']['commonargs']
    award_cfg = config.get('awards', {})

    parsed = {'name': infobox_cfg['name']}

    te = TemplateEditor(page_text)
    infobox = te.templates[infobox_cfg['name']][0]

    # Start time / end time

    if infobox.has_param(common_cfg['year']) and infobox.has_param(common_cfg['week']):
        year = int(cleanup_input(infobox.parameters[common_cfg['year']].value))
        startweek = int(cleanup_input(infobox.parameters[common_cfg['week']].value))
        if infobox.has_param(common_cfg['week2']):
            endweek = cleanup_input(infobox.parameters[common_cfg['week2']].value)
            if endweek == '':
                endweek = startweek
        else:
            endweek = startweek
        endweek = int(endweek)

        startweek = Week(year, startweek)
        endweek = Week(year, endweek)
        parsed['start_time'] = wiki_tz.localize(datetime.combine(startweek.monday(), dt_time(0, 0, 0)))
        parsed['end_time'] = wiki_tz.localize(datetime.combine(endweek.sunday(), dt_time(23, 59, 59)))
    else:
        start_value = cleanup_input(infobox.parameters[infobox_cfg['start']].value)
        end_value = cleanup_input(infobox.parameters[infobox_cfg['end']].value)
        parsed['start_time'] = wiki_tz.localize(datetime.strptime(start_value + ' 00 00 00', '%Y-%m-%d %H %M %S'))
        parsed['end_time'] = wiki_tz.localize(datetime.strptime(end_value + ' 23 59 59', '%Y-%m-%d %H %M %S'))

    # Organizers

    parsed['organizers'] = []
    if infobox_cfg['organizer'] in infobox.parameters:
        parsed['organizers'] = re.findall(
            r'\[\[(?:User|%s):([^\|\]]+)' % userprefix,
            cleanup_input(infobox.parameters[infobox_cfg['organizer']].value),
            flags=re.I
        )

    # Awards

    parsed['awards'] = []
    for award_name in award_cfg.keys():
        if infobox.has_param(award_name):
            award_value = cleanup_input(infobox.parameters[award_name].value)
            if award_value != '':
                award_value = award_value.lower().replace('&nbsp;', ' ').split()[0]
                if award_value == infobox_cfg['winner'].lower():
                    parsed['awards'].append([award_name, 'winner', 0])
                elif award_value != '':
                    try:
                        parsed['awards'].append([award_name, 'pointlimit', int(award_value)])
                    except ValueError:
                        pass
                        #raise InvalidContestPage('Klarte ikke tolke verdien til parameteren %s gitt til {{tl|infoboks ukens konkurranse}}.' % col)

    winner_awards = [k for k, v in award_cfg.items() if v.get('winner') is True]
    if len(parsed['awards']) != 0 and 'winner' not in [award[1] for award in parsed['awards']]:
        winner_awards = ', '.join(['{{para|%s|%s}}' % (k, infobox_cfg['winner']) for k in winner_awards])
        # raise InvalidContestPage(_('Found no winner award in {{tl|%(template)s}}. Winner award is set by one of the following: %(awards)s.') % {'template': ibcfg['name'], 'awards': winner_awards})
        logger.warning(
            'Found no winner award in {{tl|%s}}. Winner award is set by one of the following: %s.',
            infobox_cfg['name'],
            winner_awards
        )

    return parsed


def sync_contests_table(sql, homesite, config, wiki_tz, server_tz):

    cursor = sql.cursor()

    infobox_cfg = config['templates']['infobox']
    infobox_page = homesite.pages['Template:' + infobox_cfg['name']]
    contest_pages = list(infobox_page.embeddedin())

    cursor.execute('SELECT name, start_date, end_date, ended, closed FROM contests WHERE site=%s', [homesite.key])
    contests = cursor.fetchall()
    contest_names = [c[0] for c in contests]

    for page in contest_pages:
        if not page.name.startswith(config['pages']['base']):
            continue

        if page.name not in contest_names:
            logger.info('Found new contest: %s', page.name)

            try:
                infobox = parse_infobox(page.text(), homesite.namespaces[2], config, wiki_tz)
            except ValueError:
                logger.error('Failed to parse infobox for contest %s', page.name)
                continue

            cursor.execute('INSERT INTO contests (config, site, name, start_date, end_date) VALUES (%s,%s,%s,%s,%s)', [
                config['filename'],
                homesite.key,
                page.name,
                infobox['start_time'].strftime('%F %T'),
                infobox['end_time'].strftime('%F %T')
            ])
            sql.commit()
    cursor.close()


def get_contest_page_titles(sql, homesite, config, wiki_tz, server_tz):

    cursor = sql.cursor()

    now = server_tz.localize(datetime.now())
    now_w = now.astimezone(wiki_tz)
    now_s = now_w.strftime('%F %T')

    # 1) Check if there are contests to close

    cursor.execute(
        'SELECT name FROM contests WHERE site=%s AND name LIKE %s AND update_date IS NOT NULL AND ended=1 AND closed=0',
        [homesite.key, config['pages']['base'] + '%%']
    )
    for row in cursor.fetchall():
        page_title = row[0]
        if 'awardstatus' in config:
            if award_delivery_confirmed(homesite, config['awardstatus'], page_title):
                logger.info('Award delivery confirmed for [[%s]]', page_title)
                yield (STATE_CLOSING, page_title)
        else:
            logger.info('Contest ended: [[%s]]. Auto-closing since there\'s no award delivery', page_title)
            cursor.execute('UPDATE contests SET closed=1 WHERE site=%s AND name=%s', [homesite.key, page_title])
            sql.commit()

    # 2) Check if there are contests to end

    cursor.execute(
        'SELECT name FROM contests WHERE site=%s AND name LIKE %s AND update_date IS NOT NULL AND ended=0 AND closed=0 AND end_date < %s',
        [homesite.key, config['pages']['base'] + '%%', now_s]
    )
    for row in cursor.fetchall():
        page_title = row[0]
        logger.info('Contest [[%s]] just ended', page_title)
        yield (STATE_ENDING, page_title)

    # 3) Check if there are other contests to update

    cursor.execute(
        'SELECT name FROM contests WHERE site=%s AND name LIKE %s AND ended=0 AND closed=0 AND start_date < %s',
        [homesite.key, config['pages']['base'] + '%%', now_s]
    )
    for row in cursor.fetchall():
        page_title = row[0]
        yield (STATE_NORMAL, page_title)

    cursor.close()


def get_contest_pages(sql, homesite, config, wiki_tz, server_tz, page_title=None):

    sync_contests_table(sql, homesite, config, wiki_tz, server_tz)

    if page_title is not None:
        cursor = sql.cursor()

        cursor.execute('SELECT ended, closed FROM contests WHERE site=%s AND name=%s', [
            homesite.key,
            page_title,
        ])
        contests = cursor.fetchall()
        pages = [(STATE_NORMAL, page_title)]
        if len(contests) == 1:
            if contests[0][1] == 1:
                logger.error('Contest %s is closed, cannot be updated', page_title)
                pages = []
            elif contests[0][0] == 1:
                if award_delivery_confirmed(homesite, config['awardstatus'], page_title):
                    pages = [(STATE_CLOSING, page_title)]
                else:
                    pages = [(STATE_ENDING, page_title)]

    else:
        pages = get_contest_page_titles(sql, homesite, config, wiki_tz, server_tz)


    for p in pages:
        page = homesite.pages[p[1]]
        if not page.exists:
            logger.warning('Page does not exist: %s', p[1])
            continue
        page = page.resolve_redirect()

        yield (p[0], page)


############################################################################################################################
# Main
############################################################################################################################


    # try:
    #     log( "Opening message file %s for locale %s" % (filename, loc[0]) )
    #     trans = gettext.GNUTranslations(open( filename, "rb" ) )
    # except IOError:
    #     log( "Locale not found. Using default messages" )
    #     trans = gettext.NullTranslations()
    # trans.install(unicode = True)


class SiteManager(object):

    def __init__(self, sites, homesite):
        """

        :param sites: (dict) Dictionary {key: Site} of sites, including the homesite
        :param homesite: (Site)
        """
        self.sites = sites
        self.homesite = homesite

    def keys(self):
        return self.sites.keys()
    
    def resolve_page(self, value, default_ns=0, force_ns=False):
        logger.debug('Resolving: %s', value)
        values = value.lstrip(':').split(':')
        site = self.homesite
        ns = None

        # check all prefixes
        article_name = ''
        for val in values[:-1]:
            site_from_prefix = self.from_prefix(val)
            if val in site.namespaces.values():
                # reverse namespace lookup
                ns = val  # [k for k, v in site.namespaces.items() if v == val][0]
            elif site_from_prefix is not None:
                site = site_from_prefix
            else:
                article_name += '%s:' % val
        article_name += values[-1]

        # Note: we should check this *after* we know which site to use
        if ns is None:
            ns = site.namespaces[default_ns]
        elif force_ns:
            ns = '%s:%s' % (site.namespaces[default_ns], ns)

        article_name = article_name[0].upper() + article_name[1:]

        value = '%s:%s' % (ns, article_name)
        logger.debug('proceed: %s', value)

        if article_name == '*':
            page = WildcardPage(site)
        else:
            page = site.pages[value]
            if not page.exists:
                raise InvalidContestPage(_('Page does not exist: [[%(pagename)s]]') % {
                    'pagename': site.link_to(page)
                })
        return page

    def from_prefix(self, key, raise_on_error=False):
        """
        Get Site instance from interwiki prefix.

        :param key: interwiki prefix (e.g. "no", "nn", "wikidata", "d", ...)
        :param raise_on_error: Throw error if site not found, otherwise return None
        :return: Site
        """
        for site in self.sites.values():
            if site.match_prefix(key):
                return site
        if raise_on_error:
            raise InvalidContestPage(_('Could not found a site matching the prefix "%(key)s"') % {
                'key': key
            })

    def only(self, sites):
        return SiteManager(sites, self.homesite)


def init_sites(config):

    if 'ignore' not in config:
        config['ignore'] = []

    # Configure home site (where the contests live)
    host = config['homesite']
    homesite = Site(host, prefixes=[''])

    assert homesite.logged_in

    iwmap = homesite.interwikimap
    prefixes = [''] + [k for k, v in iwmap.items() if v == host]
    homesite.prefixes = prefixes

    # Connect to DB
    sql = db_conn()
    logger.debug('Connected to database')

    sites = {homesite.host: homesite}
    if 'othersites' in config:
        for host in config['othersites']:
            prefixes = [k for k, v in iwmap.items() if v == host]
            sites[host] = Site(host, prefixes=prefixes)

    for site in sites.values():
        msg = site.get_revertpage_regexp()
        if msg != '':
            logger.debug('Revert page regexp: %s', msg)
            config['ignore'].append(msg)

    return SiteManager(sites, homesite), sql


def main():
    parser = argparse.ArgumentParser(description='The UKBot')
    parser.add_argument('config', help='Config file', type=argparse.FileType('r', encoding='UTF-8'))
    parser.add_argument('--page', required=False, help='Name of the contest page to work with')
    parser.add_argument('--simulate', action='store_true', default=False, help='Do not write results to wiki')
    parser.add_argument('--output', nargs='?', default='', help='Write results to file')
    parser.add_argument('--log', nargs='?', default='', help='Log file')
    parser.add_argument('--verbose', action='store_true', default=False, help='More verbose logging')
    parser.add_argument('--close', action='store_true', help='Close contest')
    parser.add_argument('--action', nargs='?', default='', help='"uploadplot" or "run"')
    parser.add_argument('--job_id', required=False, help='Job ID')
    args = parser.parse_args()

    if args.verbose:
        syslog.setLevel(logging.DEBUG)
    else:
        syslog.setLevel(logging.INFO)

    if args.log != '':
        logfile = open(args.log, 'a')

    config = load_config(args.config)
    config['filename'] = args.config.name
    args.config.close()

    # rollbar.init(config['rollbar_token'], 'production')
    wiki_tz = pytz.timezone(config['wiki_timezone'])
    server_tz = pytz.timezone(config['server_timezone'])

    working_dir = os.path.realpath(os.getcwd())
    logger.info('Working dir: %s', working_dir)

    Localization().init(config['locale'])

    mainstart = server_tz.localize(datetime.now())
    mainstart_s = time.time()

    logger.info('Current server time: %s, wiki time: %s',
                mainstart.strftime('%F %T'),
                mainstart.astimezone(wiki_tz).strftime('%F %T'))
    logger.info(
        'Platform: Python %s, Mwclient %s, %s',
        platform.python_version(),
        mwclient.__version__,
        platform.platform()
    )

    status_template = config['templates']['botinfo']

    sites, sql = init_sites(config)

    # Determine what to work with
    active_contests = list(get_contest_pages(sql, sites.homesite, config, wiki_tz, server_tz, args.page))

    logger.info('Number of active contests: %d', len(active_contests))
    for contest_state, contest_page in active_contests:
        try:
            contest = Contest(contest_page,
                              state=contest_state,
                              sites=sites,
                              sql=sql,
                              config=config,
                              wiki_tz=wiki_tz,
                              server_tz=server_tz,
                              project_dir=working_dir,
                              job_id=args.job_id)
        except InvalidContestPage as e:
            if args.simulate:
                logger.error(e.msg)
                sys.exit(1)

            error_msg = "\n* '''%s'''" % e.msg

            te = TemplateEditor(contest_page.text())
            if status_template in te.templates:
                te.templates[status_template][0].parameters[1] = 'error'
                te.templates[status_template][0].parameters[2] = error_msg
                contest_page.save(te.wikitext(), summary=_('UKBot encountered a problem'))
            else:
                out = '\n{{%s | error | %s }}' % (config['templates']['botinfo'], error_msg)
                contest_page.save('dummy', summary=_('UKBot encountered a problem'), appendtext=out)
            raise

        if args.action == 'uploadplot':
            contest.uploadplot(args.simulate, args.output)
        elif args.action == 'plot':
            filename = os.path.join(working_dir, config['plot']['datafile'] % {'year': contest.year, 'week': contest.startweek, 'month': contest.month})
            with open(filename, 'r') as fp:
                plotdata = json.load(fp)
            contest.plot(plotdata)
        else:
            contest.run(args.simulate, args.output)

    # Update WP:UK

    normal_contests = [
        contest_page.name for contest_state, contest_page in active_contests
        if contest_state == STATE_NORMAL and contest_page.name.startswith(config['pages']['base'])
    ]
    if 'redirect' in config['pages'] and len(normal_contests) == 1:
        contest_name = normal_contests[0]
        pages = config['pages']['redirect']
        if not isinstance(pages, list):
            pages = [pages]
        for pagename in pages:
            page = sites.homesite.pages[pagename]
            txt = _('#REDIRECT [[%s]]') % contest_name
            if page.text() != txt and not args.simulate:
                page.save(txt, summary=_('Redirecting to %s') % contest_name)

    runend = server_tz.localize(datetime.now())
    runend_s = time.time()

    runtime = runend_s - runstart_s
    logger.info('UKBot finishing at %s. Runtime was %.f seconds (total) or %.f seconds (excluding initialization).',
                runend.strftime('%F %T'),
                runend_s - runstart_s,
                runend_s - mainstart_s)

    #try:
    #    main()
    #except IOError:
    #    rollbar.report_message('Got an IOError in the main loop', 'warning')
    #except:
    #    # catch-all
    #    rollbar.report_exc_info()

if __name__ == '__main__':
    main()
