# encoding=utf-8
# vim: fenc=utf-8 et sw=4 ts=4 sts=4 ai
import re
from lxml.html import fromstring
import lxml
from mwtextextractor import condition_for_lxml

from ..common import _
from ..contributions import UserContribution
from .rule import Rule
from .decorators import family


class RefRule(Rule):

    rule_name = 'ref'

    def __init__(self, sites, template, trans=None):
        """
        sourcepoints: points for adding new sources
        refpoints: points for referring to existing sources
        """
        Rule.__init__(self, sites, template, trans)
        self.sourcepoints = self.get_param(2, datatype=float)
        self.refpoints = self.get_param(3, datatype=float)
        self.totalsources = 0

    @staticmethod
    def count_sources(txt):

        s1 = 0  # kilder
        r1 = 0  # kildehenvisninger

        # Count all <ref> tags
        try:
            if txt == '':
                return 0, 0
            xml = fromstring(condition_for_lxml(re.sub(r'<!--.*?-->', '', txt, flags=re.I)))
            allref1 = xml.findall('.//ref')
            for tag in allref1:
                if tag.text is None:
                    r1 += 1
                else:
                    s1 += 1
            del xml
        except lxml.etree.XMLSyntaxError:
            s1 = 0
            r1 = 0

        # Count list item under section heading "Kilder" or "Kjelder"
        refsection = False
        for line in txt.split('\n'):
            if refsection:
                if re.match(r'==', line):
                    refsection = False
                    continue
                if re.match(r'\*', line):
                    s1 += 1
            elif re.match(r'==[\s]*(Kilder|Kjelder|Gáldut)[\s]*==', line):
                refsection = True

        return s1, r1

    @family('wikipedia.org', 'wikibooks.org')
    def test(self, rev):

        s1, r1 = self.count_sources(rev.parenttext)
        s2, r2 = self.count_sources(rev.text)

        sources_added = s2 - s1
        refs_added = r2 - r1

        self.totalsources += sources_added

        if sources_added > 0 or refs_added > 0:
            points = 0.
            description = []

            if sources_added > 0:
                points += sources_added * self.sourcepoints
                description.append(_('references') % {'num': sources_added})

            if refs_added > 0:
                points += refs_added * self.refpoints
                description.append(_('reference pointers') % {'num': refs_added})

            yield UserContribution(rev=rev, points=points, rule=self,
                                   description=', '.join(description))
