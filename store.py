"""Armazenamento JSON com lock e escrita atomica (prospectos e campanhas)."""
import json
import os
import threading

DATA_DIR = '/root/prospeccao/data'
PROSPECTOS_PATH = os.path.join(DATA_DIR, 'prospectos.json')
CAMPANHAS_PATH = os.path.join(DATA_DIR, 'campanhas.json')
CATEGORIAS_PATH = os.path.join(DATA_DIR, 'categorias.json')

LOCK = threading.RLock()


def _read(path, default):
    if not os.path.exists(path):
        return default
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def _write(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_prospectos():
    with LOCK:
        return _read(PROSPECTOS_PATH, [])


def save_prospectos(data):
    with LOCK:
        _write(PROSPECTOS_PATH, data)


def mutate_prospectos(fn):
    """Le, aplica fn(lista) e grava, tudo sob lock. Retorna o retorno de fn."""
    with LOCK:
        data = _read(PROSPECTOS_PATH, [])
        res = fn(data)
        _write(PROSPECTOS_PATH, data)
        return res


def load_campanhas():
    with LOCK:
        return _read(CAMPANHAS_PATH, [])


def mutate_campanhas(fn):
    with LOCK:
        data = _read(CAMPANHAS_PATH, [])
        res = fn(data)
        _write(CAMPANHAS_PATH, data)
        return res


def load_categorias():
    return _read(CATEGORIAS_PATH, [])
