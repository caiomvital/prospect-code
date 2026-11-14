
import requests
import json
import time
import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv('/root/prospeccao/.env')
API_KEY = os.getenv('GOOGLE_PLACES_API_KEY')

def buscar_empresas(keyword, tipo, cidade, api_key):
    url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    empresas = []
    next_page_token = None
    while True:
        params = {"query": f"{keyword} em {cidade}", "key": api_key, "language": "pt-BR"}
        if next_page_token:
            params["pagetoken"] = next_page_token
            time.sleep(2)
        response = requests.get(url, params=params)
        data = response.json()
        if data.get("status") not in ("OK", "ZERO_RESULTS"):
            print(f"Erro na API: {data.get('status')}")
            break
        for place in data.get("results", []):
            empresas.append({
                "place_id": place.get("place_id"),
                "nome": place.get("name"),
                "endereco": place.get("formatted_address"),
                "categoria": keyword,
                "tipo": tipo,
            })
        next_page_token = data.get("next_page_token")
        if not next_page_token:
            break
    return empresas

def buscar_detalhes(place_id, api_key):
    url = "https://maps.googleapis.com/maps/api/place/details/json"
    params = {"place_id": place_id, "fields": "name,formatted_phone_number,website,url", "key": api_key, "language": "pt-BR"}
    response = requests.get(url, params=params)
    data = response.json()
    result = data.get("result", {})
    return {
        "telefone": result.get("formatted_phone_number", ""),
        "site": result.get("website", ""),
        "maps_url": result.get("url", ""),
    }

def tem_site_ruim(site):
    if not site:
        return True
    redes_sociais = ["facebook.com", "instagram.com", "wa.me", "linktr.ee", "linktree"]
    return any(r in site.lower() for r in redes_sociais)

def rodar_busca(cidade, categorias):
    print(f"Buscando em {cidade}...")
    todas = []
    for keyword, tipo in categorias:
        print(f"  -> {keyword}...")
        empresas = buscar_empresas(keyword, tipo, cidade, API_KEY)
        print(f"     {len(empresas)} encontradas")
        todas.extend(empresas)
        time.sleep(1)

    print()
    print(f"Total: {len(todas)} empresas. Buscando detalhes...")
    prospectos_novos = []
    for i, empresa in enumerate(todas):
        detalhes = buscar_detalhes(empresa["place_id"], API_KEY)
        empresa.update(detalhes)
        if tem_site_ruim(empresa["site"]):
            empresa["status"] = "novo"
            empresa["anotacao"] = ""
            empresa["site_gerado"] = ""
            empresa["data_criacao"] = datetime.now().isoformat()
            prospectos_novos.append(empresa)
            print(f"  [{i+1}/{len(todas)}] {empresa['nome']} OK")
        else:
            print(f"  [{i+1}/{len(todas)}] {empresa['nome']} - tem site")
        time.sleep(0.1)

    json_path = '/root/prospeccao/data/prospectos.json'
    with open(json_path, 'r', encoding='utf-8') as f:
        existentes = json.load(f)

    ids_existentes = {p['place_id'] for p in existentes}
    novos_unicos = [p for p in prospectos_novos if p['place_id'] not in ids_existentes]
    todos_prospectos = existentes + novos_unicos

    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(todos_prospectos, f, ensure_ascii=False, indent=2)

    print()
    print(f"{len(novos_unicos)} novos prospectos adicionados ({len(existentes)} ja existiam)")
    print(f"Total no banco: {len(todos_prospectos)}")
    return len(novos_unicos)

if __name__ == "__main__":
    categorias = [
        ("salão de beleza", "beauty_salon"),
        ("clínica", "doctor"),
        ("padaria", "bakery"),
        ("restaurante", "restaurant"),
        ("pet shop", "pet_store"),
        ("veterinário", "veterinary_care"),
        ("academia", "gym"),
        ("oficina mecânica", "car_repair"),
        ("farmácia", "pharmacy"),
        ("pousada", "lodging"),
        ("dentista", "dentist"),
        ("escola", "school"),
        ("loja de roupa", "clothing_store"),
    ]
    rodar_busca("Olinda, Pernambuco, Brasil", categorias)
