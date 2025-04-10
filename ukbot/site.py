# encoding=utf-8
# vim: fenc=utf-8 et sw=4 ts=4 sts=4 ai
import logging
import re
import os
import mwclient
from requests import Session
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from requests_oauthlib import OAuth1


logger = logging.getLogger(__name__)


class Site(mwclient.Site):

    key = None

    def __init__(self, host, prefixes, **kwargs):
        session = Session()
        retries = Retry(total=5, backoff_factor=1, status_forcelist=[502, 503, 504])
        session.mount('https://', HTTPAdapter(max_retries=retries))

        consumer_token = os.getenv('MW_CONSUMER_TOKEN')
        consumer_secret = os.getenv('MW_CONSUMER_SECRET')
        access_token = os.getenv('MW_ACCESS_TOKEN')
        access_secret = os.getenv('MW_ACCESS_SECRET')
        session.auth = OAuth1(consumer_token, consumer_secret, access_token, access_secret)
        session.headers['User-Agent'] = 'UKBot (https://ukbot.toolforge.org/; ukbot@wikimedia.no)'

        self.errors = []
        self.name = host
        self.key = host
        self.prefixes = prefixes
        logger.debug('Initializing site: %s', host)
        mwclient.Site.__init__(self, host, pool=session, **kwargs)

        res = self.api('query', meta='siteinfo', siprop='magicwords|namespaces|namespacealiases|interwikimap')['query']

        self.file_prefixes = [res['namespaces']['6']['*'], res['namespaces']['6']['canonical']] \
            + [x['*'] for x in res['namespacealiases'] if x['id'] == 6]

        logger.debug('File prefixes: %s', '|'.join(self.file_prefixes))

        redirect_words = [x['aliases'] for x in res['magicwords'] if x['name'] == 'redirect'][0]
        logger.debug('Redirect words: %s', '|'.join(redirect_words))
        self.redirect_regexp = re.compile('(?:%s)' % '|'.join(redirect_words), re.I)

        self.interwikimap = {
            x['prefix']: x['url'].split('//')[1].split('/')[0].split('?')[0] for x in res['interwikimap']
        }

    def __repr__(self):
        return "Site(%s)" % self.host

    def __str__(self):
        return self.__repr__()

    def __hash__(self):
        return hash(self.__repr__())

    def get_revertpage_regexp(self):
        msg = self.pages['MediaWiki:Revertpage'].text()
        msg = re.sub(r'\[\[[^\]]+\]\]', '.*?', msg)
        msg = re.sub(r'(?i)\{\{PLURAL:\$\d\|(.+)\}\}', '(\1)', msg)
        return msg

    def match_prefix(self, prefix):
        return prefix in self.prefixes or prefix == self.key

    def link_to(self, page):
        # Create a link to the Page or Article, including a site prefix if not the homesite
        if self.prefixes[0] == '':
            return ':%s' % page.name
        else:
            return ':%s:%s' % (self.prefixes[0], page.name)


class WildcardPage:

    def __init__(self, site):
        self.site = site
