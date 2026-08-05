"""
importar_vendas_agosto.py - Importa os lançamentos diários de vendas de
agosto/2026 (26 registros, sem quantidade de clientes atendidos).

Usa db.upsert_venda_valor(), que só grava o valor vendido do dia e NÃO mexe na
quantidade de clientes já lançada para aquele vendedor/dia (se o dia ainda não
existir, entra com 0 clientes — lance depois em "👥 Lançar clientes atendidos
por dia" no app).

Pré-requisito: os vendedores já devem estar cadastrados.

Execução:
    python importar_vendas_agosto.py

Pode rodar mais de uma vez sem duplicar: cada (vendedor, data) é um upsert —
rodar de novo só atualiza o valor para o mesmo número.
"""
import db

# (nome, dia, mes, ano, valor)
REGISTROS = [
    ("TAINA SANTOS", 1, 8, 2026, 2388.17),
    ("TAINA SANTOS", 3, 8, 2026, 2404.13),
    ("POLIANE", 1, 8, 2026, 3079.39),
    ("GLEICIA", 3, 8, 2026, 3303.49),
    ("IGOR SILVA", 3, 8, 2026, 3820.99),
    ("IGOR SILVA", 1, 8, 2026, 3897.73),
    ("TAIS BISPO", 1, 8, 2026, 4461.21),
    ("ADRIELY", 1, 8, 2026, 5537.24),
    ("ELIZANGELA BATISTA", 1, 8, 2026, 5694.94),
    ("JADILA", 3, 8, 2026, 6028.83),
    ("JESSE", 1, 8, 2026, 6032.89),
    ("ELIZANGELA BATISTA", 3, 8, 2026, 6220.39),
    ("GLEICIA", 1, 8, 2026, 6407.85),
    ("NAIARA", 1, 8, 2026, 6585.02),
    ("FELIPE", 1, 8, 2026, 6633.81),
    ("POLIANE", 3, 8, 2026, 7251.50),
    ("NAIARA", 3, 8, 2026, 7475.03),
    ("TAMIRES LARCEDA", 3, 8, 2026, 7986.33),
    ("JADILA", 1, 8, 2026, 8194.62),
    ("RENATA RIBEIRO", 1, 8, 2026, 8741.57),
    ("ADRIELY", 3, 8, 2026, 8892.58),
    ("FELIPE", 3, 8, 2026, 10178.60),
    ("JESSE", 3, 8, 2026, 10696.55),
    ("RENATA RIBEIRO", 3, 8, 2026, 13561.72),
    ("TAMIRES LARCEDA", 1, 8, 2026, 14634.62),
    ("TAIS BISPO", 3, 8, 2026, 16119.23),
]


def importar():
    from datetime import date

    db.init_db()
    vendedores = db.get_vendedores()
    nomes_ids = {row["nome"].strip().lower(): int(row["id"]) for _, row in vendedores.iterrows()}

    importados = 0
    nao_encontrados = set()

    for nome, dia, mes, ano, valor in REGISTROS:
        vendedor_id = nomes_ids.get(nome.strip().lower())
        if vendedor_id is None:
            nao_encontrados.add(nome)
            continue
        db.upsert_venda_valor(vendedor_id, date(ano, mes, dia), valor)
        importados += 1

    print(f"Importação concluída: {importados} de {len(REGISTROS)} registros gravados.")
    if nao_encontrados:
        print("Nomes não encontrados no cadastro (nada foi gravado para eles):")
        for nome in sorted(nao_encontrados):
            print(f"  - {nome}")
        print("Cadastre-os na aba Cadastros e rode este script de novo para completar.")


if __name__ == "__main__":
    importar()
