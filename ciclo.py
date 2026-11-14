"""Ciclo autonomo diario: buscar -> avaliar -> ler Instagram publico -> selecionar -> gerar site e
mensagem -> enviar (enviador.py) -> respostas/descadastro -> resumo no Telegram.

Uso: venv/bin/python ciclo.py [--simular]
  --simular: faz busca/avaliacao/leitura de Instagram/selecao (sujeitas ao teto de custo), mostra 3
             mensagens de exemplo e NAO gera sites nem envia nada.
Kill switch: existir data/PAUSA. Log: data/logs/ciclo-AAAAMMDD.log (rotacao).
"""
import argparse
import fcntl
import json
import logging
import logging.handlers
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
import store
import ciclo_lib as L
import presenca
import campanhas
import instagram_leitura
from pre_envio import is_celular, verificar_site

TZ = ZoneInfo('America/Recife')
JANELA = (8.5, 18.5)
FILA = os.path.join(store.DATA_DIR, 'fila_envio.json')
LOGDIR = os.path.join(store.DATA_DIR, 'logs')
SEGREDOS = ('TOKEN', 'KEY', 'PASS', 'SECRET')

logger = logging.getLogger('ciclo')


def _setup_log():
    os.makedirs(LOGDIR, exist_ok=True)
    fn = os.path.join(LOGDIR, f'ciclo-{datetime.now(TZ).strftime("%Y%m%d")}.log')
    h = logging.handlers.RotatingFileHandler(fn, maxBytes=1_000_000, backupCount=3, encoding='utf-8')
    h.setFormatter(logging.Formatter('%(asctime)s %(message)s', '%H:%M:%S'))
    logger.addHandler(h)
    logger.addHandler(logging.StreamHandler(sys.stdout))
    logger.setLevel(logging.INFO)
    for f in os.listdir(LOGDIR):                       # guarda 30 dias
        p = os.path.join(LOGDIR, f)
        if f.startswith('ciclo-') and time.time() - os.path.getmtime(p) > 30 * 86400:
            os.remove(p)


def log(*a):
    logger.info(' '.join(str(x) for x in a))


# ---------------------------------------------------------------- selecao
def _contatado(p):
    return bool(p.get('mensagem_enviada_em')) or p.get('status') not in ('novo', 'site_gerado')


def elegivel(p, minimo):
    return (not _contatado(p) and not p.get('excluir_envio') and not p.get('sem_whatsapp')
            and is_celular(p.get('telefone')) and (p.get('score_oportunidade') or 0) >= minimo)


def selecionar(n, minimo):
    todos = store.load_prospectos()
    telefones = {re.sub(r'\D', '', p.get('telefone') or '')[-9:] for p in todos if _contatado(p)}
    em_fila = {i.get('place_id') for i in _fila()}
    cand = []
    for p in todos:
        t = re.sub(r'\D', '', p.get('telefone') or '')[-9:]
        if elegivel(p, minimo) and t not in telefones and p['place_id'] not in em_fila:
            cand.append(p)
    cand.sort(key=lambda p: -(p.get('score_oportunidade') or 0))
    return cand[:n], len([p for p in todos if elegivel(p, minimo)])


def _fila():
    try:
        return json.load(open(FILA, encoding='utf-8'))
    except Exception:
        return []


# ---------------------------------------------------------------- mensagem (DeepSeek)
EXEMPLOS = [
    'Olá, tudo bem? Caio aqui. A ABM Oficina Mecânica, no Ouro Preto, apareceu na minha busca sem site. '
    'Montei uma página de demonstração organizada como um checklist técnico (revisão, freios, suspensão, '
    'elétrica e injeção), com pedido de orçamento pelo WhatsApp: {url} É só um modelo; se curtir, coloco as '
    'informações e as fotos reais. Se não, me avisa que eu paro por aqui.',
    'Oi! Sou o Caio. O Carlos Peças e Serviços, em Peixinhos, tem oficina e auto peças, mas não achei um site. '
    'Fiz um exemplo que mostra os dois lados, serviços mecânicos e peças, com orçamento pelo WhatsApp: {url} '
    'Tudo ali é de exemplo, e eu ajusto ao que vocês fazem de fato. Se não tiver interesse, é só dizer.']


def _prompt_msg(p):
    fatos = [f'- Nome: {p["nome"]}', f'- Categoria: {p.get("categoria", "")}',
             f'- Endereço (Google): {p.get("endereco") or p.get("cidade", "")}']
    if p.get('rating') and p.get('n_avaliacoes'):
        fatos.append(f'- Avaliação no Google: {p["rating"]} ({p["n_avaliacoes"]} avaliações)')
    pd = p.get('presenca_digital') or {}
    ig = (pd.get('instagram') or {}).get('handle')
    if not p.get('site'):
        fatos.append('- Não há site da empresa na ficha do Google')
    elif p.get('presenca') == 'so_rede_social':
        fatos.append('- A ficha do Google aponta só para rede social, não para um site próprio')
    if ig:
        fatos.append(f'- Tem Instagram: @{ig}')
    return ('Escreva a primeira mensagem de WhatsApp do Caio (desenvolvedor autônomo) para o dono deste negócio, '
            'avisando que montou uma página de demonstração para ele.\n\nFATOS (use apenas estes):\n'
            + '\n'.join(fatos) +
            '\n\nREGRAS:\n- Português do Brasil, tom humano e simples, 3 a 5 frases curtas, sem emojis ou no máximo um.\n'
            '- Cite UM dado real dos fatos acima (ex.: o bairro como aparece no endereço, ou a nota), sem inventar nada.\n'
            '- Diga que a página é só um modelo/exemplo e que o conteúdo (textos, serviços) é ilustrativo.\n'
            '- NÃO prometa preço, prazo, resultado ou garantia. NÃO cite valores nem números de telefone.\n'
            '- Inclua exatamente uma vez o marcador {url} onde vai o link.\n'
            '- Termine deixando claro que, se não houver interesse, é só avisar.\n'
            '- Varie a abertura e a estrutura; não copie os exemplos.\n\nEXEMPLOS DE ESTILO (de outros negócios):\n'
            + '\n'.join('"' + e + '"' for e in EXEMPLOS) +
            '\n\nRetorne APENAS o texto da mensagem, sem aspas e sem explicações.')


def validar_msg(t, p):
    t = (t or '').strip().strip('"').strip()
    if t.count('{url}') != 1:
        return None, 'marcador {url} ausente ou repetido'
    sem = t.replace('{url}', '')
    if re.search(r'https?://|www\.', sem):
        return None, 'link no texto'
    if re.search(r'R\$|\d{4,}|garant|gr[aá]tis|100%', sem, re.I):
        return None, 'promessa/valor/numero no texto'
    if not 80 <= len(t) <= 700:
        return None, f'tamanho {len(t)}'
    primeiro = re.sub(r'\W+', ' ', p['nome']).split()[:1]
    if primeiro and primeiro[0].lower() not in t.lower():
        return None, 'nome do negocio nao citado'
    return t, ''


def escrever_mensagem(p):
    from gerar_site import client
    erro = ''
    for _ in range(2):
        r = client.chat.completions.create(model='deepseek-chat', max_tokens=400, temperature=0.9,
                                           messages=[{'role': 'user', 'content': _prompt_msg(p)}])
        L.registrar_custo('deepseek_msg')
        t, erro = validar_msg(r.choices[0].message.content, p)
        if t:
            return t, ''
    return None, erro


# ---------------------------------------------------------------- etapas
def etapa_buscar(cfg, resumo, simular):
    alvo = cfg['n_novos_por_dia'] * 2
    estoque = len([p for p in store.load_prospectos() if elegivel(p, cfg['min_oportunidade'])])
    ativas = [c for c in store.load_campanhas() if c.get('ativa') and c.get('raio_km') and c.get('lat') is not None
              and c['status'] in ('concluida', 'interrompida', 'erro', 'cancelada')]
    resumo['estoque_antes'] = estoque
    if estoque >= alvo:
        log(f'busca: estoque suficiente ({estoque} elegiveis >= {alvo})'); return
    if not ativas:
        log('busca: nenhuma campanha marcada como ativa no ciclo (marque em /admin/ciclo)'); return
    pr = L.precos()
    for c in ativas:
        ult = c.get('ultima_execucao')
        if ult and datetime.now() - datetime.fromisoformat(ult) < timedelta(days=7):
            log(f'busca: campanha {c["id"]} rodou ha menos de 7 dias; pulando'); continue
        est = (c.get('estimativa') or campanhas.estimar(c['raio_km'], len(c['categorias']), None))
        minimo = est['consultas'] * pr['nearby']
        if L.custo_hoje() + minimo > cfg['custo_max_dia_usd']:
            resumo['parou_por_custo'] = True
            log(f'busca: ate a busca minima (US$ {minimo:.2f}) estoura o teto do dia; pulando {c["id"]}'); continue
        antes = L.custo_hoje()
        teto = cfg['custo_max_dia_usd']

        def parar(prog, antes=antes, teto=teto):          # teto vale DURANTE a busca
            gasto = antes + prog['chamadas_busca'] * pr['nearby'] + prog['chamadas_detalhes'] * pr['details']
            return gasto >= teto
        log(f'busca: rodando campanha {c["id"]} ({c["cidade"]}, {c["raio_km"]} km); estimativa US$ '
            f'{est["custo_min_usd"]}-{est["custo_max_usd"]}, teto restante US$ {teto - antes:.2f}')
        try:
            campanhas.executar(c['id'], parar=parar)
        except Exception as e:
            campanhas._atualizar(c['id'], status='erro', erro=str(e)[:200])
            resumo['erros'].append(f'busca {c["id"]}: {str(e)[:120]}')
            log('busca: erro', str(e)[:200]); continue
        atual = campanhas.obter(c['id']) or {}
        if atual.get('status') == 'erro':
            resumo['erros'].append(f'busca {c["id"]}: {str(atual.get("erro"))[:100]}')
            log('busca: campanha terminou em erro:', str(atual.get('erro'))[:150])
        pg = atual.get('progresso', {})
        L.registrar_custo('nearby', pg.get('chamadas_busca', 0))
        L.registrar_custo('details', pg.get('chamadas_detalhes', 0))
        resumo['buscados'] += pg.get('candidatos', 0)
        resumo['novos'] += pg.get('novos', 0)
        if pg.get('parcial'):
            resumo['parou_por_custo'] = True       # nao marca como rodada: amanha continua pelo que faltou
            log('busca: PARCIAL, parou pelo teto de custo')
        else:
            campanhas._atualizar(c['id'], ultima_execucao=datetime.now().isoformat(timespec='seconds'))
        log(f'busca: {pg.get("candidatos")} candidatos, {pg.get("novos")} novos')
        if L.custo_hoje() >= cfg['custo_max_dia_usd']:
            resumo['parou_por_custo'] = True; break


def etapa_avaliar(resumo):
    def fn(ps):
        n = 0
        for p in ps:
            if 'presenca_digital' not in p:
                presenca.aplicar(p, consultar_site=True)
                n += 1
        return n
    resumo['avaliados'] = store.mutate_prospectos(fn)
    log(f'avaliar: {resumo["avaliados"]} prospects avaliados (inclui consulta ao site proprio)')


def etapa_instagram(cfg, resumo):
    restante = cfg['ig_leituras_dia'] - instagram_leitura.leituras_hoje()
    if restante <= 0:
        log('instagram: limite diario atingido'); return
    cand = [p for p in store.load_prospectos() if not _contatado(p) and is_celular(p.get('telefone'))
            and (p.get('instagram_handle') or p.get('instagram')) and not p.get('excluir_envio')]
    cand.sort(key=lambda p: -(p.get('score_oportunidade') or 0))
    ids = {p['place_id'] for p in cand}
    # so os que a leitura ainda pode mudar a decisao: perto do corte ou acima dele
    r = instagram_leitura.ler_lote(restante, apenas=ids, log=log)
    resumo['ig_ok'], resumo['ig_indisp'] = r['ok'] + r['sem_dados'], r['indisponivel']
    resumo['ig_erro'] = r['erro']
    if r['indisponivel'] and os.getenv('IG_SESSIONID'):
        L.notificar('Prospeccao: leitura de Instagram bloqueada com o cookie de sessao (expirou, checkpoint ou limite). '
                    'Leituras encerradas hoje; renove IG_SESSIONID no .env se necessario.')

    def fn(ps):
        for p in ps:
            if p['place_id'] in ids and p.get('instagram_leitura'):
                presenca.aplicar(p, consultar_site=False)
    store.mutate_prospectos(fn)
    log(f'instagram: ok/sem_dados={resumo["ig_ok"]} indisponivel={resumo["ig_indisp"]} erro={r["erro"]}')


def etapa_gerar(cfg, resumo, simular):
    ps = {p['place_id']: p for p in store.load_prospectos()}
    pendentes = sum(1 for i in _fila() if (ps.get(i.get('place_id')) or {}).get('status') == 'site_gerado'
                    and not (ps[i['place_id']].get('excluir_envio')))
    escolhidos, total_elegiveis = selecionar(max(0, cfg['n_novos_por_dia'] - pendentes), cfg['min_oportunidade'])
    resumo['fila_pendente'] = pendentes
    resumo['elegiveis'] = total_elegiveis
    resumo['selecionados'] = [p['nome'] for p in escolhidos]
    log(f'selecao: {total_elegiveis} elegiveis (score >= {cfg["min_oportunidade"]}); {len(escolhidos)} selecionados')
    if simular:
        for p in escolhidos[:3]:
            try:
                t, e = escrever_mensagem(p)
            except Exception as ex:
                t, e = None, type(ex).__name__
            log(f'  exemplo [{p["nome"]} | score {p.get("score_oportunidade")}]: {t or "FALHOU: " + e}')
            resumo['exemplos'].append({'nome': p['nome'], 'texto': t, 'erro': e})
        return
    from gerar_site import gerar_e_deployar, atualizar_prospecto
    pr = L.precos()
    falhas = 0
    for p in escolhidos:
        if L.pausado():
            log('PAUSA durante a geracao; parando'); break
        custo_item = (0 if p.get('site_gerado') else pr['deepseek_site']) + pr['deepseek_msg']
        if L.custo_hoje() + custo_item > cfg['custo_max_dia_usd']:
            resumo['parou_por_custo'] = True
            log('teto de custo do dia atingido: parando a geracao'); break
        try:
            if not p.get('site_gerado'):
                rel, url = gerar_e_deployar(p)
                if not url:
                    raise RuntimeError('deploy falhou')
                L.registrar_custo('deepseek_site')
                atualizar_prospecto(p['place_id'], rel, url)
                p = next(x for x in store.load_prospectos() if x['place_id'] == p['place_id'])
            ok, m = verificar_site(p)
            if not ok:
                raise RuntimeError('site invalido: ' + str(m)[:80])
            texto, e = escrever_mensagem(p)
            if not texto:
                raise RuntimeError('mensagem reprovada: ' + e)
            fila = _fila()
            fila.append({'nome': p['nome'], 'place_id': p['place_id'], 'texto': texto})
            json.dump(fila, open(FILA + '.tmp', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
            os.replace(FILA + '.tmp', FILA)
            resumo['kits'] += 1
            falhas = 0
            log(f'pronto: {p["nome"]} (site + mensagem na fila)')
        except Exception as ex:
            falhas += 1
            resumo['pulados'].append(f'{p["nome"]}: {str(ex)[:80]}')
            log(f'falha em {p["nome"]}: {str(ex)[:150]}')
            if falhas >= 2:
                resumo['erros'].append('2 falhas seguidas na geracao')
                log('2 falhas seguidas: interrompendo a geracao'); break


def _agora():
    return datetime.now(TZ)


def etapa_enviar(cfg, resumo, mcp, token):
    if cfg['teto_envios'] <= 0:
        log('envio: teto de envios = 0 (envio desligado em /admin/ciclo); a fila fica guardada'); return
    n = _agora()
    if n.weekday() == 6:
        log('envio: domingo, fora da janela; a fila fica para amanha'); return
    ini = n.replace(hour=int(JANELA[0]), minute=int((JANELA[0] % 1) * 60), second=0, microsecond=0)
    fim = n.replace(hour=int(JANELA[1]), minute=int((JANELA[1] % 1) * 60), second=0, microsecond=0)
    if n >= fim:
        log('envio: depois da janela; a fila fica para amanha'); return
    while _agora() < ini:                               # espera abrir a janela (8h30 Recife)
        if L.pausado():
            log('PAUSA enquanto aguardava a janela'); return
        time.sleep(min(60, max(1, (ini - _agora()).total_seconds())))
    if L.pausado():
        log('PAUSA: nao envio'); return
    antes = sum(1 for p in store.load_prospectos() if (p.get('mensagem_enviada_em') or '').startswith(_agora().date().isoformat()))
    cmd = [sys.executable, os.path.join(BASE_DIR, 'enviador.py'), '--teto', str(int(cfg['teto_envios'])),
           '--inicio', str(JANELA[0]), '--fim', str(JANELA[1])]
    log('envio: iniciando enviador.py')
    proc = subprocess.Popen(cmd, cwd=BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            env={**os.environ, 'XDG_RUNTIME_DIR': '/run/user/0', 'HOME': '/root'})
    for linha in proc.stdout:
        linha = linha.rstrip()
        log('  [env] ' + linha)
        if 'PULADO' in linha:
            resumo['pulados'].append(linha.split('PULADO', 1)[1].strip()[:100])
        L.registrar_custo('checagem_ativo') if ('ENVIADO' in linha or 'PULADO' in linha) else None
    cod = proc.wait()
    depois = sum(1 for p in store.load_prospectos() if (p.get('mensagem_enviada_em') or '').startswith(_agora().date().isoformat()))
    resumo['enviados'] = depois - antes
    resumo['enviados_hoje'] = depois
    if cod:
        resumo['erros'].append('enviador terminou com erro (codigo %d)' % cod)


# ---------------------------------------------------------------- principal
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--simular', action='store_true')
    a = ap.parse_args()
    _setup_log()
    os.makedirs(store.DATA_DIR, exist_ok=True)
    lock = open(os.path.join(store.DATA_DIR, '.ciclo.lock'), 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log('outro ciclo ja esta rodando; saindo'); return 0
    cfg = L.config()
    resumo = {'inicio': datetime.now(TZ).isoformat(timespec='seconds'), 'simulacao': a.simular, 'status': 'ok',
              'buscados': 0, 'novos': 0, 'avaliados': 0, 'ig_ok': 0, 'ig_indisp': 0, 'kits': 0, 'enviados': 0,
              'pulados': [], 'erros': [], 'exemplos': [], 'respostas': 0, 'saidas': 0, 'parou_por_custo': False}
    log('=== ciclo ' + ('(SIMULACAO) ' if a.simular else '') + 'config:', json.dumps(cfg))
    if L.pausado():
        log('PAUSA ativa (data/PAUSA): ciclo nao roda')
        L.notificar('Prospeccao: ciclo NAO rodou, data/PAUSA existe.')
        resumo['status'] = 'pausado'; L.registrar_ciclo(resumo); return 0
    import enviador
    token = None
    try:
        token = enviador._token()
        h = enviador.mcp('health', {}, token)
        if h.get('status') != 'ok':
            raise RuntimeError('WhatsApp: ' + str(h.get('summary'))[:150])
    except Exception as e:
        resumo['status'] = 'erro'
        resumo['erros'].append('MCP/WhatsApp: ' + str(e)[:150])
        log('ERRO MCP:', str(e)[:200])
        _fechar(resumo, cfg, a.simular); return 1
    try:
        r = L.verificar_respostas(enviador.mcp, token, log)
        resumo['respostas'], resumo['saidas'] = r['respostas'], r['saidas']
        etapa_buscar(cfg, resumo, a.simular)
        etapa_avaliar(resumo)                  # avaliar e ler Instagram nao custam: rodam sempre
        etapa_instagram(cfg, resumo)
        if not resumo['parou_por_custo'] and not L.pausado():
            etapa_gerar(cfg, resumo, a.simular)
        elif resumo['parou_por_custo']:
            log('teto de custo atingido: sem gerar sites/mensagens hoje')
        if not a.simular and not resumo['parou_por_custo'] and not any('2 falhas' in e for e in resumo['erros']):
            etapa_enviar(cfg, resumo, enviador.mcp, token)
            r = L.verificar_respostas(enviador.mcp, token, log)
            resumo['respostas'] += r['respostas']; resumo['saidas'] += r['saidas']
    except Exception as e:
        resumo['erros'].append('excecao: ' + type(e).__name__ + ' ' + str(e)[:150])
        log('EXCECAO no ciclo:', type(e).__name__, str(e)[:300])
    if resumo['erros']:
        resumo['status'] = 'erro'
    _fechar(resumo, cfg, a.simular)
    return 1 if resumo['status'] == 'erro' else 0


def _fechar(resumo, cfg, simular):
    resumo['custo_usd'] = round(L.custo_hoje(), 3)
    resumo['fim'] = datetime.now(TZ).isoformat(timespec='seconds')
    ant = L.historico(1)
    if resumo['status'] == 'erro' and not simular:
        if ant and ant[0].get('status') == 'erro' and not ant[0].get('simulacao'):
            L.pausar()
            resumo['erros'].append('2 ciclos seguidos com erro: PAUSA criada')
            L.notificar('Prospeccao: 2 ciclos seguidos com erro. Criei data/PAUSA; apague para retomar.')
    if not simular:
        L.registrar_ciclo(resumo)
    txt = (f'Prospeccao {"(simulacao) " if simular else ""}{resumo["status"].upper()}\n'
           f'buscados {resumo["buscados"]} (novos {resumo["novos"]}) | avaliados {resumo["avaliados"]}\n'
           f'Instagram: {resumo["ig_ok"]} lidos / {resumo["ig_indisp"]} indisponiveis\n'
           f'kits gerados {resumo["kits"]} | enviados no ciclo {resumo["enviados"]} (hoje: {resumo.get("enviados_hoje", 0)})\n'
           f'pulados {len(resumo["pulados"])}' + (': ' + '; '.join(resumo['pulados'][:4]) if resumo['pulados'] else '') +
           f'\nrespostas {resumo["respostas"]} (pediram para sair: {resumo["saidas"]})\n'
           f'custo estimado hoje US$ {resumo["custo_usd"]:.2f} / teto {cfg["custo_max_dia_usd"]:.2f}' +
           ('\nPAROU POR TETO DE CUSTO' if resumo['parou_por_custo'] else '') +
           ('\nERROS: ' + '; '.join(resumo['erros']) if resumo['erros'] else ''))
    log('--- resumo ---\n' + txt)
    if not simular or resumo['erros']:
        L.notificar(txt)


if __name__ == '__main__':
    sys.exit(main())
