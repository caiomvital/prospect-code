import os
import re
import sys
import json
import subprocess
from functools import wraps
from flask import Flask, session, redirect, request, render_template, jsonify, Response
from dotenv import load_dotenv

sys.path.insert(0, '/root/prospeccao')
from gerar_site import gerar_e_deployar, atualizar_prospecto
import store
import campanhas
import presenca as presenca_mod

load_dotenv('/root/prospeccao/.env')

app = Flask(__name__)
app.secret_key = os.urandom(24).hex()

USER = os.getenv('DASHBOARD_USER', 'admin')
PASS = os.getenv('DASHBOARD_PASS', 'admin123')
JSON_PATH = '/root/prospeccao/data/prospectos.json'
BUSCAR_SCRIPT = '/root/prospeccao/buscar_empresas.py'


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
        if request.form.get('usuario') == USER and request.form.get('senha') == PASS:
            session['logged_in'] = True
            return redirect('/admin/')
        else:
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


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=False)
