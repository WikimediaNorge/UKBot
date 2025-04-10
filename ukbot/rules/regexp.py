# encoding=utf-8
# vim: fenc=utf-8 et sw=4 ts=4 sts=4 ai
import re
import logging
from ..common import i18n
from ..contributions import UserContribution
from .rule import Rule

logger = logging.getLogger(__name__)


class RegexpRule(Rule):

    rule_name = 'regexp'

    def __init__(self, sites, template, trans=None):
        Rule.__init__(self, sites, template, trans)
        self.total = 0
        self.description = self.get_param('description', datatype=str, default=i18n('bot-rule-regexp'))
        self.patterns = [re.compile(str(pattern).strip()) for pattern in self.get_anon_params()]

    def has_pattern(self, txt):
        for pattern in self.patterns:
            if pattern.search(txt):
                return True

        return False

    def test(self, rev):
        had_pattern = self.has_pattern(rev.parenttext)
        has_pattern = self.has_pattern(rev.text)

        if has_pattern and not had_pattern:
            self.total += 1
            yield UserContribution(rev=rev, points=self.points, rule=self, description=self.description)


class SectionRule(RegexpRule):

    rule_name = 'section'

    def __init__(self, sites, template, trans=None):
        RegexpRule.__init__(self, sites, template, trans)
        self.description = self.get_param('description', datatype=str, default=i18n('bot-rule-section'))
        self.patterns = [re.compile(r'\n===?\s*(?:%s)\s*===?\s*\n' % pattern.pattern) for pattern in self.patterns]
