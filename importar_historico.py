"""
importar_historico.py - Importa o histórico de Meta x Realizado extraído de
"metas passadas.pdf" (67 lançamentos, fev a jul/2026, 13 vendedores).

Observação: no PDF, "THAIS" (fev, mar, abr/2026) foi identificada como a mesma
pessoa que "TAIS BISPO" (mai, jun, jul/2026) — os registros dela já estão
consolidados sob o nome "Tais Bispo" abaixo.

Pré-requisito: os vendedores já devem estar cadastrados (rode primeiro, se
ainda não rodou, a importação em lote de vendedores na aba Cadastros, ou o
seed_data.py). Esse script casa os nomes pelo cadastro existente — se algum
nome não for encontrado, ele avisa e pula, sem travar a importação dos demais.

Execução:
    python importar_historico.py

Pode rodar mais de uma vez sem duplicar: cada (vendedor, ano, mês) é um
upsert — rodar de novo só atualiza os valores para os mesmos números.
"""
import db

# (nome, ano, mes, meta, realizado)
REGISTROS = [
    ("ADRIELY", 2026, 5, 115000.00, 119665.85),
    ("ADRIELY", 2026, 6, 125000.00, 133469.84),
    ("ADRIELY", 2026, 7, 130000.00, 164564.35),
    ("JADILA", 2026, 6, 80000.00, 99048.72),
    ("JADILA", 2026, 7, 105000.00, 164794.56),
    ("JESSE", 2026, 5, 160000.00, 143992.43),
    ("JESSE", 2026, 6, 145000.00, 264735.27),
    ("JESSE", 2026, 7, 170000.00, 196235.96),
    ("NAIARA", 2026, 5, 130000.00, 150897.07),
    ("NAIARA", 2026, 6, 120000.00, 169831.71),
    ("NAIARA", 2026, 7, 155000.00, 120994.24),
    ("POLIANE", 2026, 5, 115000.00, 82429.80),
    ("POLIANE", 2026, 6, 105000.00, 148295.25),
    ("POLIANE", 2026, 7, 130000.00, 177881.50),
    ("TAIS BISPO", 2026, 5, 160000.00, 205752.67),
    ("TAIS BISPO", 2026, 6, 145000.00, 210372.53),
    ("TAIS BISPO", 2026, 7, 170000.00, 225724.58),
    ("TAMIRES LARCEDA", 2026, 5, 160000.00, 235161.56),
    ("TAMIRES LARCEDA", 2026, 6, 145000.00, 274650.59),
    ("TAMIRES LARCEDA", 2026, 7, 170000.00, 269304.82),
    ("RENATA RIBEIRO", 2026, 5, 160000.00, 184359.02),
    ("RENATA RIBEIRO", 2026, 6, 145000.00, 220677.02),
    ("RENATA RIBEIRO", 2026, 7, 170000.00, 268738.83),
    ("ELIZANGELA BATISTA", 2026, 5, 130000.00, 77217.64),
    ("ELIZANGELA BATISTA", 2026, 6, 120000.00, 66050.63),
    ("ELIZANGELA BATISTA", 2026, 7, 155000.00, 107455.36),
    ("IGOR SILVA", 2026, 5, 115000.00, 109045.46),
    ("IGOR SILVA", 2026, 6, 105000.00, 129016.66),
    ("IGOR SILVA", 2026, 7, 130000.00, 134450.33),
    ("TAINA SANTOS", 2026, 5, 115000.00, 71417.44),
    ("TAINA SANTOS", 2026, 6, 105000.00, 109272.60),
    ("TAINA SANTOS", 2026, 7, 130000.00, 130264.97),
    ("FELIPE", 2026, 5, 90000.00, 104812.03),
    ("FELIPE", 2026, 6, 80000.00, 106789.69),
    ("FELIPE", 2026, 7, 105000.00, 70528.95),
    ("GLEICIA", 2026, 5, 90000.00, 130053.08),
    ("GLEICIA", 2026, 6, 80000.00, 110140.46),
    ("GLEICIA", 2026, 7, 105000.00, 115459.18),
    ("ELIZANGELA BATISTA", 2026, 3, 105000.00, 117923.44),
    ("FELIPE", 2026, 3, 80000.00, 122328.25),
    ("GLEICIA", 2026, 3, 80000.00, 109575.08),
    ("IGOR SILVA", 2026, 3, 80000.00, 109575.08),
    ("RENATA RIBEIRO", 2026, 3, 158000.00, 250420.55),
    ("TAINA SANTOS", 2026, 3, 145000.00, 120267.02),
    ("TAMIRES LARCEDA", 2026, 3, 120000.00, 285554.32),
    ("ELIZANGELA BATISTA", 2026, 4, 128000.00, 100507.88),
    ("FELIPE", 2026, 4, 85000.00, 105000.62),
    ("GLEICIA", 2026, 4, 85000.00, 115506.40),
    ("IGOR SILVA", 2026, 4, 85000.00, 143306.93),
    ("RENATA RIBEIRO", 2026, 4, 125000.00, 192879.76),
    ("TAINA SANTOS", 2026, 4, 85000.00, 97551.16),
    ("TAMIRES LARCEDA", 2026, 4, 145000.00, 213506.01),
    ("ADRIELY", 2026, 2, 60000.00, 128839.12),
    ("JESSE", 2026, 2, 100000.00, 153695.66),
    ("NAIARA", 2026, 2, 100000.00, 103488.47),
    ("POLIANE", 2026, 2, 85000.00, 102216.95),
    ("TAIS BISPO", 2026, 2, 100000.00, 201043.46),  # "THAIS" no PDF
    ("ADRIELY", 2026, 3, 80000.00, 99666.87),
    ("JESSE", 2026, 3, 158000.00, 170902.85),
    ("NAIARA", 2026, 3, 105000.00, 148665.03),
    ("POLIANE", 2026, 3, 95000.00, 131475.59),
    ("TAIS BISPO", 2026, 3, 120000.00, 225187.07),  # "THAIS" no PDF
    ("ADRIELY", 2026, 4, 85000.00, 120666.13),
    ("JESSE", 2026, 4, 125000.00, 167515.27),
    ("NAIARA", 2026, 4, 128000.00, 188166.81),
    ("POLIANE", 2026, 4, 105000.00, 34670.91),
    ("TAIS BISPO", 2026, 4, 145000.00, 193435.96),  # "THAIS" no PDF
]


def importar():
    db.init_db()
    vendedores = db.get_vendedores()
    nomes_ids = {row["nome"].strip().lower(): int(row["id"]) for _, row in vendedores.iterrows()}

    importados = 0
    nao_encontrados = set()

    for nome, ano, mes, meta, realizado in REGISTROS:
        vendedor_id = nomes_ids.get(nome.strip().lower())
        if vendedor_id is None:
            nao_encontrados.add(nome)
            continue
        db.upsert_meta(vendedor_id, ano, mes, meta)
        db.upsert_realizado_mensal(vendedor_id, ano, mes, realizado)
        importados += 1

    print(f"Importação concluída: {importados} de {len(REGISTROS)} registros gravados.")
    if nao_encontrados:
        print("Nomes não encontrados no cadastro (nada foi gravado para eles):")
        for nome in sorted(nao_encontrados):
            print(f"  - {nome}")
        print("Cadastre-os na aba Cadastros e rode este script de novo para completar.")


if __name__ == "__main__":
    importar()