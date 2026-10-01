#!/usr/bin/env python3
"""
Диспетчер запусков для Hetzner: в нужную минуту просит GitHub запустить
workflow (событие workflow_dispatch). Сам ничего не собирает — сборка,
коммит данных и сайт остаются в GitHub.

Зачем: расписание `schedule:` GitHub соблюдает плохо. 27.09–01.10 «ежечасные»
табло и аэропорты коммитили 3–5 раз в сутки, интервал около 6 часов.
Ручные запуски так не троттлятся.

    python3 gh_dispatch.py seats-watch.yml   # запустить один workflow
    python3 gh_dispatch.py --check           # проверить токен и показать workflow

Живёт в /opt/tabloda-dispatch (установка — install-dispatch.sh). Окружение —
/opt/tabloda-dispatch/dispatch.env, права 600:
    GH_TOKEN   fine-grained токен: только этот репозиторий, Actions: Read and write
    GH_REPO    по умолчанию SeanKOFF/uz_rail_board
    GH_REF     по умолчанию main

Выход 0 — GitHub принял запуск (HTTP 204), иначе 1 и строка ✗ в журнале.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

API = os.environ.get("GH_API", "https://api.github.com")
REPO = os.environ.get("GH_REPO", "SeanKOFF/uz_rail_board")
REF = os.environ.get("GH_REF", "main")
TOKEN = os.environ.get("GH_TOKEN", "")
UA = "tabloda.uz-dispatch/1.0"


def call(method, path, body=None):
    """-> (HTTP-код, тело)"""
    req = urllib.request.Request(API + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={
                                     "Authorization": f"Bearer {TOKEN}",
                                     "Accept": "application/vnd.github+json",
                                     "X-GitHub-Api-Version": "2022-11-28",
                                     "User-Agent": UA,
                                     "Content-Type": "application/json",
                                 })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, repr(e)


def dispatch(workflow):
    path = f"/repos/{REPO}/actions/workflows/{workflow}/dispatches"
    for attempt in (1, 2):
        code, text = call("POST", path, {"ref": REF})
        if code == 204:
            print(f"✓ {workflow}: запуск принят")
            return True
        # сеть или 5xx — одна повторная попытка; 401/403/404/422 повторять бессмысленно
        if attempt == 1 and (code == 0 or code >= 500):
            time.sleep(30)
            continue
        hint = {401: "токен неверный или истёк",
                403: "у токена нет права Actions: write на этот репозиторий",
                404: "нет такого workflow или токен не видит репозиторий",
                422: "у workflow нет workflow_dispatch или нет ветки " + REF}.get(code, "")
        print(f"✗ {workflow}: HTTP {code} {hint} {text[:200]}")
        return False


def check():
    code, text = call("GET", f"/repos/{REPO}/actions/workflows?per_page=100")
    if code != 200:
        print(f"✗ HTTP {code}: {text[:300]}")
        return False
    flows = json.loads(text).get("workflows", [])
    print(f"✓ токен видит {REPO}, workflow: {len(flows)}")
    for w in flows:
        print(f"  {os.path.basename(w.get('path', '')):22} {w.get('state', ''):10} {w.get('name', '')}")
    return True


def main():
    args = sys.argv[1:]
    if not TOKEN:
        sys.exit("✗ нет GH_TOKEN (см. /opt/tabloda-dispatch/dispatch.env)")
    if not args:
        sys.exit(__doc__)
    if args == ["--check"]:
        sys.exit(0 if check() else 1)
    ok = all([dispatch(w) for w in args])
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
