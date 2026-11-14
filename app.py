import os
import re
import sys
import json
import subprocess
from functools import wraps
from flask import Flask, session, redirect, request, render_template, jsonify, Response
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
from gerar_site import gerar_e_deployar, atualizar_prospecto
import store
import campanhas
import presenca as presenca_mod
import ciclo_lib
import instagram_client
import instagram_leitura

load_dotenv(os.path.join(BASE_DIR, '.env'))

app = Flask(__name__)

_SK = os.getenv('SECRET_KEY', '')
if len(_SK) < 32:
    sys.exit('SECRET_KEY ausente ou curta no .env: recusando subir.')
app.secret_key = _SK

USER = os.getenv('DASHBOARD_USER', 'admin')
PASS = os.getenv('DASHBOARD_PASS', '')
if not PASS or PASS == 'admin123':
    sys.exit('DASHBOARD_PASS vazio ou "admin123": defina uma senha forte no .env. Recusando subir.')
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=True)

# ---- limite de tentativas de login: 5 falhas/IP em 15 min => bloqueio de 30 min (em memoria)
import time as _time
import hmac as _hmac
_FALHAS, _BLOQ = {}, {}


def _ip():
    return request.headers.get('X-Real-IP') or request.remote_addr or '?'   # nginx define X-Real-IP


def _bloqueado(ip):
    ate = _BLOQ.get(ip, 0)
    if ate > _time.time():
        return int(ate - _time.time())
    _BLOQ.pop(ip, None)
    return 0


def _falhou(ip):
    agora = _time.time()
    L = [t for t in _FALHAS.get(ip, []) if agora - t < 900] + [agora]
    _FALHAS[ip] = L
    if len(L) >= 5:
        _BLOQ[ip] = agora + 1800
        _FALHAS.pop(ip, None)
JSON_PATH = os.path.join(BASE_DIR, 'data', 'prospectos.json')
BUSCAR_SCRIPT = os.path.join(BASE_DIR, 'buscar_empresas.py')


def is_whatsapp(telefone):
    if not telefone:
        return False
    nums = re.sub(r'\D', '', telefone)
    if len(nums) >= 10:
        local = nums[2:]
        return len(local) == 9 and local[0] == '9'
    return False


load_prospectos = store.load_prospectos
save_prospectos = store.save_prospectos

campanhas.migrar()
campanhas.iniciar_worker()


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect('/admin/login')
        return f(*args, **kwargs)
    return decorated


@app.route('/admin/login', methods=['GET', 'POST'])
def login():
    erro = None
    if request.method == 'POST':
        ip = _ip()
        resto = _bloqueado(ip)
        if resto:
            return render_template('admin_login.html', erro=f'Muitas tentativas. Tente de novo em {resto // 60 + 1} min.'), 429
        ok_u = _hmac.compare_digest(request.form.get('usuario', ''), USER)
        ok_s = _hmac.compare_digest(request.form.get('senha', ''), PASS)
        if ok_u and ok_s:
            _FALHAS.pop(ip, None)
            session['logged_in'] = True
            return redirect('/admin/')
        else:
            _falhou(ip)
            erro = 'Usuario ou senha incorretos'
    return render_template('admin_login.html', erro=erro)


@app.route('/admin/logout')
def logout():
    session.pop('logged_in', None)
    return redirect('/admin/login')


@app.route('/admin/', methods=['GET'])
@login_required
def dashboard():
    prospectos = load_prospectos()
    cat_filtro = request.args.get('categoria', '')
    sts_filtro = request.args.get('status', '')
    wp_filtro = request.args.get('whatsapp', '')
    pres_filtro = request.args.get('presenca', '')
    prio_filtro = request.args.get('prioridade', '')
    ordem = request.args.get('ordem', '')
    opp_min = request.args.get('opp_min', type=int)

    msgs = {c['id']: c.get('mensagem_whatsapp') or campanhas.MSG_PADRAO
            for c in store.load_campanhas()}
    for p in prospectos:
        p['tem_whatsapp'] = is_whatsapp(p.get('telefone', ''))
        modelo = msgs.get(p.get('campanha_id'), campanhas.MSG_PADRAO)
        p['wa_msg'] = modelo.replace('{nome}', p.get('nome', '')).replace('{url}', p.get('site_gerado', ''))

    if cat_filtro:
        prospectos = [p for p in prospectos if p.get('categoria') == cat_filtro]
    if sts_filtro:
        prospectos = [p for p in prospectos if p.get('status', 'novo') == sts_filtro]
    if wp_filtro == 'true':
        prospectos = [p for p in prospectos if p.get('tem_whatsapp')]
    if pres_filtro:
        prospectos = [p for p in prospectos if p.get('presenca') == pres_filtro]
    if prio_filtro:
        prospectos = [p for p in prospectos if p.get('prioridade') == prio_filtro]
    if opp_min:
        prospectos = [p for p in prospectos if (p.get('score_oportunidade') or 0) >= opp_min]
    if ordem == 'oportunidade':
        prospectos.sort(key=lambda p: -(p.get('score_oportunidade') or 0))
    if ordem == 'prioridade':
        peso = {'alta': 0, 'media': 1, 'baixa': 2}
        prospectos.sort(key=lambda p: (peso.get(p.get('prioridade'), 3),
                                       -(p.get('n_avaliacoes') or 0)))

    todas = load_prospectos()
    for p in todas:
        p['tem_whatsapp'] = is_whatsapp(p.get('telefone', ''))
    categorias = sorted(set(p.get('categoria', '') for p in todas if p.get('categoria')))
    sem_site = sum(1 for p in todas if not p.get('site_gerado'))
    com_site = sum(1 for p in todas if p.get('site_gerado'))
    qtd_wpp = sum(1 for p in todas if p.get('tem_whatsapp'))
    qtd_fixo = sum(1 for p in todas if p.get('telefone') and not p.get('tem_whatsapp'))
    qtd_sem_tel = sum(1 for p in todas if not p.get('telefone'))

    return render_template('admin_dashboard.html',
                           prospectos=prospectos,
                           total=len(todas),
                           categorias=categorias,
                           cat_filtro=cat_filtro,
                           sts_filtro=sts_filtro,
                           wp_filtro=wp_filtro,
                           pres_filtro=pres_filtro,
                           prio_filtro=prio_filtro,
                           ordem=ordem,
                           opp_min=opp_min or '',
                           pres_rotulo=campanhas.PRESENCA_ROTULO,
                           sem_site=sem_site,
                           com_site=com_site,
                           qtd_wpp=qtd_wpp,
                           qtd_fixo=qtd_fixo,
                           qtd_sem_tel=qtd_sem_tel)


@app.route('/admin/buscar', methods=['POST'])
@login_required
def buscar():
    # Busca fixa (Olinda) desativada: use /admin/nova-busca
    return jsonify({'ok': False, 'erro': 'Use a tela Nova busca', 'redirect': '/admin/nova-busca'})


@app.route('/admin/nova-busca')
@login_required
def nova_busca():
    return render_template('admin_nova_busca.html', categorias=store.load_categorias(),
                           msg_padrao=campanhas.MSG_PADRAO)


@app.route('/admin/importar-lista', methods=['GET'])
@login_required
def importar_lista_page():
    return render_template('admin_importar_lista.html', categorias=store.load_categorias(),
                           msg_padrao=campanhas.MSG_PADRAO)


@app.route('/admin/importar-lista', methods=['POST'])
@login_required
def importar_lista_post():
    d = request.get_json(force=True)
    try:
        r = campanhas.importar_lista(d.get('texto', ''), d.get('cidade', ''),
                                     d.get('categoria', ''), d.get('mensagem', ''))
        return jsonify({'ok': True, **r})
    except ValueError as e:
        return jsonify({'ok': False, 'erro': str(e)}), 400


@app.route('/admin/api/cidades')
@login_required
def api_cidades():
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify([])
    try:
        return jsonify(campanhas.autocomplete_cidades(q))
    except Exception as e:
        return jsonify({'erro': str(e)}), 502


@app.route('/admin/api/cidade/<place_id>')
@login_required
def api_cidade(place_id):
    try:
        return jsonify(campanhas.coordenadas_cidade(place_id))
    except Exception as e:
        return jsonify({'erro': str(e)}), 502


@app.route('/admin/api/estimativa')
@login_required
def api_estimativa():
    try:
        raio = float(request.args.get('raio', 5))
        n = int(request.args.get('n_categorias', 1))
        lim = request.args.get('limite')
        return jsonify(campanhas.estimar(raio, n, int(lim) if lim else None))
    except ValueError:
        return jsonify({'erro': 'parametros invalidos'}), 400


@app.route('/admin/campanhas', methods=['GET'])
@login_required
def listar_campanhas():
    return render_template('admin_campanhas.html', campanhas=list(reversed(store.load_campanhas())))


@app.route('/admin/campanhas', methods=['POST'])
@login_required
def criar_campanha():
    try:
        camp = campanhas.criar_campanha(request.get_json(force=True))
        campanhas.enfileirar(camp['id'])
        return jsonify({'ok': True, 'id': camp['id']})
    except (ValueError, KeyError) as e:
        return jsonify({'ok': False, 'erro': str(e)}), 400


@app.route('/admin/campanhas/<cid>/status')
@login_required
def status_campanha(cid):
    c = campanhas.obter(cid)
    if not c:
        return jsonify({'ok': False}), 404
    ps = [p for p in store.load_prospectos() if p.get('campanha_id') == cid]
    c = dict(c)
    c['prospectos'] = [{'place_id': p['place_id'], 'nome': p['nome'], 'categoria': p.get('categoria'),
                        'telefone': p.get('telefone', ''), 'rating': p.get('rating'),
                        'n_avaliacoes': p.get('n_avaliacoes')} for p in ps]
    return jsonify(c)


@app.route('/admin/campanhas/<cid>/cancelar', methods=['POST'])
@login_required
def cancelar_campanha(cid):
    campanhas.cancelar(cid)
    return jsonify({'ok': True})


@app.route('/admin/campanhas/<cid>/mensagem', methods=['POST'])
@login_required
def mensagem_campanha(cid):
    msg = (request.get_json(force=True).get('mensagem') or '').strip()
    if not msg:
        return jsonify({'ok': False, 'erro': 'Mensagem vazia'}), 400

    def fn(cs):
        for c in cs:
            if c['id'] == cid:
                c['mensagem_whatsapp'] = msg
                return True
        return False
    return jsonify({'ok': store.mutate_campanhas(fn)})


@app.route('/admin/gerar-site/<place_id>', methods=['POST'])
@login_required
def gerar_site(place_id):
    prospectos = load_prospectos()
    prospecto = None
    for p in prospectos:
        if p['place_id'] == place_id:
            prospecto = p
            break

    if not prospecto:
        return jsonify({'ok': False, 'erro': 'Prospecto nao encontrado'})

    if prospecto.get('site_gerado'):
        return jsonify({'ok': False, 'erro': 'Prospecto ja tem site'})

    try:
        slug, url = gerar_e_deployar(prospecto)
        if url:
            atualizar_prospecto(place_id, slug, url)
            return jsonify({'ok': True, 'url': url})
        else:
            return jsonify({'ok': False, 'erro': 'Falha ao gerar site'})
    except Exception as e:
        return jsonify({'ok': False, 'erro': str(e)})


@app.route('/admin/atualizar-status/<place_id>', methods=['POST'])
@login_required
def atualizar_status(place_id):
    data = request.get_json(force=True)
    def fn(prospectos):
        for p in prospectos:
            if p['place_id'] == place_id:
                if 'status' in data:
                    p['status'] = data['status']
                if 'anotacao' in data:
                    p['anotacao'] = data['anotacao']
                if 'instagram_handle' in data:
                    h = re.sub(r'^(https?://)?(www\.)?instagram\.com/', '', (data['instagram_handle'] or '').strip())
                    h = h.split('?')[0].strip('/@ ')
                    if h and not re.fullmatch(r'[A-Za-z0-9_.]{1,30}', h):
                        return 'invalido'
                    p['instagram_handle'] = h
                    p['instagram'] = h
                    presenca_mod.aplicar(p, consultar_site=False)
                return True
        return False

    r = store.mutate_prospectos(fn)
    if r == 'invalido':
        return jsonify({'ok': False, 'erro': '@ invalido'})
    if not r:
        return jsonify({'ok': False, 'erro': 'Nao encontrado'})
    return jsonify({'ok': True})


@app.route('/admin/exportar.csv')
@login_required
def exportar_csv():
    import csv, io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['nome', 'categoria', 'telefone', 'site', 'instagram_handle', 'score'])
    for p in sorted(load_prospectos(), key=lambda x: -(x.get('score_oportunidade') or 0)):
        w.writerow([p.get('nome', ''), p.get('categoria', ''), p.get('telefone', ''), p.get('site', ''),
                    p.get('instagram_handle', ''), p.get('score_oportunidade', '')])
    return Response('\ufeff' + buf.getvalue(), mimetype='text/csv; charset=utf-8',
                    headers={'Content-Disposition': 'attachment; filename=prospectos.csv'})


# ---------------------------------------------------------------- Instagram (conta propria)
@app.route('/admin/instagram')
@login_required
def pagina_instagram():
    st = instagram_client.check_token()
    perfil, posts, erro = None, [], st.get('erro')
    if st.get('ok'):
        try:
            perfil = instagram_client.get_profile()
            posts = instagram_client.list_media(9)
        except instagram_client.IGErro as e:
            erro = str(e)
    return render_template('admin_instagram.html', st=st, perfil=perfil, posts=posts, erro=erro)


# ---------------------------------------------------------------- ciclo autonomo
@app.route('/admin/ciclo')
@login_required
def pagina_ciclo():
    camps = [c for c in store.load_campanhas() if c.get('raio_km')]
    custo = ciclo_lib.custo_hoje()
    return render_template('admin_ciclo.html', pausado=ciclo_lib.pausado(), cfg=ciclo_lib.config(),
                           hist=ciclo_lib.historico(14), camps=camps, custo=custo)


@app.route('/admin/ciclo/pausa', methods=['POST'])
@login_required
def ciclo_pausa():
    ciclo_lib.retomar() if ciclo_lib.pausado() else ciclo_lib.pausar()
    return redirect('/admin/ciclo')


@app.route('/admin/ciclo/config', methods=['POST'])
@login_required
def ciclo_config():
    try:
        ciclo_lib.salvar_config({k: request.form[k] for k in ciclo_lib.PADRAO if request.form.get(k, '') != ''})
    except ValueError:
        pass
    return redirect('/admin/ciclo')


@app.route('/admin/ciclo/campanha/<cid>', methods=['POST'])
@login_required
def ciclo_campanha(cid):
    def fn(cs):
        for c in cs:
            if c['id'] == cid:
                c['ativa'] = not c.get('ativa')
    store.mutate_campanhas(fn)
    return redirect('/admin/ciclo')


# ---------------------------------------------------------------- kit
@app.route('/admin/kit/<place_id>', methods=['POST'])
@login_required
def gerar_kit(place_id):
    import kit
    p = next((x for x in load_prospectos() if x['place_id'] == place_id), None)
    if not p:
        return jsonify({'ok': False, 'erro': 'Prospecto nao encontrado'})
    try:
        kit.gerar(p)
        return jsonify({'ok': True, 'url': '/admin/kit/' + place_id})
    except Exception as e:
        return jsonify({'ok': False, 'erro': str(e)[:200]})


@app.route('/admin/kit/<place_id>', methods=['GET'])
@login_required
def ver_kit(place_id):
    import kit
    f = os.path.join(kit.pasta(place_id), 'kit.html')
    if not os.path.exists(f):
        return 'Kit ainda nao gerado', 404
    return Response(open(f, encoding='utf-8').read(), mimetype='text/html')


# ---------------------------------------------------------------- sincronizacao do script local de Instagram
def _sync_negado():
    """Auth por token (IG_SYNC_TOKEN) para o script na maquina do usuario. Reusa o limite de falhas por IP."""
    ip = _ip()
    if _bloqueado(ip):
        return jsonify({'erro': 'bloqueado'}), 429
    tok = os.getenv('IG_SYNC_TOKEN', '')
    dado = request.headers.get('Authorization', '')
    if len(tok) >= 32 and _hmac.compare_digest(dado.encode(), ('Bearer ' + tok).encode()):
        return None
    _falhou(ip)
    return jsonify({'erro': 'nao autorizado'}), 401


@app.route('/admin/api/ig/pendentes')
def ig_pendentes():
    neg = _sync_negado()
    if neg:
        return neg
    n = min(request.args.get('limite', 10, type=int), 30)
    return jsonify({'handles': instagram_leitura.pendentes(n)})


@app.route('/admin/api/ig/resultados', methods=['POST'])
def ig_resultados():
    neg = _sync_negado()
    if neg:
        return neg
    corpo = request.get_json(silent=True) or {}
    itens = corpo.get('resultados')
    if not isinstance(itens, list) or len(itens) > 100:
        return jsonify({'erro': 'formato invalido'}), 400
    return jsonify(instagram_leitura.aplicar_resultados([i for i in itens if isinstance(i, dict)]))


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=False)
