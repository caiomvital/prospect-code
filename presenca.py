"""Presenca digital e oportunidade de um prospect.

avaliar(prospecto) -> dict com 4 blocos: google, site, instagram, whatsapp, mais os scores.

FORMULA (todos os componentes ficam em [0,1]; pesos em data/categorias.json, chave "pesos"
de cada categoria; se faltar, valem os PESOS_PADRAO abaixo):

  presenca_digital (0-100) = 100 * soma(peso_i * c_i) / soma(peso_i), i em
     site      (peso 40): 0 sem site proprio (rede social / site de terceiros = 0);
                          site proprio: 0.4 existe + 0.3 responde 200 <=8s + 0.15 meta viewport
                          + 0.15 tem wa.me/telefone. Se o site nao foi consultado, so conta "existe".
     instagram (peso 20): 1 se ha @ (do site ou digitado no painel), senao 0
     google    (peso 25): media de: telefone (1/0), reviews (min(n/50,1)), fotos (min(n/5,1)),
                          horario (1/0). Dado nao salvo (None) conta 0: sem dado, sem credito.
     whatsapp  (peso 15): 1 celular divulgado (9 apos o DDD), 0.4 so fixo, 0 sem telefone

  demanda (0-100) = 100 * (0.7 * min(log10(1+reviews)/log10(1+200), 1)
                           + 0.3 * clamp((nota-3.5)/1.5, 0, 1))        [sem reviews/nota salvos: 0]

  score_oportunidade (0-100) = peso_demanda * demanda + peso_ausencia * (100 - presenca_digital)
                               (padrao 0.6 e 0.4: demanda alta + presenca baixa)

Uso: avaliar(p) faz no maximo 1 requisicao HTTP (o site do prospect, se houver e for proprio).
avaliar(p, consultar_site=False) nao faz nenhuma. Nunca acessa Instagram nem a API do Google.
"""
import math
import re
import requests

import store

PESOS_PADRAO = {'site': 40, 'instagram': 20, 'google': 25, 'whatsapp': 15,
                'demanda': 0.6, 'ausencia': 0.4}
UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
      'Chrome/124.0 Safari/537.36')
_IG_IGNORAR = {'p', 'reel', 'reels', 'explore', 'accounts', 'stories', 'tv', 'about', 'share', 'direct'}
_RE_IG = re.compile(r'instagram\.com/([A-Za-z0-9_.]{2,30})', re.I)
_RE_FB = re.compile(r'(?:https?:)?//(?:www\.|m\.|pt-br\.)?facebook\.com/[^\s"\'<>)]+', re.I)
_RE_WA = re.compile(r'(?:wa\.me/|api\.whatsapp\.com/send\?phone=|whatsapp://send\?phone=)(\d{10,15})', re.I)
_RE_TEL = re.compile(r'href=["\']tel:', re.I)


def pesos_da(prospecto):
    p = dict(PESOS_PADRAO)
    for c in store.load_categorias():
        if c.get('keyword') == prospecto.get('categoria') or c.get('tipo') == prospecto.get('tipo'):
            p.update(c.get('pesos') or {})
            break
    return p


def _digitos(tel):
    d = re.sub(r'\D', '', tel or '')
    return d[2:] if d.startswith('55') and len(d) > 11 else d


def celular(tel):
    d = _digitos(tel)
    return len(d) == 11 and d[2] == '9'


def _classe_site(site):
    import campanhas                      # import tardio: campanhas importa este modulo
    return campanhas.classificar_presenca(site)


def handle_instagram(texto):
    for m in _RE_IG.finditer(texto or ''):
        h = m.group(1).rstrip('.')
        if h.lower() not in _IG_IGNORAR:
            return h
    return None


def _consultar_site(url):
    """1 requisicao. Retorna dict com ok/viewport/contato/links."""
    r = {'consultado': True, 'ok': False, 'status': None, 'viewport': False, 'contato': False,
         'instagram': [], 'facebook': [], 'whatsapp': [], 'erro': ''}
    if not re.match(r'https?://', url, re.I):
        url = 'http://' + url
    try:
        resp = requests.get(url, timeout=8, headers={'User-Agent': UA, 'Accept-Language': 'pt-BR,pt;q=0.9'},
                            allow_redirects=True)
    except requests.RequestException as e:
        r['erro'] = type(e).__name__
        return r
    r['status'] = resp.status_code
    r['ok'] = resp.status_code == 200
    if not r['ok']:
        return r
    html = resp.text[:600000]
    r['viewport'] = bool(re.search(r'<meta[^>]+name=["\']viewport["\']', html, re.I))
    r['instagram'] = sorted({h for m in _RE_IG.finditer(html)
                             if (h := m.group(1).rstrip('.')).lower() not in _IG_IGNORAR})[:5]
    r['facebook'] = sorted(set(_RE_FB.findall(html)))[:3]
    r['whatsapp'] = sorted(set(_RE_WA.findall(html)))[:3]
    r['contato'] = bool(r['whatsapp'] or _RE_TEL.search(html))
    return r


def avaliar(p, consultar_site=True):
    pesos = pesos_da(p)
    reviews = p.get('n_avaliacoes') or 0
    nota = p.get('rating')
    tel = p.get('telefone') or ''
    site_url = (p.get('site') or '').strip()
    classe = _classe_site(site_url)

    google = {'reviews': reviews or None, 'nota': nota, 'fotos': p.get('fotos'),
              'horario': p.get('horario'), 'site': bool(site_url), 'telefone': bool(tel)}

    site = {'url': site_url or None, 'tipo': classe, 'consultado': False, 'ok': None, 'viewport': None,
            'contato': None, 'instagram': [], 'facebook': [], 'whatsapp': []}
    if classe == 'site_proprio' and consultar_site:
        site.update(_consultar_site(site_url))
    elif classe == 'so_rede_social':
        h = handle_instagram(site_url)
        if h:
            site['instagram'] = [h]
        if 'facebook.com' in site_url.lower() or 'fb.com' in site_url.lower():
            site['facebook'] = [site_url]
        m = _RE_WA.search(site_url)
        if m:
            site['whatsapp'] = [m.group(1)]

    handle = (p.get('instagram_handle') or p.get('instagram') or '').lstrip('@').strip() or None
    origem = 'painel' if handle else None
    if not handle and site['instagram']:
        handle, origem = site['instagram'][0], 'site'
    instagram = {'handle': handle, 'origem': origem}

    cel = celular(tel)
    whatsapp = {'telefone': tel or None, 'celular': cel if tel else None}

    # ---- componentes [0,1]
    if classe == 'site_proprio':
        c_site = 0.4
        if site['consultado']:
            c_site += 0.3 * bool(site['ok']) + 0.15 * bool(site['viewport']) + 0.15 * bool(site['contato'])
    else:
        c_site = 0.0
    c_ig = 1.0 if handle else 0.0
    partes = [1.0 if tel else 0.0, min(reviews / 50, 1), min((p.get('fotos') or 0) / 5, 1),
              1.0 if p.get('horario') else 0.0]
    c_google = sum(partes) / len(partes)
    c_wa = 1.0 if cel else 0.4 if tel else 0.0
    soma = pesos['site'] + pesos['instagram'] + pesos['google'] + pesos['whatsapp']
    presenca = 100 * (pesos['site'] * c_site + pesos['instagram'] * c_ig +
                      pesos['google'] * c_google + pesos['whatsapp'] * c_wa) / soma

    d_rev = min(math.log10(1 + reviews) / math.log10(201), 1)
    d_nota = max(0.0, min(((nota or 0) - 3.5) / 1.5, 1)) if nota else 0.0
    demanda = 100 * (0.7 * d_rev + 0.3 * d_nota)
    oport = pesos['demanda'] * demanda + pesos['ausencia'] * (100 - presenca)
    return {'google': google, 'site': site, 'instagram': instagram, 'whatsapp': whatsapp,
            'score_presenca': round(presenca), 'demanda': round(demanda),
            'score_oportunidade': round(oport)}


def aplicar(p, consultar_site=True):
    """Grava presenca{}, score_oportunidade e instagram_handle no prospect (sem apagar nada)."""
    a = avaliar(p, consultar_site)
    p['presenca_digital'] = {k: a[k] for k in ('google', 'site', 'instagram', 'whatsapp',
                                              'score_presenca', 'demanda')}
    p['score_oportunidade'] = a['score_oportunidade']
    if a['instagram']['handle'] and not p.get('instagram_handle'):
        p['instagram_handle'] = a['instagram']['handle']
    if a['instagram']['handle'] and not p.get('instagram'):
        p['instagram'] = a['instagram']['handle']
    return a
