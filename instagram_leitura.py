"""Leitura PUBLICA de perfis de prospects (sem login, sem cookies, sem token).

- 1 GET em https://www.instagram.com/<handle>/ por prospect, intervalo de 8-15 s entre leituras,
  maximo IG_LEITURAS_DIA por dia (config do ciclo; padrao 20).
- 302/401/403/429 ou pagina de login => instagram_leitura='indisponivel' e as leituras do dia PARAM.
- NUNCA usa o token nem a conta @negocioemfluxo: este modulo nao importa instagram_client.
- Tolerante a falha: qualquer excecao vira 'indisponivel'/'erro' e o ciclo segue.
Campos gravados no prospect: instagram_leitura ('ok'|'sem_dados'|'indisponivel'|'erro'),
instagram_dados {seguidores, posts, ultimo_post, dias_sem_postar, link_externo, bio_contato},
instagram_lida_em.
"""
import json
import os
import random
import re
import time
from datetime import datetime, timezone

import requests

import store

ESTADO = os.path.join(store.DATA_DIR, 'ig_leituras.json')
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
      'Chrome/124.0 Safari/537.36')
REPETIR_APOS_DIAS = 7


def _estado():
    try:
        return json.load(open(ESTADO))
    except Exception:
        return {}


def _salvar(e):
    with open(ESTADO + '.tmp', 'w') as f:
        json.dump(e, f)
    os.replace(ESTADO + '.tmp', ESTADO)


def _hoje():
    return datetime.now().date().isoformat()


def leituras_hoje():
    e = _estado()
    return e.get('dia') == _hoje() and e.get('n', 0) or 0


def bloqueado_hoje():
    e = _estado()
    return e.get('dia') == _hoje() and bool(e.get('bloqueado'))


def _num(txt):
    """'1,234' '1.234' '12,5 mil' '1.2K' '3M' -> int."""
    t = txt.strip().lower().replace('\xa0', ' ')
    mult = 1
    m = re.match(r'([\d.,]+)\s*(mil|k|mi|m)?', t)
    if not m:
        return None
    n, suf = m.group(1), m.group(2)
    if suf in ('mil', 'k'):
        mult = 1000
    elif suf in ('mi', 'm'):
        mult = 1_000_000
    if mult > 1:
        n = n.replace(',', '.')
        try:
            return int(float(n) * mult)
        except ValueError:
            return None
    n = re.sub(r'[.,](?=\d{3}\b)', '', n)
    try:
        return int(float(n.replace(',', '.')))
    except ValueError:
        return None


def parse_pagina(html):
    """Extrai dados de og:description + JSON embutido. Retorna dict (pode vir vazio)."""
    d = {}
    m = re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)', html, re.I) or \
        re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:description', html, re.I)
    if m:
        desc = re.sub(r'&#x?\w+;|&\w+;', ' ', m.group(1))
        for chave, pat in (('seguidores', r'([\d.,]+\s*(?:mil|k|mi|m)?)\s*(?:seguidores|followers)'),
                           ('posts', r'([\d.,]+\s*(?:mil|k|mi|m)?)\s*(?:posts|publica\w+)')):
            x = re.search(pat, desc, re.I)
            if x:
                d[chave] = _num(x.group(1))
    ts = [int(x) for x in re.findall(r'"taken_at_timestamp"\s*:\s*(\d{9,11})', html)]
    ts += [int(x) for x in re.findall(r'"taken_at"\s*:\s*(\d{9,11})', html)]
    if ts:
        ult = max(ts)
        d['ultimo_post'] = datetime.fromtimestamp(ult, timezone.utc).date().isoformat()
        d['dias_sem_postar'] = (datetime.now(timezone.utc) - datetime.fromtimestamp(ult, timezone.utc)).days
    x = re.search(r'"external_url"\s*:\s*"([^"]+)"', html)
    if x:
        d['link_externo'] = x.group(1).replace('\\u0026', '&').replace('\\/', '/')
    bio = re.search(r'"biography"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
    texto_bio = bio.group(1) if bio else ''
    base = texto_bio + ' ' + (d.get('link_externo') or '')
    d['bio_contato'] = bool(re.search(r'wa\.me/|api\.whatsapp\.com|\(?\b\d{2}\)?\s?9?\d{4}[-\s]?\d{4}\b', base))
    return d


def ler_perfil(handle):
    """Retorna ('ok'|'sem_dados'|'indisponivel'|'erro', dados)."""
    try:
        r = requests.get(f'https://www.instagram.com/{handle}/', timeout=15, allow_redirects=False,
                         headers={'User-Agent': UA, 'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.8',
                                  'Accept': 'text/html,application/xhtml+xml'})
    except requests.RequestException as e:
        return 'erro', {'erro': type(e).__name__}
    if r.status_code in (301, 302, 303, 307, 308, 401, 403, 429):
        return 'indisponivel', {'http': r.status_code}
    if r.status_code == 404:
        return 'sem_dados', {'http': 404, 'perfil_inexistente': True}
    html = r.text[:800000]
    d = parse_pagina(html)
    if d.get('seguidores') is not None or d.get('posts') is not None or d.get('ultimo_post') or 'biography' in html:
        return 'ok', d
    # 200 sem dados do perfil = casca generica/pagina de login do Instagram (anonimo bloqueado)
    return 'indisponivel', {'http': r.status_code, 'motivo': 'sem dados publicos (login exigido)'}


def _candidatos(prospectos, apenas=None):
    lim = REPETIR_APOS_DIAS * 86400
    agora = time.time()
    saida = []
    for p in prospectos:
        h = (p.get('instagram_handle') or p.get('instagram') or '').strip().lstrip('@')
        if not h or (apenas and p['place_id'] not in apenas):
            continue
        lida = p.get('instagram_lida_em')
        if lida:
            try:
                if agora - datetime.fromisoformat(lida).timestamp() < lim:
                    continue
            except ValueError:
                pass
        saida.append(p['place_id'])
    return saida


def ler_lote(limite, apenas=None, log=print, esperar=True):
    """Le ate `limite` perfis (respeitando o teto do dia). Retorna resumo."""
    e = _estado()
    if e.get('dia') != _hoje():
        e = {'dia': _hoje(), 'n': 0, 'bloqueado': False}
    res = {'ok': 0, 'sem_dados': 0, 'indisponivel': 0, 'erro': 0, 'bloqueado': e.get('bloqueado', False)}
    if e['bloqueado']:
        log('Instagram: leituras bloqueadas hoje (indisponivel); pulando')
        return res
    pids = _candidatos(store.load_prospectos(), apenas)
    for pid in pids:
        if e['n'] >= limite:
            break
        p = next((x for x in store.load_prospectos() if x['place_id'] == pid), None)
        h = (p.get('instagram_handle') or p.get('instagram') or '').strip().lstrip('@')
        st, dados = ler_perfil(h)
        e['n'] += 1
        res[st] += 1

        def fn(L, pid=pid, st=st, dados=dados):
            for x in L:
                if x['place_id'] == pid:
                    x['instagram_leitura'] = st
                    x['instagram_dados'] = dados
                    x['instagram_lida_em'] = datetime.now().isoformat(timespec='seconds')
        store.mutate_prospectos(fn)
        log(f'  IG @{h}: {st}')
        if st == 'indisponivel':
            e['bloqueado'] = True
            res['bloqueado'] = True
            _salvar(e)
            log('Instagram: resposta de bloqueio/login; leituras do dia encerradas')
            return res
        _salvar(e)
        if esperar:
            time.sleep(random.uniform(8, 15))
    _salvar(e)
    return res
