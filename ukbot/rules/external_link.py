# encoding=utf-8
# vim: fenc=utf-8 et sw=4 ts=4 sts=4 ai
import re

from ..common import i18n
from ..contributions import UserContribution
from .rule import Rule
from .decorators import family


class ExternalLinkRule(Rule):

    rule_name = 'external_link'

    @staticmethod
    def count_links(txt):
        # We don't want to include links in references as these are covered by the RefRule
        txt = re.sub(r'<ref[^>]*>.*?</ref>', '', txt, flags=re.MULTILINE)
        return len(re.findall(r'(?<!\[)\[[^\[\] ]+ [^\[\]]+\](?!])', txt))

    @family('wikipedia.org', 'wikibooks.org')
    def test(self, rev):
        links_before = self.count_links(rev.parenttext)
        links_after = self.count_links(rev.text)
        links_added = links_after - links_before

        if links_added > 0:
            points = links_added * self.points
            yield UserContribution(rev=rev, points=points, rule=self,
                                   description=i18n('bot-rule-links', links_added))
