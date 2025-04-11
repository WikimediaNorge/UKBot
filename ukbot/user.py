# encoding=utf-8
# vim: fenc=utf-8 et sw=4 ts=4 sts=4 ai
import logging
import re
import time
from collections import OrderedDict
from copy import copy

import pydash
import weakref
import numpy as np
from datetime import datetime
import pytz
import pymysql
from more_itertools import first

from .contributions import UserContributions
from .common import _
from .db import result_iterator
from .util import unix_time
from .article import Article

logger = logging.getLogger(__name__)


class User:

    def __init__(self, username, contest):
        self.name = username
        self.articles = OrderedDict()
        self.revisions = OrderedDict()
        self.contest = weakref.ref(contest)
        self.suspended_since = None
        self.contributions = UserContributions(self, contest.config)
        self.disqualified_articles = []
        self.point_deductions = []

    def __del__(self):
        logger.info('Destructing %s', repr(self))

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

        if article_key not in self.articles:
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

        ignore_tags = set(self.contest().config['ignoreTags'])
        logger.debug('Edit tags that should be ignored: %s', ', '.join(sorted(ignore_tags)))
        new_revisions = []
        # stored_revisions = set(copy(self.revisions.keys()))
        stored_revisions = set([rev.revid for rev in self.revisions.values() if rev.article().site() == site])
        current_revisions = set()
        t0 = time.time()
        t1 = time.time()
        tnr = 0
        n_articles = len(self.articles)
        for c in site.usercontributions(self.name, ts_start, ts_end, 'newer', prop='ids|title|timestamp|comment|tags', **args):
            tnr += 1

            dt1 = time.time() - t1
            if dt1 > 10:
                dt0 = time.time() - t0
                t1 = time.time()
                logger.info('Found %d new revisions from API so far (%.0f secs elapsed)',
                            len(new_revisions), dt0)

            ignored_tags_present = set(c['tags']) & ignore_tags

            logger.debug('Revision %d has the following tags: %s', c['revid'], ', '.join(sorted(c['tags'])))

            if ignored_tags_present:
                logger.info('Ignoring revision %d of %s:%s because it has the following edit tags: %s', c['revid'], site_key, c['title'], ', '.join(ignored_tags_present))
                break
            else:
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

    def add_contribs_from_db(self, sql, start, end, sites):
        """
        Populates self.articles with entries from MySQL DB

            sql       : SQL Connection object
            start : datetime object
            end   : datetime object
            sites : list of sites
        """
        logger.info('Reading user contributions from database')
        sql.close()
        sql.open_conn()

        cur = sql.cursor()
        ts_start = start.astimezone(pytz.utc).strftime('%F %T')
        ts_end = end.astimezone(pytz.utc).strftime('%F %T')
        nrevs = 0
        narts = 0
        t0 = time.time()
        cur.execute(
            '''
            SELECT
                c.revid, c.site, c.parentid, c.page, c.timestamp, c.size, c.parentsize, c.parsedcomment, c.ns,
                ft.revtxt,
                ft2.revtxt
            FROM contribs AS c
            LEFT JOIN fulltexts AS ft ON ft.revid = c.revid AND ft.site = c.site
            LEFT JOIN fulltexts AS ft2 ON ft2.revid = c.parentid AND ft2.site = c.site
            WHERE c.user = %s
            AND c.timestamp >= %s AND c.timestamp <= %s
            ''',
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
                    if rule.site is None or article.site().key in rule.site:
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
            out += '%s, {{formatnum:%.2f}} kB\n' % (_('articles') % {'articlecount': len(entries)}, self.bytes / 1000.)
        if len(entries) > 10:
            out += _('{{Kolonner}}\n')
        out += '\n'.join(entries)
        out += '\n\n'

        return out
