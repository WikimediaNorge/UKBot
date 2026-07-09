# encoding=utf-8
# vim: fenc=utf-8 et sw=4 ts=4 sts=4 ai
import re
import json
import logging
from jsonpath_rw import parse
from ..common import i18n
from ..contributions import UserContribution
from .rule import Rule
from .decorators import family

logger = logging.getLogger(__name__)


class WikidataRule(Rule):

    rule_name = 'wikidata'

    def __init__(self, sites, params, trans=None):
        """
        sourcepoints: points for adding new sources
        refpoints: points for referring to existing sources
        """

        Rule.__init__(self, sites, params, trans)

        self.labels = self.get_param('labels', datatype=list, default=[])
        self.labels = [re.sub('[^a-z-]', '', x.lower()) for x in self.labels]

        self.descriptions = self.get_param('descriptions', datatype=list, default=[])
        self.descriptions = [re.sub('[^a-z-]', '', x.lower()) for x in self.descriptions]

        self.aliases = self.get_param('aliases', datatype=list, default=[])
        self.aliases = [re.sub('[^a-z-]', '', x.lower()) for x in self.aliases]

        self.properties = self.get_param('properties', datatype=list, default=[])
        self.properties = [re.sub('[^P0-9]', '', property.upper()) for property in self.properties]

        self.require_reference = self.get_param('require_reference', datatype=bool, default=False)

        self.all = self.get_param('all', datatype=bool, default=False)

        self.matchers = {}
        for lang in self.labels:
            self.matchers['label:%s' % lang] = {
                'rules': [parse('labels.%s' % lang)],
                'msg': i18n('bot-rule-label-lang', lang),
                'opts': {'lang': lang},
            }
        for lang in self.descriptions:
            self.matchers['description:%s' % lang] = {
                'rule': [parse('descriptions.%s' % lang)],
                'msg': i18n('bot-rule-description-lang', lang),
                'opts': {'lang': lang},
            }
        for lang in self.aliases:
            self.matchers['alias:%s' % lang] = {
                'rule': [parse('aliases.%s' % lang)],
                'msg': i18n('bot-rule-alias-lang', lang),
                'opts': {'lang': lang},
            }
        for prop in self.properties:
            if self.require_reference:
                rules = [parse('claims.%s[*].references[0]' % prop)]
            else:
                rules = [
                    parse('claims.%s[*]' % prop),
                    parse('claims.*[*].qualifiers.%s[*]' % prop),
                ]
            self.matchers['prop:%s' % prop] = {
                'rules': rules,
                'msg': i18n('bot-rule-properties', '%(count)s', prop),
                'opts': {'property': prop},
            }

    def count(self, txt):
        out = {}
        for prop in self.matchers.keys():
            out[prop] = 0
        if txt == '':
            # New page
            return out
        data = json.loads(txt)
        for key, matcher in self.matchers.items():
            n = 0
            for jp in matcher['rules']:
                matches = jp.find(data)
                n += len(matches)
            out[key] = n

        return out

    @family('wikidata.org')
    def test(self, rev):
        try:
            before = self.count(rev.parenttext)
            after = self.count(rev.text)
        except json.decoder.JSONDecodeError:
            logger.error('Failed to parse Wikidata revision %s' % rev.revid)
            return
        added = {}
        report = {}

        for prop in self.matchers.keys():
            added[prop] = after[prop] - before[prop]

            if added[prop] > 0:
                if self.all is True:
                    report[prop] = added[prop]
                elif before[prop] == 0:
                    report[prop] = 1  # max 1

        if len(report.keys()) > 0:
            points = 0
            description = []

            for prop, n in report.items():
                points += n * self.points

                opts = self.matchers[prop]['opts']
                opts['count'] = n
                if 'msg_plural' in self.matchers[prop] and n > 1:
                    description.append(self.matchers[prop]['msg_plural'] % opts)
                else:
                    description.append(self.matchers[prop]['msg'] % opts)

            yield UserContribution(rev=rev, points=points, rule=self,
                                   description=', '.join(description))
