"""Enviador de 1a mensagem via WhatsApp MCP, com ritmo de uso humano e baixo volume.

- Le a fila em data/fila_envio.json: [{"nome": "...", "texto": "... {url} ..."}]
  (os textos sao escritos e aprovados pelo usuario, um por prospect).
- Antes de cada envio: site no ar, negocio ativo no Google, numero com WhatsApp,
  prospect ainda com status 'site_gerado' (nunca manda duas vezes).
- Ritmo: janela de horario comercial (America/Recife), teto diario, intervalos
  de duracao variavel (log-normal), pausas mais longas de vez em quando, e
  'digitando...' com duracao proporcional ao tamanho do texto.
- Nao responde conversas. Para ao atingir o teto ou o fim da janela; o que sobrar
  continua na fila para o dia seguinte.

Uso: venv/bin/python enviador.py [--simular] [--teto 10] [--inicio 8.5] [--fim 18.5]
"""
import argparse
import json
import math
import os
import random
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, '/root/prospeccao')
import store
from pre_envio import verificar_site, negocio_ativo, is_celular

TZ = ZoneInfo('America/Recife')
FILA = '/root/prospeccao/data/fila_envio.json'


def agora():
    return datetime.now(TZ)


def log(*a):
    print(agora().strftime('%H:%M:%S'), *a, flush=True)


# ---------------------------------------------------------------- ritmo
def intervalo_s():
    """Mediana ~6 min; cauda longa; limitado entre 2 e 22 min."""
    return max(120, min(22 * 60, random.lognormvariate(math.log(360), 0.55)))


def pausa_longa_s():
    return random.randint(15 * 60, 35 * 60)


def duracao_digitando_s(texto):
    """Proporcional ao tamanho: ~5 a 8 caracteres/s, entre 15 e 70 s."""
    return max(15, min(70, len(texto) / random.uniform(5.0, 8.0)))


# ---------------------------------------------------------------- MCP
def _token():
    return subprocess.run(['/root/.local/bin/whatsapp-mcp', 'token', 'show'], capture_output=True, text=True,
                          env={**os.environ, 'XDG_RUNTIME_DIR': '/run/user/0', 'HOME': '/root'}).stdout.strip()


def mcp(tool, args, token):
    req = urllib.request.Request(
        'http://127.0.0.1:47821/mcp', method='POST',
        data=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                         'params': {'name': tool, 'arguments': args}}).encode(),
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
                 'Accept': 'application/json, text/event-stream'})
    for l in urllib.request.urlopen(req, timeout=60).read().decode().splitlines():
        l = l[6:] if l.startswith('data: ') else l
        try:
            d = json.loads(l)
        except ValueError:
            continue
        if 'error' in d:
            raise RuntimeError(str(d['error'])[:200])
        r = d['result']
        if r.get('isError'):
            raise RuntimeError(r['content'][0]['text'][:200])
        t = r['content'][0]['text']
        return json.loads(t) if t.lstrip()[:1] in '{[' else t
    raise RuntimeError('sem resposta do MCP')


def digitar(num, texto, token, simular):
    total = duracao_digitando_s(texto)
    log(f'   digitando por ~{total:.0f}s')
    if simular:
        return
    time.sleep(random.uniform(2, 6))                 # "lendo" antes de comecar
    t0, pausou = time.time(), False
    while time.time() - t0 < total:
        mcp('send_typing', {'to': num, 'typing': True}, token)
        time.sleep(4)
        if not pausou and time.time() - t0 > total * 0.5 and random.random() < 0.6:
            pausou = True                            # hesita no meio, como quem revisa
            mcp('send_typing', {'to': num, 'typing': False}, token)
            time.sleep(random.uniform(1.5, 4))


# ---------------------------------------------------------------- principal
def enviados_hoje():
    h = agora().date().isoformat()
    return sum(1 for p in store.load_prospectos() if (p.get('mensagem_enviada_em') or '').startswith(h))


def dentro_da_janela(ini, fim):
    n = agora()
    return n.weekday() < 6 and ini <= n.hour + n.minute / 60 < fim      # segunda a sabado


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--simular', action='store_true', help='nao envia nada; mostra o plano')
    ap.add_argument('--teto', type=int, default=10)
    ap.add_argument('--inicio', type=float, default=8.5)
    ap.add_argument('--fim', type=float, default=18.5)
    a = ap.parse_args()
    fila = json.load(open(FILA, encoding='utf-8'))
    token = '' if a.simular else _token()
    enviados_sessao, proximo_longo = 0, random.randint(3, 5)
    log(f'fila: {len(fila)} | ja enviados hoje: {enviados_hoje()} | teto {a.teto} | janela {a.inicio}-{a.fim}h '
        f'{"| SIMULACAO" if a.simular else ""}')
    for item in fila:
        if not a.simular:
            if enviados_hoje() >= a.teto:
                log('teto diario atingido: parando (o resto fica para amanha)'); break
            if not dentro_da_janela(a.inicio, a.fim):
                log('fora da janela de horario: parando'); break
        p = next((x for x in store.load_prospectos() if x['nome'] == item['nome']), None)
        if not p or p.get('status') != 'site_gerado' or p.get('excluir_envio') or p.get('sem_whatsapp'):
            log(f'PULADO {item["nome"]}: ja contatado, excluido ou nao encontrado'); continue
        texto = item['texto'].replace('{url}', p.get('site_gerado', ''))
        if a.simular:
            log(f'[simulacao] {item["nome"]} | texto {len(texto)} caracteres')
            digitar('', texto, token, True)
        else:
            ok, m = verificar_site(p)
            if not ok or not is_celular(p.get('telefone')):
                log(f'PULADO {item["nome"]}: {m}'); continue
            ativo, mot = negocio_ativo(p)
            if not ativo:
                log(f'PULADO {item["nome"]}: {mot}'); continue
            num = '+55' + ''.join(c for c in p['telefone'] if c.isdigit())[-11:]
            try:
                if not mcp('check_numbers', {'numbers': [num]}, token)['results'][0].get('registered'):
                    store.mutate_prospectos(lambda L, pid=p['place_id']: [x.update(sem_whatsapp=True) for x in L if x['place_id'] == pid])
                    log(f'PULADO {item["nome"]}: sem WhatsApp'); continue
                mcp('send_text_message', {'to': num, 'text': texto, 'dry_run': True}, token)
                digitar(num, texto, token, False)
                res = mcp('send_text_message', {'to': num, 'text': texto}, token)
                log(f'ENVIADO {item["nome"]} | id {res.get("message_id")} | sent={res.get("sent")}')

                def fn(ps, pid=p['place_id']):
                    for x in ps:
                        if x['place_id'] == pid:
                            x['status'] = 'mensagem_enviada'
                            x['mensagem_enviada_em'] = agora().isoformat(timespec='seconds')
                            n = 'Enviado via WhatsApp MCP (mensagem personalizada) em ' + agora().strftime('%d/%m/%Y %H:%M')
                            x['anotacao'] = ((x.get('anotacao') or '').strip() + ' | ' if (x.get('anotacao') or '').strip() else '') + n
                store.mutate_prospectos(fn)
            except Exception as e:     # qualquer erro: para tudo, nao insiste
                log(f'ERRO em {item["nome"]}: {str(e)[:200]} -> INTERROMPIDO'); break
        enviados_sessao += 1
        if item is not fila[-1]:
            if enviados_sessao % proximo_longo == 0:
                d = pausa_longa_s(); proximo_longo = random.randint(3, 5)
                log(f'   pausa longa de {d/60:.0f} min')
            else:
                d = intervalo_s(); log(f'   proximo em {d/60:.1f} min')
            if not a.simular:
                time.sleep(d)
    log(f'FIM | enviados nesta execucao: {enviados_sessao if not a.simular else 0}')


if __name__ == '__main__':
    main()
