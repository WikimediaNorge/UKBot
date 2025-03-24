# vim: fenc=utf-8 et sw=4 ts=4 sts=4 ai
import re
import json
import time
from flask import Flask
from flask import request
from flask import render_template, redirect, make_response
from mwclient import Site
from requests import ConnectionError
from mwtextextractor import get_body_text
from copy import copy
import os
from ukbot.db import db_cursor
import logging
import subprocess
import urllib.parse
from datetime import datetime
from pathlib import Path

logger = logging.getLogger('app')

project_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

contest_setups = [
    {
        "id": "no-mk",
        "name": "Månedens konkurranse",
        "url": "https://no.wikipedia.org/wiki/WP:MK",
    },
    {
        "id": "no-fd",
        "name": "Fiks det!",
        "url": "https://no.wikipedia.org/wiki/WP:FD!",
    },
    {
        "id": "no-uk",
        "name": "Ukens konkurranse",
        "url": "https://no.wikipedia.org/wiki/WP:UK",
    },
    {
        "id": "fi-vk",
        "name": "Viikon kilpailu",
        "url": "https://fi.wikipedia.org/wiki/WP:VK",
    },
    {
        "id": "fi-hl",
        "name": "Heinäkuun lähdetalkoot",
        "url": "https://fi.wikipedia.org/wiki/Wikipedia:Heinäkuun lähdetalkoot",
    },
    {
        "id": "fi-ek",
        "name": "Elokuun kuvitustalkoot",
        "url": "https://fi.wikipedia.org/wiki/Wikipedia:Elokuun kuvitustalkoot",
    },
    {
        "id": "fi-pln2021",
        "name": "Punaisten linkkien naiset",
        "url": "https://fi.wikipedia.org/wiki/Wikiprojekti:Punaisten_linkkien_naiset",
    },
    {
        "id": "eu",
        "name": "Atari:Hezkuntza/Lehiaketak",
        "url": "https://eu.wikipedia.org/wiki/Atari:Hezkuntza/Lehiaketak",
    },
    {
        "id": "es-estonia",
		"name": "Wikiconcurso Estonia",
		"url": "https://es.wikipedia.org/wiki/Wikipedia:Wikiconcurso/Reto_de_Estonia",
    },
]

app = Flask(__name__, static_url_path='/static')


def touch(fname, mode=0o664):
    flags = os.O_CREAT | os.O_APPEND
    with os.fdopen(os.open(fname, flags=flags, mode=mode)) as stream:
        os.utime(stream.fileno(), None)


def read_status(fname):
    try:
        with open(fname) as fp:
            status = json.load(fp)
    except IOError:
        logger.error('Could not read status file: %s', fname)
        return '<em>Could not read status</em>';

    args = {
        'job_status': status.get('status'),
        'job_date': datetime.fromtimestamp(int(status.get('update_date'))).strftime('%F %T'),
        'job_id': status.get('job_id'),
        'runtime': status.get('runtime'),
        'time_ago': int(time.time()) - int(status.get('update_date')),
    }

    if args['job_status'] == 'running':
        msg = 'Update started %(time_ago)d secs ago. Job ID: %(job_id)s' % args
    elif args['job_status'] == '0':
        msg = 'Last update completed %(job_date)s. Job ID: %(job_id)s. Runtime was %(runtime)s secs.' % args
    else:
        msg = '<em>Failed</em>'
    return {
        'msg': msg,
        'job_id': args['job_id'],
        'job_date': args['job_date'],
    }


@app.errorhandler(404)
def page_not_found(err):
    return render_template('404.html'), 404


@app.context_processor
def inject_current_time():
    return dict(current_time=datetime.now())


@app.route('/')
def show_home():

    cf = copy(contest_setups)
    for contest_setup in cf:
        status_file = os.path.join(project_dir, 'logs', '%s.status.json' % contest_setup['id'])
        contest_setup['status'] = read_status(status_file)

    return render_template('home.html', contest_setups=cf)

# @app.route('/<contest>/')
# def show_contest(contest):
#     if contest not in [c['id'] for c in contests]:
#         return error_404()
#     konk = []
#     points = []

#     with db_cursor() as cur:
#         cur.execute(u'SELECT C.name, U.user, U.points, T.summedPoints FROM (SELECT users.contest, users.user, MAX(users.points) usersPoints, SUM(users.points) summedPoints FROM users GROUP BY users.contest) as T JOIN users as U ON U.points=T.usersPoints JOIN contests as C on C.name=U.contest WHERE U.lang = %s', [contest])
#         for row in cur.fetchall():
#             s = row[0].split()[-1]
#             konk.append({'id': s, 'name': row[0], 'winner': {'name': row[1], 'points': row[2]}, 'sum': {'points': row[3]}})
#             points.append('%s' % row[3])

#     return render_template('uk.html',
#             lang=contest,
#             points=','.join(points),
#             contests=konk
#         )


@app.route('/jobs/<job_id>')
def show_log(job_id):
    status = request.args.get('status', '')

    return render_template('status.html', job_id=job_id, status=status)


@app.route('/jobs/<job_id>/memory')
def show_job_memory(job_id):
    contest_id, job_id = job_id.rsplit('_', 1)
    contest_id = re.sub('[^0-9a-z_-]', '', contest_id)
    job_id = re.sub('[^0-9a-zA-Z-]', '', job_id)
    project_dir = Path(__file__).resolve().parent.parent.parent
    status_file = project_dir.joinpath('logs', f'{job_id}.mem.log')
    try:
        with open(status_file) as fp:
            data = fp.read()
    except IOError:
        return {"error": "could not be opened"}

    # data = "vmem,data\n" + data.replace(" ", ",")
    output = make_response(data)
    output.headers["Content-type"] = "text/plain; charset=utf-8"
    output.headers["Access-Control-Allow-Origin"] = "*"
    return output


# @app.route('/<lang>/contest/<week>/')
# def show_uk_contest_details(lang, week):
#     if lang not in ['no', 'fi']:
#         return error_404()

#     # TODO: Migrate to MYSQL!
#     sql = db.connect()
#     cur = sql.cursor()
#     konk = []
#     cur.execute(u'SELECT name FROM contests WHERE name LIKE ?', ['%' + week])

#     row = cur.fetchone()
#     if row:
#         name = row[0]
#         users = []
#         for row2 in cur.execute(u'select user, points, bytes, newpages from users where contest=? ORDER BY points DESC', [name]):
#             users.append({ 'name': row2[0], 'points': row2[1], 'bytes': row2[2], 'newpages': row2[3]})
#         return render_template('uk_contest_details.html',
#                                lang=lang, name=name, users=users)
#     else:
#         return error_404()

# @app.route('/<lang>/user/<user>/')
# def show_uk_user_details(lang, user):
#     if lang not in ['no', 'fi']:
#         return error_404()
#     sql = db.connect() # TODO: Migrate to MySQL
#     cur = sql.cursor()
#     konk = []
#     for row in cur.execute(u'SELECT U.contest, U.points, U.bytes, U.newpages, (SELECT COUNT(DISTINCT(users.points)) FROM users WHERE users.contest=U.contest AND users.points >= U.points) AS place, (SELECT COUNT(DISTINCT(points)) FROM users WHERE users.contest=U.contest) AS npart FROM users AS U WHERE U.user=?', [user]):
#         s = row[0].split()[-1]
#         konk.append({'id': s, 'name': row[0], 'points': row[1], 'bytes': row[2], 'newpages': row[3], 'pos': row[4], 'cnt': row[5] })

#     return render_template('uk_user_details.html',
#         lang=lang,
#         name=user,
#         contests=konk
#         )

def validate(data):
    errors = []
    if len(data.get('lang', '')) < 2 or len(data.get('lang', '')) > 3:
        errors.append('invalid_lang')
    if len(data.get('page', '')) < 1:
        errors.append('no_page')
    if len(errors) != 0:
        return None, errors
    try:
        site = Site('{}.wikipedia.org'.format(data.get('lang')))
    except ConnectionError:
        return None, ['invalid_lang']
    page = site.pages[data.get('page')]
    if not page.exists:
        return None, ['invalid_page']
    return page, errors


@app.route('/wordcount')
def show_wordcount():
    lang = request.args.get('lang', '')
    page = request.args.get('page', '')
    revision = request.args.get('revision', '')
    args = {'lang': lang, 'page': page, 'revision': revision}
    if lang == '' or page == '':
        return render_template('wordcount.html', **args)
    mwpage, errors = validate(request.args)
    if len(errors) != 0:
        return render_template('wordcount.html', errors=errors, **args)

    if revision == '':
        revision = mwpage.revision
        args['revision'] = revision

    rev = next(mwpage.revisions(revision, prop='ids|content', limit=1))
    txt = rev['*']
    body = get_body_text(txt)
    return render_template(
        'wordcount.html',
        body=body,
        word_count=len(body.split()),
        **args
    )


@app.route('/contests', methods=['GET'])
def show_contests():
    status = request.args.get('status', '')
    error = request.args.get('error', '')
    contests = []
    with db_cursor() as cur:
        cur.execute(
            """SELECT
            C.contest_id, C.name, C.site, C.ended, C.closed, C.start_date, C.end_date, C.update_date, C.last_job_id
            FROM contests as C
            WHERE C.update_date IS NOT NULL
            ORDER BY C.update_date DESC
            LIMIT 10"""
        )
        for row in cur.fetchall():
            contests.append({
                'id': row[0],
                'name': row[1],
                'site': row[2],
                'ended': row[3],
                'closed': row[4],
                'start_date': row[5],
                'end_date': row[6],
                'update_date': row[7],
                'last_job_id': row[8],
            })

    return render_template('contests.html', contests=contests, status=status, error=error)


@app.route('/contests', methods=['POST'])
def update_contest():
    contest_id = request.form['contest_id']
    with db_cursor() as cur:
        cur.execute('SELECT C.config, C.name, C.last_job_id FROM contests as C WHERE C.contest_id=%s', [contest_id])
        rows = cur.fetchall()
        if len(rows) != 1:
            qs = urllib.parse.urlencode({'error': 'Contest not found'})
            return redirect('/contests?' + qs, code=302)

        config_file = rows[0][0]
        page_name = rows[0][1]
        last_job_id = rows[0][2]

    if config_file is None:
        return redirect('contests?%s' % urllib.parse.urlencode({
            'error': 'Unknown config file',
        }), code=302)

    try:
        config_short_name = re.match(r'^config/config\.(.*)\.yml$', config_file).groups()[0]
    except AttributeError:
        return redirect('contests?%s' % urllib.parse.urlencode({
            'error': 'Unknown config file',
        }), code=302)

    cmd = [
        'jsub',
        '-l', 'release=trusty',
        '-once',
        '-j', 'y',
        '-cwd',
        '-N', config_short_name,
        '-mem', '1524m',
        '/data/project/ukbot/jobs/run.sh', '--page', '\'%s\'' % page_name
        # Double-quoting is necessary due to a qsub bug,
        # see <https://phabricator.wikimedia.org/T50811>
    ]
    proc = subprocess.Popen([x.encode('utf-8') for x in cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, errs = proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, errs = proc.communicate()

    out = out.decode('utf-8') if out is not None else ''
    errs = errs.decode('utf-8') if errs is not None else ''

    m = re.match(r'^Your job ([0-9]+) ', out)
    if m:
        job_id = m.groups()[0]
        log_file = os.path.join(project_dir, 'logs', '%s_%s.log' % (config_short_name, job_id))
        touch(log_file)
        return redirect('jobs/%s_%s?%s' % (config_short_name, job_id, urllib.parse.urlencode({
            'status': 'Job started',
        })), code=302)

    qs = urllib.parse.urlencode({
        'status': out,
        'error': errs,
    })
    return redirect('contests?%s' % qs, code=302)
