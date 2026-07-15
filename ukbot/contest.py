# encoding=utf-8
# vim: fenc=utf-8 et sw=4 ts=4 sts=4 ai
import re
import sys
import time
import numpy as np
import calendar
import logging
from datetime import datetime
import pydash
import pytz
import json
import os
import urllib.parse
import codecs
import mwclient
from mwtemplates import TemplateEditor

from .rules import NewPageRule, ByteRule, WordRule, RefRule, ImageRule, TemplateRemovalRule, SectionRule
from .common import _, STATE_ENDING, STATE_CLOSING, InvalidContestPage
from .rules import rule_classes
from .filters import CatFilter, TemplateFilter, NewPageFilter, ExistingPageFilter, ByteFilter, SparqlFilter, \
    BackLinkFilter, ForwardLinkFilter, NamespaceFilter, PageFilter
from .db import result_iterator
from .user import User
from .util import cleanup_input, unix_time, parse_infobox

logger = logging.getLogger(__name__)


def sum_stats_by(values, key=None, user=None):
    the_sum = 0
    for value in values:
        if key is not None and key != value['key']:
            continue
        if user is not None and user != value['user']:
            continue
        the_sum += value['value']
    return the_sum


class FilterTemplate(object):

    def __init__(self, template, translations, sites):
        self.template = template
        self.sites = sites
        self.named_params_raw_values = {
            cleanup_input(k): v.value
            for k, v in template.parameters.items()
        }
        self.named_params = {
            k: cleanup_input(v)
            for k, v in self.named_params_raw_values.items()
        }
        self.anon_params = [
            cleanup_input(v) if v is not None else None
            for v in template.get_anonymous_parameters()
        ]
        self.translations = translations

        def get_type(value):
            for k, v in translations['params'].items():
                if v.get('name') == value:
                    return k
            raise InvalidContestPage(_('The filter name "%s" was not understood') % value)

        self.type = get_type(self.anon_params[1].lower())

    def get_localized_name(self, name):
        return pydash.get(
            self.translations,
            'params.%s.params.%s' % (self.type, name),
            'params._all.params.%s' % (name)
        )

    def has_param(self, name):
        return self.template.has_param(self.get_localized_name(name))

    def get_param(self, name, default=None, datatype=str):
        value = self.named_params.get(self.get_localized_name(name), default)
        if value is None:
            return default
        if datatype == list:
            return [x.strip() for x in str(value).split(',')]
        return datatype(value)

    def get_raw_param(self, name, default=None):
        return self.named_params_raw_values.get(self.get_localized_name(name), default)

    def make(self, contest):
        filter_cls = {
            'new': NewPageFilter,
            'existing': ExistingPageFilter,
            'template': TemplateFilter,
            'bytes': ByteFilter,
            'category': CatFilter,
            'sparql': SparqlFilter,
            'backlink': BackLinkFilter,
            'forwardlink': ForwardLinkFilter,
            'namespace': NamespaceFilter,
            'pages': PageFilter,
        }[self.type]

        return filter_cls.make(contest=contest, tpl=self, cfg=self.translations['params'][self.type])


class Contest(object):

    def __init__(self, page, state, sites, sql, config, project_dir, job_id, username=None):
        """
            page: mwclient.Page object
            sites: list
            sql: mysql Connection object
        """
        logger.info('<<< Initializing contest [[%s]], state: %s >>>', page.name, state)
        self.page = page
        self.state = state
        self.name = self.page.name
        self.config = config
        self.project_dir = project_dir
        self.job_id = job_id
        txt = page.text()

        self.sql = sql
        self.wiki_tz = config['wiki_timezone']
        self.server_tz = config['server_timezone']

        self.sites = sites
        if username is None:
            self.users = [User(n, self) for n in self.extract_userlist(txt)]
        else:
            self.users = [User(username, self)]

        self.rules, self.filters = self.extract_rules(txt, self.config.get('catignore', ''))

        logger.info("- %d participants", len(self.users))
        logger.info("- %d rule(s)" % len(self.rules))
        for rule in self.rules:
            logger.info("  - %s" % rule.__class__.__name__)

        logger.info('- Open from %s to %s',
                    self.start.strftime('%F %T'),
                    self.end.strftime('%F %T'))

    def __del__(self):
        logger.info('Destructing %s', repr(self))

    def __repr__(self):
        return "<Contest %s>" % self.page.name

    def extract_userlist(self, txt):
        lst = []
        m = re.search(r'==\s*%s\s*==' % self.config['contestPages']['participantsSection'], txt)
        if not m:
            raise InvalidContestPage(_("Couldn't find the list of participants!"))
        deltakerliste = txt[m.end():]
        m = re.search('==[^=]+==', deltakerliste)
        if not m:
            raise InvalidContestPage('Fant ingen overskrift etter deltakerlisten!')
        deltakerliste = deltakerliste[:m.start()]
        for d in deltakerliste.split('\n'):
            q = re.search(r'\[\[(?:[^|\]]+):([^|\]]+)', d)
            if q:
                lst.append(q.group(1))
        return lst

    def extract_rules(self, txt, catignore_page=''):
        rules = []

        # Syntax is compatible with https://stackoverflow.com/questions/6875361/using-lepl-to-parse-a-boolean-search-query
        # In the future we could also support a boolean string input from the criterion template.
        filters = [
            ()
        ]

        config = self.config

        rulecfg = config['templates']['rule']

        dp = TemplateEditor(txt)

        if config['templates']['rule']['name'] not in dp.templates:
            raise InvalidContestPage(_('There are no point rules defined for this contest. Point rules are defined by {{tl|%(template)s}}.') % {'template': config['templates']['rule']['name']})

        # if not 'ukens konkurranse kriterium' in dp.templates.keys():
        #    raise InvalidContestPage('Denne konkurransen har ingen bidragskriterier. Kriterier defineres med {{tl|ukens konkurranse kriterium}}.')

        # Read infobox

        infobox = parse_infobox(txt, self.sites.homesite.namespaces[2], self.config)
        self.start = infobox['start_time']
        self.end = infobox['end_time']

        # args = {'week': commonargs['week'], 'year': commonargs['year'], 'start': ibcfg['start'], 'end': ibcfg['end'], 'template': ibcfg['name']}
        # raise InvalidContestPage(_('Did not find %(week)s+%(year)s or %(start)s+%(end)s in {{tl|%(templates)s}}.') % args)

        self.year = self.start.isocalendar()[0]
        self.startweek = self.start.isocalendar()[1]
        self.endweek = self.end.isocalendar()[1]
        self.month = self.start.month

        self.ledere = infobox['organizers']
        if len(self.ledere) == 0:
            logger.warning('Found no organizers in {{tl|%s}}.', infobox['name'])

        self.prices = infobox['awards']
        self.prices.sort(key=lambda x: x[2], reverse=True)

        # Read filters

        nfilters = 0
        # print dp.templates.keys()
        filter_template_config = config['templates']['filters']
        if filter_template_config['name'] in dp.templates:
            for template in dp.templates[filter_template_config['name']]:
                filter_tpl = FilterTemplate(template, filter_template_config, self.sites)

                if filter_tpl.type in ['new', 'existing', 'namespace']:
                    op = 'AND'
                else:
                    op = 'OR'

                try:
                    filter_inst = filter_tpl.make(self)
                except RuntimeError as exp:
                    # Only abort for truly fatal errors (like missing required arguments)
                    fatal_errors = [
                        'Too few arguments',
                        'No category values given',
                        'No byte limit',
                        'No "%s" parameter given',
                        'Could not parse the catignore page',
                    ]
                    msg = str(exp)
                    if any(fe in msg for fe in fatal_errors):
                        raise InvalidContestPage(
                            _('Could not parse {{tlx|%(template)s|%(firstarg)s}} template: %(err)s')
                            % {
                                'template': filter_template_config['name'],
                                'firstarg': filter_tpl.anon_params[1],
                                'err': msg
                            }
                        )
                    # Otherwise, treat as warning and continue
                    logger.warning('Non-fatal filter error: %s', msg)
                    try:
                        self.sites.homesite.errors.append(_('Warning: %(msg)s') % {'msg': msg})
                    except Exception as e:
                        logger.warning('Could not attach warning to homesite: %s', e)
                    continue

                nfilters += 1
                if op == 'OR':
                    # Append filter to the last tuple in the filters list
                    filters[-1] = filters[-1] + (filter_inst,)
                else:
                    # Prepend filter to the filters list
                    filters.insert(0, filter_inst)

        # Read rules

        rule_classes_map = {
            rulecfg[rule_cls.rule_name]: rule_cls for rule_cls in rule_classes
        }

        nrules = 0
        for rule_template in dp.templates[rulecfg['name']]:
            nrules += 1
            rule_name = rule_template.parameters[1].value.lower()
            try:
                rule_cls = rule_classes_map[rule_name]
            except KeyError:
                raise InvalidContestPage(
                    _('Unkown argument given to {{tl|%(template)s}}: %(argument)s')
                    % {'template': rulecfg['name'], 'argument': rule_name}
                )

            rule = rule_cls(self.sites, rule_template.parameters, rulecfg)
            rules.append(rule)

        # Check if contest is in DB yet

        cur = self.sql.cursor()
        now = datetime.now()
        cur.execute('UPDATE contests SET start_date=%s, end_date=%s, update_date=%s, last_job_id=%s WHERE site=%s AND name=%s', [
            self.start.strftime('%F %T'),
            self.end.strftime('%F %T'),
            now.strftime('%F %T'),
            self.job_id,
            self.sites.homesite.key,
            self.name,
        ])
        self.sql.commit()
        cur.close()

        # Read disqualifications

        sucfg = self.config['templates']['suspended']
        if sucfg['name'] in dp.templates:
            for template in dp.templates[sucfg['name']]:
                uname = cleanup_input(template.parameters[1].value)
                try:
                    sdate = self.wiki_tz.localize(datetime.strptime(cleanup_input(template.parameters[2].value), '%Y-%m-%d %H:%M'))
                except ValueError:
                    raise InvalidContestPage(_("Couldn't parse the date given to the {{tl|%(template)s}} template.") % sucfg['name'])

                # print 'Suspendert bruker:',uname,sdate
                ufound = False
                for u in self.users:
                    if u.name == uname:
                        # print " > funnet"
                        u.suspended_since = sdate
                        ufound = True
                if not ufound:
                    pass
                    # TODO: logging.warning
                    # raise InvalidContestPage('Fant ikke brukeren %s gitt til {{tl|UK bruker suspendert}}-malen.' % uname)

        dicfg = self.config['templates']['disqualified']
        if dicfg['name'] in dp.templates:
            logger.info('Disqualified contributions:')
            for template in dp.templates[dicfg['name']]:
                uname = cleanup_input(template.parameters[1].value)
                anon = template.get_anonymous_parameters()
                uname = anon[1]
                if not template.has_param('s'):
                    for article_name in anon[2:]:
                        page = self.sites.resolve_page(article_name)
                        article_key = page.site.key + ':' + page.name

                        ufound = False
                        for u in self.users:
                            if u.name == uname:
                                logger.info('- [%s] %s', uname, article_key)
                                u.disqualified_articles.append(article_key)
                                ufound = True
                        if not ufound:
                            raise InvalidContestPage(_('Could not find the user %(user)s given to the {{tl|%(template)s}} template.') % {'user': uname, 'template': dicfg['name']})

        pocfg = self.config['templates']['penalty']
        if pocfg['name'] in dp.templates:
            for templ in dp.templates[pocfg['name']]:
                uname = cleanup_input(templ.parameters[1].value)
                revid = int(cleanup_input(templ.parameters[2].value))
                site_key = ''
                if 'site' in templ.parameters:
                    site_key = cleanup_input(templ.parameters['site'].value)

                site = self.sites.from_prefix(site_key)
                if site is None:
                    raise InvalidContestPage(_('Failed to parse the %(template)s template: Did not find a site matching the site prefix %(prefix)s') % {
                        'template': pocfg['name'],
                        'prefix': site_key,
                    })

                points = float(cleanup_input(templ.parameters[3].value).replace(',', '.'))
                reason = cleanup_input(templ.parameters[4].value)
                ufound = False
                logger.info('Point deduction: %d points to "%s" for revision %s:%s. Reason: %s', points, uname, site.key, revid, reason)
                for u in self.users:
                    if u.name == uname:
                        u.point_deductions.append({
                            'site': site.key,
                            'revid': revid,
                            'points': points,
                            'reason': reason,
                        })
                        ufound = True
                if not ufound:
                    raise InvalidContestPage(_("Couldn't find the user %(user)s given to the {{tl|%(template)s}} template.") % {
                        'user': uname,
                        'template': pocfg['name'],
                    })

        pocfg = self.config['templates']['bonus']
        if pocfg['name'] in dp.templates:
            for templ in dp.templates[pocfg['name']]:
                uname = cleanup_input(templ.parameters[1].value)
                revid = int(cleanup_input(templ.parameters[2].value))
                site_key = ''
                if 'site' in templ.parameters:
                    site_key = cleanup_input(templ.parameters['site'].value)

                site = None
                for s in self.sites.sites.values():
                    if s.match_prefix(site_key):
                        site = s
                        break

                if site is None:
                    raise InvalidContestPage(_('Failed to parse the %(template)s template: Did not find a site matching the site prefix %(prefix)s') % {
                        'template': pocfg['name'],
                        'prefix': site_key,
                    })

                points = float(cleanup_input(templ.parameters[3].value).replace(',', '.'))
                reason = cleanup_input(templ.parameters[4].value)
                ufound = False
                logger.info('Point addition: %d points to %s for revision %s:%s. Reason: %s', points, uname, site.key, revid, reason)
                for u in self.users:
                    if u.name == uname:
                        u.point_deductions.append({
                            'site': site.key,
                            'revid': revid,
                            'points': -points,
                            'reason': reason
                        })
                        ufound = True
                if not ufound:
                    raise InvalidContestPage(_("Couldn't find the user %(user)s given to the {{tl|%(template)s}} template.") % {
                        'user': uname,
                        'template': pocfg['name'],
                    })

        return rules, filters

    def prepare_plotdata(self, results):
        if 'plot' not in self.config:
            return

        plotdata = []
        for result in results:
            tmp = {'name': result['name'], 'values': []}
            for point in result['plotdata']:
                tmp['values'].append({'x': point[0], 'y': point[1]})
            plotdata.append(tmp)

        if 'datafile' in self.config['plot']:
            filename = os.path.join(self.project_dir, self.config['plot']['datafile'] % {'year': self.year, 'week': self.startweek, 'month': self.month})
            with open(filename, 'w') as fp:
                json.dump(plotdata, fp)

        return plotdata

    def plot(self, plotdata):
        if 'plot' not in self.config:
            return
        import matplotlib.pyplot as plt

        w = 20 / 2.54
        goldenratio = 1.61803399
        h = w / goldenratio
        fig = plt.figure(figsize=(w, h))

        ax = fig.add_subplot(1, 1, 1, frame_on=True)
        # ax.grid(True, which='major', color='gray', alpha=0.5)
        fig.subplots_adjust(left=0.10, bottom=0.09, right=0.65, top=0.94)

        t0 = float(unix_time(self.start))

        datediff = self.end.date() - self.start.date()  # Compare just dates to avoid issues with daylight saving time
        ndays = datediff.days + 1

        xt = t0 + np.arange(ndays + 1) * 86400
        xt_mid = t0 + 43200 + np.arange(ndays) * 86400

        now = float(unix_time(self.server_tz.localize(datetime.now()).astimezone(pytz.utc)))

        yall = []
        cnt = 0

        for result in plotdata:
            x = [t['x'] for t in result['values']]
            y = [t['y'] for t in result['values']]

            if len(x) > 0:
                cnt += 1
                yall.extend(y)
                x.insert(0, xt[0])
                y.insert(0, 0)
                if now < xt[-1]:
                    x.append(now)
                    y.append(y[-1])
                else:
                    x.append(xt[-1])
                    y.append(y[-1])
                line = ax.plot(x, y, linewidth=1.2, label=result['name'])  # markerfacecolor='#FF8C00', markeredgecolor='#888888', label = u['name'])
                # ax.plot(x[1:-1], y[1:-1], marker='.', markersize=4, markerfacecolor=c, markeredgecolor=c, linewidth=0., alpha=0.5)  # markerfacecolor='#FF8C00', markeredgecolor='#888888', label = u['name'])
                if cnt >= 15:
                    break

        if now < xt[-1]:   # showing vertical line indicating when the plot was updated
            ax.axvline(now, color='black', alpha=0.5)

        abday = [calendar.day_abbr[x] for x in [0, 1, 2, 3, 4, 5, 6]]

        x_ticks_major_size = 5
        x_ticks_minor_size = 0

        if ndays < 7:
            # Tick marker every midnight
            ax.set_xticks(xt, minor=False)
            ax.set_xticklabels([], minor=False)

            # Tick labels at the middle of every day
            # Just a number if ndays < 7
            ax.set_xticks(xt_mid, minor=True)
            ax.set_xticklabels(list(range(1, ndays + 1)), minor=True)
        elif ndays == 7:
            # Tick marker every midnight
            ax.set_xticks(xt, minor=False)
            ax.set_xticklabels([], minor=False)

            # Tick labels at the middle of every day
            ax.set_xticks(xt_mid, minor=True)
            ax.set_xticklabels(abday, minor=True)
        elif ndays == 14:
            # Tick marker every midnight
            ax.set_xticks(xt, minor=False)
            ax.set_xticklabels([], minor=False)

            # Tick labels at the middle of every day
            ax.set_xticks(xt_mid, minor=True)
            ax.set_xticklabels([abday[0], '', abday[2], '', abday[4], '', abday[6], '', abday[1], '', abday[3], '', abday[5], ''], minor=True)
        elif ndays > 14:

            # Tick marker every week
            x_ticks_major_labels = np.arange(0, ndays + 1, 7)
            x_ticks_major = t0 + x_ticks_major_labels * 86400
            ax.set_xticks(x_ticks_major, minor=False)
            ax.set_xticklabels(x_ticks_major_labels, minor=False)

            # Tick every day
            x_ticks_minor = t0 + np.arange(ndays + 1) * 86400
            ax.set_xticks(x_ticks_minor, minor=True)
            x_ticks_minor_size = 3

            # ax.set_xticklabels(['1', '', '', '', '5', '', '', '', '', '10', '', '', '', '', '15', '', '', '', '', '20', '', '', '', '', '25', '', '', '', '', '30'], minor=True)
        # elif ndays == 31:
        #     ax.set_xticklabels(['1', '', '', '', '5', '', '', '', '', '10', '', '', '', '', '15', '', '', '', '', '20', '', '', '', '', '25', '', '', '', '', '', '31'], minor=True)

        for i in range(1, ndays, 2):
            ax.axvspan(xt[i], xt[i + 1], facecolor='#000099', linewidth=0., alpha=0.03)

        for i in range(0, ndays, 2):
            ax.axvspan(xt[i], xt[i + 1], facecolor='#000099', linewidth=0., alpha=0.07)

        for line in ax.xaxis.get_ticklines(minor=False):
            line.set_markersize(x_ticks_major_size)

        for line in ax.xaxis.get_ticklines(minor=True):
            line.set_markersize(x_ticks_minor_size)

        for line in ax.yaxis.get_ticklines(minor=False):
            line.set_markersize(x_ticks_major_size)

        if len(yall) > 0:
            ax.set_xlim(t0, xt[-1])
            ax.set_ylim(0, 1.05 * np.max(yall))

            ax.set_xlabel(_('Day'))
            ax.set_ylabel(_('Points'))

            now = self.server_tz.localize(datetime.now())
            now2 = now.astimezone(self.wiki_tz).strftime(_('%e. %B %Y, %H:%M'))
            ax_title = _('Updated %(date)s')

            # print ax_title.encode('utf-8')
            # print now2.encode('utf-8')
            ax_title = ax_title % {'date': now2}
            ax.set_title(ax_title)

            plt.legend()
            ax = plt.gca()
            ax.legend(
                # ncol = 4, loc = 3, bbox_to_anchor = (0., 1.02, 1., .102), mode = "expand", borderaxespad = 0.
                loc=2, bbox_to_anchor=(1.0, 1.0), borderaxespad=0., frameon=0.
            )
            figname = os.path.join(self.project_dir, self.config['plot']['figname'] % {'year': self.year, 'week': self.startweek, 'month': self.month})
            plt.savefig(figname, dpi=200)
            logger.info('Wrote plot: %s', figname)

    def format_msg(self, template_name, awards):
        template = self.config['award_messages'][template_name]
        arg_yes = self.config['templates']['commonargs'][True]
        arg_endweek = self.config['templates']['commonargs']['week2']
        args = {
            'year': str(self.year),
            'week': str(self.startweek),
            'month': str(self.month),
            'awards': '|'.join(['%s=%s' % (award, arg_yes) for award in awards]),
        }
        if self.startweek != self.endweek:
            args['week'] += '|%s=%s' % (arg_endweek, self.endweek)

        return template % args

    def format_heading(self):
        if self.config.get('contest_type') == 'weekly':
            if self.startweek == self.endweek:
                return _('Weekly contest for week %(week)d') % {'week': self.startweek}
            else:
                return _('Weekly contest for week %(startweek)d–%(endweek)d') % {'startweek': self.startweek, 'endweek': self.endweek}
        else:
            return self.config.get('name') % {'month': self.month, 'year': self.year}

    def deliver_message(self, username, topic, body, sig='~~~~'):
        logger.info('Delivering message to %s', username)

        prefix = self.sites.homesite.namespaces[3]
        prefixed = prefix + ':' + username

        res = self.sites.homesite.api(action='query', prop='flowinfo', titles=prefixed)
        pageinfo = list(res['query']['pages'].values())[0]
        flow_enabled = 'missing' not in pageinfo and 'enabled' in pageinfo['flowinfo']['flow']

        pagename = '%s:%s' % (prefix, username)

        if flow_enabled:
            token = self.sites.homesite.get_token('csrf')
            self.sites.homesite.api(action='flow',
                                    submodule='new-topic',
                                    page=pagename,
                                    nttopic=topic,
                                    ntcontent=body,
                                    ntformat='wikitext',
                                    token=token)

        else:
            page = self.sites.homesite.pages[pagename]
            page.save(text=body + ' ' + sig, bot=False, section='new', summary=topic)

    def deliver_prices(self, results, simulate=False):
        heading = self.format_heading()

        cur = self.sql.cursor()
        cur.execute('SELECT contest_id FROM contests WHERE site=%s AND name=%s', [self.sites.homesite.key, self.name])
        contest_id = cur.fetchall()[0][0]

        logger.info('Delivering prices for contest %d' % (contest_id,))

        # self.sql.commit()
        # cur.close()

        for i, result in enumerate(results):
            prices = []

            if i == 0:
                # Contest winenr
                for price in self.prices:
                    # Is there's a special winner's prize?
                    if price[1] == 'winner':
                        prices.append(price[0])

            # Append the first point limit price, if any
            for price in self.prices:
                if price[1] == 'pointlimit' and result['points'] >= price[2]:
                    prices.append(price[0])
                    break

            if len(prices) == 0:
                logger.info('No price for %s', result['name'])
                continue

            now = self.server_tz.localize(datetime.now())
            now_l = now.astimezone(self.wiki_tz)
            dateargs = {
                'year': now_l.year,
                'week': now_l.isocalendar()[1],
                'month': now_l.month,
            }
            userprefix = self.sites.homesite.namespaces[2]

            tpl = 'winner_template' if i == 0 else 'participant_template'
            msg = self.format_msg(tpl, prices) + '\n'
            msg += self.config['award_messages']['reminder_msg'] % {
                'url': self.config['pages']['default'] % dateargs,
                **dateargs,
            }
            sig = _('Regards') + ' ' + ', '.join(['[[%s:%s|%s]]' % (userprefix, s, s) for s in self.ledere]) + ' ' + _('and') + ' ~~~~'

            if not simulate:
                cur.execute('SELECT prize_id FROM prizes WHERE contest_id=%s AND site=%s AND user=%s', [contest_id, self.sites.homesite.key, result['name']])
                rows = cur.fetchall()
                if len(rows) == 0:
                    self.deliver_message(result['name'], heading, msg, sig)
                    cur.execute('INSERT INTO prizes (contest_id, site, user, timestamp) VALUES (%s,%s,%s, NOW())', [contest_id, self.sites.homesite.key, result['name']])
                    self.sql.commit()

    def deliver_ended_contest_notification(self):
        if 'awardstatus' not in self.config:
            return

        heading = self.format_heading()
        args = {
            'prefix': self.sites.homesite.site['server'] + self.sites.homesite.site['script'],
            'page': self.config['awardstatus']['pagename'],
            'title': urllib.parse.quote(self.config['awardstatus']['send'])
        }
        link = '%(prefix)s?title=%(page)s&action=edit&section=new&preload=%(page)s/Preload&preloadtitle=%(title)s' % args
        awards = []
        for key, award in self.config['awards'].items():
            if 'organizer' in award:
                awards.append(key)
        if len(awards) == 0:
            raise Exception('No organizer award found in config')
        for u in self.ledere:
            mld = self.format_msg('organizer_template', awards)
            mld += _('Now you must check if the results look ok. If there are error messages at the bottom of the [[%(page)s|contest page]], you should check that the related contributions have been awarded the correct number of points. Also check if there are comments or complaints on the discussion page. If everything looks fine, [%(link)s click here] (and save) to indicate that I can send out the awards at first occasion.') % {'page': self.name, 'link': link}
            sig = _('Thanks, ~~~~')

            logger.info('Delivering notification about ended contenst to the contest organizers')
            self.deliver_message(u, heading, mld, sig)

    def deliver_receipt_to_leaders(self):
        heading = self.format_heading()
        usertalkprefix = self.sites.homesite.namespaces[3]

        args = {'prefix': self.sites.homesite.site['server'] + self.sites.homesite.site['script'], 'page': 'Special:Contributions'}
        link = '%(prefix)s?title=%(page)s&contribs=user&target=UKBot&namespace=3' % args
        mld = '\n:' + _('Awards have been [%(link)s sent out].') % {'link': link}
        for u in self.ledere:
            page = self.sites.homesite.pages['%s:%s' % (usertalkprefix, u)]
            logger.info('Leverer kvittering til %s', page.name)

            # Find section number
            txt = page.text()
            sections = [s.strip() for s in re.findall(r'^[\s]*==([^=]+)==', txt, flags=re.M)]
            try:
                csection = sections.index(heading) + 1
            except ValueError:
                logger.error('Fant ikke "%s" i "%s', heading, page.name)
                return

            # Append text to section
            txt = page.text(section=csection)
            page.save(appendtext=mld, bot=False, summary='== ' + heading + ' ==')

    def delete_contribs_from_db(self):
        cur = self.sql.cursor()
        cur2 = self.sql.cursor()
        ts_start = self.start.astimezone(pytz.utc).strftime('%F %T')
        ts_end = self.end.astimezone(pytz.utc).strftime('%F %T')
        ndel = 0
        cur.execute('SELECT site,revid,parentid FROM contribs WHERE timestamp >= %s AND timestamp <= %s', (ts_start, ts_end))
        for row in result_iterator(cur):
            cur2.execute('DELETE FROM fulltexts WHERE site=%s AND revid=%s', [row[0], row[1]])
            ndel += cur2.rowcount
            cur2.execute('DELETE FROM fulltexts WHERE site=%s AND revid=%s', [row[0], row[2]])
            ndel += cur2.rowcount

        cur.execute('SELECT COUNT(*) FROM fulltexts')
        nremain = cur.fetchone()[0]
        logger.info('Cleaned %d rows from fulltexts-table. %d rows remain', ndel, nremain)

        cur.execute('DELETE FROM contribs WHERE timestamp >= %s AND timestamp <= %s', (ts_start, ts_end))
        ndel = cur.rowcount
        cur.execute('SELECT COUNT(*) FROM contribs')
        nremain = cur.fetchone()[0]
        logger.info('Cleaned %d rows from contribs-table. %d rows remain', ndel, nremain)

        cur.close()
        cur2.close()
        self.sql.commit()

    def deliver_warnings(self, simulate=False):
        """
        Inform users about problems with their contribution(s)
        """
        usertalkprefix = self.sites.homesite.namespaces[3]
        cur = self.sql.cursor()
        for u in self.users:
            msgs = []
            if u.suspended_since is not None:
                d = [self.sites.homesite.key, self.name, u.name, 'suspension', '']
                cur.execute('SELECT id FROM notifications WHERE site=%s AND contest=%s AND user=%s AND class=%s AND args=%s', d)
                if len(cur.fetchall()) == 0:
                    msgs.append('Du er inntil videre suspendert fra konkurransen med virkning fra %s. Dette innebærer at dine bidrag gjort etter dette tidspunkt ikke teller i konkurransen, men alle bidrag blir registrert og skulle suspenderingen oppheves i løpet av konkurranseperioden vil også bidrag gjort i suspenderingsperioden telle med. Vi oppfordrer deg derfor til å arbeide med problemene som førte til suspenderingen slik at den kan oppheves.' % u.suspended_since.strftime(_('%e. %B %Y, %H:%M')))
                    if not simulate:
                        cur.execute('INSERT INTO notifications (site, contest, user, class, args) VALUES (%s,%s,%s,%s,%s)', d)
            discs = []
            for article_key, article in u.articles.items():
                if article.disqualified:
                    d = [self.sites.homesite.key, self.name, u.name, 'disqualified', article_key]
                    cur.execute('SELECT id FROM notifications WHERE site=%s AND contest=%s AND user=%s AND class=%s AND args=%s', d)
                    if len(cur.fetchall()) == 0:
                        discs.append('[[:%s|%s]]' % (article_key, article.name))
                        if not simulate:
                            cur.execute('INSERT INTO notifications (site, contest, user, class, args) VALUES (%s,%s,%s,%s,%s)', d)
            if len(discs) > 0:
                if len(discs) == 1:
                    s = discs[0]
                else:
                    s = ', '.join(discs[:-1]) + ' og ' + discs[-1]
                msgs.append('Bidragene dine til %s er diskvalifisert fra konkurransen. En diskvalifisering kan oppheves hvis du selv ordner opp i problemet som førte til diskvalifiseringen. Hvis andre brukere ordner opp i problemet er det ikke sikkert at den vil kunne oppheves.' % s)

            if len(msgs) > 0:
                if self.startweek == self.endweek:
                    heading = '== Viktig informasjon angående Ukens konkurranse uke %d ==' % self.startweek
                else:
                    heading = '== Viktig informasjon angående Ukens konkurranse uke %d–%d ==' % (self.startweek, self.endweek)
                # msg = 'Arrangøren av denne [[%(pagename)s|ukens konkurranse]] har registrert problemer ved noen av dine bidrag:
                # så langt. Det er dessverre registrert problemer med enkelte av dine bidrag som medfører at vi er nødt til å informere deg om følgende:\n' % { 'pagename': self.name }

                msg = ''.join(['* %s\n' % m for m in msgs])
                msg += 'Denne meldingen er generert fra anmerkninger gjort av konkurransearrangør på [[%(pagename)s|konkurransesiden]]. Du finner mer informasjon på konkurransesiden og/eller tilhørende diskusjonsside. Så lenge konkurransen ikke er avsluttet, kan problemer løses i løpet av konkurransen. Om du ønsker det, kan du fjerne denne meldingen når du har lest den. ~~~~' % {'pagename': self.name}

                # print '------------------------------',u.name
                # print msg
                # print '------------------------------'

                page = self.sites.homesite.pages['%s:%s' % (usertalkprefix, u.name)]
                logger.info('Leverer advarsel til %s', page.name)
                if simulate:
                    logger.info(msg)
                else:
                    page.save(text=msg, bot=False, section='new', summary=heading)
            self.sql.commit()

    def run(self, simulate=False, output=''):
        config = self.config

        if not self.page.exists:
            logger.error('Contest page [[%s]] does not exist! Exiting', self.page.name)
            return        # Loop over users

        stats = []

        # extraargs = {'namespace': 0}
        extraargs = {}
        # host_filter = None
        # for f in self.filters:
        #     if isinstance(f, NamespaceFilter):
        #         extraargs['namespace'] = '|'.join(f.namespaces)
        #         host_filter = f.site

        article_errors = {}
        results = []

        while True:
            if len(self.users) == 0:
                break
            user = self.users.pop()

            logger.info('=== User:%s ===', user.name)

            # First read contributions from db
            user.add_contribs_from_db(self.sql, self.start, self.end, self.sites.sites)

            # Then fill in new contributions from wiki
            for site in self.sites.sites.values():

                # if host_filter is None or site.host == host_filter:
                user.add_contribs_from_wiki(site, self.start, self.end, fulltext=True, **extraargs)

            # And update db
            user.save_contribs_to_db(self.sql)

            user.backfill_article_creation_dates(self.sql)

            try:

                # Filter out relevant articles
                user.filter(self.filters)

                # And calculate points
                logger.info('Calculating points')
                tp0 = time.time()
                user.analyze(self.rules)
                tp1 = time.time()
                logger.info('%s: %.f points (calculated in %.1f secs)', user.name,
                            user.contributions.sum(), tp1 - tp0)

                stats.extend(user.count_bytes_per_site())
                stats.extend(user.count_words_per_site())
                stats.extend(user.count_pages_per_site())
                stats.extend(user.count_newpages_per_site())

                tp2 = time.time()
                logger.info('Wordcount done in %.1f secs', tp2 - tp1)

                for article in user.articles.values():
                    k = article.link()
                    if len(article.errors) > 0:
                        article_errors[k] = article.errors
                    for rev in article.revisions.values():
                        if len(rev.errors) > 0:
                            if k in article_errors:
                                article_errors[k].extend(rev.errors)
                            else:
                                article_errors[k] = rev.errors

                results.append({
                    'name': user.name,
                    'points': user.contributions.sum(),
                    'result': user.contributions.format(homesite=self.sites.homesite),
                    'plotdata': user.plotdata,
                })

            except InvalidContestPage as e:
                err = "\n* '''%s'''" % e.msg
                out = '\n{{%s | error | %s }}' % (config['templates']['botinfo'], err)
                if simulate:
                    logger.error(out)
                else:
                    self.page.save('dummy', summary=_('UKBot encountered a problem'), appendtext=out)
                raise

            del user

        # Sort users by points

        logger.info('Sorting contributions and preparing contest page')

        results.sort(key=lambda x: x['points'], reverse=True)

        # Make outpage

        out = ''
        # out += '[[File:Nowp Ukens konkurranse %s.svg|thumb|400px|Resultater (oppdateres normalt hver natt i halv ett-tiden, viser kun de ti med høyest poengsum)]]\n' % self.start.strftime('%Y-%W')

        summary_tpl = None
        if 'status' in config['templates']:

            summary_tpl_args = ['|pages=%d' % sum_stats_by(stats, key='pages')]

            trn = 0
            for rule in self.rules:
                if isinstance(rule, NewPageRule):
                    summary_tpl_args.append('%s=%d' % (rule.key, sum_stats_by(stats, key='newpages')))
                elif isinstance(rule, ByteRule):
                    nbytes = sum_stats_by(stats, key='bytes')
                    if nbytes >= 10000:
                        summary_tpl_args.append('kilo%s=%.f' % (rule.key, nbytes / 1000.))
                    else:
                        summary_tpl_args.append('%s=%d' % (rule.key, nbytes))
                elif isinstance(rule, WordRule):
                    summary_tpl_args.append('%s=%d' % (rule.key, sum_stats_by(stats, key='words')))
                elif isinstance(rule, RefRule):
                    summary_tpl_args.append('%s=%d' % (rule.key, rule.totalsources))
                elif isinstance(rule, SectionRule):
                    summary_tpl_args.append('|%s=%d' % (rule.key, rule.total))
                elif isinstance(rule, ImageRule):
                    summary_tpl_args.append('%s=%d' % (rule.key, rule.total))
                elif isinstance(rule, TemplateRemovalRule):
                    for tpl in rule.templates:
                        trn += 1
                        summary_tpl_args.append('%(key)s%(idx)d=%(tpl)s' % {'key': rule.key, 'idx': trn, 'tpl': tpl['name']})
                        summary_tpl_args.append('%(key)s%(idx)dn=%(cnt)d' % {'key': rule.key, 'idx': trn, 'cnt': tpl['total']})

            summary_tpl = '{{%s|%s}}' % (config['templates']['status'], '|'.join(summary_tpl_args))

        now = self.server_tz.localize(datetime.now())
        if self.state == STATE_ENDING:
            # Konkurransen er nå avsluttet – takk til alle som deltok! Rosetter vil bli delt ut så snart konkurransearrangøren(e) har sjekket resultatene.
            out += "''" + _('This contest is closed – thanks to everyone who participated! Awards will be sent out as soon as the contest organizer has checked the results.') + "''\n\n"
        elif self.state == STATE_CLOSING:
            out += "''" + _('This contest is closed – thanks to everyone who participated!') + "''\n\n"
        else:
            oargs = {
                'lastupdate': now.astimezone(self.wiki_tz).strftime(_('%e. %B %Y, %H:%M')),
                'startdate': self.start.strftime(_('%e. %B %Y, %H:%M')),
                'enddate': self.end.strftime(_('%e. %B %Y, %H:%M'))
            }
            out += "''" + _('Last updated %(lastupdate)s. The contest is open from %(startdate)s to %(enddate)s.') % oargs + "''\n\n"

        for i, result in enumerate(results):
            awards = ''
            if self.state == STATE_CLOSING:
                if i == 0:
                    for price in self.prices:
                        if price[1] == 'winner':
                            awards += '[[File:%s|20px]] ' % config['awards'][price[0]]['file']
                            break
                for price in self.prices:
                    if price[1] == 'pointlimit' and result['points'] >= price[2]:
                        awards += '[[File:%s|20px]] ' % config['awards'][price[0]]['file']
                        break
            out += result['result'].replace('{awards}', awards)

        errors = []
        for art, err in article_errors.items():
            if len(err) > 8:
                err = err[:8]
                err.append('(...)')
            errors.append('\n* ' + _('UKBot encountered the following problems with the page [[%s]]') % art + ''.join(['\n** %s' % e for e in err]))

        for site in self.sites.sites.values():
            for error in site.errors:
                errors.append('\n* %s' % error)

        if len(errors) == 0:
            out += '{{%s | ok | %s }}' % (config['templates']['botinfo'], now.astimezone(self.wiki_tz).strftime('%F %T'))
        else:
            out += '{{%s | 1=note | 2=%s | 3=%s }}' % (config['templates']['botinfo'], now.astimezone(self.wiki_tz).strftime('%F %T'), ''.join(errors))

        out += '\n' + config['contestPages']['footer'] % {'year': self.year} + '\n'

        ib = config['templates']['infobox']

        if not simulate:
            txt = self.page.text()
            tp = TemplateEditor(txt)

            if summary_tpl is not None:
                tp.templates[ib['name']][0].parameters[ib['status']] = summary_tpl
            txt = tp.wikitext()
            secstart = -1
            secend = -1

            # Check if <!-- Begin:ResultsSection --> exists first
            try:
                trs1 = next(re.finditer(r'<!--\s*Begin:ResultsSection\s*-->', txt, re.I))
                trs2 = next(re.finditer(r'<!--\s*End:ResultsSection\s*-->', txt, re.I))
                secstart = trs1.end()
                secend = trs2.start()

            except StopIteration:
                if 'resultsSection' not in config['contestPages']:
                    raise InvalidContestPage(_('Results markers %(start_marker)s and %(end_marker)s not found') % {
                        'start_marker': '<!-- Begin:ResultsSection -->',
                        'end_marker': '<!-- End:ResultsSection -->',
                    })
                for s in re.finditer(r'^[\s]*==([^=]+)==[\s]*\n', txt, flags=re.M):
                    if s.group(1).strip() == config['contestPages']['resultsSection']:
                        secstart = s.end()
                    elif secstart != -1:
                        secend = s.start()
                        break
            if secstart == -1:
                raise InvalidContestPage(_('No "%(section_name)s" section found.') % {
                    'section_name': config['contestPages']['resultsSection'],
                })
            if secend == -1:
                txt = txt[:secstart] + out
            else:
                txt = txt[:secstart] + out + txt[secend:]

            logger.info('Updating wiki')
            if self.state == STATE_ENDING:
                self.page.save(txt, summary=_('Updating with final results, the contest is now closed.'))
            elif self.state == STATE_CLOSING:
                self.page.save(txt, summary=_('Checking results and handing out awards'))
            else:
                self.page.save(txt, summary=_('Updating'))

        if output != '':
            logger.info("Writing output to file")
            f = codecs.open(output, 'w', 'utf-8')
            f.write(out)
            f.close()

        if self.state == STATE_ENDING:
            logger.info('Ending contest')
            if not simulate:
                if 'awardstatus' in config:
                    aws = config['awardstatus']
                    page = self.sites.homesite.pages[aws['pagename']]
                    page.save(text=aws['wait'], summary=aws['wait'], bot=True)

                cur = self.sql.cursor()
                cur.execute('UPDATE contests SET ended=1 WHERE site=%s AND name=%s', [self.sites.homesite.key, self.name])
                self.sql.commit()
                count = cur.rowcount
                cur.close()

                if count == 0:
                    logger.info('Leader notifications have already been delivered')
                else:
                    self.deliver_ended_contest_notification()

        if self.state == STATE_CLOSING:
            logger.info('Delivering prices')

            self.deliver_prices(results, simulate)

            cur = self.sql.cursor()

            # Aggregate stats
            stats_agg = {}
            for stat in stats:
                if stat['key'] not in stats_agg:
                    stats_agg[stat['key']] = {}
                if stat['site'] not in stats_agg[stat['key']]:
                    stats_agg[stat['key']][stat['site']] = 0
                stats_agg[stat['key']][stat['site']] += stat['value']

            if not simulate:

                # Store stats
                for result in results:
                    sum_bytes = max(0, sum_stats_by(stats, user=result['name'], key='bytes'))
                    sum_pages = max(0, sum_stats_by(stats, user=result['name'], key='pages'))
                    sum_newpages = max(0, sum_stats_by(stats, user=result['name'], key='newpages'))
                    cur.execute(
                        'INSERT INTO users (site, contest, user, points, bytes, pages, newpages) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                        [
                            self.sites.homesite.key,
                            self.name,
                            result['name'],
                            result['points'],
                            sum_bytes,
                            sum_pages,
                            sum_newpages
                        ]
                    )

                    for dimension, values in stats_agg.items():
                        for contribsite, value in values.items():
                            nonzero_value = max(0, value)
                            cur.execute(
                                'INSERT INTO stats (contestsite, contest, contribsite, dimension, value) VALUES (%s,%s,%s,%s,%s)',
                                [
                                    self.sites.homesite.key,
                                    self.name,
                                    contribsite,
                                    dimension,
                                    nonzero_value,
                                ]
                            )
                cur.execute(
                    'UPDATE contests SET closed=1 WHERE site=%s AND name=%s',
                    [self.sites.homesite.key, self.name]
                )
                self.sql.commit()

            cur.close()

            aws = config['awardstatus']
            page = self.sites.homesite.pages[aws['pagename']]
            page.save(text=aws['sent'], summary=aws['sent'], bot=True)

            # if not simulate:
            #
            # Skip for now: not Flow compatible
            #     self.deliver_receipt_to_leaders()

            logger.info('Cleaning database')
            if not simulate:
                self.delete_contribs_from_db()

        # Notify users about issues

        # self.deliver_warnings(simulate=simulate)

        # Update Wikipedia:Portal/Oppslagstavle

        if 'noticeboard' in config:
            boardname = config['noticeboard']['name']
            boardtpl = config['noticeboard']['template']
            commonargs = config['templates']['commonargs']
            tplname = boardtpl['name']
            oppslagstavle = self.sites.homesite.pages[boardname]
            txt = oppslagstavle.text()

            dp = TemplateEditor(txt)
            ntempl = len(dp.templates[tplname])
            if ntempl != 1:
                raise Exception('Feil: Fant %d %s-maler i %s' % (ntempl, tplname, boardname))

            tpl = dp.templates[tplname][0]
            now2 = now.astimezone(self.wiki_tz)
            if int(tpl.parameters['uke']) != int(now2.strftime('%V')):
                logger.info('Updating noticeboard: %s', boardname)
                tpllist = config['templates']['contestlist']
                commonargs = config['templates']['commonargs']
                tema = self.sites.homesite.api('parse', text='{{subst:%s|%s=%s}}' % (tpllist['name'], commonargs['week'], now2.strftime('%Y-%V')), pst=1, onlypst=1)['parse']['text']['*']
                tpl.parameters[1] = tema
                tpl.parameters[boardtpl['date']] = now2.strftime('%e. %h')
                tpl.parameters[commonargs['year']] = now2.isocalendar()[0]
                tpl.parameters[commonargs['week']] = now2.isocalendar()[1]
                txt2 = dp.wikitext()
                if txt != txt2:
                    if not simulate:
                        oppslagstavle.save(txt2, summary=_('The weekly contest is: %(link)s') % {'link': tema})

        # Make a nice plot
        if 'plot' in config:
            plotdata = self.prepare_plotdata(results)
            self.plot(plotdata)

            if self.state == STATE_ENDING:
                self.uploadplot(simulate)

    def uploadplot(self, simulate=False, output=''):
        if not self.page.exists:
            logger.error('Contest page [[%s]] does not exist! Exiting', self.page.name)
            return

        if 'plot' not in self.config:
            return

        figname = self.config['plot']['figname'] % {
            'year': self.year,
            'week': self.startweek,
            'month': self.month,
        }
        remote_filename = os.path.basename(figname).replace(' ', '_')
        local_filename = os.path.join(self.project_dir, figname)

        if not os.path.isfile(local_filename):
            logger.error('File "%s" was not found', local_filename)
            sys.exit(1)

        weeks = '%d' % self.startweek
        if self.startweek != self.endweek:
            weeks += '-%s' % self.endweek

        pagetext = self.config['plot']['description'] % {
            'pagename': self.name,
            'week': weeks,
            'year': self.year,
            'month': self.month,
            'start': self.start.strftime('%F')
        }

        logger.info('Uploading: %s', figname)
        commons = mwclient.Site('commons.wikimedia.org',
                                consumer_token=os.getenv('MW_CONSUMER_TOKEN'),
                                consumer_secret=os.getenv('MW_CONSUMER_SECRET'),
                                access_token=os.getenv('MW_ACCESS_TOKEN'),
                                access_secret=os.getenv('MW_ACCESS_SECRET'))
        file_page = commons.pages['File:' + remote_filename]

        if simulate:
            return

        with open(local_filename.encode('utf-8'), 'rb') as file_buf:
            if not file_page.exists:
                logger.info('Adding plot')
                res = commons.upload(file_buf, remote_filename,
                                     comment='Bot: Uploading new plot',
                                     description=pagetext,
                                     ignore=True)
                logger.info(res)
            else:
                logger.info('Updating plot')
                res = commons.upload(file_buf, remote_filename,
                                     comment='Bot: Updating plot',
                                     ignore=True)
                logger.info(res)
