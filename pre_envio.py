"""Checagem obrigatoria antes de qualquer envio: o site existe e esta no ar?
Somente leitura: nao envia nada."""
import os
import re

import requests

import store

SITES_DIR = '/var/www/fluxos/sites'


def is_celular(telefone):
    nums = re.sub(r'\D', '', telefone or '')
    if nums.startswith('55') and len(nums) > 11:
        nums = nums[2:]
    return len(nums) == 11 and nums[2] == '9'


def negocio_ativo(p):
    """Consulta o Google (Places Details): nome igual e OPERATIONAL. Retorna (ok, motivo).
    Nao detecta mudanca de ramo que o Google ainda nao refletiu."""
    import os
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
    r = requests.get('https://maps.googleapis.com/maps/api/place/details/json', params={
        'place_id': p['place_id'], 'fields': 'name,business_status', 'language': 'pt-BR',
        'key': os.getenv('GOOGLE_PLACES_API_KEY')}, timeout=20).json().get('result', {})
    if r.get('business_status') and r['business_status'] != 'OPERATIONAL':
        return False, 'Google: ' + r['business_status']
    if (r.get('name') or '').strip().lower() != (p.get('nome') or '').strip().lower():
        return False, 'nome mudou no Google: ' + str(r.get('name'))
    return True, 'ok'


def verificar_site(p):
    """Retorna (ok, motivo). Exige: URL preenchida, arquivo no disco, HTTP 200 e HTML completo."""
    url = p.get('site_gerado') or ''
    if '/sites/' not in url:
        return False, 'site nao gerado'
    rel = url.split('/sites/', 1)[1].strip('/')
    f = os.path.join(SITES_DIR, rel, 'index.html')
    if not os.path.isfile(f) or os.path.getsize(f) < 2000:
        return False, 'arquivo ausente ou muito pequeno no disco'
    try:
        r = requests.get(url, timeout=15)
    except requests.RequestException as e:
        return False, f'URL inacessivel: {str(e)[:50]}'
    if r.status_code != 200:
        return False, f'HTTP {r.status_code}'
    h = r.text.lower()
    if '<html' not in h[:600] or '</html>' not in h:
        return False, 'HTML incompleto'
    return True, 'ok'


def prontos_para_envio():
    """Lista prospects aptos a receber a 1a mensagem, com o motivo de cada exclusao."""
    aptos, excluidos = [], []
    for p in store.load_prospectos():
        if not p.get('site_gerado'):
            continue
        if p.get('status') not in ('site_gerado',):
            excluidos.append((p['nome'], f"status '{p.get('status')}' (ja contatado ou encerrado)"))
            continue
        if p.get('excluir_envio'):
            excluidos.append((p['nome'], 'excluido: ' + str(p.get('excluir_envio'))))
            continue
        if p.get('sem_whatsapp'):
            excluidos.append((p['nome'], 'numero sem WhatsApp'))
            continue
        if not is_celular(p.get('telefone')):
            excluidos.append((p['nome'], 'sem telefone celular (possivel WhatsApp)'))
            continue
        ok, motivo = verificar_site(p)
        (aptos if ok else excluidos).append(p if ok else (p['nome'], motivo))
    return aptos, excluidos


if __name__ == '__main__':
    a, e = prontos_para_envio()
    print(f'{len(a)} aptos | {len(e)} excluidos')
    for p in a:
        print('  APTO:', p['nome'], '|', p['telefone'], '|', p['site_gerado'])
    for n, m in e:
        print('  FORA:', n, '|', m)
