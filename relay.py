#!/usr/bin/env python3
"""Telegram <-> Flames hub relay. Runs in GitHub Actions on a schedule (Yandex Cloud can't reach Telegram).
Each run: poll Telegram for WINDOW seconds, hand updates (+ small files) to the hub, execute the hub's queued
Telegram calls, report sent message ids back. Updates are confirmed only after the hub accepted them."""
import base64, json, os, sys, time, urllib.request, urllib.error

TOKEN, HUB, KEY = os.environ['TG_TOKEN'].strip(), os.environ['HUB_URL'].strip(), os.environ['RELAY_KEY'].strip()
WINDOW = int(os.environ.get('WINDOW', '50'))
BASE = os.environ.get('TG_BASE', 'https://api.telegram.org')
TG = BASE + '/bot%s/' % TOKEN
MAXF = 2_300_000  # hub request body limit ~3.5 MB after base64


def post(url, data, timeout=60, ctype='application/json'):
    r = urllib.request.Request(url, data=json.dumps(data).encode(), headers={'Content-Type': ctype})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return json.loads(resp.read() or b'{}')
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {'ok': False, 'description': 'http %s' % e.code}


def tg(method, _t=40, **p):
    return post(TG + method, p, _t)


def hub(body):
    r = post(HUB + '?relay=' + KEY, body, 90, 'text/plain')
    if not r.get('ok'):
        sys.exit('hub refused: %s' % str(r)[:300])
    return r


def files_of(u):
    m = u.get('message') or {}
    f = None
    if m.get('photo'):
        f = dict(m['photo'][-1], mime_type='image/jpeg')
    else:
        for k in ('video', 'document', 'voice', 'audio', 'animation'):
            if m.get(k):
                f = m[k]
                break
    if not f or (f.get('file_size') or 0) > MAXF:
        return {}
    r = tg('getFile', file_id=f['file_id'])
    if not r.get('ok'):
        return {}
    path = r['result']['file_path']
    with urllib.request.urlopen(BASE + '/file/bot%s/%s' % (TOKEN, path), timeout=60) as resp:
        data = resp.read()
    return {f['file_id']: {'data': base64.b64encode(data).decode(), 'name': f.get('file_name') or path.rsplit('/', 1)[-1],
                           'mime': f.get('mime_type')}}


def run_out(out, sent):
    for e in out:
        r = tg(e['m'], **e['p'])
        if r.get('ok') and e.get('ref') and isinstance(r.get('result'), dict) and r['result'].get('message_id'):
            sent[str(r['result']['message_id'])] = e['ref']
        elif not r.get('ok') and e['m'] != 'answerCallbackQuery':
            print('tg', e['m'], r.get('description'))


def main():
    me = tg('getMe')
    if not me.get('ok'):
        sys.exit('bad bot token: %s' % me.get('description'))
    bot = me['result'].get('username')
    tg('deleteWebhook')
    tg('setMyCommands', commands=[{'command': 'p', 'description': 'выбрать страницу'},
                                  {'command': 'now', 'description': 'куда пишу сейчас'}])
    end, offset, sent, n = time.time() + WINDOW, None, {}, 0
    first = True
    while True:
        left = int(end - time.time())
        ups = tg('getUpdates', 70, **{'offset': offset, 'timeout': max(0, min(25, left)),
                                      'allowed_updates': ['message', 'callback_query']})
        ups = ups.get('result') or []
        # one update per hub call when it carries a file (body size), otherwise batch
        batches, cur = [], []
        for u in ups:
            if files_of_needed(u):
                if cur:
                    batches.append(cur)
                    cur = []
                batches.append([u])
            else:
                cur.append(u)
        if cur or not batches:
            batches.append(cur)
        for bt in batches:
            if not bt and not first and not sent:
                continue
            files = {}
            for u in bt:
                try:
                    files.update(files_of(u))
                except Exception as e:
                    print('file', e)
            r = hub({'updates': bt, 'files': files, 'sent': sent, 'bot': bot})
            sent = {}
            first = False
            run_out(r.get('out') or [], sent)
            n += len(bt)
            if bt:
                offset = bt[-1]['update_id'] + 1
        if offset:
            tg('getUpdates', offset=offset, timeout=0)  # confirm
        if time.time() >= end - 1:
            break
    if sent:
        run_out(hub({'updates': [], 'sent': sent, 'bot': bot}).get('out') or [], {})
    print('updates', n)


def files_of_needed(u):
    m = u.get('message') or {}
    return any(m.get(k) for k in ('photo', 'video', 'document', 'voice', 'audio', 'animation'))


if __name__ == '__main__':
    main()
