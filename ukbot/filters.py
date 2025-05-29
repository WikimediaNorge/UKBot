# encoding=utf-8
# vim: fenc=utf-8 et sw=4 ts=4 sts=4 ai
import sys
import re
from copy import copy

from more_itertools import first
import logging
import time
import urllib
import requests
from collections import OrderedDict
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from mwtemplates.templateeditor2 import TemplateParseError
from .common import _, InvalidContestPage
from .site import WildcardPage

from typing import List, Union, Optional
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .ukbot import FilterTemplate, SiteManager
    from .article import Article
    from mwclient.page import Page
    Articles = OrderedDict[str, 'Article']

logger = logging.getLogger(__name__)


def requests_retry_session(
    retries=3,
    backoff_factor=0.8,
    status_forcelist=(500, 502, 504),
    session=None,
):
    session = session or requests.Session()
    retry = Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session


class CategoryLoopError(Exception):
    """Raised when a category loop is found."""
    def __init__(self, catpath):
        """
        Args:
            catpath -- category path followed while getting lost in the loop
        """
        self.catpath = catpath
        self.msg = 'Entered a category loop'


class Filter(object):

    def __init__(self, sites: 'SiteManager'):
        """
        Args:
            sites (SiteManager): A SiteManager instance with the sites relevant for this filter.
        """
        self.sites = sites
        self.page_keys = set()

    @classmethod
    def make(cls, tpl: 'FilterTemplate', **kwargs):
        return cls(tpl.sites)

    def test_page(self, page):
        """
        Return True if the page matches the current filter, False otherwise.
        """
        return page.key in self.page_keys

    def filter(self, articles: 'Articles'):
        out = OrderedDict()
        for article_key, article in articles.items():
            if self.test_page(article):
                out[article_key] = article
        logger.info(' - %s: Articles reduced from %d to %d',
                    type(self).__name__, len(articles), len(out))
        return out

#class StubFilter(Filter):
#    """ Filters articles that was stubs, but is no more """

#    def __init__(self):
#        Filter.__init__(self)

#    def is_stub(self, text):
#        """ Checks if a given text is a stub """

#        m = re.search(r'{{[^}]*(?:stubb|spire)[^}]*}}', text, re.IGNORECASE)
#        if m:
#            if self.verbose:
#                log(" >> %s " % m.group(0), newline = False)
#            return True
#        return False

    #def filter(self, articles):

    #    out = OrderedDict()
    #    for article_key, article in articles.items():

    #        firstrevid = article.revisions.firstkey()
    #        lastrevid = article.revisions.lastkey()

    #        firstrev = article.revisions[firstrevid]
    #        lastrev = article.revisions[lastrevid]

    #        try:

    #            # skip pages that are definitely not stubs to avoid timeconsuming parsing
    #            if article.new is False and article.redirect is False and len(firstrev.parenttext) < 20000:

    #                # Check if first revision is a stub
    #                if self.is_stub(firstrev.parenttext):

    #                    # Check if last revision is a stub
    #                    if not self.is_stub(lastrev.text):

    #                        out[article_key] = article

    #                    if self.verbose:
    #                        log('')

            #except DanmicholoParseError as e:
            #    log(" >> DanmicholoParser failed to parse " + article_key)
            #    parentid = firstrev.parentid
            #    args = { 'article': article_key, 'prevrev': firstrev.parentid, 'rev': lastrev.revid, 'error': e.msg }
            #    article.site().errors.append(_('Could not analyze the article %(article)s because one of the revisions %(prevrev)d or %(rev)d could not be parsed: %(error)s') % args)

    #    log("  [+] Applying stub filter: %d -> %d" % (len(articles), len(out)))

    #    return out


class TemplateFilter(Filter):
    """ Filters articles that had any of a given set of templates (or their aliases) at a point"""

    @classmethod
    def make(cls, tpl, **kwargs):
        if len(tpl.anon_params) < 3:
            raise RuntimeError(_('Too few arguments given to this template.'))

        params = {
            'sites': tpl.sites,
            'templates': tpl.anon_params[2:],
        }

        return cls(**params)

    def __init__(self, sites, templates, include_aliases=True):
        """
        The TemplateFilter keeps pages that had any of a given set of templates
        (or their aliases) when the user made their first edit during the
        timeframe of this contest.

        Args:
            sites (SiteManager): References to the sites part of this contest
            templates (list): List of templates to include
            include_aliases (bool): Whether to include aliases, defaults to True
        """
        Filter.__init__(self, sites)

        aliases = []
        for template_name in templates:
            template_page = self.sites.homesite.pages['Template:%s' % template_name]
            if template_page.exists:
                aliases.extend([x.page_title for x in template_page.backlinks(filterredir='redirects')])

        self.templates = templates + aliases

    def text_contains_template(self, text):
        """ Checks if a given text contains the template"""

        tpls = [x.replace('*', '[^}]*?') for x in self.templates]
        m = re.search(r'{{(%s)[\s]*(\||}})' % '|'.join(tpls), text, flags=re.IGNORECASE)
        if m:
            return m.group(1)
        return None

    def test_page(self, page):
        rev_id = first(page.revisions)
        rev = page.revisions[rev_id]

        try:
            template_name = self.text_contains_template(rev.parenttext)
        except TemplateParseError as e:
            logger.warning('TemplateParser failed to parse %s', page.key)
            page.site().errors.append(
                _('Could not analyze page %(article)s because the revision %(rev)d could not be parsed: %(error)s') % {
                    'article': page.key,
                    'rev': rev.parentid,
                    'error': str(e),
                }
            )

        if template_name is None:
            return False
        logger.debug('Found template {{%s}} in [[%s]] @ %d', template_name, page.key, rev.parentid)
        return True


class CatFilter(Filter):
    """ Filters articles that belong to a given overcategory """

    ignore_sites = [
        'www.wikidata.org',
    ]

    @staticmethod
    def get_ignore_list(tpl: 'FilterTemplate', page_name: str):
        if page_name is None or page_name == '':
            return []

        catignore_txt = tpl.sites.homesite.pages[page_name].text()

        if catignore_txt == '':
            logger.info('Note: catignore page is empty or does not exist')
            return []

        try:
            m = re.search(r'<pre>(.*?)</pre>', catignore_txt, flags=re.DOTALL)
            return m.group(1).strip().splitlines()
        except (IndexError, KeyError):
            raise RuntimeError(_('Could not parse the catignore page'))

    @classmethod
    def make(cls, tpl: 'FilterTemplate', **kwargs):
        if len(tpl.anon_params) < 3:
            raise RuntimeError(_('No category values given!'))

        params = {
            'sites': tpl.sites,
            'ignore': cls.get_ignore_list(tpl, kwargs.get('cfg', {}).get('ignore_page')),
            'categories': [
                tpl.sites.resolve_page(cat_name, 14, True)
                for cat_name in tpl.anon_params[2:] if cat_name.strip() != ''
            ],
        }

        if tpl.has_param('ignore'):
            params['ignore'].extend([
                a.strip()
                for a in tpl.get_param('ignore').split(',')
            ])

        if tpl.has_param('maxdepth'):
            params['maxdepth'] = int(tpl.get_param('maxdepth'))

        return cls(**params)

    def __init__(self, sites: 'SiteManager', categories: List[Union['Page', WildcardPage]], maxdepth: int = 5,
                 ignore: List[str] = []):
        """
        Arguments:
            sites (SiteManager): References to the sites part of this contest
            categories (list): Page objects
            maxdepth (int):  number of subcategory levels to traverse
            ignore (list): list of categories to ignore
        """
        Filter.__init__(self, sites)

        self.ignore = ignore

        self.include = [
            '%s:%s' % (page.site.key, page.name)  # Includes namespace prefix
            for page in categories
            if not isinstance(page, WildcardPage)
        ]

        self.categories_cache = {site_key: {} for site_key in self.sites.keys()}

        # Sites for which we should accept all contributions
        self.wildcard_include = [
            page.site.key
            for page in categories
            if isinstance(page, WildcardPage)
        ]
        self.maxdepth = int(maxdepth)
        logger.debug('Initializing CatFilter with categories: "%s", maxdepth: %d',
                     '" OR "'.join(self.include), maxdepth)

    def add_to_category_cache(self, articles: 'Articles'):
        """
        Fetch n levels of categories for a set of articles from the API, and store the category memberhips in
        the flat `self.categories_cache` dictionary. The `self.categories_cache` is retained for the whole bot
        run, so we only have to query the API once for each page/category, even if multiple users have contributed
        to the same page/category.
        """

        for site_key, site in self.sites.items():
            if site_key in self.ignore_sites:
                continue

            if 'bot' in site.rights:
                requestlimit = 500
                returnlimit = 5000
            else:
                requestlimit = 50
                returnlimit = 500

            # Titles of articles that belong to this site
            titles_to_check = set([page.name for page in articles.values() if page.site().key == site_key])

            if len(titles_to_check) == 0:
                continue

            logger.debug('CatFilter [%s]: Planning to fetch categories for %d articles', site_key, len(titles_to_check))

            for level in range(self.maxdepth + 1):

                titles0 = copy(titles_to_check)
                titles_to_check = set()  # make a new list of titles to search
                cache_misses = []

                for member_title in titles0:
                    if member_title in self.categories_cache[site_key]:
                        for category_title in self.categories_cache[site_key][member_title]:
                            titles_to_check.add(category_title)
                    else:
                        cache_misses.append(member_title)

                logger.debug('CatFilter [%s, level %d]: Cache hits: %d, cache misses: %d', site_key, level, len(titles0) - len(cache_misses), len(cache_misses))

                for s0 in range(0, len(cache_misses), requestlimit):
                    logger.debug('CatFilter [%s, level %d] Fetching categories for %d pages. Batch %d to %d', site_key, level, len(cache_misses), s0, s0+requestlimit)
                    ids = '|'.join(cache_misses[s0:s0+requestlimit])

                    cont = True
                    clcont = {'continue': ''}
                    while cont:
                        args = {'prop': 'categories', 'titles': ids, 'cllimit': returnlimit}
                        args.update(clcont)
                        q = site.api('query', **args)

                        if 'warnings' in q:
                            raise RuntimeError(q['warnings']['query']['*'])

                        for category_member in q['query']['pages'].values():
                            member_title = category_member['title']
                            if 'categories' not in category_member:
                                continue
                            for category in category_member['categories']:
                                category_title = category['title']  # Includes Category: prefix
                                category_short_name = category_title.split(':', 1)[1]
                                follow = True
                                for d in self.ignore:
                                    if re.search(d, category_short_name):
                                        logger.debug(' - Ignore: "%s" matched "%s"', category_title, d)
                                        follow = False
                                if follow:
                                    titles_to_check.add(category_title)

                                    if member_title not in self.categories_cache[site_key]:
                                        self.categories_cache[site_key][member_title] = set()

                                    self.categories_cache[site_key][member_title].add(category_title)

                        if 'continue' in q:
                            clcont = q['continue']
                        else:
                            cont = False

                for member_title in titles0:
                    for category_title in self.categories_cache[site_key].get(member_title, []):
                        titles_to_check.add(category_title)

    def filter(self, articles: 'Articles') -> 'Articles':
        """
        Filter a set of articles using category data from `self.category_cache`.

        Side effects:
            If an articles matches any of the categories in `self.include`, a category path is attached
            to the article, which can be used to explain why the article matched. If an article 'X' is a
            direct member of 'Category 1', which is a member of 'Category 2', which is itself member of
            the 'Matching category', the resulting cat_path will be:

                 artice.cat_path = ['Matching category', 'Child category 2', 'Child category 1']

            If a category path cannot be found due to category loops, an error will be attached to
            article.errors instead, but the article will still be returned as a match.
        """

        self.add_to_category_cache(articles)

        t0 = time.time()
        logger.debug('CatFilter: Checking %d articles', len(articles))

        out = OrderedDict()
        site_keys = set(self.sites.keys())

        for article_key, article in articles.items():
            site_key = article.site().key

            if site_key in self.ignore_sites or site_key not in site_keys:
                continue

            if site_key in self.wildcard_include:
                # Auto-pass all articles from this site
                out[article_key] = article
                continue

            categories = []
            categories_rev = {}

            new_buffer = [article.name]
            for level in range(self.maxdepth + 1):
                buffer = copy(new_buffer)
                new_buffer = []
                for page_name in buffer:
                    member_key = site_key + ':' + page_name

                    for category_name in self.categories_cache[site_key].get(page_name, []):
                        category_key = site_key + ':' + category_name

                        categories.append(category_key)
                        categories_rev[category_key] = member_key

                        new_buffer.append(category_name)

            # It should be slightly more efficient to first populate a list, and then
            # convert it a set for uniqueness, rather than working with a set all the way.
            categories = set(categories)

            matching_category = self.get_first_matching_category(categories)
            if matching_category is not None:
                out[article_key] = article
                try:
                    article.cat_path = self.get_category_path(
                        categories_rev,
                        matching_category,
                        article_key
                    )
                except CategoryLoopError as err:
                    loop = ' → '.join(['[[:%s|]]' % c.replace('.wikipedia.org', '') for c in err.catpath])
                    article.errors.append(_('Encountered an infinite category loop: ') + loop)

        dt = time.time() - t0
        logger.debug('CatFilter: Checked categories for %d articles in %.1f secs', len(articles), dt)
        logger.info(' - CatFilter: Articles reduced from %d to %d', len(articles), len(out))
        return out

    def get_first_matching_category(self, article_cats: set) -> Optional[str]:
        """ Checks if article_cats contains any of the cats given in self.include """
        for category_name in self.include:
            if category_name in article_cats:
                return category_name
        return None

    @staticmethod
    def get_category_path(members: dict, category_key: str, article_key: str) -> List[str]:
        """
        Try to find a path from a category to an article:

            [category_key, ..., article_key]
        """
        cat_path = [category_key]
        i = 0
        while category_key != article_key:
            if members[category_key] != article_key:
                cat_path.append(members[category_key])
            category_key = members[category_key]
            i += 1
            if i > 50:
                raise CategoryLoopError(cat_path)
        return cat_path


class ByteFilter(Filter):
    """Filters articles according to a byte treshold"""

    @classmethod
    def make(cls, tpl, sites, **kwargs):
        if len(tpl.anon_params) < 3:
            raise RuntimeError(_('No byte limit (second argument) given'))
        params = {
            'sites': tpl.sites,
            'bytelimit': int(tpl.anon_params[2]),
        }
        return cls(**params)

    def __init__(self, sites, bytelimit):
        """
        The ByteFilter keeps pages with a byte size above a certain threshold limit. 

        Args:
            sites (SiteManager): References to the sites part of this contest
            bytelimit (int): The minimum number of bytes required to keep a page
        """
        Filter.__init__(self, sites)
        self.bytelimit = bytelimit

    def test_page(self, page):
        """
        Return True if the page matches the current filter, False otherwise.
        """
        return page.bytes >= self.bytelimit


class NewPageFilter(Filter):
    """Filters new articles"""

    @classmethod
    def make(cls, tpl, contest, **kwargs):
        params = {
            'sites': tpl.sites,
            'contest': contest,
        }
        if tpl.has_param('redirects'):
            params['redirects'] = True
        return cls(**params)

    def __init__(self, sites, contest, redirects=False):
        """
        The NewPageFilter keeps pages that was created within the timeframe of a contest.
        In order to encourage collaboration on articles, the filter does not discriminate
        on which user created the page. 

        Args:
            sites (SiteManager): References to the sites part of this contest
            contest (Contest): The current contest
            redirects (bool): Whether to include redirect pages, defaults to False
        """
        Filter.__init__(self, sites)
        self.contest_start = contest.start
        self.contest_end = contest.end
        self.redirects = redirects

    def test_page(self, page):
        """
        Return True if the page matches the current filter, False otherwise.
        """
        if page.redirect and not self.redirects:
            return False
        return page.created_at >= self.contest_start and page.created_at < self.contest_end


class ExistingPageFilter(Filter):
    """ Filters non-new articles """

    @classmethod
    def make(cls, tpl, contest, **kwargs):
        params = {
            'sites': tpl.sites,
            'contest': contest,
        }
        return cls(**params)

    def __init__(self, sites, contest):
        """
        The ExistingPageFilter keeps pages that was created before the start of a contest.
        This is useful in contests that are about improving existing content.

        Args:
            sites (SiteManager): References to the sites part of this contest
            contest (Contest): The current contest
        """
        Filter.__init__(self, sites)
        self.contest_start = contest.start

    def test_page(self, page):
        """
        Return True if the page matches the current filter, False otherwise.
        """
        return page.created_at < self.contest_start


class BackLinkFilter(Filter):
    """Filters articles linked to from a list of pages."""

    @classmethod
    def make(cls, tpl, cfg, **kwargs):
        params = {
            'sites': tpl.sites,
            'pages': [tpl.sites.resolve_page(x) for x in tpl.anon_params[2:] if x.strip() != ''],
            'include_langlinks': cfg.get('include_langlinks', False),
        }
        return cls(**params)

    def __init__(self, sites, pages, include_langlinks=False):
        """
        The BackLinkFilter keeps pages linked to from a list of pages, and optionally also the
        interwiki links of those pages.

        Args:
            sites (SiteManager): References to the sites part of this contest
            pages(list): List of Page objects to extract links from.
            include_langlinks(bool): Whether to include langlinked pages as well. This is useful for 
                multi-language contests, but we can save some time by not checking them.
        """
        Filter.__init__(self, sites)

        page_names = ['%s:%s' % (x.site.key, x.name) for x in pages]
        logger.info('Initializing BackLinkFilter: %s',
                    ','.join(page_names))

        for page in pages:
            for linked_page in page.links(redirects=True):
                link = '%s:%s' % (linked_page.site.key, linked_page.name.replace('_', ' '))
                # logger.debug(' - Include: %s', link)
                self.page_keys.add(link)

                # Include langlinks as well
                if include_langlinks:
                    for langlink in linked_page.langlinks():
                        site = self.sites.from_prefix(langlink[0])
                        if site is not None:
                            link = '%s:%s' % (site.key, langlink[1].replace('_', ' '))
                            logger.debug(' - Include: %s', link)
                            self.page_keys.add(link)

        if include_langlinks:
            logger.info('BackLinkFilter ready with %d links (after having expanded langlinks)',
                        len(self.page_keys))
        else:
            logger.info('BackLinkFilter ready with %d links',
                        len(self.page_keys))


class ForwardLinkFilter(Filter):
    """Filters articles linking to <self.links>"""

    @classmethod
    def make(cls, tpl, **kwargs):

        params = {
            'sites': tpl.sites,
            'pages': [tpl.sites.resolve_page(x) for x in tpl.anon_params[2:] if x.strip() != ''],
        }
        return cls(**params)

    def __init__(self, sites, pages):
        """
        Arguments:
            sites (SiteManager): References to the sites part of this contest
            pages: list of Page objects
        """
        Filter.__init__(self, sites)

        for page in pages:
            for linked_page in page.backlinks(redirect=True):
                link = '%s:%s' % (linked_page.site.key, linked_page.name.replace('_', ' '))
                self.page_keys.add(link)

        logger.info('ForwardLinkFilter ready with %d links', len(self.page_keys))


class PageFilter(Filter):
    """Filters articles with forwardlinks to <name>"""

    @classmethod
    def make(cls, tpl, **kwargs):
        params = {
            'sites': tpl.sites,
            'pages': [tpl.sites.resolve_page(x) for x in tpl.anon_params[2:] if x.strip() != '']
        }
        return cls(**params)

    def __init__(self, sites, pages):
        """
        Arguments:
            sites (SiteManager): References to the sites part of this contest 
            pages (list): list of Page objects
        """
        Filter.__init__(self, sites)
        self.page_keys = set(['%s:%s' % (page.site.key, page.name) for page in pages])
        logger.info('PageFilter ready with %d links', len(self.page_keys))


class NamespaceFilter(Filter):
    """Filters articles with a given namespaces"""

    @classmethod
    def make(cls, tpl, cfg, **kwargs):
        params = {
            'sites': tpl.sites,
            'namespaces': [x.strip() for x in tpl.anon_params[2:]],
            'site': tpl.get_param('site', datatype=list),
        }
        return cls(**params)

    def __init__(self, sites, namespaces, site):
        """
        Args:
            sites (SiteManager): References to the sites part of this contest
            namespaces (list): List of namespaces to include
            site (list): Filter by site (optional)
        """
        Filter.__init__(self, sites)
        self.namespaces = namespaces
        self.site = site
        if self.site is not None:
            logger.info('NamespaceFilter: %s @ %s', ','.join(self.namespaces), ','.join(self.site))
        else:
            logger.info('NamespaceFilter: %s', ','.join(self.namespaces))

    def test_page(self, page):
        """
        Return True if the page matches the current filter, False otherwise.
        """
        if self.site is not None and page.site().key not in self.site:
            return False
        return page.ns in self.namespaces


class SparqlFilter(Filter):
    """Filters articles matching a SPARQL query"""

    @classmethod
    def make(cls, tpl, cfg, **kwargs):
        if not tpl.has_param('query'):
            raise RuntimeError(_('No "%s" parameter given') % cfg['params']['query'])
        params = {
            'query': tpl.get_raw_param('query'),
            'sites': tpl.sites,
        }
        return cls(**params)

    def __init__(self, sites, query):
        """
        Args:
            sites (SiteManager): References to the sites part of this contest
            query (str): The SPARQL query
        """
        Filter.__init__(self, sites)
        self.query = query
        self.fetch()

    def do_query(self, querystring):
        logger.info('Running SPARQL query: %s', querystring)
        try:
            response = requests_retry_session().get(
                'https://query.wikidata.org/sparql',
                params={
                    'query': querystring,
                },
                headers={
                    'accept': 'application/sparql-results+json',
                    'accept-encoding': 'gzip, deflate, br',
                    'user-agent': 'UKBot/1.0, run by User:Danmichaelo',
                }
            )
        except Exception as ex:
            logger.error('SPARQL query failed')
            raise ex

        if not response.ok:
            raise IOError('SPARQL query returned status %s', response.status_code)

        expected_length = response.headers.get('Content-Length')
        if expected_length is not None and 'tell' in dir(response.raw):
            actual_length = response.raw.tell()
            expected_length = int(expected_length)
            if actual_length < expected_length:
                raise IOError(
                    'Incomplete read ({} bytes read, {} more expected)'.format(
                        actual_length,
                        expected_length - actual_length
                    )
                )

        res = response.json()
        query_var = res['head']['vars'][0]
        logger.debug('SPARQL query var is: %s', query_var)

        return {
            'var': query_var,
            'rows': [x[query_var]['value'] for x in res['results']['bindings']],
        }

    def fetch(self):
        logger.debug('SparqlFilter: %s', self.query)

        item_var = 'item'

        # Implementation notes:
        # - When the contest includes multiple sites, we do one query per site. I tried using
        #   a single query with `VALUES ?site { %(sites)s }` instead, but the query time
        #   almost doubled for each additional site, making timeouts likely.
        # - I also tested doing two separate queries rather than one query with a subquery,
        #   but when the number of items became large it resulted in "request too large".
        for site in self.sites.keys():
            logger.debug('Querying site: %s', site)
            t0 = time.time()
            s0 = len(self.page_keys)
            if site == 'www.wikidata.org':
                self.add_wikidata_items(site, item_var)
            else:
                self.add_linked_articles(site, item_var)

            t1 = time.time() - t0
            s1 = len(self.page_keys) - s0
            logger.info('SparqlFilter: Got %d results for %s in %.1f secs', s1, site, t1)

        logger.info('SparqlFilter: Initialized with %d articles', len(self.page_keys))

    def add_linked_articles(self, site, item_var):
        article_var = 'article19472065'  # "random string" to avoid matching anything in the subquery
        query = """
            SELECT ?%(article)s
            WHERE {
              { %(query)s }
              ?%(article)s schema:about ?%(item)s .
              ?%(article)s schema:isPartOf <https://%(site)s/> .
            }
        """ % {
            'item': item_var,
            'article': article_var,
            'query': self.query,
            'site': site,
        }
        logger.debug('SparqlFilter: %s', query)

        for res in self.do_query(query)['rows']:
            article = '/'.join(res.split('/')[4:])
            article = urllib.parse.unquote(article).replace('_', ' ')
            page_key = '%s:%s' % (site, article)
            self.page_keys.add(page_key)

    def add_wikidata_items(self, site, item_var):
        query = """
            SELECT ?%(item)s
            WHERE {
                { %(query)s }
            }
        """ % {
            'item': item_var,
            'query': self.query,
            'site': site,
        }
        logger.debug('SparqlFilter: %s', query)

        for res in self.do_query(query)['rows']:
            qid = res.split('/')[4]
            page_key = '%s:%s' % (site, qid)
            self.page_keys.add(page_key)
