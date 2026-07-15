# encoding=utf-8
import unittest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime
import pytz
from collections import OrderedDict

from ukbot.article import Article
from ukbot.revision import Revision


class TestArticle(unittest.TestCase):

    def setUp(self):
        # Create a complete mock setup
        self.site_mock = Mock()
        self.site_mock.key = 'test.wikipedia.org'
        self.site_mock.match_prefix = Mock(return_value=False)
        self.site_mock.link_to = Mock(side_effect=lambda article: f'{article.site().key}:{article.name}')

        # Mock pages dict for MediaWiki API calls
        page_mock = Mock()
        page_mock.revisions = Mock(return_value=iter([{'timestamp': (2020, 1, 1, 0, 0, 0, 0, 0, 0)}]))
        self.site_mock.pages = {'Test Article': page_mock}

        self.contest_mock = Mock()
        self.contest_mock.sql = Mock()
        self.contest_mock.sql.cursor = Mock()
        self.contest_mock.sql.commit = Mock()

        self.user_mock = Mock()
        self.user_mock.name = 'TestUser'
        self.user_mock.point_deductions = []
        self.user_mock.revisions = OrderedDict()
        self.user_mock.contest = Mock(return_value=self.contest_mock)

        self.article = Article(self.site_mock, self.user_mock, 'Test Article', ns='0')

    def test_init(self):
        """Test Article initialization"""
        self.assertEqual(self.article.name, 'Test Article')
        self.assertEqual(self.article.ns, '0')
        self.assertEqual(self.article.disqualified, False)
        self.assertIsInstance(self.article.revisions, OrderedDict)
        self.assertEqual(len(self.article.errors), 0)

    def test_key_property(self):
        """Test article key generation"""
        expected_key = 'test.wikipedia.org:Test Article'
        self.assertEqual(self.article.key, expected_key)

    def test_site_method(self):
        """Test site() method returns the site"""
        self.assertEqual(self.article.site(), self.site_mock)

    def test_user_method(self):
        """Test user() method returns the user"""
        self.assertEqual(self.article.user(), self.user_mock)

    def test_add_revision(self):
        """Test adding a revision to an article"""
        rev = self.article.add_revision(123, timestamp=1234567890, username='TestUser')

        self.assertIsInstance(rev, Revision)
        self.assertEqual(len(self.article.revisions), 1)
        self.assertIn(123, self.article.revisions)
        self.assertEqual(self.article.revisions[123], rev)

    def test_firstrev(self):
        """Test firstrev property returns first revision"""
        rev1 = self.article.add_revision(100, timestamp=1000000000, username='TestUser')
        rev2 = self.article.add_revision(200, timestamp=2000000000, username='TestUser')

        self.assertEqual(self.article.firstrev, rev1)

    def test_lastrev(self):
        """Test lastrev property returns last revision"""
        rev1 = self.article.add_revision(100, timestamp=1000000000, username='TestUser')
        rev2 = self.article.add_revision(200, timestamp=2000000000, username='TestUser')

        self.assertEqual(self.article.lastrev, rev2)

    def test_new_property_with_new_page(self):
        """Test new property returns True for new pages"""
        rev = self.article.add_revision(100, timestamp=1000000000, username='TestUser')
        rev.parentid = 0

        self.assertTrue(self.article.new)

    def test_new_property_with_existing_page(self):
        """Test new property returns False for existing pages"""
        rev = self.article.add_revision(100, timestamp=1000000000, username='TestUser')
        rev.parentid = 50

        self.assertFalse(self.article.new)

    def test_redirect_property(self):
        """Test redirect property"""
        rev = self.article.add_revision(100, timestamp=1000000000, username='TestUser')
        rev.text = '#REDIRECT [[Target]]'

        self.site_mock.redirect_regexp = Mock()
        self.site_mock.redirect_regexp.search = Mock(return_value=True)

        self.assertTrue(self.article.redirect)

    def test_new_non_redirect(self):
        """Test new_non_redirect property"""
        rev = self.article.add_revision(100, timestamp=1000000000, username='TestUser')
        rev.parentid = 0  # Makes it a new page
        rev.text = 'Normal content'

        # Mock redirect_regexp.match() to return None (no redirect)
        redirect_mock = Mock()
        redirect_mock.match = Mock(return_value=None)
        self.site_mock.redirect_regexp = redirect_mock

        self.assertTrue(self.article.new_non_redirect)

    def test_bytes_property(self):
        """Test bytes property calculation"""
        rev1 = self.article.add_revision(100, timestamp=1000000000, username='TestUser')
        rev1.size = 200
        rev1.parentsize = 50
        rev2 = self.article.add_revision(200, timestamp=2000000000, username='TestUser')
        rev2.size = 275
        rev2.parentsize = 200

        # rev1.bytes = 200-50 = 150, rev2.bytes = 275-200 = 75, total = 225
        self.assertEqual(self.article.bytes, 225)

    def test_words_property(self):
        """Test words property calculation"""
        rev1 = self.article.add_revision(100, timestamp=1000000000, username='TestUser')
        rev1.text = 'one two three four five six seven'  # 7 words
        rev1.parenttext = ''
        rev2 = self.article.add_revision(200, timestamp=2000000000, username='TestUser')
        rev2.text = 'one two three four five six seven eight nine ten'  # 10 words
        rev2.parenttext = 'one two three four five six seven'  # 7 words, so 3 new words

        # rev1 adds 7 words, rev2 adds 3 words, total = 10
        self.assertEqual(self.article.words, 10)

    def test_link_method(self):
        """Test link() method generates correct link"""
        expected_link = 'test.wikipedia.org:Test Article'
        self.assertEqual(self.article.link(), expected_link)

    def test_created_at_property(self):
        """Test created_at property"""
        # Test when _created_at is set
        test_time = pytz.utc.localize(datetime(2020, 1, 1, 12, 0, 0))
        self.article._created_at = test_time
        self.assertEqual(self.article.created_at, test_time)

    def test_created_at_from_firstrev(self):
        """Test created_at falls back to firstrev timestamp"""
        rev = self.article.add_revision(100, timestamp=1577836800.0, username='TestUser')  # 2020-01-01

        self.assertIsNotNone(self.article.created_at)
        self.assertIsInstance(self.article.created_at, datetime)

    def test_eq_method(self):
        """Test __eq__ method"""
        article2 = Article(self.site_mock, self.user_mock, 'Test Article', ns='0')
        article3 = Article(self.site_mock, self.user_mock, 'Different Article', ns='0')

        self.assertEqual(self.article, article2)
        self.assertNotEqual(self.article, article3)

    def test_repr_and_str(self):
        """Test __repr__ and __str__ methods"""
        expected = 'Article(test.wikipedia.org, Test Article, TestUser)'
        self.assertEqual(repr(self.article), expected)
        self.assertEqual(str(self.article), expected)

    def test_hash(self):
        """Test __hash__ method"""
        article2 = Article(self.site_mock, self.user_mock, 'Test Article', ns='0')

        self.assertEqual(hash(self.article), hash(article2))


if __name__ == '__main__':
    unittest.main()
