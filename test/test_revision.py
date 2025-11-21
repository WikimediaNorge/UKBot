# encoding=utf-8
import unittest
from unittest.mock import Mock
from datetime import datetime
import pytz
from collections import OrderedDict

from ukbot.revision import Revision


class TestRevision(unittest.TestCase):

    def setUp(self):
        # Create complete mock setup
        self.site_mock = Mock()
        self.site_mock.key = 'test.wikipedia.org'
        self.site_mock.host = 'test.wikipedia.org'
        self.site_mock.match_prefix = Mock(return_value=False)

        self.contest_mock = Mock()
        self.contest_mock.wiki_tz = pytz.timezone('UTC')

        self.user_mock = Mock()
        self.user_mock.name = 'TestUser'
        self.user_mock.point_deductions = []
        self.user_mock.revisions = OrderedDict()
        self.user_mock.contest = Mock(return_value=self.contest_mock)

        self.article = Mock()
        self.article.name = 'Test Article'
        self.article.site = Mock(return_value=self.site_mock)
        self.article.user = Mock(return_value=self.user_mock)

        self.timestamp = 1577836800.0  # 2020-01-01 00:00:00 UTC
        self.revision = Revision(self.article, 12345, timestamp=self.timestamp, username='TestUser')

    def test_init(self):
        """Test Revision initialization"""
        self.assertEqual(self.revision.revid, 12345)
        self.assertEqual(self.revision.username, 'TestUser')
        self.assertEqual(self.revision.text, '')
        self.assertEqual(self.revision.parenttext, '')
        self.assertFalse(self.revision.saved)
        self.assertFalse(self.revision.dirty)
        self.assertEqual(len(self.revision.errors), 0)

    def test_article_method(self):
        """Test article() method returns article"""
        self.assertEqual(self.revision.article(), self.article)

    def test_new_property_when_parentid_zero(self):
        """Test new property returns True when parentid is 0"""
        self.revision.parentid = 0
        self.assertTrue(self.revision.new)

    def test_new_property_when_has_parent(self):
        """Test new property returns False when parentid is not 0"""
        self.revision.parentid = 12344
        self.assertFalse(self.revision.new)

    def test_bytes_property(self):
        """Test bytes property calculates diff correctly"""
        self.revision.size = 500
        self.revision.parentsize = 300
        self.assertEqual(self.revision.bytes, 200)

    def test_bytes_property_negative(self):
        """Test bytes property when content removed"""
        self.revision.size = 300
        self.revision.parentsize = 500
        self.assertEqual(self.revision.bytes, -200)

    def test_words_property(self):
        """Test words property calculates word diff"""
        self.revision.text = 'one two three four five'
        self.revision.parenttext = 'one two three'

        # Should be 2 new words (four, five)
        self.assertEqual(self.revision.words, 2)

    def test_words_property_with_empty_parent(self):
        """Test words on new page"""
        self.revision.text = 'one two three'
        self.revision.parenttext = ''

        self.assertEqual(self.revision.words, 3)

    def test_redirect_property(self):
        """Test redirect property"""
        self.revision.text = '#REDIRECT [[Target Page]]'
        site_mock = Mock()
        site_mock.redirect_regexp = Mock()
        site_mock.redirect_regexp.search = Mock(return_value=True)
        self.article.site = Mock(return_value=site_mock)

        self.assertTrue(self.revision.redirect)

    def test_parentredirect_property(self):
        """Test parentredirect property"""
        self.revision.parenttext = '#REDIRECT [[Old Target]]'
        site_mock = Mock()
        site_mock.redirect_regexp = Mock()
        site_mock.redirect_regexp.search = Mock(return_value=True)
        self.article.site = Mock(return_value=site_mock)

        self.assertTrue(self.revision.parentredirect)

    def test_get_link(self):
        """Test get_link method"""
        homesite = Mock()
        homesite.host = 'test.wikipedia.org'
        homesite.link_to = Mock(return_value=':test:Test Article')

        link = self.revision.get_link(homesite)
        # Should contain revision ID in the link
        self.assertIn('12345', link)
        # When same host, should be just Special:Diff/revid
        self.assertEqual(link, 'Special:Diff/12345')

    def test_utc_property(self):
        """Test utc property returns UTC datetime"""
        utc_time = self.revision.utc
        self.assertIsInstance(utc_time, datetime)
        self.assertEqual(utc_time.tzinfo, pytz.UTC)

    def test_wiki_tz_property(self):
        """Test wiki_tz property returns datetime in wiki timezone"""
        wiki_time = self.revision.wiki_tz
        self.assertIsInstance(wiki_time, datetime)

    def test_repr_and_str(self):
        """Test __repr__ and __str__ methods"""
        expected = 'Revision(12345 of test.wikipedia.org:Test Article)'
        self.assertEqual(repr(self.revision), expected)
        self.assertEqual(str(self.revision), expected)

    def test_hash(self):
        """Test __hash__ method"""
        rev2 = Revision(self.article, 12345, timestamp=self.timestamp, username='TestUser')

        self.assertEqual(hash(self.revision), hash(rev2))

    def test_add_point_deduction(self):
        """Test add_point_deduction method"""
        self.revision.add_point_deduction(10, 'Test reason')

        self.assertEqual(len(self.revision.point_deductions), 1)
        self.assertEqual(self.revision.point_deductions[0][0], 10)
        self.assertEqual(self.revision.point_deductions[0][1], 'Test reason')


if __name__ == '__main__':
    unittest.main()
