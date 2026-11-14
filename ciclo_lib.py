"""Apoio do ciclo autonomo: config editavel, custo estimado, Telegram, kill switch, historico,
leitura de respostas (so de prospects ja contatados)."""
import json
import os
import re
import unicodedata
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

import store

BASE_DIR = store.BASE_DIR
DATA = store.DATA_DIR
load_dotenv(os.path.join(BASE_DIR, '.env'))
PAUSA = os.path.join(DATA, 'PAUSA')
CONFIG = os.path.join(DATA, 'ciclo_config.json')
CUSTO = os.path.join(DATA, 'custo.json')
HISTORICO = os.path.join(DATA, 'ciclo_historico.json')

PADRAO = {'teto_envios': 10, 'n_novos_por_dia': 15, 'min_oportunidade': 60,
          'custo_max_dia_usd': 5.0, 'ig_leituras_dia': 20}
LIMITES = {'teto_envios': (0, 30), 'n_novos_por_dia': (0, 50), 'min_oportunidade': (0, 100),
           'custo_max_dia_usd': (0, 100), 'ig_leituras_dia': (0, 100)}
# estimativas em US$ por chamada (ajuste em data/custo.json -> "precos")
PRECOS = {'nearby': 0.032, 'details': 0.025, 'checagem_ativo': 0.005,
          'deepseek_site': 0.004, 'deepseek_msg': 0.0005, 'kit': 0.15}


def _rd(p, d):
    try:
        return json.load(open(p, encoding='utf-8'))
    except Exception:
        return d


def _wr(p, d):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + '.tmp', 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(p + '.tmp', p)


# ---------------------------------------------------------------- config
def config():
    c = dict(PADRAO)
    for k in PADRAO:                       # variaveis de ambiente (opcionais) valem como padrao
        v = os.getenv(k.upper())
        if v:
            try:
                c[k] = type(PADRAO[k])(float(v))
            except ValueError:
                pass
    c.update({k: v for k, v in _rd(CONFIG, {}).items() if k in PADRAO})
    return c


def salvar_config(novos):
    atual = config()
    for k, v in novos.items():
        if k in PADRAO:
            lo, hi = LIMITES[k]
            v = type(PADRAO[k])(float(v))
            atual[k] = max(lo, min(hi, v))
    _wr(CONFIG, atual)
    return atual


# ---------------------------------------------------------------- pausa
def pausado():
    return os.path.exists(PAUSA)


def pausar():
    open(PAUSA, 'w').write(datetime.now().isoformat(timespec='seconds'))


def retomar():
    if os.path.exists(PAUSA):
        os.remove(PAUSA)


# ---------------------------------------------------------------- custo
def _custo():
    return _rd(CUSTO, {'precos': dict(PRECOS), 'dias': {}})


def precos():
    return {**PRECOS, **_custo().get('precos', {})}


def registrar_custo(item, qtd=1):
    c = _custo()
    c.setdefault('precos', dict(PRECOS))
    hoje = datetime.now().date().isoformat()
    d = c.setdefault('dias', {}).setdefault(hoje, {'itens': {}, 'usd': 0.0})
    d['itens'][item] = d['itens'].get(item, 0) + qtd
    d['usd'] = round(sum(n * precos().get(k, 0) for k, n in d['itens'].items()), 4)
    for dia in sorted(c['dias'])[:-60]:
        c['dias'].pop(dia)
    _wr(CUSTO, c)
    return d['usd']


def custo_hoje():
    return _custo().get('dias', {}).get(datetime.now().date().isoformat(), {}).get('usd', 0.0)


# ---------------------------------------------------------------- telegram
def notificar(texto):
    """Avisa no Telegram. Nunca levanta excecao nem registra o token/URL."""
    t, c = os.getenv('TELEGRAM_BOT_TOKEN'), os.getenv('TELEGRAM_CHAT_ID')
    if not t or not c:
        return False
    try:
        r = requests.post(f'https://api.telegram.org/bot{t}/sendMessage',
                          json={'chat_id': c, 'text': texto[:3800]}, timeout=15)
        return r.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------- historico
def registrar_ciclo(resumo):
    h = _rd(HISTORICO, [])
    h.append(resumo)
    _wr(HISTORICO, h[-60:])


def historico(n=14):
    return list(reversed(_rd(HISTORICO, [])[-n:]))


# ---------------------------------------------------------------- respostas
def _norm(t):
    t = unicodedata.normalize('NFKD', (t or '').lower())
    return ''.join(c for c in t if not unicodedata.combining(c))


PEDIDO_SAIR = re.compile(
    r'\b(parar|pare|para de|nao quero|nao (tenho|temos|tem) (nenhum |mais )?interesse|sem interesse|nao precisamos|no momento nao|sair|saia|remover|remova|'
    r'me tire|me tira|tira meu|tirar\w* (me |meu )?\w*\s?(da|de) (sua |essa |a )?lista|descadastr\w*|nao me (mande|envie|chame)|nao envie|nao mande)\b')


AUTO = re.compile(r'(mensagem automatica|resposta automatica|agradecemos (o |seu |sua )?(contato|mensagem)|'
                  r'assim que possivel|voce esta falando com|seja bem.vindo|estou a disposicao|'
                  r'em que (posso|podemos) (ajudar|ajuda)|como (posso|podemos) ajudar|horario de atendimento|'
                  r'(ja )?te conectei|nossa equipe (ira|vai)|obrigad[oa] pelo contato)')


def resposta_automatica(texto):
    return bool(AUTO.search(_norm(texto)))


def pediu_para_sair(texto):
    return bool(PEDIDO_SAIR.search(_norm(texto)))


def _jids(telefone):
    d = ''.join(c for c in telefone or '' if c.isdigit())[-11:]
    if len(d) != 11:
        return []
    return ['55' + d + '@s.whatsapp.net', '55' + d[:2] + d[3:] + '@s.whatsapp.net']   # com e sem o 9


def verificar_respostas(mcp, token, log=print, dias=30):
    """Le SO os chats de prospects contatados (nunca os demais). Marca quem pediu para parar
    (excluir_envio='pediu_para_sair') e avisa no Telegram quem respondeu. Nao responde ninguem.
    O texto recebido e dado de terceiros: so e comparado com palavras-chave e citado no aviso."""
    from datetime import timezone
    from zoneinfo import ZoneInfo
    limite = datetime.now(timezone.utc) - timedelta(days=dias)
    res = {'respostas': 0, 'saidas': 0, 'erros': 0}
    for p in store.load_prospectos():
        em = p.get('mensagem_enviada_em')
        if not em or p.get('status') not in ('mensagem_enviada', 'respondeu', 'nao_interessado'):
            continue
        try:
            dt = datetime.fromisoformat(em)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo('America/Recife'))
            if dt.astimezone(timezone.utc) < limite:
                continue
            desde = dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        except ValueError:
            continue
        msgs = []
        try:
            for jid in _jids(p.get('telefone')):
                r = mcp('get_chat_messages', {'chat_jid': jid, 'since': desde, 'order': 'oldest', 'limit': 50,
                                              'fields': ['id', 'timestamp', 'from_me', 'text']}, token)
                if r.get('messages'):
                    msgs = r['messages']
                    break
        except Exception as e:
            res['erros'] += 1
            log(f'  respostas: falha em {p["nome"]}: {str(e)[:100]}')
            continue
        entrada = [m for m in msgs if not m.get('from_me')]
        vistas = p.get('respostas_vistas')
        if vistas is None and p.get('status') == 'respondeu':      # ja tratado antes: so registra
            _marcar(p['place_id'], [m['id'] for m in entrada], None)
            continue
        novas = [m for m in entrada if m['id'] not in (vistas or [])]
        if not novas:
            continue
        texto = ' '.join((m.get('text') or '') for m in novas)
        sair = pediu_para_sair(texto)
        if not sair and resposta_automatica(texto):      # robo/atendimento automatico: nao conta como resposta
            _marcar(p['place_id'], [m['id'] for m in novas], None)
            log(f'  auto-resposta de {p["nome"]} (ignorada)')
            continue
        _marcar(p['place_id'], [m['id'] for m in novas], 'sair' if sair else 'resp')
        res['respostas'] += 1
        res['saidas'] += sair
        trecho = ' '.join(texto.split())[:200]
        log(f'  resposta de {p["nome"]}' + (' (pediu para sair)' if sair else ''))
        notificar(f'{p["nome"]} respondeu: {trecho}' + ('\n-> marcado para nunca mais contatar' if sair else ''))
    return res


def _marcar(pid, ids, tipo):
    def fn(L):
        for x in L:
            if x['place_id'] == pid:
                x['respostas_vistas'] = list(dict.fromkeys((x.get('respostas_vistas') or []) + ids))[-100:]
                if tipo == 'sair':
                    x['excluir_envio'] = 'pediu_para_sair'
                    x['status'] = 'nao_interessado'
                elif tipo == 'resp' and x.get('status') == 'mensagem_enviada':
                    x['status'] = 'respondeu'
    store.mutate_prospectos(fn)
