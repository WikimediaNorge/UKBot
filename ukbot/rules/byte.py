# encoding=utf-8
# vim: fenc=utf-8 et sw=4 ts=4 sts=4 ai
from ..common import i18n
from ..contributions import UserContribution
from .rule import Rule
from .decorators import family


class ByteRule(Rule):

    rule_name = 'byte'

    @family('wikipedia.org', 'wikibooks.org')
    def test(self, rev):
        bytes_added = rev.bytes

        if bytes_added > 0.:
            points = bytes_added * self.points
            yield UserContribution(rev=rev, points=points, rule=self,
                                   description=i18n('bot-rule-bytes', bytes_added))
