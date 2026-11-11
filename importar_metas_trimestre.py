"""
importar_metas_trimestre.py - Importa em lote as metas individuais do
Plano de Metas Comerciais (Nov/2026 a Jan/2027) para a tabela `metas`.

Grava apenas a META estabelecida (o objetivo = meta x 1,20 é derivado).
Idempotente: usa o mesmo upsert de db.upsert_meta (UNIQUE vendedor_id/ano/mes).
Valida totais contra o PDF antes de gravar; tudo em uma única transação.

Uso:
    python importar_metas_trimestre.py            # dry-run (não grava)
    python importar_metas_trimestre.py --aplicar  # grava
"""
import sys
import db
from sqlalchemy import text

MESES = [(2026, 11), (2026, 12), (2027, 1)]

# (loja, vendedor): metas Nov, Dez, Jan  — fonte: PDF "Plano de Metas" emitido em 03/11/2026
METAS = {
    ("Casa de Adubo", "Tais Bispo"): (175000, 170000, 165000),
    ("Casa de Adubo", "Jesse"):      (175000, 170000, 165000),
    ("Casa de Adubo", "Poliane"):    (137500, 135000, 132500),
    ("Casa de Adubo", "Adriely"):    (137500, 135000, 132500),
    ("Casa de Adubo", "Gleicia"):    (137500, 135000, 132500),
    ("Casa de Adubo", "Naiara"):     (137500, 135000, 132500),
    ("Casa de Adubo", "Luan"):       (40000, 35000, 32500),
    ("Porteira", "Tamires Larceda"):    (175000, 170000, 165000),
    ("Porteira", "Renata Ribeiro"):     (175000, 170000, 165000),
    ("Porteira", "Elizangela Batista"): (125000, 122500, 120000),
    ("Porteira", "Jadila"):             (137500, 135000, 132500),
    ("Porteira", "Taina Santos"):       (137500, 135000, 132500),
    ("Porteira", "Igor Silva"):         (137500, 135000, 132500),
    ("Porteira", "Felipe"):             (137500, 135000, 132500),
}

# Totais por loja no PDF (Nov, Dez, Jan) — checagem de digitação
TOTAIS_PDF = {
    "Casa de Adubo": (940000, 915000, 892500),
    "Porteira": (1025000, 1002500, 980000),
}


def validar():
    for loja, esperado in TOTAIS_PDF.items():
        soma = tuple(sum(v[i] for (l, _), v in METAS.items() if l == loja) for i in range(3))
        assert soma == esperado, f"{loja}: soma {soma} != PDF {esperado}"


def main(aplicar):
    validar()
    vend = db.get_vendedores()
    ids = {}
    for (loja, nome) in METAS:
        m = vend[(vend["nome"] == nome) & (vend["loja"] == loja)]
        assert len(m) == 1, f"Vendedor não encontrado/ambíguo: {nome} ({loja}) -> {len(m)}"
        ids[(loja, nome)] = int(m.iloc[0]["id"])

    linhas = [(ids[k], a, m, float(v[i])) for k, v in METAS.items() for i, (a, m) in enumerate(MESES)]
    print(f"{len(linhas)} metas a gravar ({'APLICAR' if aplicar else 'dry-run'})")
    if not aplicar:
        return
    with db.get_engine().begin() as conn:
        for vid, ano, mes, val in linhas:
            conn.execute(text(
                "INSERT INTO metas (vendedor_id, ano, mes, valor_meta) VALUES (:v,:a,:m,:x) "
                "ON CONFLICT (vendedor_id, ano, mes) DO UPDATE SET valor_meta = EXCLUDED.valor_meta"
            ), {"v": vid, "a": ano, "m": mes, "x": val})
    db.limpar_cache()
    print("Gravado.")


if __name__ == "__main__":
    main("--aplicar" in sys.argv)
