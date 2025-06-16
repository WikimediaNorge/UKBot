# encoding=utf-8
import re
from datetime import datetime
import unittest.mock as mock
from unittest import TestCase

import pytz

from ukbot.rules import RefRule, TemplateRemovalRule, ByteRule, WordRule, NewPageRule, WikidataRule, SectionRule
from ukbot.contributions import UserContribution
import unittest

from ukbot.revision import Revision
from ukbot.util import unix_time


class RuleTestCase(TestCase):
    def setUp(self) -> None:
        self.site = self.site_mock()
        self.article = self.article_mock(self.site)
        self.rev = self.make_rev(self.article)
        self.patcher1 = mock.patch('ukbot.sites.SiteManager')
        self.sites = self.patcher1.start()

    def tearDown(self) -> None:
        self.patcher1.stop()

    @staticmethod
    def site_mock():
        site = mock.Mock()
        site.host = 'test.wikipedia.org'
        site.redirect_regexp = re.compile(u'(?:%s)' % u'|'.join(['redirect']), re.I)
        return site

    @classmethod
    def page_mock(cls, name, aliases=None, site=None):
        page = mock.Mock()
        page.page_title = name
        page.site = site or cls.site_mock()
        page.backlinks.return_value = aliases or []
        return page

    @classmethod
    def user_mock(cls, site=None, point_deductions=[]):
        user = mock.Mock()
        user.site = site or cls.site_mock()
        user.point_deductions = point_deductions or []
        return user

    @classmethod
    def article_mock(cls, site=None, user=None):
        article = mock.Mock()
        article.site.return_value = site or cls.site_mock()
        article.user.return_value = user or cls.user_mock()
        return article

    def make_rev(self, article=None, ts=None):
        ts = ts or unix_time(pytz.utc.localize(datetime.now()))
        article = article or self.article
        rev = Revision(article, 1, timestamp=ts)
        rev.timestamp = pytz.utc.localize(datetime.now())
        return rev


class TestNewPageRule(RuleTestCase):

    def test_it_gives_points_for_new_pages_on_wikipedia(self):
        self.site.host = 'no.wikipedia.org'
        self.rev.parentid = 0

        points_per_page = 5
        rule = NewPageRule(self.sites, {2: points_per_page})
        contribs = list(rule.test(self.rev))

        assert len(contribs) == 1
        assert 5 == contribs[0].points

    def test_it_does_not_give_points_for_new_pages_on_wikidata(self):
        self.site.host = 'www.wikidata.org'
        self.rev.parentid = 0

        points_per_page = 5
        rule = NewPageRule(self.sites, {2: points_per_page})
        contribs = list(rule.test(self.rev))

        assert len(contribs) == 0


class TestByteRule(RuleTestCase):

    def test_it_gives_points_for_text_addition(self):
        self.rev.size = 190
        self.rev.parentsize = 90
        points_per_byte = 0.1

        rule = ByteRule(self.sites, {2: points_per_byte})
        contribs = list(rule.test(self.rev))

        assert len(contribs) == 1
        assert 10 == contribs[0].points

    def test_it_does_not_give_negative_points_for_text_removal(self):
        self.rev.size = 90
        self.rev.parentsize = 190
        points_per_byte = 0.1

        rule = ByteRule(self.sites, {2: points_per_byte})
        contribs = list(rule.test(self.rev))

        assert len(contribs) == 0


class TestWordRule(RuleTestCase):

    def test_it_gives_points_for_text_addition(self):
        self.rev.text = 'Lorem ipsum dolor sit amet'
        self.rev.parenttext = 'Lorem sit amet'
        points_per_word = 0.25

        rule = WordRule(self.sites, {2: points_per_word})
        contribs = list(rule.test(self.rev))

        assert len(contribs) == 1
        assert 0.5 == contribs[0].points

    def test_it_does_not_give_negative_points_for_text_removal(self):
        self.rev.text = 'Lorem sit amet'
        self.rev.parenttext = 'Lorem ipsum dolor sit amet'
        points_per_word = 0.25

        rule = WordRule(self.sites, {2: points_per_word})
        contribs = list(rule.test(self.rev))

        assert len(contribs) == 0


class TestTemplateRemovalRule(RuleTestCase):

    def test_it_yields_nothing_by_default(self):
        self.rev.text = '<root>Hello</root>'
        self.rev.parenttext = '<root>Hello world</root>'
        self.sites.resolve_page.return_value = self.page_mock('World', [])

        rule = TemplateRemovalRule(self.sites, {2: 10, 3: 'world'})
        contribs = list(rule.test(self.rev))

        assert len(rule.templates) == 1
        assert len(contribs) == 0

    def test_it_gives_points_for_template_removal(self):
        points_per_template = 10
        self.rev.text = '<root>Hello</root>'
        self.rev.parenttext = '<root>Hello {{world}} {{ world }} {{ world | param | 3=other param }}</root>'

        self.sites.resolve_page.return_value = self.page_mock('World', [], site=self.site)
        rule = TemplateRemovalRule(self.sites, {2: points_per_template, 3: 'world'})
        contribs = list(rule.test(self.rev))

        assert len(rule.templates) == 1
        assert len(contribs) == 1
        assert contribs[0].points == points_per_template * 3


class TestRefRule(RuleTestCase):

    def test_it_yields_nothing_by_default(self):
        self.rev.text = '<root>Hello</root>'
        self.rev.parenttext = '<root>Hello world</root>'

        rule = RefRule(self.sites, {2: 10, 3: 20})
        contribs = list(rule.test(self.rev))

        assert len(contribs) == 0

    def test_it_gives_points_for_addition_of_ref_tags(self):
        self.rev.text = '<root>Hello <ref>world</ref><ref>other</ref></root>'
        self.rev.parenttext = '<root>Hello</root>'

        rule = RefRule(self.sites, {2: 100, 3: 1})
        contribs = list(rule.test(self.rev))

        assert len(contribs) == 1
        assert isinstance(contribs[0], UserContribution)
        assert contribs[0].rev == self.rev
        assert contribs[0].points == 100 * 2

    def test_it_ignores_refs_within_comments(self):
        self.rev.text = '<root>Hello <!-- <ref>world</ref> --></root>'
        self.rev.parenttext = '<root>Hello</root>'
        rule = RefRule(self.sites, {2: 100, 3: 1})
        contribs = list(rule.test(self.rev))

        assert len(contribs) == 0


class TestWikidataRule(RuleTestCase):

    translations = {
        'properties': 'egenskaper',
        'require_reference': 'krevreferanse',
        'all': 'alle',
        'labels': 'etiketter',
        'aliases': 'alias',
        'descriptions': 'descriptions',
    }

    def test_it_gives_points_for_the_first_statement_added(self):
        # If no P18 statements exist, points will be given for the first P18
        # statement added, but not for additional ones.
        self.site.host = 'www.wikidata.org'
        self.rev.text = '{"claims": {"P18": [{},{},{}]}}'
        self.rev.parenttext = '{"claims": {}}'

        points_per_claim = 5
        rule = WikidataRule(self.sites, {
            2: points_per_claim,
            'egenskaper': 'P18',
        }, self.translations)
        contribs = list(rule.test(self.rev))

        assert len(contribs) == 1
        assert 5 == contribs[0].points

    def test_it_does_not_give_points_for_additional_statements_added(self):
        # If a P18 statement already exists, points will not be given for
        # additional P18 statement added.
        self.site.host = 'www.wikidata.org'
        self.rev.text = '{"claims": {"P18": [{},{}]}}'
        self.rev.parenttext = '{"claims": {"P18": [{}]}}'

        points_per_claim = 5
        rule = WikidataRule(self.sites, {
            2: points_per_claim,
            'egenskaper': 'P18',
        }, self.translations)
        contribs = list(rule.test(self.rev))

        assert len(contribs) == 0

    def test_it_gives_points_for_additional_statements_added_if_specified(self):
        self.site.host = 'www.wikidata.org'
        self.rev.text = '{"claims": {"P18": [{},{}]}}'
        self.rev.parenttext = '{"claims": {"P18": [{}]}}'

        points_per_claim = 5
        rule = WikidataRule(self.sites, {
            2: points_per_claim,
            'egenskaper': 'P18',
            'alle': 'ja',
        }, self.translations)
        contribs = list(rule.test(self.rev))

        assert len(contribs) == 1
        assert 5 == contribs[0].points

    def test_it_gives_points_for_qualifiers_added(self):
        self.site.host = 'www.wikidata.org'
        self.rev.text = '{"claims": {"P18": [{"qualifiers": {"P2096": [{}]}}]}}'
        self.rev.parenttext = '{"claims": {}}'

        points_per_claim = 5
        rule = WikidataRule(self.sites, {
            2: points_per_claim,
            'egenskaper': 'P2096',
        }, self.translations)
        contribs = list(rule.test(self.rev))

        assert len(contribs) == 1
        assert 5 == contribs[0].points

    def test_it_does_not_give_points_if_required_reference_not_included(self):
        self.site.host = 'www.wikidata.org'
        self.rev.text = '{"claims": {"P20": [{}]}}'
        self.rev.parenttext = '{"claims": {}}'

        points_per_claim = 5
        rule = WikidataRule(self.sites, {
            2: points_per_claim,
            'egenskaper': 'P20',
            'krevreferanse': 'ja',
        }, self.translations)
        contribs = list(rule.test(self.rev))

        assert len(contribs) == 0

    def test_it_does_give_points_if_required_reference_is_included(self):
        self.site.host = 'www.wikidata.org'
        self.rev.text = '{"claims": {"P20": [{"rank": "normal", "references": [{}]}]}}'
        self.rev.parenttext = '{"claims": {}}'

        points_per_claim = 5
        rule = WikidataRule(self.sites, {
            2: points_per_claim,
            'egenskaper': 'P20',
            'krevreferanse': 'ja',
        }, self.translations)
        contribs = list(rule.test(self.rev))

        assert len(contribs) == 1

    def test_it_gives_points_for_adding_labels_in_specified_languages(self):
        self.site.host = 'www.wikidata.org'
        self.rev.text = '{"labels": {"fi": {"language": "fi", "value": "Test"}, "en": {"language": "en", "value": "Test"}}}'
        self.rev.parenttext = '{"labels": {"en": {"language": "en", "value": "Test"}}}'

        points_per_claim = 5
        rule = WikidataRule(self.sites, {
            2: points_per_claim,
            'etiketter': 'se,fi',
        }, self.translations)
        contribs = list(rule.test(self.rev))

        assert len(contribs) == 1
        assert 5 == contribs[0].points


class TestSectionRule(RuleTestCase):

    translations = {
        'description': 'description',
    }

    def test_it_yields_nothing_by_default(self):
        self.rev.text = 'Lorem ipsum\n== Kilder ==\nLorem ipsum'
        self.rev.parenttext = 'Lorem ipsum\nLorem ipsum'
        self.sites.resolve_page.return_value = self.page_mock('World', [])

        rule = SectionRule(self.sites, {2: 10, 3: 'Referans[ea]r'}, self.translations)
        contribs = list(rule.test(self.rev))

        assert len(contribs) == 0

    def test_it_gives_points_for_adding_section(self):
        self.rev.text = 'Lorem ipsum\n== Referanser ==\nLorem ipsum'
        self.rev.parenttext = 'Lorem ipsum\nLorem ipsum'
        self.sites.resolve_page.return_value = self.page_mock('World', [])

        rule = SectionRule(self.sites, {2: 10, 3: 'Referans[ea]r'}, self.translations)
        contribs = list(rule.test(self.rev))

        assert len(contribs) == 1
        assert contribs[0].points == 10


if __name__ == '__main__':
    unittest.main()
