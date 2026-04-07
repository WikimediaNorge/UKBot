# encoding=utf-8
# vim: fenc=utf-8 et sw=4 ts=4 sts=4 ai
from ..common import _
from ..contributions import UserContribution
from .rule import Rule
from .decorators import family


class ContribRule(Rule):

    rule_name = 'contrib'

    @family('wikipedia.org', 'wikibooks.org', 'incubator.wikimedia.org', 'wikivoyage.org')
    def test(self, rev):
        yield UserContribution(rev=rev, points=self.points, rule=self.rule,
                               description=_('contribution'))
