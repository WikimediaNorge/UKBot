# encoding=utf-8
# vim: fenc=utf-8 et sw=4 ts=4 sts=4 ai
from ..common import _
from ..contributions import UserContribution
from .rule import Rule
from .decorators import family


class NewPageRule(Rule):

    rule_name = 'new'

    @family('wikipedia.org', 'wikibooks.org', 'incubator.wikimedia.org', 'wikivoyage.org')
    def test(self, rev):
        if rev.new and not rev.redirect:
            yield UserContribution(rev=rev, points=self.points, rule=self, description=_('new page'))
