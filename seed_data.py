"""
seed_data.py - Popula o banco PostgreSQL com dados fictícios para demonstração.

Cria 5 vendedores (3 na Porteira, 2 na Casa de Adubo), metas para o mês
passado e o mês atual, e lançamentos diários fictícios de vendas/pedidos,
para que o dashboard já abra com dados visíveis.

Pré-requisito: DATABASE_URL configurada (via .env local — veja .env.example).

Execução:
    python seed_data.py

Se o banco já tiver vendedores cadastrados, o script não faz nada (para
evitar duplicar dados). Para recriar do zero, apague as linhas das tabelas
vendedores/metas/vendas_diarias no Supabase (ou rode um TRUNCATE) e rode de novo.
"""
import calendar
import random
from datetime import date

import db

random.seed(42)


def mes_anterior(ano, mes):
    if mes == 1:
        return ano - 1, 12
    return ano, mes - 1


def gerar_vendas_mes(vendedores, ano, mes, ultimo_dia, hoje):
    for _, v in vendedores.iterrows():
        for dia in range(1, ultimo_dia + 1):
            data_ref = date(ano, mes, dia)
            if data_ref.weekday() == 6:  # domingo, loja fechada
                continue
            if data_ref > hoje:
                continue
            valor = round(random.uniform(1500, 4500), 2)
            pedidos = random.randint(5, 25)
            db.upsert_venda(v["id"], data_ref, valor, pedidos)


def seed():
    db.init_db()

    vendedores_seed = [
        ("Carlos Eduardo Silva", "Porteira"),
        ("Fernanda Lima Souza", "Porteira"),
        ("Ricardo Alves Pereira", "Porteira"),
        ("Juliana Costa Rocha", "Casa de Adubo"),
        ("Marcos Vinicius Teixeira", "Casa de Adubo"),
    ]

    existentes = db.get_vendedores()
    if not existentes.empty:
        print("O banco já contém vendedores. Seed cancelado para evitar duplicidade.")
        print("Limpe as tabelas no Neon se quiser recriar os dados do zero.")
        return

    for nome, loja in vendedores_seed:
        db.add_vendedor(nome, loja)

    vendedores = db.get_vendedores()

    hoje = date.today()
    ano_atual, mes_atual = hoje.year, hoje.month
    ano_passado, mes_passado = mes_anterior(ano_atual, mes_atual)

    metas_base = {"Porteira": 80000.0, "Casa de Adubo": 60000.0}

    for _, v in vendedores.iterrows():
        base = metas_base[v["loja"]]
        meta_passado = round(base * random.uniform(0.9, 1.1), 2)
        meta_atual = round(base * random.uniform(0.95, 1.15), 2)
        db.upsert_meta(v["id"], ano_passado, mes_passado, meta_passado)
        db.upsert_meta(v["id"], ano_atual, mes_atual, meta_atual)

    ultimo_dia_passado = calendar.monthrange(ano_passado, mes_passado)[1]
    gerar_vendas_mes(vendedores, ano_passado, mes_passado, ultimo_dia_passado, hoje)

    ultimo_dia_atual = hoje.day
    gerar_vendas_mes(vendedores, ano_atual, mes_atual, ultimo_dia_atual, hoje)

    print("Seed concluído com sucesso!")
    print(f"- {len(vendedores_seed)} vendedores cadastrados")
    print(f"- Metas lançadas para {db.MESES_PT[mes_passado]}/{ano_passado} e {db.MESES_PT[mes_atual]}/{ano_atual}")
    print("- Lançamentos diários fictícios gerados para ambos os meses")


if __name__ == "__main__":
    seed()