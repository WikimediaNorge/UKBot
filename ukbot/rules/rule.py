# encoding=utf-8
# vim: fenc=utf-8 et sw=4 ts=4 sts=4 ai
from ..common import _
from ..contributions import UserContribution
from .decorators import family


class Rule(object):
    rule_name: str = ""

    def __init__(self, sites, params, trans=None):
        self.sites = sites
        self.params = params
        self.trans = trans or {}
        self.points = float(params[2])

    def get_param(self, name, default=None, datatype=str):
        if isinstance(name, int):
            value = self.params.get(name)
        else:
            value = self.params.get(self.trans[name])
        if value is None:
            return default
        if datatype == list:
            return [x.strip() for x in str(value).split(',')]
        return datatype(value)

    def get_anon_params(self):
        tmp = {}
        for key, val in self.params.items():
            if type(key) == int:
                tmp[key] = val
        lst = []
        for param in range(3, max(tmp.keys()) + 1):
            lst.append(tmp[param])
        return lst

    @property
    def maxpoints(self):
        return self.get_param('maxpoints', datatype=float)

    @property
    def site(self):
        return self.get_param('site', datatype=list)

    @property
    def key(self):
        return self.trans[self.rule_name]


class BonusRule(Rule):

    def __init__(self, sites, params, trans=None):
        Rule.__init__(self, sites, params, trans)
        self.limit = self.get_param(3, datatype=int)

    def get_metric(self, rev):
        raise NotImplementedError()  # Should be overridden

    @family('wikipedia.org', 'wikibooks.org')
    def test(self, current_rev):
        total = 0
        this_rev = False
        passed_limit = False
        for rev in current_rev.article().revisions.values():
            total += self.get_metric(rev)

            if passed_limit is False and total >= self.limit:
                passed_limit = True
                if rev == current_rev:
                    this_rev = True

        if total >= self.limit and this_rev is True:
            yield UserContribution(rev=current_rev, points=self.points, rule=self,
                                   description=_('bonus %(words)d words') % {'words': self.limit})
