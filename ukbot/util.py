# encoding=utf-8
# vim: fenc=utf-8 et sw=4 ts=4 sts=4 ai
import sys
import unicodedata
import logging
import os
from copy import deepcopy
import pytz
import yaml
import re
from datetime import datetime
from datetime import time as dt_time
from isoweek import Week  # Sort-of necessary until datetime supports %V, see http://bugs.python.org/issue12006
                          # and http://stackoverflow.com/questions/5882405/get-date-from-iso-week-number-in-python
from mwtemplates import TemplateEditor

logger = logging.getLogger(__name__)
control_char_re = None  # lazy-load


def unix_time(dt):
    """ OS-independent method to get unix time from a datetime object (strftime('%s') does not work on solaris) """
    epoch = pytz.utc.localize(datetime.utcfromtimestamp(0))
    delta = dt - epoch
    return delta.total_seconds()


def cleanup_input(value):
    global control_char_re

    if not isinstance(value, str):
        return value

    if control_char_re is None:
        logger.info('Preparing control char regexp...')
        all_chars = (chr(i) for i in range(sys.maxunicode))
        control_chars = ''.join(c for c in all_chars if unicodedata.category(c) in {'Cc', 'Cf'})
        control_char_re = re.compile('[%s]' % re.escape(control_chars))
        logger.info('Control char regexp is ready')

    value = value.strip()
    value = re.sub(r'<!--.+?-->', r'', value)
    value = control_char_re.sub('', value)

    return value


def merge(base, current):
    """
    Merges `current` onto `base`.

    >>> a = { 'first' : { 'all_rows' : { 'pass' : 'dog', 'number' : '1' } } }
    >>> b = { 'first' : { 'all_rows' : { 'fail' : 'cat', 'number' : '5' } } }
    >>> merge(a, b) == { 'first' : { 'all_rows' : { 'pass' : 'dog', 'fail' : 'cat', 'number' : '5' } } }
    True
    """
    out = deepcopy(base)
    for key, value in current.items():
        if isinstance(value, dict):
            # get node or create one
            node = out.setdefault(key, {})
            out[key] = merge(node, value)
        else:
            out[key] = value

    return out


def load_config(fp):
    folder = os.path.split(fp.name)[0]
    logger.info('Loading config %s', fp.name)

    config = {}
    base_config = {}

    main_config = yaml.safe_load(fp)
    if '_extends' in main_config:
        filename = os.path.join(folder, main_config['_extends'])
        with open(filename, encoding='utf-8') as fp2:
            base_config = load_config(fp2)

    config = merge(base_config, main_config)

    if isinstance(config.get('wiki_timezone'), str):
        config['wiki_timezone'] = pytz.timezone(config['wiki_timezone'])
    if isinstance(config.get('server_timezone'), str):
        config['server_timezone'] = pytz.timezone(config['server_timezone'])

    logger.debug('Config: %s', config)
    return config


def parse_infobox(page_text, userprefix, config):
    infobox_cfg = config['templates']['infobox']
    common_cfg = config['templates']['commonargs']
    award_cfg = config.get('awards', {})

    parsed = {'name': infobox_cfg['name']}

    te = TemplateEditor(page_text)
    infobox = te.templates[infobox_cfg['name']][0]

    # Start time / end time

    if infobox.has_param(common_cfg['year']) and infobox.has_param(common_cfg['week']):
        year = int(cleanup_input(infobox.parameters[common_cfg['year']].value))
        startweek = int(cleanup_input(infobox.parameters[common_cfg['week']].value))
        if infobox.has_param(common_cfg['week2']):
            endweek = cleanup_input(infobox.parameters[common_cfg['week2']].value)
            if endweek == '':
                endweek = startweek
        else:
            endweek = startweek
        endweek = int(endweek)

        startweek = Week(year, startweek)
        endweek = Week(year, endweek)
        parsed['start_time'] = config['wiki_timezone'].localize(datetime.combine(startweek.monday(), dt_time(0, 0, 0)))
        parsed['end_time'] = config['wiki_timezone'].localize(datetime.combine(endweek.sunday(), dt_time(23, 59, 59)))
    else:
        start_value = cleanup_input(infobox.parameters[infobox_cfg['start']].value)
        end_value = cleanup_input(infobox.parameters[infobox_cfg['end']].value)
        parsed['start_time'] = config['wiki_timezone'].localize(datetime.strptime(start_value + ' 00 00 00', '%Y-%m-%d %H %M %S'))
        parsed['end_time'] = config['wiki_timezone'].localize(datetime.strptime(end_value + ' 23 59 59', '%Y-%m-%d %H %M %S'))

    # Organizers

    parsed['organizers'] = []
    if infobox_cfg['organizer'] in infobox.parameters:
        parsed['organizers'] = re.findall(
            r'\[\[(?:User|%s):([^|\]]+)' % userprefix,
            cleanup_input(infobox.parameters[infobox_cfg['organizer']].value),
            flags=re.I
        )

    # Awards

    parsed['awards'] = []
    for award_name in award_cfg.keys():
        if infobox.has_param(award_name):
            award_value = cleanup_input(infobox.parameters[award_name].value)
            if award_value != '':
                award_value = award_value.lower().replace('&nbsp;', ' ').split()[0]
                if award_value == infobox_cfg['winner'].lower():
                    parsed['awards'].append([award_name, 'winner', 0])
                elif award_value != '':
                    try:
                        parsed['awards'].append([award_name, 'pointlimit', int(award_value)])
                    except ValueError:
                        pass

    winner_awards = [k for k, v in award_cfg.items() if v.get('winner') is True]
    if len(parsed['awards']) != 0 and 'winner' not in [award[1] for award in parsed['awards']]:
        winner_awards = ', '.join(['{{para|%s|%s}}' % (k, infobox_cfg['winner']) for k in winner_awards])
        logger.warning(
            'Found no winner award in {{tl|%s}}. Winner award is set by one of the following: %s.',
            infobox_cfg['name'],
            winner_awards
        )

    return parsed
