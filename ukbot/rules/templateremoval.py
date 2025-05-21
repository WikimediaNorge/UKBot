# encoding=utf-8
# vim: fenc=utf-8 et sw=4 ts=4 sts=4 ai
import logging

from ..common import _
from ..contributions import UserContribution
from .rule import Rule
from .decorators import family

logger = logging.getLogger(__name__)


class TemplateRemovalRule(Rule):

    rule_name = 'templateremoval'

    def __init__(self, sites, params, trans=None):
        Rule.__init__(self, sites, params, trans)

        templates = [str(tpl_name).strip() for tpl_name in self.get_anon_params()]
        templates = [
            self.sites.resolve_page(tpl_name, 10, True)
            for tpl_name in templates
            if tpl_name != ''
        ]

        # Make page_name -> [aliases] map
        self.templates = []
        logger.info('Initializing TemplateRemovalRule with %d templates:', len(templates))
        for page in templates:
            tpl = {
                'site': page.site,
                'name': page.page_title,
                'values': [page.page_title.lower()],
                'total': 0,
            }
            if page.exists:
                for alias in page.backlinks(filterredir='redirects'):
                    tpl['values'].append(alias.page_title.lower())
            self.templates.append(tpl)

            logger.info('  - Template site="%s" name="%s", aliases="%s"',
                        tpl['site'].host, tpl['name'], ','.join(tpl['values']))

    @staticmethod
    def matches_template(template, text):
        """Check if the text matches the template name or any of its aliases. Supports wildcards."""
        text = text.lower()
        for tpl_name in template['values']:
            if tpl_name[0] == '*' and tpl_name[-1] == '*' and text.find(tpl_name[1:-1]) != -1:
                return True
            elif tpl_name[0] == '*' and text.endswith(tpl_name[1:]):
                return True
            elif tpl_name[-1] == '*' and text.startswith(tpl_name[:-1]):
                return True
            elif text == tpl_name:
                return True
        return False

    def count_instances(self, template, parsed_text):
        """Count the number of instances of a template in a given text."""
        tc = 0
        for node in parsed_text.templates.doc.findall('.//template'):
            for elem in node:
                if (elem.tag == 'title') and (elem.text is not None):
                    if self.matches_template(template, elem.text.strip()):
                        tc += 1
        return tc

    def get_templates_removed(self, template, rev):
        pt = self.count_instances(template, rev.te_parenttext())
        ct = self.count_instances(template, rev.te_text())
        return pt - ct

    @family('wikipedia.org', 'wikibooks.org')
    def test(self, rev):
        if rev.redirect or rev.parentredirect:
            # skip redirects
            return

        for template in self.templates:
            if template['site'] != rev.article().site():
                continue
            removed = self.get_templates_removed(template, rev)
            if removed > 0:
                template['total'] += removed
                # Add language code if template is from a different site
                lang_code = ''
                if 'lang' in template['site'].site and template['site'].site['lang'] != self.sites.homesite.site['lang']:
                    lang_code = '|' + template['site'].site['lang']
                yield UserContribution(
                    rev=rev,
                    rule=self,
                    points=removed * self.points,
                    description=_('removal of {{tl|%(template)s%(lang)s}}') % {'template': template['name'], 'lang': lang_code}
                )

