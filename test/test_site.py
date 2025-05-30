import os
import unittest
from unittest import TestCase
from unittest.mock import patch, MagicMock
from ukbot.site import Site, WildcardPage

class TestSiteClass(TestCase):
    def mock_metadata(self, mock_api: MagicMock) -> None:
        # Mocks the API response for site metadata (magicwords, namespaces, interwikimap, etc.)
        mock_api.return_value = {
            'query': {
                'magicwords': [{'name': 'redirect', 'aliases': ['REDIRECT', '#REDIRECT']}],
                'namespaces': {'6': {'*': 'File', 'canonical': 'File'}},
                'namespacealiases': [{'id': 6, '*': 'F'}],
                'interwikimap': [
                    {'prefix': 'en', 'url': 'https://en.wikipedia.org/wiki/$1'},
                    {'prefix': 'de', 'url': 'https://de.wikipedia.org/wiki/$1'},
                    {'prefix': None, 'url': 'https://none.wikimedia.org/wiki/$1'}, 
                    {'prefix': 'UPPERcase', 'url': 'https://uppercase.wikimedia.org/wiki/$1'},
                    {'prefix': '  en  ', 'url': 'https://whitespace.wikimedia.org/wiki/$1'},
                    {'prefix': '', 'url': 'https://en.wikipedia.org/wiki/$1'}
                ]
            }
        }

    @patch.dict(os.environ, {
        'MW_CONSUMER_TOKEN': 'dummy_consumer',
        'MW_CONSUMER_SECRET': 'dummy_secret',
        'MW_ACCESS_TOKEN': 'dummy_access',
        'MW_ACCESS_SECRET': 'dummy_secret'
    })

    @patch('ukbot.site.mwclient.Site.__init__', return_value=None)
    @patch('ukbot.site.mwclient.Site.api')
    def test_prefix_handling(self, mock_api, mock_super_init):
        # Tests that Site initializes correctly with various prefix formats and that
        # all provided prefixes are present in the interwikimap.
        self.mock_metadata(mock_api)
        cases = [
            ('en.wikipedia.org', ['en']),
            ('de.wikipedia.org', ['en', '', 'UPPERcase', '  en  ', 'de']),
            ('none.wikimedia.org', [None]),
            ('uppercase.wikimedia.org', ['UPPERcase']),
            ('whitespace.wikimedia.org', ['  en  ']),
        ]

        for host, prefixes in cases:
            with self.subTest(case='normal', host=host):
                site = Site(host=host, prefixes=prefixes)
                self.assertEqual(site.name, host)
                self.assertEqual(site.prefixes, prefixes)
                for prefix in prefixes:
                    self.assertIn(prefix, site.interwikimap)

    @patch.dict(os.environ, {
        'MW_CONSUMER_TOKEN': 'dummy_consumer',
        'MW_CONSUMER_SECRET': 'dummy_secret',
        'MW_ACCESS_TOKEN': 'dummy_access',
        'MW_ACCESS_SECRET': 'dummy_secret'
    })

    @patch('ukbot.site.mwclient.Site.__init__', return_value=None)
    @patch('ukbot.site.mwclient.Site.api')
    def test_prefix_handling_with_unknown_prefixes(self, mock_api, mock_super_init):
        # Tests that Site does not crash when prefixes are not present in the interwikimap.
        self.mock_metadata(mock_api)
        unknown_prefixes = ['zz', 'not_in_map', 42]
        site = Site(host='en.wikipedia.org', prefixes=unknown_prefixes)
        self.assertEqual(site.name, 'en.wikipedia.org')
        self.assertEqual(site.prefixes, unknown_prefixes)
        for prefix in unknown_prefixes:
            self.assertNotIn(prefix, site.interwikimap)


    def test_match_prefix(self):
        # Tests that match_prefix returns True for valid prefixes and False otherwise.
        site = Site.__new__(Site)
        site.key = 'en'
        site.prefixes = ['en', 'enwiki']
        self.assertTrue(site.match_prefix('en'))
        self.assertTrue(site.match_prefix('enwiki'))
        self.assertFalse(site.match_prefix('fr'))

    def test_link_to_with_blank_prefix(self):
        # Tests link_to returns the correct format when the prefix is blank.
        site = Site.__new__(Site)
        site.prefixes = ['']
        mock_page = MagicMock()
        mock_page.name = 'ExamplePage'
        self.assertEqual(site.link_to(mock_page), ':ExamplePage')

    def test_link_to_with_prefix(self):
        # Tests link_to returns the correct format when a prefix is present.
        site = Site.__new__(Site)
        site.prefixes = ['en']
        mock_page = MagicMock()
        mock_page.name = 'ExamplePage'
        self.assertEqual(site.link_to(mock_page), ':en:ExamplePage')

    def test_repr_str_hash(self):
        # Tests __repr__, __str__, and __hash__ methods for correct output.
        site = Site.__new__(Site)
        site.host = 'en.wikipedia.org'
        self.assertEqual(str(site), 'Site(en.wikipedia.org)')
        self.assertEqual(repr(site), 'Site(en.wikipedia.org)')
        self.assertEqual(hash(site), hash('Site(en.wikipedia.org)'))


class TestWildcardPage(unittest.TestCase):
    def test_wildcard_page_init(self):
        # Tests that WildcardPage initializes with the correct site.
        site = MagicMock()
        wp = WildcardPage(site)
        self.assertEqual(wp.site, site)


if __name__ == '__main__':
    unittest.main()
