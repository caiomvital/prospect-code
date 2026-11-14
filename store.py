"""Armazenamento JSON com lock e escrita atomica (prospectos e campanhas)."""
import fcntl
import json
import os
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
PROSPECTOS_PATH = os.path.join(DATA_DIR, 'prospectos.json')
CAMPANHAS_PATH = os.path.join(DATA_DIR, 'campanhas.json')
CATEGORIAS_PATH = os.path.join(DATA_DIR, 'categorias.json')

class _Lock:
    """RLock entre threads + flock entre PROCESSOS (painel, ciclo e enviador gravam os mesmos JSON).
    Reentrante por thread. Uso: `with LOCK:` (igual ao RLock anterior)."""
    def __init__(self):
        self._t = threading.RLock()
        self._n = 0
        self._fd = None

    def __enter__(self):
        self._t.acquire()
        if self._n == 0:
            os.makedirs(DATA_DIR, exist_ok=True)
            self._fd = open(os.path.join(DATA_DIR, '.lock'), 'a')
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        self._n += 1

    def __exit__(self, *exc):
        self._n -= 1
        if self._n == 0:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            self._fd.close()
            self._fd = None
        self._t.release()


LOCK = _Lock()


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
