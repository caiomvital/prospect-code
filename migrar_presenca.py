"""Roda a avaliacao de presenca em todos os prospects, usando so o que ja esta salvo
(nao chama a API do Google). --sites consulta o site proprio (1 GET por prospect)."""
import sys
sys.path.insert(0, '/root/prospeccao')
import store, presenca
consultar = '--sites' in sys.argv
def fn(L):
    for p in L:
        presenca.aplicar(p, consultar_site=consultar)
    return len(L)
print('avaliados:', store.mutate_prospectos(fn))
