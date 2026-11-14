"""Cliente da Instagram Graph API (conta @negocioemfluxo).
Usa so IG_ACCESS_TOKEN / IG_USER_ID / IG_GRAPH_VERSION do .env. O token nunca e logado nem
aparece em mensagens de erro. Alem do perfil proprio, business_discovery() le dados PUBLICOS de
perfis profissionais (Business/Creator) de terceiros pela API oficial, em nome da conta
(autorizado pelo dono em 15/11/2026; antes a regra era so conta propria)."""
import os
import requests
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, '.env'))


class IGErro(Exception):
    pass


def _cfg():
    t, u = os.getenv('IG_ACCESS_TOKEN', ''), os.getenv('IG_USER_ID', '')
    if not t or not u:
        raise IGErro('IG_ACCESS_TOKEN/IG_USER_ID nao configurados')
    return t, u, os.getenv('IG_GRAPH_VERSION', 'v21.0')


def _get(caminho, params=None):
    t, u, v = _cfg()
    try:
        r = requests.get(f'https://graph.facebook.com/{v}/{caminho.format(uid=u)}', params=params or {},
                         headers={'Authorization': 'Bearer ' + t}, timeout=20)
    except requests.RequestException as e:          # a mensagem de requests pode conter a URL
        raise IGErro('falha de rede: ' + type(e).__name__)
    try:
        d = r.json()
    except ValueError:
        raise IGErro(f'resposta invalida (HTTP {r.status_code})')
    if 'error' in d:
        e = d['error']
        raise IGErro(f"{e.get('type', 'erro')} {e.get('code', '')}: {str(e.get('message', ''))[:160]}")
    return d


def get_profile():
    return _get('{uid}', {'fields': 'username,name,media_count,followers_count,follows_count,biography'})


def list_media(limit=12):
    d = _get('{uid}/media', {'fields': 'id,caption,media_type,permalink,timestamp,like_count,comments_count',
                             'limit': max(1, min(int(limit), 50))})
    return d.get('data', [])


def check_token():
    """{'ok': bool, 'username': ..., 'erro': ...} sem expor o token."""
    try:
        d = _get('{uid}', {'fields': 'username,media_count'})
        return {'ok': True, 'username': d.get('username'), 'media_count': d.get('media_count')}
    except IGErro as e:
        return {'ok': False, 'erro': str(e)}


class IGNaoProfissional(IGErro):
    """Perfil inexistente ou pessoal: a API so enxerga contas Business/Creator."""


def business_discovery(handle):
    """Dados publicos de um perfil profissional de terceiros, via API oficial."""
    campos = ('business_discovery.username(%s){username,followers_count,media_count,biography,website,'
              'media.limit(6){timestamp}}' % handle)
    try:
        return _get('{uid}', {'fields': campos}).get('business_discovery') or {}
    except IGErro as e:
        m = str(e)
        if 'Cannot find' in m or 'business' in m.lower() and 'not' in m.lower() or ' 110:' in m or ' 24:' in m \
                or 'Invalid user id' in m or 'does not exist' in m.lower():
            raise IGNaoProfissional(m)
        raise
