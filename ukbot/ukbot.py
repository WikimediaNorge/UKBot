# encoding=utf-8
# vim: fenc=utf-8 et sw=4 ts=4 sts=4 ai
import time

runstart_s = time.time()
print('Loading')

import sys
import logging
import matplotlib
from datetime import datetime
import pytz
import json
import os
import argparse
import mwclient
from mwtemplates import TemplateEditor
import platform
from dotenv import load_dotenv
from importlib.metadata import version

from .common import get_mem_usage, STATE_NORMAL, InvalidContestPage, Localization, i18n, fetch_parsed_i18n
from .util import load_config
from .contest import Contest
from .contests import discover_contest_pages
from .sites import init_sites

matplotlib.use('svg')

__version__ = version('ukbot')

class AppFilter(logging.Filter):

    @staticmethod
    def format_as_mins_and_secs(msecs):
        secs = msecs / 1000.
        mins = int(secs / 60.)
        secs = int(secs % 60.)
        return '%3.f:%02.f' % (mins, secs)

    def filter(self, record):
        record.mem_usage = '%.0f' % (get_mem_usage(),)
        record.relative_time = AppFilter.format_as_mins_and_secs(record.relativeCreated)
        return True


logging.getLogger('requests').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)
logging.getLogger('requests_oauthlib').setLevel(logging.WARNING)
logging.getLogger('oauthlib').setLevel(logging.WARNING)
logging.getLogger('mwtemplates').setLevel(logging.INFO)

logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
syslog = logging.StreamHandler()
logger.addHandler(syslog)
syslog.setLevel(logging.INFO)
formatter = logging.Formatter('[%(relative_time)s] {%(mem_usage)s MB} %(name)-20s %(levelname)s : %(message)s')
syslog.setFormatter(formatter)
syslog.addFilter(AppFilter())

load_dotenv()


def process_contest(contest_page, contest_state, sites, sql, config, working_dir, args):

    contest = Contest(contest_page,
                      state=contest_state,
                      sites=sites,
                      sql=sql,
                      config=config,
                      project_dir=working_dir,
                      job_id=args.job_id,
                      username=args.user)

    if args.action == 'uploadplot':
        contest.uploadplot(args.simulate, args.output)

    elif args.action == 'plot':
        filename = os.path.join(working_dir, config['plot']['datafile'] % {
            'year': contest.year,
            'week': contest.startweek,
            'month': contest.month
        })
        with open(filename, 'r') as fp:
            plotdata = json.load(fp)
        contest.plot(plotdata)
    else:
        contest.run(args.simulate, args.output)


def main():
    parser = argparse.ArgumentParser(description='The UKBot')
    parser.add_argument('config', help='Config file', type=argparse.FileType('r', encoding='UTF-8'))
    parser.add_argument('--page', required=False, help='Name of the contest page to work with')
    parser.add_argument('--user', required=False, help='For testing, check the contributions of a single user.')
    parser.add_argument('--simulate', action='store_true', default=False, help='Do not write results to wiki')
    parser.add_argument('--output', nargs='?', default='', help='Write results to file')
    parser.add_argument('--verbose', action='store_true', default=False, help='More verbose logging')
    parser.add_argument('--close', action='store_true', help='Close contest')
    parser.add_argument('--action', nargs='?', default='', help='"uploadplot" or "run"')
    parser.add_argument('--job_id', required=False, help='Job ID')
    args = parser.parse_args()

    if args.verbose:
        syslog.setLevel(logging.DEBUG)
    else:
        syslog.setLevel(logging.INFO)

    config = load_config(args.config)
    config['filename'] = args.config.name
    args.config.close()

    working_dir = os.path.realpath(os.getcwd())
    logger.info('Working dir: %s', working_dir)

    mainstart = config['server_timezone'].localize(datetime.now())
    mainstart_s = time.time()

    logger.info('Current server time: %s, wiki time: %s',
                mainstart.strftime('%F %T'),
                mainstart.astimezone(config['wiki_timezone']).strftime('%F %T'))
    logger.info(
        'Platform: Python %s, UKBot %s, Mwclient %s, %s',
        platform.python_version(),
        __version__,
        mwclient.__version__,
        platform.platform()
    )

    sites, sql = init_sites(config)

    Localization().init(sites.homesite)

    active_contests = list(discover_contest_pages(sql, sites.homesite, config, args.page))
    logger.info('Number of active contests: %d', len(active_contests))

    for contest_state, contest_page in active_contests:
        try:
            process_contest(contest_page, contest_state, sites, sql, config, working_dir, args)
        except InvalidContestPage as e:
            if args.simulate:
                logger.error(e.msg)
                sys.exit(1)

            error_msg = "\n* '''%s'''" % e.msg
            status_template = config['templates']['botinfo']

            te = TemplateEditor(contest_page.text())
            if status_template in te.templates:
                te.templates[status_template][0].parameters[1] = 'error'
                te.templates[status_template][0].parameters[2] = error_msg
                contest_page.save(te.wikitext(), summary=fetch_parsed_i18n('bot-problem-encountered'))
            else:
                out = '\n{{%s | error | %s }}' % (config['templates']['botinfo'], error_msg)
                contest_page.save('dummy', summary=fetch_parsed_i18n('bot-problem-encountered'), appendtext=out)
            raise

    # Update redirect page

    if 'redirect' in config['pages']:
        normal_contests = [
            contest_page.name for contest_state, contest_page in active_contests
            if contest_state == STATE_NORMAL and contest_page.name.startswith(config['pages']['base'])
        ]

        if len(normal_contests) == 1:
            contest_name = normal_contests[0]
            pages = config['pages']['redirect']
            magic_words = sites.homesite.api('query', meta='siteinfo', siprop='magicwords')['query']['magicwords']
            redirect_localized = next((word for word in magic_words if word['name'] == 'redirect'), None)['aliases'][0]
            logger.debug('Magic words for redirects is %s' % redirect_localized)
            if not isinstance(pages, list):
                pages = [pages]
            for pagename in pages:
                page = sites.homesite.pages[pagename]
                txt = '%s [[%s]]' % (redirect_localized, contest_name)
                if page.text() != txt and not args.simulate:
                    page.save(txt, summary=fetch_parsed_i18n('bot-redirecting', contest_name))

    runend = config['server_timezone'].localize(datetime.now())
    runend_s = time.time()

    logger.info('UKBot finishing at %s. Runtime was %.f seconds (total) or %.f seconds (excluding initialization).',
                runend.strftime('%F %T'),
                runend_s - runstart_s,
                runend_s - mainstart_s)


if __name__ == '__main__':
    main()
