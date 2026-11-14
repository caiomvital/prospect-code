"""Campanhas de busca: grade de circulos, execucao em segundo plano, migracao."""
import math
import os
import queue
import threading
import time
import uuid
from datetime import datetime

import requests
from dotenv import load_dotenv

import store
import presenca as presenca_mod
from gerar_site import slugify

load_dotenv(os.path.join(store.BASE_DIR, '.env'))
API_KEY = os.getenv('GOOGLE_PLACES_API_KEY')
PLACES = 'https://maps.googleapis.com/maps/api/place/'

# Precos aproximados em USD por chamada (Places API legada). Ajustaveis por env.
PRECO_BUSCA = float(os.getenv('PRECO_NEARBY_USD', '0.032'))
PRECO_DETALHES = float(os.getenv('PRECO_DETAILS_USD', '0.003'))

RAIO_CELULA_KM = 5.0      # raio de cada circulo da grade (raios > 5 km)
MAX_PAGINAS = 3           # 20 resultados x 3 paginas = 60 por consulta
MAX_CIRCULOS = 400        # trava de seguranca

MSG_PADRAO = ('Ola {nome}! Vi que o seu negocio ainda nao tem um site. Criei uma versao '
              'gratuita para voce dar uma olhada: {url} - caso queira manter no ar e '
              'personalizar, fico a disposicao!')

_fila = queue.Queue()
_cancelar = set()
_worker_iniciado = False


# ---------------------------------------------------------------- geometria
def haversine_km(lat1, lng1, lat2, lng2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def gerar_grade(lat, lng, raio_km):
    """Retorna (lista de (lat, lng), raio_celula_km). Grade hexagonal sobreposta."""
    if raio_km <= RAIO_CELULA_KM:
        return [(lat, lng)], float(raio_km)
    r = RAIO_CELULA_KM
    passo = r * math.sqrt(3)          # empacotamento hexagonal cobre toda a area
    limite = raio_km + r * 0.5
    pts = []
    linha_passo = passo * math.sqrt(3) / 2     # = 1,5 * r
    linhas = int(limite / linha_passo) + 1
    for j in range(-linhas, linhas + 1):
        dy = j * linha_passo
        desloc = (passo / 2) if j % 2 else 0
        colunas = int(limite / passo) + 2
        for i in range(-colunas, colunas + 1):
            dx = i * passo + desloc
            if math.hypot(dx, dy) <= limite:
                dlat = dy / 111.32
                dlng = dx / (111.32 * math.cos(math.radians(lat)))
                pts.append((lat + dlat, lng + dlng))
    return pts, r


# ---------------------------------------------------------------- estimativa
def estimar(raio_km, n_categorias, limite=None):
    pts, r = gerar_grade(0.0, 0.0, raio_km)
    n = len(pts)
    consultas = n * n_categorias
    busca_min, busca_max = consultas, consultas * MAX_PAGINAS
    detalhes_max = min(limite, busca_max * 20) if limite else busca_max * 20
    if limite:
        busca_max = min(busca_max, max(consultas, int(limite / 20 * 2) + consultas))
    custo_min = busca_min * PRECO_BUSCA
    custo_max = busca_max * PRECO_BUSCA + detalhes_max * PRECO_DETALHES
    return {
        'circulos': n, 'raio_celula_km': r, 'consultas': consultas,
        'chamadas_busca_min': busca_min, 'chamadas_busca_max': busca_max,
        'chamadas_detalhes_max': detalhes_max,
        'custo_min_usd': round(custo_min, 2), 'custo_max_usd': round(custo_max, 2),
        'tempo_estimado_min': round((busca_max * 2.5) / 60, 1),
        'excede_limite': n > MAX_CIRCULOS,
    }


# ---------------------------------------------------------------- Google
def autocomplete_cidades(texto):
    r = requests.get(PLACES + 'autocomplete/json', params={
        'input': texto, 'types': '(cities)', 'components': 'country:br',
        'language': 'pt-BR', 'key': API_KEY}, timeout=15).json()
    if r.get('status') not in ('OK', 'ZERO_RESULTS'):
        raise RuntimeError(f"Autocomplete: {r.get('status')}")
    return [{'place_id': p['place_id'], 'descricao': p['description']}
            for p in r.get('predictions', [])]


def coordenadas_cidade(place_id):
    r = requests.get(PLACES + 'details/json', params={
        'place_id': place_id, 'fields': 'geometry,name,formatted_address',
        'language': 'pt-BR', 'key': API_KEY}, timeout=15).json()
    if r.get('status') != 'OK':
        raise RuntimeError(f"Details: {r.get('status')}")
    res = r['result']
    loc = res['geometry']['location']
    return {'lat': loc['lat'], 'lng': loc['lng'], 'nome': res.get('name', ''),
            'endereco': res.get('formatted_address', '')}


def _nearby(lat, lng, raio_m, keyword, token=None):
    params = {'key': API_KEY, 'language': 'pt-BR'}
    if token:
        params['pagetoken'] = token
    else:
        params.update({'location': f'{lat},{lng}', 'radius': int(raio_m), 'keyword': keyword})
    return requests.get(PLACES + 'nearbysearch/json', params=params, timeout=20).json()


def _detalhes(place_id):
    r = requests.get(PLACES + 'details/json', params={
        'place_id': place_id, 'language': 'pt-BR', 'key': API_KEY,
        'fields': 'formatted_phone_number,website,url,formatted_address,photo,opening_hours'}, timeout=20).json()
    return r.get('status'), r.get('result', {})


REDES_SOCIAIS = ['facebook.com', 'fb.com', 'fb.me', 'instagram.com', 'wa.me', 'whatsapp.com',
                 'linktr.ee', 'linktree.com', 'beacons.ai', 'taplink.cc', 'taplink.at', 'bio.link',
                 'tiktok.com', 'youtube.com', 'youtu.be', 'twitter.com', 'x.com', 'linkin.bio',
                 'lnk.bio', 'campsite.bio']
# construtores gratuitos, paginas de delivery/marketplace e fichas de terceiros:
# a empresa nao tem um site proprio de verdade
SITES_FRACOS = ['wixsite.com', 'wix.com', 'blogspot.com', 'sites.google.com', 'business.site',
                'weebly.com', 'webnode', 'yolasite', 'jimdofree.com', 'ifood.com', 'rappi.com',
                'goomer', 'menudino', 'anota.ai', 'cardapioweb', 'olaclick', 'booksy.com',
                'cardapio.', 'mercadolivre.com', 'olx.com', 'tripadvisor', 'guiamais', 'apontador',
                'telelistas', 'wordpress.com', 'mystrikingly.com', 'carrd.co', 'godaddysites.com',
                'negocio.site', 'my.canva.site', 'canva.site', 'notion.site']
PRESENCAS = ['sem_presenca', 'so_rede_social', 'site_fraco']   # alvos de prospeccao
PRESENCA_ROTULO = {'sem_presenca': 'Sem nada', 'so_rede_social': 'So rede social',
                   'site_fraco': 'Site fraco', 'site_proprio': 'Site proprio'}


def _host(site):
    from urllib.parse import urlparse
    u = site.strip().lower()
    if '//' not in u:
        u = '//' + u
    h = (urlparse(u).hostname or '')
    return h[4:] if h.startswith('www.') else h


def classificar_presenca(site):
    """sem_presenca | so_rede_social | site_fraco | site_proprio (pelo campo site da ficha)."""
    if not site or not site.strip():
        return 'sem_presenca'
    h = _host(site)
    if any(h == d or h.endswith('.' + d) for d in REDES_SOCIAIS):
        return 'so_rede_social'
    if any(x in h for x in SITES_FRACOS):
        return 'site_fraco'
    return 'site_proprio'


def tem_site_ruim(site):
    return classificar_presenca(site) != 'site_proprio'


def extrair_instagram(site):
    """'https://instagram.com/foo/?x' -> 'foo' (so se o site da ficha for um perfil)."""
    import re as _re
    m = _re.search(r'instagram\.com/([A-Za-z0-9_.]+)', site or '')
    if m and m.group(1).lower() not in ('p', 'reel', 'explore', 'accounts', 'stories'):
        return m.group(1)
    return ''


def calcular_prioridade(presenca, nota, n_av):
    """alta | media | baixa: quem tem pouca presenca mas muita demanda (avaliacoes)."""
    pts = {'sem_presenca': 3, 'site_fraco': 2, 'so_rede_social': 1}.get(presenca, 0)
    n_av = n_av or 0
    pts += 3 if n_av >= 100 else 2 if n_av >= 30 else 1 if n_av >= 10 else 0
    pts += 1 if (nota or 0) >= 4.3 else 0
    return 'alta' if pts >= 6 else 'media' if pts >= 4 else 'baixa'


# ---------------------------------------------------------------- campanhas
def categorias_por_key():
    return {c['key']: c for c in store.load_categorias()}


def criar_campanha(dados):
    cats = categorias_por_key()
    escolhidas = [k for k in dados.get('categorias', []) if k in cats]
    if not escolhidas:
        raise ValueError('Escolha ao menos uma categoria')
    raio = float(dados['raio_km'])
    if not 1 <= raio <= 50:
        raise ValueError('Raio deve estar entre 1 e 50 km')
    est = estimar(raio, len(escolhidas), dados.get('limite_resultados') or None)
    if est['excede_limite']:
        raise ValueError('Area grande demais (mais de %d circulos)' % MAX_CIRCULOS)
    f = dados.get('filtros', {})
    lim = f.get('limite_resultados')
    camp = {
        'id': uuid.uuid4().hex[:10],
        'cidade': dados['cidade'].strip(),
        'lat': float(dados['lat']), 'lng': float(dados['lng']),
        'raio_km': raio,
        'categorias': escolhidas,
        'filtros': {
            'sem_site': bool(f.get('sem_site', True)),
            'presenca': [x for x in f.get('presenca', PRESENCAS) if x in PRESENCAS] or list(PRESENCAS),
            'nota_min': float(f.get('nota_min') or 0),
            'min_avaliacoes': int(f.get('min_avaliacoes') or 0),
            'limite_resultados': int(lim) if lim else None,
        },
        'mensagem_whatsapp': (dados.get('mensagem_whatsapp') or MSG_PADRAO).strip(),
        'status': 'pendente',
        'criada_em': datetime.now().isoformat(timespec='seconds'),
        'estimativa': est,
        'progresso': {'etapa': 'na fila', 'feito': 0, 'total': est['consultas'],
                      'chamadas_busca': 0, 'chamadas_detalhes': 0,
                      'candidatos': 0, 'novos': 0, 'ja_existiam': 0},
        'erro': '',
    }
    store.mutate_campanhas(lambda cs: cs.append(camp))
    return camp


def obter(cid):
    for c in store.load_campanhas():
        if c['id'] == cid:
            return c
    return None


def _atualizar(cid, **campos):
    def fn(cs):
        for c in cs:
            if c['id'] == cid:
                prog = campos.pop('progresso', None)
                c.update(campos)
                if prog:
                    c['progresso'].update(prog)
    store.mutate_campanhas(fn)


def enfileirar(cid):
    iniciar_worker()
    _cancelar.discard(cid)
    _atualizar(cid, status='pendente', erro='', progresso={'etapa': 'na fila'})
    _fila.put(cid)


def cancelar(cid):
    _cancelar.add(cid)


class _Cancelado(Exception):
    pass


def _checar(cid):
    if cid in _cancelar:
        raise _Cancelado()


def executar(cid, parar=None):
    """parar(prog) -> True interrompe a etapa de detalhes (teto de custo do ciclo); o que ja foi
    salvo fica, e o restante entra na proxima execucao."""
    camp = obter(cid)
    if not camp:
        return
    f = camp['filtros']
    limite = f.get('limite_resultados')
    cats = categorias_por_key()
    pts, raio_cel = gerar_grade(camp['lat'], camp['lng'], camp['raio_km'])
    consultas = [(p, cats[k]) for p in pts for k in camp['categorias'] if k in cats]
    prog = {'etapa': 'buscando', 'feito': 0, 'total': len(consultas),
            'chamadas_busca': 0, 'chamadas_detalhes': 0, 'candidatos': 0,
            'novos': 0, 'ja_existiam': 0}
    _atualizar(cid, status='rodando', progresso=prog)
    candidatos = {}   # place_id -> dict
    try:
        for idx, ((lat, lng), cat) in enumerate(consultas, 1):
            token = None
            for pagina in range(MAX_PAGINAS):
                _checar(cid)
                if token:
                    time.sleep(2)   # o token da proxima pagina leva ~2s para valer
                for tentativa in range(3):          # erros transitorios do Google: repete com espera
                    data = _nearby(lat, lng, raio_cel * 1000, cat['keyword'], token)
                    prog['chamadas_busca'] += 1
                    if data.get('status') not in ('INTERNAL', 'UNKNOWN_ERROR'):
                        break
                    time.sleep(3 * (tentativa + 1))
                if data.get('status') in ('INTERNAL', 'UNKNOWN_ERROR'):
                    prog['consultas_falhas'] = prog.get('consultas_falhas', 0) + 1   # pula so esta consulta
                    break
                if data.get('status') not in ('OK', 'ZERO_RESULTS'):
                    raise RuntimeError(f"Nearby Search: {data.get('status')} {data.get('error_message', '')}")
                for pl in data.get('results', []):
                    pid = pl.get('place_id')
                    if not pid or pid in candidatos or pl.get('business_status') == 'CLOSED_PERMANENTLY':
                        continue
                    loc = pl.get('geometry', {}).get('location', {})
                    if not loc:
                        continue
                    if haversine_km(camp['lat'], camp['lng'], loc['lat'], loc['lng']) > camp['raio_km']:
                        continue
                    nota, nav = pl.get('rating'), pl.get('user_ratings_total') or 0
                    if f['nota_min'] and (nota is None or nota < f['nota_min']):
                        continue
                    if f['min_avaliacoes'] and nav < f['min_avaliacoes']:
                        continue
                    candidatos[pid] = {
                        'place_id': pid, 'nome': pl.get('name'),
                        'endereco': pl.get('vicinity', ''), 'categoria': cat['keyword'],
                        'tipo': cat['tipo'], 'rating': nota, 'n_avaliacoes': nav,
                        'lat': loc['lat'], 'lng': loc['lng']}
                token = data.get('next_page_token')
                prog.update(candidatos=len(candidatos))
                _atualizar(cid, progresso=prog)
                if limite and len(candidatos) >= limite:
                    break
                if not token:
                    break
            prog['feito'] = idx
            _atualizar(cid, progresso=prog)
            if limite and len(candidatos) >= limite:
                break

        lista = list(candidatos.values())
        if limite:
            lista = lista[:limite]
        existentes = {p['place_id'] for p in store.load_prospectos()}
        novos_cand = [c for c in lista if c['place_id'] not in existentes]
        prog.update(etapa='detalhes', feito=0, total=len(novos_cand),
                    ja_existiam=len(lista) - len(novos_cand), candidatos=len(lista))
        _atualizar(cid, progresso=prog)

        parcial = False
        for i, emp in enumerate(novos_cand, 1):
            _checar(cid)
            if parar and parar(prog):
                parcial = True
                break
            st, det = _detalhes(emp['place_id'])
            prog['chamadas_detalhes'] += 1
            if st == 'OK':
                emp.update(telefone=det.get('formatted_phone_number', ''),
                           site=det.get('website', ''), maps_url=det.get('url', ''))
                if det.get('formatted_address'):
                    emp['endereco'] = det['formatted_address']
                emp['fotos'] = len(det.get('photos') or [])
                emp['horario'] = bool(det.get('opening_hours'))
                pres = classificar_presenca(emp['site'])
                aceita = (pres in f['presenca']) if f.get('presenca') else (
                    not f['sem_site'] or pres != 'site_proprio')
                if aceita:
                    emp.update(presenca=pres, instagram=extrair_instagram(emp['site']),
                               prioridade=calcular_prioridade(pres, emp.get('rating'),
                                                              emp.get('n_avaliacoes')))
                    try:
                        presenca_mod.aplicar(emp)
                    except Exception:
                        pass
                    emp.update(status='novo', anotacao='', site_gerado='',
                               data_criacao=datetime.now().isoformat(),
                               campanha_id=cid, cidade=camp['cidade'],
                               raio_km=camp['raio_km'])
                    def add(ps, e=emp):
                        if all(p['place_id'] != e['place_id'] for p in ps):
                            ps.append(e)
                            return 1
                        return 0
                    prog['novos'] += store.mutate_prospectos(add)
            prog['feito'] = i
            _atualizar(cid, progresso=prog)
            time.sleep(0.1)

        prog['etapa'] = 'parcial (teto de custo)' if parcial else 'concluida'
        prog['parcial'] = parcial
        _atualizar(cid, status='concluida', progresso=prog)
    except _Cancelado:
        prog['etapa'] = 'cancelada'
        _atualizar(cid, status='cancelada', progresso=prog)
    except Exception as e:  # noqa
        prog['etapa'] = 'erro'
        _atualizar(cid, status='erro', erro=str(e)[:300], progresso=prog)


def _loop():
    while True:
        cid = _fila.get()
        try:
            executar(cid)
        finally:
            _cancelar.discard(cid)
            _fila.task_done()


def iniciar_worker():
    global _worker_iniciado
    if not _worker_iniciado:
        _worker_iniciado = True
        threading.Thread(target=_loop, daemon=True, name='campanhas-worker').start()


# ---------------------------------------------------------------- lista manual (Instagram)
MAX_LINHAS_LISTA = 200


def _parse_linha(linha):
    """'@perfil; Nome; telefone' | 'https://instagram.com/perfil' -> (handle, nome, tel) ou None."""
    import re as _re
    partes = [x.strip() for x in _re.split(r'[;|\t]', linha.strip())]
    bruto = partes[0]
    m = _re.search(r'instagram\.com/([A-Za-z0-9_.]+)', bruto) or _re.match(r'^@?([A-Za-z0-9_.]{1,30})$', bruto)
    if not m:
        return None
    handle = m.group(1).rstrip('.')
    if not handle or handle.lower() in ('p', 'reel', 'explore', 'accounts', 'stories'):
        return None
    nome = partes[1] if len(partes) > 1 and partes[1] else ''
    tel = partes[2] if len(partes) > 2 else ''
    return handle, nome, tel


def importar_lista(texto, cidade, categoria_key, mensagem=''):
    """Cria prospects a partir de uma lista manual de perfis do Instagram.
    Nao acessa o Instagram nem a API do Google. Retorna um resumo."""
    cats = categorias_por_key()
    if categoria_key not in cats:
        raise ValueError('Escolha uma categoria')
    cidade = (cidade or '').strip()
    if not cidade:
        raise ValueError('Informe a cidade')
    linhas = [l for l in (texto or '').splitlines() if l.strip() and not l.strip().startswith('#')]
    if not linhas:
        raise ValueError('Cole ao menos um perfil')
    if len(linhas) > MAX_LINHAS_LISTA:
        raise ValueError(f'Maximo de {MAX_LINHAS_LISTA} linhas por importacao')
    cat = cats[categoria_key]
    cid = uuid.uuid4().hex[:10]
    invalidas, vistos, novos_p = [], set(), []
    for l in linhas:
        r = _parse_linha(l)
        if not r:
            invalidas.append(l.strip()[:60])
            continue
        handle, nome, tel = r
        if handle.lower() in vistos:
            continue
        vistos.add(handle.lower())
        pres = 'so_rede_social'
        novos_p.append({
            'place_id': 'ig_' + handle.lower(), 'nome': nome or handle, 'endereco': '',
            'categoria': cat['keyword'], 'tipo': cat['tipo'], 'telefone': tel, 'site': f'https://instagram.com/{handle}',
            'maps_url': '', 'status': 'novo', 'anotacao': '', 'site_gerado': '',
            'data_criacao': datetime.now().isoformat(), 'rating': None, 'n_avaliacoes': 0,
            'campanha_id': cid, 'cidade': cidade, 'raio_km': None, 'presenca': pres,
            'instagram': handle, 'origem': 'instagram_manual',
            'prioridade': calcular_prioridade(pres, None, 0)})
    for _p in novos_p:
        presenca_mod.aplicar(_p, consultar_site=False)
    resumo = {'campanha_id': cid, 'recebidas': len(linhas), 'invalidas': invalidas,
              'novos': 0, 'ja_existiam': 0}

    def fn(ps):
        ids = {p['place_id'] for p in ps}
        igs = {(p.get('instagram') or '').lower() for p in ps if p.get('instagram')}
        for p in novos_p:
            if p['place_id'] in ids or p['instagram'].lower() in igs:
                resumo['ja_existiam'] += 1
            else:
                ps.append(p)
                resumo['novos'] += 1
    store.mutate_prospectos(fn)

    def fc(cs):
        cs.append({'id': cid, 'cidade': cidade, 'lat': None, 'lng': None, 'raio_km': None,
                   'categorias': [categoria_key],
                   'filtros': {'sem_site': True, 'nota_min': 0, 'min_avaliacoes': 0,
                               'limite_resultados': None, 'origem': 'instagram_manual'},
                   'mensagem_whatsapp': (mensagem or MSG_PADRAO).strip(), 'status': 'concluida',
                   'criada_em': datetime.now().isoformat(timespec='seconds'),
                   'progresso': {'etapa': 'concluida', 'feito': resumo['novos'], 'total': len(linhas),
                                 'candidatos': len(linhas), 'novos': resumo['novos'],
                                 'ja_existiam': resumo['ja_existiam'], 'chamadas_busca': 0,
                                 'chamadas_detalhes': 0}, 'erro': ''})
    if resumo['novos']:
        store.mutate_campanhas(fc)
    return resumo


# ---------------------------------------------------------------- migracao
def migrar():
    """Idempotente: marca prospects antigos como campanha 'legado' / Olinda e
    interrompe campanhas que estavam rodando antes de um reinicio."""
    def fn_p(ps):
        n = 0
        for p in ps:
            if 'campanha_id' not in p:
                p['campanha_id'] = 'legado'
                p.setdefault('cidade', 'Olinda')
                p.setdefault('raio_km', None)
                n += 1
        return n
    alterados = store.mutate_prospectos(fn_p)

    def fn_pres(ps):   # preenche presenca/prioridade/instagram nos prospects antigos
        n = 0
        for p in ps:
            if 'presenca' not in p:
                p['presenca'] = classificar_presenca(p.get('site', ''))
                p['instagram'] = extrair_instagram(p.get('site', ''))
                p['prioridade'] = calcular_prioridade(p['presenca'], p.get('rating'),
                                                      p.get('n_avaliacoes'))
                n += 1
        return n
    store.mutate_prospectos(fn_pres)

    def fn_c(cs):
        if not any(c['id'] == 'legado' for c in cs):
            cs.insert(0, {'id': 'legado', 'cidade': 'Olinda', 'lat': -8.0089, 'lng': -34.8553,
                          'raio_km': None, 'categorias': [c['key'] for c in store.load_categorias()],
                          'filtros': {'sem_site': True, 'nota_min': 0, 'min_avaliacoes': 0,
                                      'limite_resultados': None},
                          'mensagem_whatsapp': MSG_PADRAO, 'status': 'concluida',
                          'criada_em': '2026-06-18T12:31:00',
                          'progresso': {'etapa': 'concluida'}, 'erro': ''})
        for c in cs:
            if c['status'] in ('rodando', 'pendente'):
                c['status'] = 'interrompida'
                c['progresso']['etapa'] = 'interrompida'
    store.mutate_campanhas(fn_c)
    return alterados
