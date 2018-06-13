#!/usr/bin/python3
from   datetime     import datetime, time, timedelta
import json
import os
from   pathlib      import Path
import sys
from   appdirs      import AppDirs
from   configparser import ConfigParser
from   cachecontrol import CacheControl
from   cachecontrol.caches.file_cache import FileCache
import click
from   dateutil.tz  import tzstr
import pyrfc3339
import requests

API_ENDPOINT = 'https://habitica.com/api/v3'

APPDIRS     = AppDirs('habits', 'jwodder')
CONFIG_FILE = Path(APPDIRS.user_config_dir, 'habitica.cfg')
CRON_FILE   = Path(APPDIRS.user_cache_dir, 'cron')
HTTP_CACHE  = Path(APPDIRS.user_cache_dir, 'http.cache')

### TODO: Either move these to the config file or fetch them via the API:
CRON_TZ   = tzstr('EST5EDT,M3.2.0,M11.1.0')
CRON_TIME = time(4,0,0)

def colorer(c):
    return lambda txt, bold=False: click.style(txt, fg=c, bold=bold)

red     = colorer('red')
green   = colorer('green')
yellow  = colorer('yellow')
blue    = colorer('blue')
magenta = colorer('magenta')
cyan    = colorer('cyan')
white   = colorer('white')

class Habitica:
    def __init__(self, api_user, api_key, aliases):
        s = requests.Session()
        s.headers["x-api-user"] = api_user
        s.headers["x-api-key"]  = api_key
        self.s = CacheControl(s, cache=FileCache(str(HTTP_CACHE)))
        self.aliases = aliases
        self.cron_tz   = CRON_TZ
        self.cron_time = CRON_TIME
        self.cron_file = CRON_FILE

    def get(self, path, **kwargs):
        return self.request('GET', path, **kwargs)

    def post(self, path, **kwargs):
        return self.request('POST', path, **kwargs)

    def request(self, method, path, **kwargs):
        url = API_ENDPOINT.rstrip('/') + '/' + path.lstrip('/')
        r = self.s.request(method, url, **kwargs)
        if not r.ok:
            ### TODO: Use requests_toolbelt.utils.dump.dump_response instead?
            if 400 <= r.status_code < 500:
                err_type = 'Client'
            elif 500 <= r.status_code < 600:
                err_type = 'Server'
            else:
                err_type = 'Unknown'
            click.echo(
                '{0.status_code} {1} Error: {0.reason} for url: {0.url}'
                    .format(r, err_type),
                err=True,
            )
            try:
                resp = r.json()
            except ValueError:
                click.echo(r.text, err=True)
            else:
                print_json(resp, err=True)
            sys.exit(1)
        return r.json()  ### TODO: Does the API ever return non-JSON?

    def cron_if_needed(self):
        if self.cron_file.exists() and \
                self.cron_file.stat().st_mtime >= self.last_scheduled_cron():
            return
        data = self.get('/user')["data"]
        if data["needsCron"]:
            self.cron()
        else:
            self.touch_cronfile(pyrfc3339.parse(data["lastCron"]).timestamp())

    def cron(self):
        print_json(self.post('/cron', data=''))
        self.touch_cronfile()

    def touch_cronfile(self, ts=None):
        self.cron_file.parent.mkdir(parents=True, exist_ok=True)
        self.cron_file.touch(exist_ok=True)
        if ts is not None:
            os.utime(str(self.cron_file), times=(ts, ts))

    def last_scheduled_cron(self):
        """
        Calculate the *nix timestamp for the most recent `cron_time` in
        `cron_tz`
        """
        now = datetime.now(self.cron_tz)
        if now.time() >= self.cron_time:
            cron_date = now.date()
        else:
            ### TODO: Handle DST changes (How does Habitica handle them?)
            cron_date = now.date() - timedelta(days=1)
        return datetime.combine(
            cron_date,
            self.cron_time.replace(tzinfo=self.cron_tz),
        ).timestamp()

    def task_up(self, tid):
        self.show_task_response(
            self.post('/tasks/{}/score/up'.format(tid), data='')
        )

    def task_down(self, tid):
        self.show_task_response(
            self.post('/tasks/{}/score/down'.format(tid), data='')
        )

    def show_task_response(self, r):
        for event, about in sorted(r["data"]["_tmp"].items()):
            if event == "quest":
                if "progressDelta" in about:
                    ### Does about["collection"] even mean anything?
                    click.echo('QUEST: Damage to boss: {progressDelta}'
                               .format(**about))
                else:
                    click.echo('QUEST:')
                    print_json(about)
            elif event == "drop":
                click.echo('DROP: ' + about["dialog"])
            elif event == "crit":
                click.echo('CRITICAL HIT!')  ### Do something with the value?
            elif event == "streakBonus":
                # Ignore
                pass
            elif event == 'leveledUp':  ### TODO: Check casing
                click.echo('LEVEL UP!')
            else:
                click.echo(event + ':')
                print_json(about)
        ### TODO: if r.get("notifications"):
        """
        Example "notifications" entry:
            {
                "data": {},
                "id": "ebceba1b-1ef1-44ee-8563-73f78fc9bef6",
                "type": "STREAK_ACHIEVEMENT"
            }
        """


def print_json(obj, err=False):
    click.echo(json.dumps(obj, sort_keys=True, indent=4), err=err)

@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    '-c', '--config',
    type         = click.Path(dir_okay=False),
    default      = str(CONFIG_FILE),
    show_default = True,
)
@click.pass_context
def main(ctx, config):
    cfg = ConfigParser()
    cfg.read(config)
    try:
        aliases = cfg['alias']
    except KeyError:
        aliases = {}
    ctx.obj = Habitica(cfg['auth']['api-user'], cfg['auth']['api-key'], aliases)

@main.command()
@click.option('--no-cron', is_flag=True)
@click.argument('task', nargs=-1)
@click.pass_obj
def up(hb, task, no_cron):
    if not no_cron:
        hb.cron_if_needed()
    tids = []
    for t in task:
        try:
            tids.append(hb.aliases[t])
        except KeyError:
            raise click.UsageError('{}: unknown task'.format(t))
    for t in tids:
        hb.task_up(t)

@main.command()
@click.option('--no-cron', is_flag=True)
@click.argument('task', nargs=-1)
@click.pass_obj
def down(hb, task, no_cron):
    if not no_cron:
        hb.cron_if_needed()
    tids = []
    for t in task:
        try:
            tids.append(hb.aliases[t])
        except KeyError:
            raise click.UsageError('{}: unknown task'.format(t))
    for t in tids:
        hb.task_down(t)

@main.command()
@click.option('-A', '--all', 'show_all', is_flag=True)
#@click.option('--ids', is_flag=True)  ### TODO
@click.pass_obj
def status(hb, show_all):
    user_data = hb.get('/user')["data"]
    ### TODO: Refresh cron file based on this information:
    click.echo('{} Last cron: {:%Y-%m-%d %H:%M:%S %Z}'.format(
        red('!', bold=True) if user_data["needsCron"] else green('✓'),
        pyrfc3339.parse(user_data["lastCron"]).astimezone(hb.cron_tz),
    ))
    task_lines = {}
    for task in hb.get('/tasks/user')["data"]:
        if task["type"] == "daily":
            if task["isDue"]:
                # Needs to be done today
                txt = green('[✓]') if task["completed"] else red('[ ]')
            else:
                txt = '[✓]' if task["completed"] else '[-]'
        elif task["type"] == "habit":
            txt = '['
            if task["up"]:
                num = '{:+3d}'.format(task["counterUp"])
                if task["counterUp"] > 0:
                    num = green(num)
                txt += num
            else:
                txt += ' - '
            txt += '/'
            if task["down"]:
                num = '-{:<2d}'.format(task["counterDown"])
                if task["counterDown"] > 0:
                    num = red(num)
                txt += num
            else:
                txt += ' - '
            txt += ']'
        elif task["type"] == "reward":
            txt = '$' + str(task["value"])
            ### No status information???
        elif task["type"] == "todo":
            # task["completed"]
            continue
        txt += ' ' + task["text"]
        task_lines[task["id"]] = txt
    tasks_order = user_data["tasksOrder"]
    if show_all:
        to_show = set(tid for order in tasks_order.values() for tid in order)
    else:
        to_show = set(hb.aliases.values())
    for header, key in [
        ('HABITS', 'habits'),
        ('DAILIES', 'dailys'),
        #('TODOS', 'todos'),
        ('REWARDS', 'rewards'),
    ]:
        click.echo(header)
        for tid in tasks_order[key]:
            if tid in to_show:
                click.echo(task_lines[tid])
        click.echo()

@main.command()
@click.option('-f', '--force', is_flag=True)
@click.pass_obj
def cron(hb, force):
    hb.cron() if force else hb.cron_if_needed()

@main.command()
@click.pass_obj
def quest(hb):
    current_quest = hb.get('/groups/party')["data"]["quest"]
    if not current_quest or current_quest.get("active") is False:
        click.echo('Not currently on a quest')
        return
    progress = current_quest["progress"]
    pending = hb.get("/user")["data"]["party"]["quest"]["progress"]
    quest = hb.get('/content')["data"]["quests"][current_quest["key"]]
    click.echo(quest["text"])
    if "boss" in quest:
        click.echo(
            'HP: {} / {} (Pending: {})'.format(
                progress["hp"],
                quest["boss"]["hp"],
                pending["up"],
            )
        )
        if quest.get("rage"):
            ### TODO: Is pending["down"] the pending rage increase?
            click.echo(
                'Rage: {} / {}'.format(
                    progress["rage"],
                    quest["boss"]["rage"]["value"],
                )
            )
    elif "collect" in quest:
        ### TODO: Check this:
        for k,v in quest["collect"].items():
            click.echo(
                '{}: {} / {} (Pending: {})'.format(
                    v["text"],
                    progress["collect"].get(k,0),
                    v["count"],
                    pending["collect"].get(k,0),
                )
            )
    else:
        print_json(progress)

if __name__ == '__main__':
    main()
