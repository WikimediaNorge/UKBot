# encoding=utf-8
# vim: fenc=utf-8 et sw=4 ts=4 sts=4 ai
from ..common import i18n
from ..contributions import UserContribution
from .rule import Rule
from .decorators import family


class WordRule(Rule):

    rule_name = 'word'

    @family('wikipedia.org', 'wikibooks.org')
    def test(self, rev):
        words_added = rev.words

        if words_added > 0.:
            points = words_added * self.points
            yield UserContribution(rev=rev, points=points, rule=self,
                                   description=i18n('bot-rule-words', words_added))
