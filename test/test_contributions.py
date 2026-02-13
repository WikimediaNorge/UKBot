# encoding=utf-8
import unittest
from unittest.mock import Mock
from datetime import datetime
import pytz

from ukbot.contributions import UserContribution, UserContributions


class TestUserContribution(unittest.TestCase):

    def setUp(self):
        # Create mocks with specific return values
        article_mock = Mock(name='Test Article')
        site_mock = Mock(key='test.wiki')
        user_mock = Mock(name='TestUser')

        article_mock.site = Mock(return_value=site_mock)
        article_mock.user = Mock(return_value=user_mock)

        self.rev = Mock()
        self.rev.revid = 12345
        self.rev.article = Mock(return_value=article_mock)

        self.rule = Mock()
        self.rule.__class__.__name__ = 'TestRule'

        self.contribution = UserContribution(
            rev=self.rev,
            points=10.5,
            rule=self.rule,
            description='Test contribution'
        )

    def test_init(self):
        """Test UserContribution initialization"""
        self.assertEqual(self.contribution.rev, self.rev)
        self.assertEqual(self.contribution.points, 10.5)
        self.assertEqual(self.contribution.rule, self.rule)
        self.assertEqual(self.contribution.description, 'Test contribution')

    def test_is_negative_false(self):
        """Test is_negative returns False for positive points"""
        self.assertFalse(self.contribution.is_negative())

    def test_is_negative_true(self):
        """Test is_negative returns True for negative points"""
        negative_contrib = UserContribution(
            rev=self.rev,
            points=-5.0,
            rule=self.rule,
            description='Negative'
        )
        self.assertTrue(negative_contrib.is_negative())

    def test_article_property(self):
        """Test article property returns article"""
        self.assertEqual(self.contribution.article, self.rev.article())

    def test_site_property(self):
        """Test site property returns site"""
        self.assertEqual(self.contribution.site, self.rev.article().site())

    def test_user_property(self):
        """Test user property returns user"""
        self.assertEqual(self.contribution.user, self.rev.article().user())


class TestUserContributions(unittest.TestCase):

    def setUp(self):
        self.user = Mock()
        self.user.name = 'TestUser'
        self.user.point_deductions = []
        self.user.suspended_since = None
        self.user.disqualified_articles = []

        self.config = {
            'point_caps': {},
            'wikidata_languages': ['en', 'es', 'fi']
        }

        self.contributions = UserContributions(self.user, self.config)

    def test_init(self):
        """Test UserContributions initialization"""
        # Note: self.contributions.user is a weakref, so we need to call it
        self.assertEqual(self.contributions.user(), self.user)
        self.assertEqual(self.contributions.wikidata_languages, ['en', 'es', 'fi'])
        self.assertEqual(len(self.contributions.contributions), 0)

    def test_add_contribution(self):
        """Test adding a contribution"""
        article_mock = Mock(name='Article')
        rev = Mock()
        rev.revid = 100
        rev.article = Mock(return_value=article_mock)

        rule = Mock()
        rule.maxpoints = None  # No capping

        contrib = UserContribution(
            rev=rev,
            points=10,
            rule=rule,
            description='Test'
        )

        self.contributions.add(contrib)

        self.assertEqual(len(self.contributions.contributions), 1)
        self.assertIn(contrib, self.contributions.contributions)

    def test_get_all_contributions(self):
        """Test getting all contributions"""
        article_mock = Mock(name='Article')
        rev = Mock()
        rev.revid = 100
        rev.article = Mock(return_value=article_mock)

        rule = Mock()
        rule.maxpoints = None  # No capping

        contrib1 = UserContribution(rev=rev, points=10, rule=rule, description='Test1')
        contrib2 = UserContribution(rev=rev, points=5, rule=rule, description='Test2')

        self.contributions.add(contrib1)
        self.contributions.add(contrib2)

        all_contribs = self.contributions.get()
        self.assertEqual(len(all_contribs), 2)

    def test_get_contributions_by_revision(self):
        """Test getting contributions by revision"""
        article1_mock = Mock(name='Article1')
        rev1 = Mock()
        rev1.revid = 100
        rev1.article = Mock(return_value=article1_mock)

        article2_mock = Mock(name='Article2')
        rev2 = Mock()
        rev2.revid = 200
        rev2.article = Mock(return_value=article2_mock)

        rule = Mock()
        rule.maxpoints = None  # No capping

        contrib1 = UserContribution(rev=rev1, points=10, rule=rule, description='Test1')
        contrib2 = UserContribution(rev=rev2, points=5, rule=rule, description='Test2')

        self.contributions.add(contrib1)
        self.contributions.add(contrib2)

        rev1_contribs = self.contributions.get(revision=rev1)
        self.assertEqual(len(rev1_contribs), 1)
        self.assertEqual(rev1_contribs[0], contrib1)

    def test_sum_contributions(self):
        """Test summing all contribution points"""
        article_mock = Mock(name='Article')
        article_mock.key = 'test:Article'

        rev = Mock()
        rev.revid = 100
        rev.article = Mock(return_value=article_mock)
        rev.utc = pytz.utc.localize(datetime(2020, 1, 1))
        rev.point_deductions = []

        article_mock.revisions = {100: rev}

        rule = Mock()
        rule.maxpoints = None  # No capping

        contrib1 = UserContribution(rev=rev, points=10, rule=rule, description='Test1')
        contrib2 = UserContribution(rev=rev, points=5.5, rule=rule, description='Test2')

        self.contributions.add(contrib1)
        self.contributions.add(contrib2)

        # Sum using get_article_points
        total = self.contributions.get_article_points(article_mock)
        self.assertEqual(total, 15.5)


if __name__ == '__main__':
    unittest.main()
