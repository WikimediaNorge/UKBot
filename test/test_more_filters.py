# encoding=utf-8
import unittest
from unittest.mock import Mock, MagicMock
from collections import OrderedDict

from ukbot.filters import (
    ByteFilter, NewPageFilter, ExistingPageFilter,
    NamespaceFilter, PageFilter, TemplateFilter
)


class TestByteFilter(unittest.TestCase):

    def test_byte_filter_keeps_articles_above_limit(self):
        """Test ByteFilter keeps articles above byte limit"""
        sites = Mock()
        byte_filter = ByteFilter(sites, bytelimit=1000)

        article1 = Mock()
        article1.bytes = 1500
        article1.key = 'site:Article1'

        article2 = Mock()
        article2.bytes = 500
        article2.key = 'site:Article2'

        articles = OrderedDict([
            ('site:Article1', article1),
            ('site:Article2', article2)
        ])

        result = byte_filter.filter(articles)

        self.assertEqual(len(result), 1)
        self.assertIn('site:Article1', result)
        self.assertNotIn('site:Article2', result)

    def test_test_page_method(self):
        """Test test_page method"""
        sites = Mock()
        byte_filter = ByteFilter(sites, bytelimit=1000)

        page = Mock()
        page.bytes = 1500

        self.assertTrue(byte_filter.test_page(page))

        page.bytes = 500
        self.assertFalse(byte_filter.test_page(page))


class TestNewPageFilter(unittest.TestCase):

    def test_new_page_filter_keeps_new_pages(self):
        """Test NewPageFilter keeps pages created within contest timeframe"""
        from datetime import datetime
        import pytz

        sites = Mock()
        contest = Mock()
        contest.start = pytz.utc.localize(datetime(2020, 1, 1))
        contest.end = pytz.utc.localize(datetime(2020, 12, 31))

        npf = NewPageFilter(sites, contest, redirects=False)

        # Page created within contest
        page1 = Mock()
        page1.created_at = pytz.utc.localize(datetime(2020, 6, 15))
        page1.redirect = False

        # Page created before contest
        page2 = Mock()
        page2.created_at = pytz.utc.localize(datetime(2019, 6, 15))
        page2.redirect = False

        self.assertTrue(npf.test_page(page1))
        self.assertFalse(npf.test_page(page2))

    def test_new_page_filter_excludes_redirects(self):
        """Test NewPageFilter excludes redirects by default"""
        from datetime import datetime
        import pytz

        sites = Mock()
        contest = Mock()
        contest.start = pytz.utc.localize(datetime(2020, 1, 1))
        contest.end = pytz.utc.localize(datetime(2020, 12, 31))

        npf = NewPageFilter(sites, contest, redirects=False)

        page = Mock()
        page.created_at = pytz.utc.localize(datetime(2020, 6, 15))
        page.redirect = True

        self.assertFalse(npf.test_page(page))

    def test_new_page_filter_includes_redirects_when_enabled(self):
        """Test NewPageFilter includes redirects when enabled"""
        from datetime import datetime
        import pytz

        sites = Mock()
        contest = Mock()
        contest.start = pytz.utc.localize(datetime(2020, 1, 1))
        contest.end = pytz.utc.localize(datetime(2020, 12, 31))

        npf = NewPageFilter(sites, contest, redirects=True)

        page = Mock()
        page.created_at = pytz.utc.localize(datetime(2020, 6, 15))
        page.redirect = True

        self.assertTrue(npf.test_page(page))


class TestExistingPageFilter(unittest.TestCase):

    def test_existing_page_filter_keeps_old_pages(self):
        """Test ExistingPageFilter keeps pages created before contest"""
        from datetime import datetime
        import pytz

        sites = Mock()
        contest = Mock()
        contest.start = pytz.utc.localize(datetime(2020, 1, 1))

        epf = ExistingPageFilter(sites, contest)

        # Page created before contest
        page1 = Mock()
        page1.created_at = pytz.utc.localize(datetime(2019, 6, 15))

        # Page created during/after contest
        page2 = Mock()
        page2.created_at = pytz.utc.localize(datetime(2020, 6, 15))

        self.assertTrue(epf.test_page(page1))
        self.assertFalse(epf.test_page(page2))


class TestNamespaceFilter(unittest.TestCase):

    def test_namespace_filter_filters_by_namespace(self):
        """Test NamespaceFilter filters by namespace"""
        sites = Mock()
        nsf = NamespaceFilter(sites, namespaces=['0', '14'], site=None)

        page1 = Mock()
        page1.ns = '0'
        page1.site = Mock(return_value=Mock(key='site1'))

        page2 = Mock()
        page2.ns = '14'
        page2.site = Mock(return_value=Mock(key='site1'))

        page3 = Mock()
        page3.ns = '2'
        page3.site = Mock(return_value=Mock(key='site1'))

        self.assertTrue(nsf.test_page(page1))
        self.assertTrue(nsf.test_page(page2))
        self.assertFalse(nsf.test_page(page3))

    def test_namespace_filter_with_site_restriction(self):
        """Test NamespaceFilter with site restriction"""
        sites = Mock()
        nsf = NamespaceFilter(sites, namespaces=['0'], site=['site1'])

        page1 = Mock()
        page1.ns = '0'
        page1.site = Mock(return_value=Mock(key='site1'))

        page2 = Mock()
        page2.ns = '0'
        page2.site = Mock(return_value=Mock(key='site2'))

        self.assertTrue(nsf.test_page(page1))
        self.assertFalse(nsf.test_page(page2))


class TestPageFilter(unittest.TestCase):

    def test_page_filter_filters_specific_pages(self):
        """Test PageFilter filters specific pages"""
        sites = Mock()

        page_mock1 = Mock()
        page_mock1.site = Mock(key='site1')
        page_mock1.name = 'Page1'

        page_mock2 = Mock()
        page_mock2.site = Mock(key='site1')
        page_mock2.name = 'Page2'

        pf = PageFilter(sites, pages=[page_mock1, page_mock2])

        # Test page that should match
        test_page1 = Mock()
        test_page1.key = 'site1:Page1'

        # Test page that should not match
        test_page2 = Mock()
        test_page2.key = 'site1:Page3'

        self.assertTrue(pf.test_page(test_page1))
        self.assertFalse(pf.test_page(test_page2))


class TestTemplateFilter(unittest.TestCase):

    def test_text_contains_template(self):
        """Test TemplateFilter detects templates in text"""
        sites = Mock()
        sites.homesite = Mock()

        # Mock template page
        template_page = Mock()
        template_page.exists = False
        template_page.backlinks = Mock(return_value=[])

        # Use MagicMock for pages to support __getitem__
        pages_mock = MagicMock()
        pages_mock.__getitem__ = Mock(return_value=template_page)
        sites.homesite.pages = pages_mock

        tf = TemplateFilter(sites, templates=['stub'], include_aliases=True)

        text_with_template = '{{stub}} This is stub text'
        text_without_template = 'This is normal text'

        self.assertIsNotNone(tf.text_contains_template(text_with_template))
        self.assertIsNone(tf.text_contains_template(text_without_template))

    def test_template_filter_with_parameters(self):
        """Test TemplateFilter handles templates with parameters"""
        sites = Mock()
        sites.homesite = Mock()

        template_page = Mock()
        template_page.exists = False
        template_page.backlinks = Mock(return_value=[])

        # Use MagicMock for pages to support __getitem__
        pages_mock = MagicMock()
        pages_mock.__getitem__ = Mock(return_value=template_page)
        sites.homesite.pages = pages_mock

        tf = TemplateFilter(sites, templates=['citation'], include_aliases=True)

        text = '{{citation|author=Smith|year=2020}}'

        self.assertIsNotNone(tf.text_contains_template(text))


if __name__ == '__main__':
    unittest.main()
