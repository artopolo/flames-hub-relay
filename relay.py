#!/usr/bin/env python3
"""Telegram <-> Flames hub relay (GitHub Actions). Yandex Cloud can't call Telegram, so every Telegram call is made here.

The hub wakes this workflow on demand (workflow_dispatch) when it has something to send or a file to fetch;
a schedule is only a fallback.
- Webhook mode: Telegram posts updates straight into the hub (text is handled there at once). This run downloads
  files the hub is waiting for and sends the messages it queued, then exits after a short idle period.
- Poll mode (webhook not wanted or not reachable): long-poll getUpdates for WINDOW seconds, as before.
Updates are confirmed only after the hub accepted them."""
import base64, json, os, sys, time, urllib.request, urllib.error

TOKEN, KEY = os.environ['TG_TOKEN'].strip(), os.environ['RELAY_KEY'].strip()
HUBS = []
for u in (os.environ.get('HUB_URLS') or '').split(',') + [os.environ.get('HUB_URL') or '']:
    if u.strip() and u.strip() not in HUBS:
        HUBS.append(u.strip())
HOOK = (os.environ.get('HOOK_URL') or '').strip() or HUBS[0]  # address Telegram posts to
WINDOW = int(os.environ.get('WINDOW', '50'))  # poll mode: seconds of long-polling
BUSY = int(os.environ.get('BUSY', '150'))  # webhook mode: longest run
IDLE = int(os.environ.get('IDLE', '25'))  # webhook mode: stop after this long with nothing to do
STEP = float(os.environ.get('STEP', '3'))
CHECK = float(os.environ.get('CHECK', '20'))  # webhook mode: how often to ask Telegram whether delivery works
BASE = os.environ.get('TG_BASE', 'https://api.telegram.org')
TG = BASE + '/bot%s/' % TOKEN
MAXF = 2_300_000  # hub request body limit ~3.5 MB after base64: bigger files go to the hub in chunks (up_*)
TGMAX = 20_000_000  # Bot API getFile limit
ALLOWED = ['message', 'callback_query']


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
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise RuntimeError('%s: %s' % ('hub' if url.startswith(tuple(HUBS)) else 'tg ' + url.rsplit('/', 1)[-1], e)) from e


def tg(method, _t=40, **p):
    return post(TG + method, p, _t)


def hub(body):
    """Call the hub; try each address (API Gateway first), stick to the one that answered."""
    err = None
    for i, u in enumerate(list(HUBS)):
        try:
            r = post(u + ('&' if '?' in u else '?') + 'relay=' + KEY, body, 90, 'text/plain')
        except RuntimeError as e:
            err = e
            print('::warning::%s' % e)
            continue
        if i:
            HUBS.insert(0, HUBS.pop(i))
        if not r.get('ok'):
            sys.exit('hub refused: %s' % str(r)[:300])
        return r
    raise RuntimeError('hub: %s' % err)


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
    if not f or (f.get('file_size') or 0) > TGMAX:
        return {}
    r = tg('getFile', file_id=f['file_id'])
    if not r.get('ok'):
        return {}
    path = r['result']['file_path']
    with urllib.request.urlopen(BASE + '/file/bot%s/%s' % (TOKEN, path), timeout=120) as resp:
        data = resp.read()
    name = f.get('file_name') or path.rsplit('/', 1)[-1]
    if len(data) > MAXF:
        url = up_big(data, name, f.get('mime_type') or '')
        return {f['file_id']: {'url': url, 'name': name, 'mime': f.get('mime_type'), 'size': len(data)}} if url else {}
    return {f['file_id']: {'data': base64.b64encode(data).decode(), 'name': name, 'mime': f.get('mime_type')}}


def up_big(data, name, mime):
    """File bigger than one hub request: upload it in chunks (hub 1.7+). None if the hub can't take it."""
    def q(step, **kw):
        r = post(HUBS[0] + ('&' if '?' in HUBS[0] else '?') + 'relay=' + KEY, {'up': dict(kw, step=step)}, 120, 'text/plain')
        if not r.get('ok'):
            raise RuntimeError('up %s: %s' % (step, str(r)[:200]))
        return r
    try:
        r = q('begin', name=name, size=len(data), mime=mime)
        ch, per = int(r['chunk']), int(r['per'])
        chunks = [data[i:i + ch] for i in range(0, len(data), ch)]
        parts = [chunks[i:i + per] for i in range(0, len(chunks), per)]
        for p, cs in enumerate(parts, 1):
            for c, blob in enumerate(cs):
                q('part', id=r['id'], p=p, c=c, data=base64.b64encode(blob).decode(), last=c == len(cs) - 1)
        return q('end', id=r['id'], n=len(parts))['media']['u']
    except Exception as e:
        print('::warning::big file %s: %s' % (name, e))
        return None


def needs_file(u):
    m = u.get('message') or {}
    return any(m.get(k) for k in ('photo', 'video', 'document', 'voice', 'audio', 'animation'))


def run_out(out, sent):
    for e in out:
        r = tg(e['m'], **e['p'])
        if r.get('ok') and e.get('ref') and isinstance(r.get('result'), dict) and r['result'].get('message_id'):
            sent[str(r['result']['message_id'])] = e['ref']
        elif not r.get('ok') and e['m'] != 'answerCallbackQuery':
            print('tg', e['m'], r.get('description'))


def deliver(updates, sent, extra=None):
    """Hand updates (one per call when it carries a file) to the hub, run what it answers. Returns hub answer."""
    files = {}
    for u in updates:
        try:
            files.update(files_of(u))
        except Exception as e:
            print('file', e)
    r = hub(dict(extra or {}, updates=updates, files=files, sent=dict(sent), bot=BOT))
    sent.clear()
    run_out(r.get('out') or [], sent)
    return r


def take_needed(r, sent):
    """The hub lists webhook updates still waiting for their files; fetch and hand them over one by one."""
    n = 0
    for u in r.get('need') or []:
        deliver([u], sent)
        n += 1
    return n


def hook_broken(info):
    le = info.get('last_error_date') or 0
    return bool(le) and time.time() - le < 1800 and (info.get('pending_update_count') or 0) > 0


def poll(sent, seconds):
    end, offset, n = time.time() + seconds, None, 0
    while True:
        left = int(end - time.time())
        ups = tg('getUpdates', 70, **{'offset': offset, 'timeout': max(0, min(25, left)), 'allowed_updates': ALLOWED})
        if not ups.get('ok'):
            print('getUpdates', ups.get('description'))
            break
        ups = ups.get('result') or []
        batches, cur = [], []
        for u in ups:
            if needs_file(u):
                if cur:
                    batches.append(cur)
                    cur = []
                batches.append([u])
            else:
                cur.append(u)
        if cur or not batches:
            batches.append(cur)
        for bt in batches:
            r = deliver(bt, sent, {'pull': True, 'mode': 'poll'})
            n += len(bt) + take_needed(r, sent)
            if bt:
                offset = bt[-1]['update_id'] + 1
        if offset:
            tg('getUpdates', offset=offset, timeout=0)  # confirm
        if time.time() >= end - 1:
            break
    return n


def webhook_loop(sent):
    """Returns 'idle' when done, or 'broken' when Telegram can't reach the hub."""
    start = last = time.time()
    checked = start
    while time.time() - start < BUSY:
        r = hub({'pull': True, 'sent': dict(sent), 'bot': BOT, 'mode': 'webhook'})
        sent.clear()
        got = len(r.get('out') or []) + len(r.get('need') or [])
        run_out(r.get('out') or [], sent)
        take_needed(r, sent)
        if got:
            last = time.time()
        if time.time() - checked > CHECK:
            checked = time.time()
            info = tg('getWebhookInfo').get('result') or {}
            if hook_broken(info):
                return 'broken: %s' % info.get('last_error_message')
        if time.time() - last > IDLE:
            return 'idle'
        time.sleep(STEP)
    return 'idle'


BOT = None


def main():
    global BOT
    me = tg('getMe')
    if not me.get('ok'):
        sys.exit('bad bot token: %s' % me.get('description'))
    BOT = me['result'].get('username')
    sent = {}
    h = hub({'hello': True, 'pull': True, 'bot': BOT})
    run_out(h.get('out') or [], sent)
    take_needed(h, sent)
    wh = h.get('wh') or {}
    info = tg('getWebhookInfo').get('result') or {}
    mode = 'poll'
    if wh.get('want') and wh.get('secret'):
        url = HOOK + ('&' if '?' in HOOK else '?') + 'tg=' + wh['secret']
        if info.get('url') != url:
            r = tg('setWebhook', url=url, secret_token=wh['secret'], max_connections=4, allowed_updates=ALLOWED)
            if not r.get('ok'):
                print('::warning::setWebhook: %s' % r.get('description'))
                hub({'wh_fail': 'setWebhook: %s' % r.get('description')})
            info = tg('getWebhookInfo').get('result') or {}
        if info.get('url') == url and not hook_broken(info):
            mode = 'webhook'
        elif info.get('url'):
            print('::warning::webhook unreachable: %s' % info.get('last_error_message'))
            tg('deleteWebhook')  # pending updates stay with Telegram for getUpdates
            hub({'wh_fail': info.get('last_error_message') or 'нет доставки'})
    elif info.get('url'):
        tg('deleteWebhook')
    print('mode', mode)
    if mode == 'webhook':
        res = webhook_loop(sent)
        print('webhook', res)
        if res.startswith('broken'):
            print('::warning::webhook unreachable: %s' % res)
            tg('deleteWebhook')
            hub({'wh_fail': res[8:] or 'нет доставки'})
            mode = 'poll'
        else:
            hub({'wh_ok': True})
    if mode == 'poll':
        print('updates', poll(sent, WINDOW))
    for _ in range(5):  # say goodbye; hand over anything that arrived meanwhile
        r = hub({'bye': True, 'pull': True, 'sent': dict(sent), 'bot': BOT})
        sent.clear()
        run_out(r.get('out') or [], sent)
        if not r.get('need') and not sent:
            break
        take_needed(r, sent)


if __name__ == '__main__':
    try:
        main()
    except SystemExit as e:
        if e.code not in (None, 0):
            print('::error::%s' % e.code)
        raise
    except RuntimeError as e:
        if str(e).startswith('hub:'):
            # hub unreachable (Yandex Cloud side): Telegram keeps unconfirmed updates ~24h, next run retries
            print('::warning::hub unreachable, skipped run: %s' % e)
            sys.exit(0)
        print('::error::%s: %s' % (type(e).__name__, e))
        raise
    except Exception as e:
        print('::error::%s: %s' % (type(e).__name__, e))
        raise
