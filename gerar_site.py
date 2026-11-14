import os
import re
import subprocess
from openai import OpenAI
from dotenv import load_dotenv

import store

load_dotenv(os.path.join(store.BASE_DIR, '.env'))
client = OpenAI(api_key=os.getenv('DEEPSEEK_API_KEY'), base_url="https://api.deepseek.com")
BASE_URL = os.getenv('BASE_URL', 'https://fluxos.dev.br/sites')
SITES_DIR = '/var/www/fluxos/sites'

PROMPT_TEMPLATE = """Você é um web designer especialista em criar sites bonitos e modernos para pequenos negócios brasileiros.

Crie um site HTML completo e profissional para o seguinte negócio:

- Nome: {nome}
- Categoria: {categoria}
- Endereço: {endereco}
- Telefone: {telefone}
{instagram}{avaliacao}
DIREÇÃO DE DESIGN (siga com fidelidade; o layout deve ser claramente característico deste tipo de negócio e diferente do de outros segmentos):
{perfil}

REQUISITOS:
- HTML, CSS e JS em um único arquivo
- Responsivo (funciona bem no celular)
- {contato_req}
- Sem imagens externas (use gradientes, formas e ícones em CSS/SVG inline)
- Fontes do Google Fonts conforme a direção de design
- Footer com os dados de contato disponíveis (não invente os que faltam)
- Mantenha o arquivo ENXUTO: no máximo ~25 KB (CSS compacto, sem repetições, ícones em SVG simples e curtos, no máximo 5 seções). O HTML PRECISA terminar em </html>
- Não invente preços exatos, depoimentos com nome de pessoas reais nem dados que você não recebeu

IMPORTANTE: Retorne APENAS o código HTML completo, sem explicações, sem markdown, sem ```html. Comece com <!DOCTYPE html>"""

PERFIL_GENERICO = {
    'estilo': 'moderno e profissional, adequado ao tipo de negócio',
    'paleta': 'cores coerentes com o segmento',
    'fontes': 'uma fonte para títulos e outra para texto',
    'layout': 'hero com nome e slogan, serviços em cards, chamada clara para contato',
    'secoes': 'Hero, Sobre, Serviços, Contato',
}


def slugify(nome):
    slug = nome.lower()
    for a, b in [('á', 'a'), ('à', 'a'), ('ã', 'a'), ('â', 'a'), ('é', 'e'), ('ê', 'e'),
                 ('í', 'i'), ('ó', 'o'), ('ô', 'o'), ('õ', 'o'), ('ú', 'u'), ('ç', 'c')]:
        slug = slug.replace(a, b)
    slug = re.sub(r'[^a-z0-9\s-]', '', slug)
    slug = re.sub(r'[\s]+', '-', slug.strip())
    return slug[:60]


def slug_cidade(cidade):
    """'Olinda, PE, Brasil' -> 'olinda-pe'."""
    c = re.sub(r',?\s*Brasil\s*$', '', cidade.strip(), flags=re.I)
    return slugify(c.replace(',', ' ')) or 'cidade'


def limpar_telefone(telefone):
    return re.sub(r'\D', '', telefone) if telefone else ''


def perfil_da_categoria(prospecto):
    """Acha o perfil de design pela keyword ou tipo da categoria do prospect."""
    for c in store.load_categorias():
        if c['keyword'] == prospecto.get('categoria') or c['tipo'] == prospecto.get('tipo'):
            return c['perfil']
    return PERFIL_GENERICO


def formatar_perfil(perfil):
    return (f"- Estilo: {perfil['estilo']}\n- Paleta: {perfil['paleta']}\n"
            f"- Fontes: {perfil['fontes']}\n- Estrutura/layout: {perfil['layout']}\n"
            f"- Seções: {perfil['secoes']}")


def montar_prompt(prospecto):
    nota = prospecto.get('rating')
    avaliacao = ''
    if nota and prospecto.get('n_avaliacoes'):
        avaliacao = f"- Avaliação no Google: {nota} estrelas ({prospecto['n_avaliacoes']} avaliações)\n"
    tel_limpo = limpar_telefone(prospecto.get('telefone', ''))
    ig = prospecto.get('instagram', '')
    if tel_limpo:
        contato_req = f'Botão de WhatsApp flutuante com o número {tel_limpo}'
    else:
        contato_req = ('Esta empresa NÃO informou telefone: não crie botão de WhatsApp nem número. '
                       + (f'Use um botão de contato para o Instagram https://instagram.com/{ig}.'
                          if ig else 'Use apenas o endereço como contato.'))
    return PROMPT_TEMPLATE.format(
        nome=prospecto['nome'],
        categoria=prospecto.get('categoria', ''),
        endereco=prospecto.get('endereco') or prospecto.get('cidade', ''),
        telefone=prospecto.get('telefone', ''),
        avaliacao=avaliacao,
        instagram=(f"- Instagram: @{ig}\n" if ig else ''),
        contato_req=contato_req,
        perfil=formatar_perfil(perfil_da_categoria(prospecto)),
    )


def caminho_publicacao(prospecto):
    """Prospects novos: cidade/slug. Retorna (slug_relativo, slug_simples)."""
    slug = slugify(prospecto['nome'])
    cidade = slug_cidade(prospecto.get('cidade') or 'Olinda')
    rel = f"{cidade}/{slug}"
    if os.path.exists(os.path.join(SITES_DIR, rel)):      # nome repetido: nao sobrescrever
        slug = f"{slug[:50]}-{prospecto['place_id'][-6:].lower()}"
        slug = re.sub(r'[^a-z0-9-]', '', slug)
        rel = f"{cidade}/{slug}"
    return rel, slug


def gerar_e_deployar(prospecto, forcar_rel=None):
    """forcar_rel='cidade/slug' republica no mesmo endereco (regeracao)."""
    rel, slug = (forcar_rel, None) if forcar_rel else caminho_publicacao(prospecto)
    cidade, slug_final = rel.split('/')
    prompt = montar_prompt(prospecto)

    print(f"  Gerando site para {prospecto['nome']}...")
    response = client.chat.completions.create(
        model="deepseek-chat",
        max_tokens=16384,
        messages=[{"role": "user", "content": prompt}],
    )
    html = response.choices[0].message.content
    if '<!DOCTYPE' in html:
        html = html[html.find('<!DOCTYPE'):]

    tmp_path = f'/tmp/{cidade}-{slug_final}.html'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        f.write(html)

    result = subprocess.run(['/root/deploy-site-v2.sh', cidade, slug_final, tmp_path],
                            capture_output=True, text=True)
    if result.returncode == 0:
        url = f"{BASE_URL}/{cidade}/{slug_final}/"
        print(f"  OK Deployado em {url}")
        return rel, url
    print(f"  Erro no deploy: {result.stderr}")
    return None, None


def atualizar_prospecto(place_id, slug, url):
    def fn(prospectos):
        for p in prospectos:
            if p['place_id'] == place_id:
                p['site_gerado'] = url
                p['slug'] = slug
                p['status'] = 'site_gerado'
                break
    store.mutate_prospectos(fn)
