# encoding=utf-8
# vim: fenc=utf-8 et sw=4 ts=4 sts=4 ai
from ..common import i18n
from ..contributions import UserContribution
from .rule import Rule
from .decorators import family


class QualiRule(Rule):

    rule_name = 'qualified'

    def __init__(self, sites, template, trans=None):
        Rule.__init__(self, sites, template, trans)
        self.articles_seen = set()

    @family('wikipedia.org', 'wikibooks.org')
    def test(self, rev):
        if rev.article().key not in self.articles_seen:
            self.articles_seen.add(rev.article().key)
            yield UserContribution(rev=rev, points=self.points, rule=self,
                                   description=i18n('bot-rule-eligible'))
