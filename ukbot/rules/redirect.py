# encoding=utf-8
# vim: fenc=utf-8 et sw=4 ts=4 sts=4 ai
from ..common import i18n
from ..contributions import UserContribution
from .rule import Rule
from .decorators import family


class RedirectRule(Rule):

    rule_name = 'redirect'

    @family('wikipedia.org', 'wikibooks.org')
    def test(self, rev):
        if rev.new and rev.redirect:
            yield UserContribution(rev=rev, points=self.points, rule=self, description=i18n('bot-rule-redirect'))
