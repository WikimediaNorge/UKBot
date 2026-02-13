# encoding=utf-8
# vim: fenc=utf-8 et sw=4 ts=4 sts=4 ai
import logging
from datetime import datetime
from .common import STATE_NORMAL, STATE_ENDING, STATE_CLOSING
from .util import parse_infobox

logger = logging.getLogger(__name__)


def award_delivery_confirmed(site, config, page_title):
    status_page = site.pages[config['pagename']]
    confirmation_message = config['send']

    if status_page.exists:
        lastrev = status_page.revisions(prop='user|comment|content', slots='main').next()
        if lastrev['comment'].find('/* %s */' % confirmation_message) == -1 and lastrev['slots']['main']['*'].find(confirmation_message) == -1:
            logger.info('Contest [[%s]] is to be closed, but award delivery has not been confirmed yet', page_title)
        else:
            logger.info('Will close contest [[%s]], award delivery has been confirmed', page_title)
            return True


def sync_contests_table(sql, homesite, config):

    cursor = sql.cursor()

    infobox_cfg = config['templates']['infobox']
    infobox_page = homesite.pages['Template:' + infobox_cfg['name']]
    contest_pages = list(infobox_page.embeddedin())

    cursor.execute('SELECT name, start_date, end_date, ended, closed FROM contests WHERE site=%s', [homesite.key])
    contests = cursor.fetchall()
    contest_names = [c[0] for c in contests]

    for page in contest_pages:
        if not page.name.startswith(config['pages']['base']):
            continue

        if page.name not in contest_names:
            logger.info('Found new contest: %s', page.name)

            try:
                infobox = parse_infobox(page.text(), homesite.namespaces[2], config)
            except ValueError:
                logger.error('Failed to parse infobox for contest %s', page.name)
                continue

            cursor.execute('INSERT INTO contests (config, site, name, start_date, end_date) VALUES (%s,%s,%s,%s,%s)', [
                config['filename'],
                homesite.key,
                page.name,
                infobox['start_time'].strftime('%F %T'),
                infobox['end_time'].strftime('%F %T')
            ])
            sql.commit()
    cursor.close()


def get_contest_page_titles(sql, homesite, config):

    cursor = sql.cursor()

    now = config['server_timezone'].localize(datetime.now())
    now_w = now.astimezone(config['wiki_timezone'])
    now_s = now_w.strftime('%F %T')

    # 1) Check if there are contests to close

    cursor.execute(
        'SELECT name FROM contests WHERE site=%s AND name LIKE %s AND update_date IS NOT NULL AND ended=1 AND closed=0',
        [homesite.key, config['pages']['base'] + '%%']
    )
    for row in cursor.fetchall():
        page_title = row[0]
        if 'awardstatus' in config:
            if award_delivery_confirmed(homesite, config['awardstatus'], page_title):
                logger.info('Award delivery confirmed for [[%s]]', page_title)
                yield STATE_CLOSING, page_title
        else:
            logger.info('Contest ended: [[%s]]. Auto-closing since there\'s no award delivery', page_title)
            cursor.execute('UPDATE contests SET closed=1 WHERE site=%s AND name=%s', [homesite.key, page_title])
            sql.commit()

    # 2) Check if there are contests to end

    cursor.execute(
        'SELECT name, end_date FROM contests WHERE site=%s AND name LIKE %s AND update_date IS NOT NULL AND ended=0 AND closed=0 AND end_date < %s',
        [homesite.key, config['pages']['base'] + '%%', now_s]
    )
    for row in cursor.fetchall():
        page_title = row[0]
        logger.info(f"Contest [[%s]] ended at %s. Current time is %s", page_title, row[1].strftime('%F %T'), now_s)
        yield STATE_ENDING, page_title

    # 3) Check if there are other contests to update

    cursor.execute(
        'SELECT name FROM contests WHERE site=%s AND name LIKE %s AND ended=0 AND closed=0 AND start_date < %s',
        [homesite.key, config['pages']['base'] + '%%', now_s]
    )
    for row in cursor.fetchall():
        page_title = row[0]
        yield STATE_NORMAL, page_title

    cursor.close()


def discover_contest_pages(sql, homesite, config, page_title=None):

    sync_contests_table(sql, homesite, config)

    if page_title is not None:
        cursor = sql.cursor()

        cursor.execute('SELECT ended, closed FROM contests WHERE site=%s AND name=%s', [
            homesite.key,
            page_title,
        ])
        contests = cursor.fetchall()
        pages = [(STATE_NORMAL, page_title)]
        if len(contests) == 1:
            if contests[0][1] == 1:
                logger.error('Contest %s is closed, cannot be updated', page_title)
                pages = []
            elif contests[0][0] == 1:
                if award_delivery_confirmed(homesite, config['awardstatus'], page_title):
                    pages = [(STATE_CLOSING, page_title)]
                else:
                    pages = [(STATE_ENDING, page_title)]

    else:
        pages = get_contest_page_titles(sql, homesite, config)

    for p in pages:
        page = homesite.pages[p[1]]
        if not page.exists:
            logger.warning('Page does not exist: %s', p[1])
            continue
        page = page.resolve_redirect()

        yield p[0], page
