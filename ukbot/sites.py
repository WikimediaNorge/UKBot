# encoding=utf-8
# vim: fenc=utf-8 et sw=4 ts=4 sts=4 ai
import logging
from .common import _, InvalidContestPage
from .db import db_conn
from .site import WildcardPage, Site

logger = logging.getLogger(__name__)


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

    def items(self):
        return self.sites.items()

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
