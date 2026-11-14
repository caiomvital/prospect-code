#!/usr/bin/env python3
"""Leitura de perfis do Instagram na SUA maquina (IP residencial) para o painel de prospeccao.

Fluxo: pede ao painel a lista de @ pendentes -> le cada perfil como navegador logado, devagar ->
envia os dados ao painel. Python 3.8+, so biblioteca padrao. Linux/macOS (cron).

Configuracao: ~/.prospeccao_ig.env (chmod 600), uma variavel por linha:
    PAINEL_URL=https://fluxos.dev.br/admin
    SYNC_TOKEN=<valor de IG_SYNC_TOKEN do .env da VPS>
    IG_SESSIONID=<cookie sessionid da conta usada para ler>
    MAX_POR_EXECUCAO=8            (opcional, padrao 8, maximo 15)

Uso:   ig_local.py [--seco]      (--seco: so lista os @ pendentes, nao acessa o Instagram)
Cron:  veja o final do arquivo.
Seguranca: o cookie nunca vai para o painel nem para o log. Em qualquer bloqueio (429, 401, 403,
redirecionamento, login_required, checkpoint) o script PARA e fica em pausa por 24-72 h.
"""
import json
import os
import random
import re
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

CFG = os.path.expanduser('~/.prospeccao_ig.env')
DIR = os.path.expanduser('~/.local/share/prospeccao-ig')
ESTADO = os.path.join(DIR, 'estado.json')
LOG = os.path.join(DIR, 'ig.log')
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
      'Chrome/124.0 Safari/537.36')


def log(*a):
    os.makedirs(DIR, exist_ok=True)
    linha = datetime.now().strftime('%Y-%m-%d %H:%M:%S ') + ' '.join(str(x) for x in a)
    print(linha)
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(linha + '\n')
    try:
        if os.path.getsize(LOG) > 500_000:
            os.replace(LOG, LOG + '.1')
    except OSError:
        pass


def config():
    if not os.path.exists(CFG):
        sys.exit(f'Falta {CFG}. Veja o cabecalho do script.')
    if os.stat(CFG).st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        sys.exit(f'{CFG} esta legivel por outros usuarios. Rode: chmod 600 {CFG}')
    c = {}
    for l in open(CFG, encoding='utf-8'):
        l = l.strip()
        if l and not l.startswith('#') and '=' in l:
            k, v = l.split('=', 1)
            c[k.strip()] = v.strip().strip('"\'')
    for k in ('PAINEL_URL', 'SYNC_TOKEN', 'IG_SESSIONID'):
        if not c.get(k):
            sys.exit(f'{k} ausente em {CFG}')
    c['PAINEL_URL'] = c['PAINEL_URL'].rstrip('/')
    c['MAX'] = max(1, min(int(c.get('MAX_POR_EXECUCAO', 8)), 15))
    return c


class _SemRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_op = urllib.request.build_opener(_SemRedirect)


def http(url, headers, dados=None, timeout=20):
    req = urllib.request.Request(url, headers=headers, data=dados)
    try:
        with _op.open(req, timeout=timeout) as r:
            return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, ''
    except Exception as e:
        return 0, type(e).__name__


def painel(c, caminho, corpo=None):
    h = {'Authorization': 'Bearer ' + c['SYNC_TOKEN'], 'User-Agent': 'ig-local/1.0'}
    if corpo is not None:
        h['Content-Type'] = 'application/json'
        corpo = json.dumps(corpo).encode()
    st, txt = http(c['PAINEL_URL'] + caminho, h, corpo)
    if st != 200:
        sys.exit(f'painel respondeu {st} em {caminho}')
    return json.loads(txt)


def ler(handle, sid):
    """('ok'|'sem_dados'|'bloqueio'|'erro', dados)"""
    url = 'https://www.instagram.com/api/v1/users/web_profile_info/?' + urllib.parse.urlencode({'username': handle})
    st, txt = http(url, {'User-Agent': UA, 'X-IG-App-ID': '936619743392459', 'Accept': '*/*',
                         'Accept-Language': 'pt-BR,pt;q=0.9', 'X-Requested-With': 'XMLHttpRequest',
                         'Referer': f'https://www.instagram.com/{handle}/', 'Cookie': 'sessionid=' + sid}, timeout=15)
    if st == 404:
        return 'sem_dados', {}
    if st in (301, 302, 303, 307, 308, 401, 403, 429):
        return 'bloqueio', {'http': st}
    if st != 200:
        return 'erro', {'http': st}
    try:
        j = json.loads(txt)
    except ValueError:
        return 'bloqueio', {'motivo': 'nao-json'}
    if j.get('message') in ('login_required', 'checkpoint_required') or j.get('require_login'):
        return 'bloqueio', {'motivo': str(j.get('message'))[:30]}
    u = (j.get('data') or {}).get('user')
    if not u:
        return 'sem_dados', {}
    m = u.get('edge_owner_to_timeline_media') or {}
    d = {'seguidores': (u.get('edge_followed_by') or {}).get('count'), 'posts': m.get('count'),
         'privado': bool(u.get('is_private')), 'link_externo': u.get('external_url') or None}
    ts = [e['node']['taken_at_timestamp'] for e in (m.get('edges') or []) if e.get('node', {}).get('taken_at_timestamp')]
    if ts:
        ult = datetime.fromtimestamp(max(ts), timezone.utc)
        d['ultimo_post'] = ult.date().isoformat()
        d['dias_sem_postar'] = (datetime.now(timezone.utc) - ult).days
    base = (u.get('biography') or '') + ' ' + (d['link_externo'] or '') + ' ' + str(u.get('business_phone_number') or '')
    d['bio_contato'] = bool(re.search(r'wa\.me/|api\.whatsapp\.com|\(?\b\d{2}\)?\s?9?\d{4}[-\s]?\d{4}\b', base))
    return 'ok', d


def estado():
    try:
        return json.load(open(ESTADO))
    except Exception:
        return {}


def main():
    seco = '--seco' in sys.argv
    c = config()
    e = estado()
    if not seco and e.get('pausado_ate', 0) > time.time():
        log('em pausa apos bloqueio ate', datetime.fromtimestamp(e['pausado_ate']).strftime('%d/%m %H:%M')); return
    handles = painel(c, f'/api/ig/pendentes?limite={c["MAX"]}')['handles']
    log(f'{len(handles)} perfis pendentes' + (' (modo seco)' if seco else ''))
    if seco:
        print('\n'.join(handles)); return
    time.sleep(random.uniform(0, float(c.get('JITTER_MAX', 90))))                  # nao comecar sempre no mesmo segundo
    resultados, bloqueio = [], None
    for i, h in enumerate(handles):
        st, d = ler(h, c['IG_SESSIONID'])
        log(f'  @{h}: {st}')
        if st == 'bloqueio':
            bloqueio = d
            break
        if st in ('ok', 'sem_dados'):
            resultados.append({'handle': h, 'status': st, 'dados': d})
        if i < len(handles) - 1:
            time.sleep(random.uniform(10, 20))
    if resultados:
        r = painel(c, '/api/ig/resultados', {'resultados': resultados})
        log('enviado ao painel:', r)
    if bloqueio:
        horas = 24 if bloqueio.get('http') == 429 else 72
        e['pausado_ate'] = time.time() + horas * 3600
        json.dump(e, open(ESTADO, 'w'))
        log(f'BLOQUEIO ({bloqueio}); pausando {horas} h. Se for login/checkpoint, renove o IG_SESSIONID.')


if __name__ == '__main__':
    main()

# Cron (ajuste o caminho do script); roda de segunda a sabado as 20:15 da sua maquina:
#   15 20 * * 1-6  /usr/bin/python3 /caminho/ig_local.py >> /dev/null 2>&1
# Instalacao:  chmod 700 ig_local.py ; crie ~/.prospeccao_ig.env ; chmod 600 ~/.prospeccao_ig.env
# Teste sem tocar no Instagram:  python3 ig_local.py --seco
