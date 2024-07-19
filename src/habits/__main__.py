from __future__ import annotations
from configparser import ConfigParser
from dataclasses import dataclass
from datetime import datetime, time, timedelta
import json
import os
from pathlib import Path
import platform
import sys
from typing import Any, Protocol
from cachecontrol import CacheControl
from cachecontrol.caches.file_cache import FileCache
import click
from dateutil.parser import isoparse
from dateutil.tz import tzstr
from platformdirs import PlatformDirs
import requests
from . import __url__, __version__

USER_AGENT = "habits/{} ({}) requests/{} {}/{}".format(
    __version__,
    __url__,
    requests.__version__,
    platform.python_implementation(),
    platform.python_version(),
)

API_ENDPOINT = "https://habitica.com/api/v3"

DIRS = PlatformDirs("habits", "jwodder")
CONFIG_FILE = DIRS.user_config_path / "habitica.cfg"
CRON_FILE = DIRS.user_cache_path / "cron"
HTTP_CACHE = DIRS.user_cache_path / "http.cache"

### TODO: Either move these to the config file or fetch them via the API:
CRON_TZ = tzstr("EST5EDT,M3.2.0,M11.1.0")
CRON_TIME = time(4, 0, 0)


class Colorer(Protocol):
    def __call__(self, txt: str, bold: bool = False) -> str: ...


def colorer(c: str) -> Colorer:
    return lambda txt, bold=False: click.style(txt, fg=c, bold=bold)


red = colorer("red")
green = colorer("green")
yellow = colorer("yellow")
blue = colorer("blue")
magenta = colorer("magenta")
cyan = colorer("cyan")
white = colorer("white")


class Habitica:
    def __init__(self, api_user: str, api_key: str, aliases: dict[str, str]) -> None:
        s = requests.Session()
        s.headers["x-api-user"] = api_user
        s.headers["x-api-key"] = api_key
        s.headers["x-client"] = "1c26156c-0419-4622-bfe5-94ce36f70551 - habits"
        self.s = CacheControl(s, cache=FileCache(str(HTTP_CACHE)))
        self.aliases = aliases
        self.cron_tz = CRON_TZ
        self.cron_time = CRON_TIME
        self.cron_file = CRON_FILE

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.request("POST", path, **kwargs)

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = API_ENDPOINT.rstrip("/") + "/" + path.lstrip("/")
        r = self.s.request(method, url, **kwargs)
        if not r.ok:
            if 400 <= r.status_code < 500:
                err_type = "Client"
            elif 500 <= r.status_code < 600:
                err_type = "Server"
            else:
                err_type = "Unknown"
            ### TODO: Raise a dedicated exception (handled by a decorator on
            ### all click.command functions) instead of this:
            click.echo(
                f"{r.status_code} {err_type} Error: {r.reason} for URL: {r.url}",
                err=True,
            )
            try:
                resp = r.json()
            except ValueError:
                click.echo(r.text, err=True)
            else:
                print_json(resp, err=True)
            sys.exit(1)
        return r.json()  # Does the API ever return non-JSON?

    def cron_if_needed(self) -> None:
        if (
            self.cron_file.exists()
            and self.cron_file.stat().st_mtime >= self.last_scheduled_cron().timestamp()
        ):
            return
        data = self.get("/user")["data"]
        if data["needsCron"]:
            self.cron()
        else:
            self.touch_cronfile(isoparse(data["lastCron"]))

    def cron(self) -> None:
        print_json(self.post("/cron", data=""))
        self.touch_cronfile()

    def touch_cronfile(self, ts: datetime | None = None) -> None:
        self.cron_file.parent.mkdir(parents=True, exist_ok=True)
        self.cron_file.touch(exist_ok=True)
        if ts is not None:
            ts_secs = ts.timestamp()
            os.utime(self.cron_file, times=(ts_secs, ts_secs))

    def last_scheduled_cron(self) -> datetime:
        """
        Calculate the timestamp for the most recent `cron_time` in `cron_tz`
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
        )

    def task_up(self, tid: str) -> TaskResponse:
        return TaskResponse(self.post(f"/tasks/{tid}/score/up", data=""))

    def task_down(self, tid: str) -> TaskResponse:
        return TaskResponse(self.post(f"/tasks/{tid}/score/down", data=""))


@dataclass
class TaskResponse:
    response_json: dict[str, Any]

    def show(self) -> None:
        for event, about in sorted(self.response_json["data"]["_tmp"].items()):
            if event == "quest":
                if "progressDelta" in about:
                    click.echo(
                        "QUEST: Damage to boss: {progressDelta}".format_map(about)
                    )
                if about.get("collection", 0) > 0:
                    click.echo(
                        "QUEST: Collected {} quest item{}".format(
                            about["collection"], "s" if about["collection"] > 1 else ""
                        )
                    )
                # else:
                #    click.echo('QUEST:')
                #    print_json(about)
            elif event == "drop":
                click.echo("DROP: " + about["dialog"])
            elif event == "crit":
                click.echo("CRITICAL HIT!")  ### Do something with the value?
            elif event == "streakBonus":
                # Ignore
                pass
            elif event == "leveledUp":
                click.echo("LEVEL UP!")
            else:
                click.echo(event + ":")
                print_json(about)
        ### TODO: if self.response_json.get("notifications"):
        """
        Example "notifications" entry:
            {
                "data": {},
                "id": "ebceba1b-1ef1-44ee-8563-73f78fc9bef6",
                "type": "STREAK_ACHIEVEMENT"
            }
        """

    def show_json(self) -> None:
        print_json(self.response_json)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "-c",
    "--config",
    type=click.Path(dir_okay=False, path_type=Path),
    default=CONFIG_FILE,
    show_default=True,
)
@click.pass_context
def main(ctx: click.Context, config: Path) -> None:
    cfg = ConfigParser()
    cfg.read(config)
    aliases: dict[str, str]
    try:
        aliases = dict(cfg["alias"])
    except KeyError:
        aliases = {}
    ctx.obj = Habitica(cfg["auth"]["api-user"], cfg["auth"]["api-key"], aliases)


@main.command()
@click.option("-J", "--show-json", is_flag=True)
@click.option("--no-cron", is_flag=True)
@click.argument("task", nargs=-1)
@click.pass_obj
def up(hb: Habitica, task: str, no_cron: bool, show_json: bool) -> None:
    """Check-off or +1 a task"""
    if not no_cron:
        hb.cron_if_needed()
    tids = []
    for t in task:
        try:
            tids.append(hb.aliases[t])
        except KeyError:
            raise click.UsageError(f"{t}: unknown task")
    for t in tids:
        r = hb.task_up(t)
        if show_json:
            r.show_json()
        else:
            r.show()


@main.command()
@click.option("-J", "--show-json", is_flag=True)
@click.option("--no-cron", is_flag=True)
@click.argument("task", nargs=-1)
@click.pass_obj
def down(hb: Habitica, task: str, no_cron: bool, show_json: bool) -> None:
    """Uncheck or -1 a task"""
    if not no_cron:
        hb.cron_if_needed()
    tids = []
    for t in task:
        try:
            tids.append(hb.aliases[t])
        except KeyError:
            raise click.UsageError(f"{t}: unknown task")
    for t in tids:
        r = hb.task_down(t)
        if show_json:
            r.show_json()
        else:
            r.show()


@main.command()
@click.option("-A", "--all", "show_all", is_flag=True)
# @click.option('--ids', is_flag=True)  ### TODO
@click.pass_obj
def status(hb: Habitica, show_all: bool) -> None:
    """View current task progress"""
    user_data = hb.get("/user")["data"]
    ### TODO: Refresh cron file based on this information:
    click.echo(
        "{} Last cron: {:%Y-%m-%d %H:%M:%S %Z}".format(
            red("!", bold=True) if user_data["needsCron"] else green("✓"),
            isoparse(user_data["lastCron"]).astimezone(hb.cron_tz),
        )
    )
    task_lines = {}
    for task in hb.get("/tasks/user")["data"]:
        if task["type"] == "daily":
            if task["isDue"]:
                # Needs to be done today
                txt = green("[✓]") if task["completed"] else red("[ ]")
            else:
                txt = "[✓]" if task["completed"] else "[-]"
        elif task["type"] == "habit":
            txt = "["
            if task["up"]:
                num = f'{task["counterUp"]:+3d}'
                if task["counterUp"] > 0:
                    num = green(num)
                txt += num
            else:
                txt += " - "
            txt += "/"
            if task["down"]:
                num = f'-{task["counterDown"]:<2d}'
                if task["counterDown"] > 0:
                    num = red(num)
                txt += num
            else:
                txt += " - "
            txt += "]"
        elif task["type"] == "reward":
            txt = "$" + str(task["value"])
            ### No status information???
        elif task["type"] == "todo":
            # task["completed"]
            continue
        txt += " " + task["text"]
        task_lines[task["id"]] = txt
    tasks_order = user_data["tasksOrder"]
    if show_all:
        to_show = set(tid for order in tasks_order.values() for tid in order)
    else:
        to_show = set(hb.aliases.values())
    for header, key in [
        ("HABITS", "habits"),
        ("DAILIES", "dailys"),
        # ('TODOS', 'todos'),
        ("REWARDS", "rewards"),
    ]:
        click.echo(header)
        for tid in tasks_order[key]:
            if tid in to_show:
                click.echo(task_lines[tid])
        click.echo()


@main.command()
@click.option("-f", "--force", is_flag=True)
@click.pass_obj
def cron(hb: Habitica, force: bool) -> None:
    """Start new Habitica day"""
    hb.cron() if force else hb.cron_if_needed()


@main.command()
@click.pass_obj
def quest(hb: Habitica) -> None:
    """Show current quest progress"""
    current_quest = hb.get("/groups/party")["data"]["quest"]
    if not current_quest or current_quest.get("active") is False:
        click.echo("Not currently on a quest")
        return
    progress = current_quest["progress"]
    pending = hb.get("/user")["data"]["party"]["quest"]["progress"]
    quest = hb.get("/content")["data"]["quests"][current_quest["key"]]
    click.echo(quest["text"])
    if "boss" in quest:
        click.echo(
            "HP: {} / {} (Pending: {})".format(
                progress["hp"],
                quest["boss"]["hp"],
                pending["up"],
            )
        )
        if quest["boss"].get("rage"):
            click.echo(
                "Rage: {} / {}".format(
                    progress["rage"],
                    quest["boss"]["rage"]["value"],
                )
            )
    elif "collect" in quest:
        for k, v in quest["collect"].items():
            click.echo(
                "{}: {} / {}".format(
                    v["text"],
                    progress["collect"].get(k, 0),
                    v["count"],
                )
            )
        click.echo(f'Pending: {pending["collectedItems"]}')
    else:
        print_json(progress)


def print_json(obj: Any, err: bool = False) -> None:
    click.echo(json.dumps(obj, sort_keys=True, indent=4), err=err)


if __name__ == "__main__":
    main()
