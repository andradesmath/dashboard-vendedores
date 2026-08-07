"""
db.py - Camada de dados (PostgreSQL) do Dashboard de Desempenho de Vendedores.

Responsável por: conexão com o banco, schema, CRUD de vendedores/metas/vendas
diárias e regras de negócio (dias úteis, run rate, semáforo de atingimento).

Banco: PostgreSQL (ex.: Supabase, plano gratuito). A string de conexão é lida,
nesta ordem de prioridade, de:
    1) variável de ambiente DATABASE_URL (ex.: via arquivo .env local)
    2) st.secrets["DATABASE_URL"] (usado no deploy no Streamlit Community Cloud)

Este módulo não depende de estar rodando dentro de `streamlit run` — pode ser
importado por scripts de linha de comando como seed_data.py.
"""
import os
import calendar
from datetime import date, timedelta

import pandas as pd
from sqlalchemy import create_engine, text

# Carrega variáveis de um arquivo .env local, se existir (não falha se python-dotenv
# não estiver instalado ou o arquivo não existir).
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

LOJAS = ["Porteira", "Casa de Adubo"]
DIAS_UTEIS_PADRAO = 24  # segunda a sábado; usuário pode ajustar no dashboard (feriados)

MESES_PT = {
    1: "Janeiro", 2: "Fevereiro", 3: "Março", 4: "Abril", 5: "Maio", 6: "Junho",
    7: "Julho", 8: "Agosto", 9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro",
}

MODALIDADES_PAGAMENTO = [
    "Crédito", "Débito", "Dinheiro", "Pix", "Boleto", "Nota Promissória", "Voucher", "Depósito",
]
COLUNAS_PAGAMENTO = {
    "Crédito": "pct_credito",
    "Débito": "pct_debito",
    "Dinheiro": "pct_dinheiro",
    "Pix": "pct_pix",
    "Boleto": "pct_boleto",
    "Nota Promissória": "pct_nota_promissoria",
    "Voucher": "pct_voucher",
    "Depósito": "pct_deposito",
}


def get_database_url():
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    try:
        import streamlit as st
        if "DATABASE_URL" in st.secrets:
            return st.secrets["DATABASE_URL"]
    except Exception:
        pass
    raise RuntimeError(
        "DATABASE_URL não configurada. Defina a variável de ambiente DATABASE_URL "
        "(veja .env.example) para rodar localmente, ou configure em "
        "st.secrets['DATABASE_URL'] nas 'Secrets' do app no Streamlit Community Cloud."
    )


_engine = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(get_database_url(), pool_pre_ping=True)
    return _engine


def init_db():
    # Migracoes de compatibilidade com bancos criados antes de renomear
    # "clientes atendidos" para "pedidos" (roda antes das CREATE TABLE para
    # nao colidir com a tabela ja existente no nome antigo).
    migracoes_pre = [
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'clientes_mensal_manual')
               AND NOT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'pedidos_mensal_manual') THEN
                ALTER TABLE clientes_mensal_manual RENAME TO pedidos_mensal_manual;
            END IF;
        END $$;
        """,
    ]

    ddl = [
        """
        CREATE TABLE IF NOT EXISTS vendedores (
            id SERIAL PRIMARY KEY,
            nome TEXT NOT NULL,
            loja TEXT NOT NULL CHECK (loja IN ('Porteira', 'Casa de Adubo')),
            ativo BOOLEAN NOT NULL DEFAULT TRUE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS metas (
            id SERIAL PRIMARY KEY,
            vendedor_id INTEGER NOT NULL REFERENCES vendedores(id) ON DELETE CASCADE,
            ano INTEGER NOT NULL,
            mes INTEGER NOT NULL,
            valor_meta NUMERIC(14, 2) NOT NULL DEFAULT 0,
            UNIQUE (vendedor_id, ano, mes)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS vendas_diarias (
            id SERIAL PRIMARY KEY,
            vendedor_id INTEGER NOT NULL REFERENCES vendedores(id) ON DELETE CASCADE,
            data DATE NOT NULL,
            valor_realizado NUMERIC(14, 2) NOT NULL DEFAULT 0,
            qtd_pedidos INTEGER NOT NULL DEFAULT 0,
            UNIQUE (vendedor_id, data)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS realizado_mensal_manual (
            id SERIAL PRIMARY KEY,
            vendedor_id INTEGER NOT NULL REFERENCES vendedores(id) ON DELETE CASCADE,
            ano INTEGER NOT NULL,
            mes INTEGER NOT NULL,
            valor_realizado NUMERIC(14, 2) NOT NULL DEFAULT 0,
            UNIQUE (vendedor_id, ano, mes)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS pedidos_mensal_manual (
            id SERIAL PRIMARY KEY,
            vendedor_id INTEGER NOT NULL REFERENCES vendedores(id) ON DELETE CASCADE,
            ano INTEGER NOT NULL,
            mes INTEGER NOT NULL,
            qtd_pedidos INTEGER NOT NULL DEFAULT 0,
            UNIQUE (vendedor_id, ano, mes)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS pagamentos_diarios (
            id SERIAL PRIMARY KEY,
            vendedor_id INTEGER NOT NULL REFERENCES vendedores(id) ON DELETE CASCADE,
            data DATE NOT NULL,
            pct_credito NUMERIC(5, 2) NOT NULL DEFAULT 0,
            pct_debito NUMERIC(5, 2) NOT NULL DEFAULT 0,
            pct_dinheiro NUMERIC(5, 2) NOT NULL DEFAULT 0,
            pct_pix NUMERIC(5, 2) NOT NULL DEFAULT 0,
            pct_boleto NUMERIC(5, 2) NOT NULL DEFAULT 0,
            pct_nota_promissoria NUMERIC(5, 2) NOT NULL DEFAULT 0,
            pct_voucher NUMERIC(5, 2) NOT NULL DEFAULT 0,
            pct_deposito NUMERIC(5, 2) NOT NULL DEFAULT 0,
            UNIQUE (vendedor_id, data)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS pagamentos_mensal_manual (
            id SERIAL PRIMARY KEY,
            vendedor_id INTEGER NOT NULL REFERENCES vendedores(id) ON DELETE CASCADE,
            ano INTEGER NOT NULL,
            mes INTEGER NOT NULL,
            pct_credito NUMERIC(5, 2) NOT NULL DEFAULT 0,
            pct_debito NUMERIC(5, 2) NOT NULL DEFAULT 0,
            pct_dinheiro NUMERIC(5, 2) NOT NULL DEFAULT 0,
            pct_pix NUMERIC(5, 2) NOT NULL DEFAULT 0,
            pct_boleto NUMERIC(5, 2) NOT NULL DEFAULT 0,
            pct_nota_promissoria NUMERIC(5, 2) NOT NULL DEFAULT 0,
            pct_voucher NUMERIC(5, 2) NOT NULL DEFAULT 0,
            pct_deposito NUMERIC(5, 2) NOT NULL DEFAULT 0,
            UNIQUE (vendedor_id, ano, mes)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS inadimplencia_mensal (
            id SERIAL PRIMARY KEY,
            vendedor_id INTEGER NOT NULL REFERENCES vendedores(id) ON DELETE CASCADE,
            ano_venda INTEGER NOT NULL,
            mes_venda INTEGER NOT NULL,
            valor_em_aberto NUMERIC(14, 2) NOT NULL DEFAULT 0,
            UNIQUE (vendedor_id, ano_venda, mes_venda)
        )
        """,
    ]
    with get_engine().begin() as conn:
        for stmt in migracoes_pre:
            conn.execute(text(stmt))
        for stmt in ddl:
            conn.execute(text(stmt))
        # Migração: remove a coluna email de bancos criados com a versão anterior do schema.
        conn.execute(text("ALTER TABLE vendedores DROP COLUMN IF EXISTS email"))
        # Migração: adiciona a modalidade "Depósito" em bancos criados antes dela existir.
        conn.execute(text(
            "ALTER TABLE pagamentos_diarios ADD COLUMN IF NOT EXISTS pct_deposito NUMERIC(5, 2) NOT NULL DEFAULT 0"
        ))
        conn.execute(text(
            "ALTER TABLE pagamentos_mensal_manual ADD COLUMN IF NOT EXISTS pct_deposito NUMERIC(5, 2) NOT NULL DEFAULT 0"
        ))
        # Migração: renomeia qtd_clientes -> qtd_pedidos nas tabelas que ainda tiverem
        # a coluna com o nome antigo (bancos criados antes da renomeação para "pedidos").
        conn.execute(text(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'vendas_diarias' AND column_name = 'qtd_clientes'
                ) THEN
                    ALTER TABLE vendas_diarias RENAME COLUMN qtd_clientes TO qtd_pedidos;
                END IF;
            END $$;
            """
        ))
        conn.execute(text(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'pedidos_mensal_manual' AND column_name = 'qtd_clientes'
                ) THEN
                    ALTER TABLE pedidos_mensal_manual RENAME COLUMN qtd_clientes TO qtd_pedidos;
                END IF;
            END $$;
            """
        ))


# --------------------------------------------------------------------------
# Vendedores
# --------------------------------------------------------------------------
def add_vendedor(nome, loja):
    with get_engine().begin() as conn:
        conn.execute(
            text("INSERT INTO vendedores (nome, loja) VALUES (:nome, :loja)"),
            {"nome": nome, "loja": loja},
        )


def update_vendedor(vendedor_id, nome, loja, ativo=True):
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "UPDATE vendedores SET nome=:nome, loja=:loja, ativo=:ativo WHERE id=:id"
            ),
            {"nome": nome, "loja": loja, "ativo": bool(ativo), "id": vendedor_id},
        )


def delete_vendedor(vendedor_id):
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM vendedores WHERE id=:id"), {"id": vendedor_id})


def get_vendedores(loja=None, apenas_ativos=False):
    query = "SELECT * FROM vendedores WHERE 1=1"
    params = {}
    if loja and loja != "Ambas":
        query += " AND loja = :loja"
        params["loja"] = loja
    if apenas_ativos:
        query += " AND ativo = TRUE"
    query += " ORDER BY nome"
    return pd.read_sql_query(text(query), get_engine(), params=params)


def get_vendedor(vendedor_id):
    df = pd.read_sql_query(
        text("SELECT * FROM vendedores WHERE id = :id"), get_engine(), params={"id": vendedor_id}
    )
    return df.iloc[0] if not df.empty else None


# --------------------------------------------------------------------------
# Metas
# --------------------------------------------------------------------------
def upsert_meta(vendedor_id, ano, mes, valor_meta):
    with get_engine().begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO metas (vendedor_id, ano, mes, valor_meta)
                VALUES (:vendedor_id, :ano, :mes, :valor_meta)
                ON CONFLICT (vendedor_id, ano, mes)
                DO UPDATE SET valor_meta = EXCLUDED.valor_meta
                """
            ),
            {"vendedor_id": vendedor_id, "ano": ano, "mes": mes, "valor_meta": valor_meta},
        )


def get_meta_vendedor(vendedor_id, ano, mes):
    df = pd.read_sql_query(
        text("SELECT valor_meta FROM metas WHERE vendedor_id=:vid AND ano=:ano AND mes=:mes"),
        get_engine(),
        params={"vid": vendedor_id, "ano": ano, "mes": mes},
    )
    return float(df.iloc[0]["valor_meta"]) if not df.empty else 0.0


def get_metas_historico(loja=None):
    query = """
        SELECT m.id, v.id as vendedor_id, v.nome, v.loja, m.ano, m.mes, m.valor_meta
        FROM metas m JOIN vendedores v ON v.id = m.vendedor_id
        WHERE 1=1
    """
    params = {}
    if loja and loja != "Ambas":
        query += " AND v.loja = :loja"
        params["loja"] = loja
    query += " ORDER BY m.ano DESC, m.mes DESC, v.nome"
    return pd.read_sql_query(text(query), get_engine(), params=params)


def get_historico_meta_realizado(loja=None):
    """Histórico por vendedor/mês combinando meta lançada com o realizado somado a
    partir das vendas diárias + realizado manual importado (inclui meses com venda
    mas sem meta lançada e vice-versa)."""
    query = """
        WITH venda_agg AS (
            SELECT vendedor_id,
                   EXTRACT(YEAR FROM data)::int AS ano,
                   EXTRACT(MONTH FROM data)::int AS mes,
                   SUM(valor_realizado) AS realizado_diario,
                   SUM(qtd_pedidos) AS pedidos_diario
            FROM vendas_diarias
            GROUP BY vendedor_id, EXTRACT(YEAR FROM data), EXTRACT(MONTH FROM data)
        ),
        combinado AS (
            SELECT
                COALESCE(m.vendedor_id, va.vendedor_id, rm.vendedor_id, cm.vendedor_id) AS vendedor_id,
                COALESCE(m.ano, va.ano, rm.ano, cm.ano) AS ano,
                COALESCE(m.mes, va.mes, rm.mes, cm.mes) AS mes,
                COALESCE(m.valor_meta, 0) AS valor_meta,
                COALESCE(va.realizado_diario, 0) + COALESCE(rm.valor_realizado, 0) AS realizado,
                COALESCE(va.pedidos_diario, 0) + COALESCE(cm.qtd_pedidos, 0) AS pedidos
            FROM metas m
            FULL OUTER JOIN venda_agg va
              ON m.vendedor_id = va.vendedor_id AND m.ano = va.ano AND m.mes = va.mes
            FULL OUTER JOIN realizado_mensal_manual rm
              ON COALESCE(m.vendedor_id, va.vendedor_id) = rm.vendedor_id
             AND COALESCE(m.ano, va.ano) = rm.ano
             AND COALESCE(m.mes, va.mes) = rm.mes
            FULL OUTER JOIN pedidos_mensal_manual cm
              ON COALESCE(m.vendedor_id, va.vendedor_id, rm.vendedor_id) = cm.vendedor_id
             AND COALESCE(m.ano, va.ano, rm.ano) = cm.ano
             AND COALESCE(m.mes, va.mes, rm.mes) = cm.mes
        )
        SELECT c.ano, c.mes, c.valor_meta, c.realizado, c.pedidos,
               v.id AS vendedor_id, v.nome, v.loja
        FROM combinado c
        JOIN vendedores v ON v.id = c.vendedor_id
        WHERE 1=1
    """
    params = {}
    if loja and loja != "Ambas":
        query += " AND v.loja = :loja"
        params["loja"] = loja
    query += " ORDER BY c.ano DESC, c.mes DESC, v.nome"
    df = pd.read_sql_query(text(query), get_engine(), params=params)
    if not df.empty:
        df["valor_meta"] = df["valor_meta"].astype(float)
        df["realizado"] = df["realizado"].astype(float)
        df["pedidos"] = df["pedidos"].astype(int)
        df["ano"] = df["ano"].astype(int)
        df["mes"] = df["mes"].astype(int)
    return df


def get_metas_mes(ano, mes, loja=None):
    """Uma linha por vendedor ativo, com valor_meta = 0 quando não houver meta lançada."""
    query = """
        SELECT v.id as vendedor_id, v.nome, v.loja, COALESCE(m.valor_meta, 0) as valor_meta
        FROM vendedores v
        LEFT JOIN metas m ON m.vendedor_id = v.id AND m.ano = :ano AND m.mes = :mes
        WHERE v.ativo = TRUE
    """
    params = {"ano": ano, "mes": mes}
    if loja and loja != "Ambas":
        query += " AND v.loja = :loja"
        params["loja"] = loja
    query += " ORDER BY v.nome"
    df = pd.read_sql_query(text(query), get_engine(), params=params)
    if not df.empty:
        df["valor_meta"] = df["valor_meta"].astype(float)
    return df


# --------------------------------------------------------------------------
# Vendas diárias
# --------------------------------------------------------------------------
def upsert_venda(vendedor_id, data_venda, valor_realizado, qtd_pedidos):
    with get_engine().begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO vendas_diarias (vendedor_id, data, valor_realizado, qtd_pedidos)
                VALUES (:vendedor_id, :data, :valor_realizado, :qtd_pedidos)
                ON CONFLICT (vendedor_id, data)
                DO UPDATE SET valor_realizado = EXCLUDED.valor_realizado,
                              qtd_pedidos = EXCLUDED.qtd_pedidos
                """
            ),
            {
                "vendedor_id": vendedor_id,
                "data": data_venda,
                "valor_realizado": valor_realizado,
                "qtd_pedidos": qtd_pedidos,
            },
        )


def upsert_venda_valor(vendedor_id, data_venda, valor_realizado):
    """Lança/atualiza só o valor vendido do dia, sem mexer na quantidade de pedidos
    já registrada (se o dia ainda não existir, cria com 0 pedidos)."""
    data_str = data_venda.isoformat() if hasattr(data_venda, "isoformat") else data_venda
    with get_engine().begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO vendas_diarias (vendedor_id, data, valor_realizado, qtd_pedidos)
                VALUES (:vendedor_id, :data, :valor_realizado, 0)
                ON CONFLICT (vendedor_id, data)
                DO UPDATE SET valor_realizado = EXCLUDED.valor_realizado
                """
            ),
            {"vendedor_id": vendedor_id, "data": data_str, "valor_realizado": valor_realizado},
        )


def upsert_pedidos_dia(vendedor_id, data_venda, qtd_pedidos):
    """Lança/atualiza só a quantidade de pedidos no dia, sem mexer no valor
    vendido já registrado (se o dia ainda não existir, cria com valor R$ 0)."""
    data_str = data_venda.isoformat() if hasattr(data_venda, "isoformat") else data_venda
    with get_engine().begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO vendas_diarias (vendedor_id, data, valor_realizado, qtd_pedidos)
                VALUES (:vendedor_id, :data, 0, :qtd_pedidos)
                ON CONFLICT (vendedor_id, data)
                DO UPDATE SET qtd_pedidos = EXCLUDED.qtd_pedidos
                """
            ),
            {"vendedor_id": vendedor_id, "data": data_str, "qtd_pedidos": qtd_pedidos},
        )


def delete_venda(venda_id):
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM vendas_diarias WHERE id=:id"), {"id": venda_id})


def get_vendas_mes(ano, mes, loja=None):
    ini = date(ano, mes, 1)
    ultimo_dia = calendar.monthrange(ano, mes)[1]
    fim = date(ano, mes, ultimo_dia)
    query = """
        SELECT vd.id, v.id as vendedor_id, v.nome, v.loja, vd.data, vd.valor_realizado, vd.qtd_pedidos
        FROM vendas_diarias vd JOIN vendedores v ON v.id = vd.vendedor_id
        WHERE vd.data BETWEEN :ini AND :fim
    """
    params = {"ini": ini, "fim": fim}
    if loja and loja != "Ambas":
        query += " AND v.loja = :loja"
        params["loja"] = loja
    query += " ORDER BY vd.data"
    df = pd.read_sql_query(text(query), get_engine(), params=params)
    if not df.empty:
        df["data"] = pd.to_datetime(df["data"]).dt.date
        df["valor_realizado"] = df["valor_realizado"].astype(float)
    return df


def get_vendas_vendedor_mes(vendedor_id, ano, mes):
    ini = date(ano, mes, 1)
    ultimo_dia = calendar.monthrange(ano, mes)[1]
    fim = date(ano, mes, ultimo_dia)
    df = pd.read_sql_query(
        text(
            "SELECT data, valor_realizado, qtd_pedidos FROM vendas_diarias "
            "WHERE vendedor_id=:vid AND data BETWEEN :ini AND :fim ORDER BY data"
        ),
        get_engine(),
        params={"vid": vendedor_id, "ini": ini, "fim": fim},
    )
    if not df.empty:
        df["data"] = pd.to_datetime(df["data"]).dt.date
        df["valor_realizado"] = df["valor_realizado"].astype(float)
    return df


def get_lancamentos_recentes(limite=30, loja=None):
    query = """
        SELECT vd.id, v.nome, v.loja, vd.data, vd.valor_realizado, vd.qtd_pedidos
        FROM vendas_diarias vd JOIN vendedores v ON v.id = vd.vendedor_id
        WHERE 1=1
    """
    params = {"limite": limite}
    if loja and loja != "Ambas":
        query += " AND v.loja = :loja"
        params["loja"] = loja
    query += " ORDER BY vd.data DESC, vd.id DESC LIMIT :limite"
    return pd.read_sql_query(text(query), get_engine(), params=params)


# --------------------------------------------------------------------------
# Realizado mensal manual (para importar histórico de meses sem lançamento
# diário — ex.: dados de antes deste sistema, importados de uma planilha/PDF).
# Esse valor é somado ao realizado diário nos totais e no histórico, mas não
# entra nos gráficos que dependem de granularidade por dia (evolução diária,
# mapa de calor por dia da semana) nem na contagem de pedidos.
# --------------------------------------------------------------------------
def upsert_realizado_mensal(vendedor_id, ano, mes, valor_realizado):
    with get_engine().begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO realizado_mensal_manual (vendedor_id, ano, mes, valor_realizado)
                VALUES (:vendedor_id, :ano, :mes, :valor_realizado)
                ON CONFLICT (vendedor_id, ano, mes)
                DO UPDATE SET valor_realizado = EXCLUDED.valor_realizado
                """
            ),
            {"vendedor_id": vendedor_id, "ano": ano, "mes": mes, "valor_realizado": valor_realizado},
        )


def get_realizado_manual_mes(ano, mes, loja=None):
    query = """
        SELECT rm.vendedor_id, v.nome, v.loja, rm.valor_realizado
        FROM realizado_mensal_manual rm
        JOIN vendedores v ON v.id = rm.vendedor_id
        WHERE rm.ano = :ano AND rm.mes = :mes
    """
    params = {"ano": ano, "mes": mes}
    if loja and loja != "Ambas":
        query += " AND v.loja = :loja"
        params["loja"] = loja
    df = pd.read_sql_query(text(query), get_engine(), params=params)
    if not df.empty:
        df["valor_realizado"] = df["valor_realizado"].astype(float)
    return df


def get_realizado_manual_vendedor(vendedor_id, ano, mes):
    df = pd.read_sql_query(
        text(
            "SELECT valor_realizado FROM realizado_mensal_manual "
            "WHERE vendedor_id=:vid AND ano=:ano AND mes=:mes"
        ),
        get_engine(),
        params={"vid": vendedor_id, "ano": ano, "mes": mes},
    )
    return float(df.iloc[0]["valor_realizado"]) if not df.empty else 0.0


# --------------------------------------------------------------------------
# Pedidos - lançamento mensal manual (quando só se sabe o total do
# mês, sem quebra diária). Mesma lógica do realizado manual, mas para pedidos.
# --------------------------------------------------------------------------
def upsert_pedidos_mensal(vendedor_id, ano, mes, qtd_pedidos):
    with get_engine().begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO pedidos_mensal_manual (vendedor_id, ano, mes, qtd_pedidos)
                VALUES (:vendedor_id, :ano, :mes, :qtd_pedidos)
                ON CONFLICT (vendedor_id, ano, mes)
                DO UPDATE SET qtd_pedidos = EXCLUDED.qtd_pedidos
                """
            ),
            {"vendedor_id": vendedor_id, "ano": ano, "mes": mes, "qtd_pedidos": qtd_pedidos},
        )


def get_pedidos_manual_mes(ano, mes, loja=None):
    query = """
        SELECT cm.vendedor_id, v.nome, v.loja, cm.qtd_pedidos
        FROM pedidos_mensal_manual cm
        JOIN vendedores v ON v.id = cm.vendedor_id
        WHERE cm.ano = :ano AND cm.mes = :mes
    """
    params = {"ano": ano, "mes": mes}
    if loja and loja != "Ambas":
        query += " AND v.loja = :loja"
        params["loja"] = loja
    df = pd.read_sql_query(text(query), get_engine(), params=params)
    if not df.empty:
        df["qtd_pedidos"] = df["qtd_pedidos"].astype(int)
    return df


def get_pedidos_manual_vendedor(vendedor_id, ano, mes):
    df = pd.read_sql_query(
        text(
            "SELECT qtd_pedidos FROM pedidos_mensal_manual "
            "WHERE vendedor_id=:vid AND ano=:ano AND mes=:mes"
        ),
        get_engine(),
        params={"vid": vendedor_id, "ano": ano, "mes": mes},
    )
    return int(df.iloc[0]["qtd_pedidos"]) if not df.empty else 0


# --------------------------------------------------------------------------
# Mix de pagamento (% por modalidade) - lançamento diário e mensal manual.
# Guarda só os percentuais; o valor em R$ por modalidade é obtido cruzando com
# o realizado já lançado (vendas_diarias para o diário, realizado_mensal_manual
# para o manual) no momento de montar os indicadores, nunca duplicando dado.
# --------------------------------------------------------------------------
def upsert_pagamento_dia(vendedor_id, data_venda, percentuais):
    """percentuais: dict {modalidade (label de MODALIDADES_PAGAMENTO): valor percentual}."""
    data_str = data_venda.isoformat() if hasattr(data_venda, "isoformat") else data_venda
    colunas = list(COLUNAS_PAGAMENTO.values())
    params = {"vendedor_id": vendedor_id, "data": data_str}
    for modalidade, coluna in COLUNAS_PAGAMENTO.items():
        params[coluna] = float(percentuais.get(modalidade, 0.0))
    with get_engine().begin() as conn:
        conn.execute(
            text(
                f"""
                INSERT INTO pagamentos_diarios (vendedor_id, data, {", ".join(colunas)})
                VALUES (:vendedor_id, :data, {", ".join(":" + c for c in colunas)})
                ON CONFLICT (vendedor_id, data)
                DO UPDATE SET {", ".join(f"{c} = EXCLUDED.{c}" for c in colunas)}
                """
            ),
            params,
        )


def get_pagamento_dia_vendedor(vendedor_id, data_venda):
    """Retorna o dict de percentuais já lançados para o vendedor/dia, ou None."""
    data_str = data_venda.isoformat() if hasattr(data_venda, "isoformat") else data_venda
    colunas = list(COLUNAS_PAGAMENTO.values())
    df = pd.read_sql_query(
        text(f"SELECT {', '.join(colunas)} FROM pagamentos_diarios WHERE vendedor_id=:vid AND data=:data"),
        get_engine(),
        params={"vid": vendedor_id, "data": data_str},
    )
    if df.empty:
        return None
    row = df.iloc[0]
    return {modalidade: float(row[coluna]) for modalidade, coluna in COLUNAS_PAGAMENTO.items()}


def upsert_pagamento_mensal(vendedor_id, ano, mes, percentuais):
    """percentuais: dict {modalidade (label de MODALIDADES_PAGAMENTO): valor percentual}."""
    colunas = list(COLUNAS_PAGAMENTO.values())
    params = {"vendedor_id": vendedor_id, "ano": ano, "mes": mes}
    for modalidade, coluna in COLUNAS_PAGAMENTO.items():
        params[coluna] = float(percentuais.get(modalidade, 0.0))
    with get_engine().begin() as conn:
        conn.execute(
            text(
                f"""
                INSERT INTO pagamentos_mensal_manual (vendedor_id, ano, mes, {", ".join(colunas)})
                VALUES (:vendedor_id, :ano, :mes, {", ".join(":" + c for c in colunas)})
                ON CONFLICT (vendedor_id, ano, mes)
                DO UPDATE SET {", ".join(f"{c} = EXCLUDED.{c}" for c in colunas)}
                """
            ),
            params,
        )


def get_pagamento_manual_vendedor(vendedor_id, ano, mes):
    """Retorna o dict de percentuais já lançados (manual mensal) para o vendedor/mês, ou None."""
    colunas = list(COLUNAS_PAGAMENTO.values())
    df = pd.read_sql_query(
        text(
            f"SELECT {', '.join(colunas)} FROM pagamentos_mensal_manual "
            "WHERE vendedor_id=:vid AND ano=:ano AND mes=:mes"
        ),
        get_engine(),
        params={"vid": vendedor_id, "ano": ano, "mes": mes},
    )
    if df.empty:
        return None
    row = df.iloc[0]
    return {modalidade: float(row[coluna]) for modalidade, coluna in COLUNAS_PAGAMENTO.items()}


def _mix_diario_vendedor_periodo(vendedor_id, ini=None, fim=None):
    """Soma, por modalidade, o valor em R$ coberto por lançamentos DIÁRIOS de mix no
    período (ou em todo o histórico, se ini/fim não informados). Retorna (valores_dict,
    total_coberto)."""
    colunas = list(COLUNAS_PAGAMENTO.values())
    query = f"""
        SELECT vd.valor_realizado, {", ".join("pg." + c for c in colunas)}
        FROM pagamentos_diarios pg
        JOIN vendas_diarias vd ON vd.vendedor_id = pg.vendedor_id AND vd.data = pg.data
        WHERE pg.vendedor_id = :vid
    """
    params = {"vid": vendedor_id}
    if ini is not None and fim is not None:
        query += " AND pg.data BETWEEN :ini AND :fim"
        params["ini"] = ini
        params["fim"] = fim
    dia_df = pd.read_sql_query(text(query), get_engine(), params=params)

    valores = {m: 0.0 for m in MODALIDADES_PAGAMENTO}
    if not dia_df.empty:
        for _, r in dia_df.iterrows():
            for modalidade, coluna in COLUNAS_PAGAMENTO.items():
                valores[modalidade] += float(r["valor_realizado"]) * float(r[coluna]) / 100.0
    total_coberto = float(dia_df["valor_realizado"].sum()) if not dia_df.empty else 0.0
    return valores, total_coberto


def get_mix_pagamento_vendedor_mes(vendedor_id, ano, mes):
    """Mix de pagamento (R$ por modalidade) de um vendedor num mês específico.
    Usa primeiro o mix DIÁRIO lançado no mês; para a parte do realizado do mês que
    ainda não tem mix diário informado, aplica o mix MENSAL agregado (se houver) —
    funciona tanto para meses com realizado vindo de lançamento diário quanto de
    importação manual (histórico). Retorna (valores_dict, total_com_mix)."""
    ini = date(ano, mes, 1)
    ultimo_dia = calendar.monthrange(ano, mes)[1]
    fim = date(ano, mes, ultimo_dia)

    valores, total_coberto_diario = _mix_diario_vendedor_periodo(vendedor_id, ini, fim)

    vendas_vend = pd.read_sql_query(
        text(
            "SELECT COALESCE(SUM(valor_realizado), 0) AS total FROM vendas_diarias "
            "WHERE vendedor_id=:vid AND data BETWEEN :ini AND :fim"
        ),
        get_engine(),
        params={"vid": vendedor_id, "ini": ini, "fim": fim},
    )
    realizado_diario_total = float(vendas_vend.iloc[0]["total"])
    realizado_manual_total = get_realizado_manual_vendedor(vendedor_id, ano, mes)
    realizado_mes_total = realizado_diario_total + realizado_manual_total

    pct_mensal = get_pagamento_manual_vendedor(vendedor_id, ano, mes)
    realizado_restante = max(realizado_mes_total - total_coberto_diario, 0.0)
    total_coberto = total_coberto_diario
    if pct_mensal and realizado_restante > 0:
        for modalidade in MODALIDADES_PAGAMENTO:
            valores[modalidade] += realizado_restante * pct_mensal.get(modalidade, 0.0) / 100.0
        total_coberto += realizado_restante

    return valores, total_coberto


def get_mix_pagamento_historico_vendedor(vendedor_id):
    """Média histórica ponderada por modalidade, somando todos os meses em que o
    vendedor tem mix de pagamento lançado (diário e/ou mensal agregado). Retorna
    (medias_pct_dict, total_com_mix)."""
    meses_df = pd.read_sql_query(
        text(
            """
            SELECT DISTINCT ano, mes FROM (
                SELECT EXTRACT(YEAR FROM data)::int AS ano, EXTRACT(MONTH FROM data)::int AS mes
                FROM pagamentos_diarios WHERE vendedor_id = :vid
                UNION
                SELECT ano, mes FROM pagamentos_mensal_manual WHERE vendedor_id = :vid
            ) t
            """
        ),
        get_engine(),
        params={"vid": vendedor_id},
    )

    valores_total = {m: 0.0 for m in MODALIDADES_PAGAMENTO}
    for _, row in meses_df.iterrows():
        valores_mes, _ = get_mix_pagamento_vendedor_mes(vendedor_id, int(row["ano"]), int(row["mes"]))
        for modalidade in MODALIDADES_PAGAMENTO:
            valores_total[modalidade] += valores_mes.get(modalidade, 0.0)

    total_geral = sum(valores_total.values())
    medias_pct = {m: (v / total_geral * 100 if total_geral > 0 else 0.0) for m, v in valores_total.items()}
    return medias_pct, total_geral


def get_mix_pagamento_mes(ano, mes, loja=None):
    """Combina o mix diário + mensal agregado de TODOS os vendedores do filtro num
    DataFrame "longo" (uma linha por vendedor/modalidade, com o valor em R$), mais a
    soma do realizado que teve mix informado (cobertura em relação ao realizado
    total do mês)."""
    vendedores_df = get_vendedores(loja=loja, apenas_ativos=True)
    linhas = []
    realizado_com_mix = 0.0
    for _, v in vendedores_df.iterrows():
        valores, total_coberto = get_mix_pagamento_vendedor_mes(int(v["id"]), ano, mes)
        realizado_com_mix += total_coberto
        for modalidade, valor in valores.items():
            if valor == 0.0:
                continue
            linhas.append({
                "vendedor_id": int(v["id"]),
                "nome": v["nome"],
                "loja": v["loja"],
                "modalidade": modalidade,
                "valor": valor,
            })

    mix_df = pd.DataFrame(linhas, columns=["vendedor_id", "nome", "loja", "modalidade", "valor"])
    return mix_df, realizado_com_mix


# Faixas de risco de exposição a Nota Promissória (% do realizado com mix informado).
RISCO_NP_LIMIAR_BAIXO = 10.0
RISCO_NP_LIMIAR_MODERADO = 25.0


def nivel_risco_nota_promissoria(pct):
    """Classifica o % de Nota Promissória sobre o realizado em Baixo/Moderado/Alto risco."""
    if pct is None:
        return "— sem dado"
    if pct < RISCO_NP_LIMIAR_BAIXO:
        return "🟢 Baixo"
    elif pct <= RISCO_NP_LIMIAR_MODERADO:
        return "🟡 Moderado"
    return "🔴 Alto"


def get_risco_nota_promissoria_mes(ano, mes, loja=None):
    """Por vendedor ativo do filtro: exposição em Nota Promissória no mês (R$ e %),
    média histórica (%) de Nota Promissória e nível de risco resultante. Cruza o mix
    do mês (get_mix_pagamento_vendedor_mes) com todo o histórico já lançado
    (get_mix_pagamento_historico_vendedor)."""
    vendedores_df = get_vendedores(loja=loja, apenas_ativos=True)
    linhas = []
    for _, v in vendedores_df.iterrows():
        vendedor_id = int(v["id"])
        valores_mes, total_mes = get_mix_pagamento_vendedor_mes(vendedor_id, ano, mes)
        valor_np_mes = valores_mes.get("Nota Promissória", 0.0)
        pct_np_mes = (valor_np_mes / total_mes * 100) if total_mes > 0 else None

        medias_hist, total_hist = get_mix_pagamento_historico_vendedor(vendedor_id)
        pct_np_hist = medias_hist.get("Nota Promissória", 0.0) if total_hist > 0 else None

        linhas.append({
            "vendedor_id": vendedor_id,
            "nome": v["nome"],
            "loja": v["loja"],
            "valor_np_mes": valor_np_mes,
            "total_com_mix_mes": total_mes,
            "pct_np_mes": pct_np_mes,
            "pct_np_historico": pct_np_hist,
            "nivel_risco": nivel_risco_nota_promissoria(pct_np_mes),
        })
    return pd.DataFrame(linhas)


# --------------------------------------------------------------------------
# Inadimplência - valores em aberto das vendas a prazo (Nota Promissória).
# O risco de um mês só se confirma (ou não) nos meses seguintes: o valor vendido
# a prazo em fevereiro deveria ser recebido a partir de março; o que não for pago
# a partir de 30 dias entra aqui como "valor em aberto" daquele mês de VENDA
# (ano_venda/mes_venda), não do mês em que a cobrança foi apurada. Cruzando com o
# valor vendido em Nota Promissória (já rastreado no mix de pagamento) chegamos
# ao índice de inadimplência (%) = valor em aberto ÷ valor vendido a prazo.
# --------------------------------------------------------------------------
INADIMPLENCIA_LIMIAR_BAIXO = 3.0
INADIMPLENCIA_LIMIAR_MODERADO = 8.0


def nivel_risco_inadimplencia(pct):
    """Classifica o índice de inadimplência (%) em Baixo/Moderado/Alto risco."""
    if pct is None:
        return "— sem dado"
    if pct < INADIMPLENCIA_LIMIAR_BAIXO:
        return "🟢 Baixo"
    elif pct <= INADIMPLENCIA_LIMIAR_MODERADO:
        return "🟡 Moderado"
    return "🔴 Alto"


def upsert_inadimplencia(vendedor_id, ano_venda, mes_venda, valor_em_aberto):
    with get_engine().begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO inadimplencia_mensal (vendedor_id, ano_venda, mes_venda, valor_em_aberto)
                VALUES (:vendedor_id, :ano_venda, :mes_venda, :valor_em_aberto)
                ON CONFLICT (vendedor_id, ano_venda, mes_venda)
                DO UPDATE SET valor_em_aberto = EXCLUDED.valor_em_aberto
                """
            ),
            {
                "vendedor_id": vendedor_id, "ano_venda": ano_venda, "mes_venda": mes_venda,
                "valor_em_aberto": valor_em_aberto,
            },
        )


def get_inadimplencia_vendedor(vendedor_id, ano_venda, mes_venda):
    """Retorna o valor em aberto já lançado para o vendedor/mês de venda, ou None
    se ainda não houver lançamento (diferente de 0, que significa 'sem pendência')."""
    df = pd.read_sql_query(
        text(
            "SELECT valor_em_aberto FROM inadimplencia_mensal "
            "WHERE vendedor_id=:vid AND ano_venda=:ano AND mes_venda=:mes"
        ),
        get_engine(),
        params={"vid": vendedor_id, "ano": ano_venda, "mes": mes_venda},
    )
    return float(df.iloc[0]["valor_em_aberto"]) if not df.empty else None


def get_inadimplencia_mes(ano_venda, mes_venda, loja=None):
    """Por vendedor ativo do filtro: valor vendido a prazo (Nota Promissória) no mês
    de venda informado, valor em aberto lançado e o índice de inadimplência (%)."""
    vendedores_df = get_vendedores(loja=loja, apenas_ativos=True)
    linhas = []
    for _, v in vendedores_df.iterrows():
        vendedor_id = int(v["id"])
        valor_aberto = get_inadimplencia_vendedor(vendedor_id, ano_venda, mes_venda)
        valores_mes, _ = get_mix_pagamento_vendedor_mes(vendedor_id, ano_venda, mes_venda)
        valor_a_prazo = valores_mes.get("Nota Promissória", 0.0)
        indice_pct = (
            (valor_aberto / valor_a_prazo * 100)
            if (valor_aberto is not None and valor_a_prazo > 0) else None
        )
        linhas.append({
            "vendedor_id": vendedor_id,
            "nome": v["nome"],
            "loja": v["loja"],
            "valor_a_prazo": valor_a_prazo,
            "valor_em_aberto": valor_aberto,
            "indice_pct": indice_pct,
            "nivel_risco": nivel_risco_inadimplencia(indice_pct),
        })
    return pd.DataFrame(linhas)


def get_inadimplencia_historico_vendedor(vendedor_id):
    """Série histórica (uma linha por mês de venda com valor em aberto lançado) do
    vendedor: valor vendido a prazo, valor em aberto e o índice de inadimplência (%)
    de cada mês, ordenada cronologicamente — base para calcular tendência."""
    df = pd.read_sql_query(
        text(
            "SELECT ano_venda, mes_venda, valor_em_aberto FROM inadimplencia_mensal "
            "WHERE vendedor_id=:vid ORDER BY ano_venda, mes_venda"
        ),
        get_engine(),
        params={"vid": vendedor_id},
    )
    if df.empty:
        return pd.DataFrame(columns=["ano_venda", "mes_venda", "valor_a_prazo", "valor_em_aberto", "indice_pct"])

    linhas = []
    for _, r in df.iterrows():
        valores_mes, _ = get_mix_pagamento_vendedor_mes(vendedor_id, int(r["ano_venda"]), int(r["mes_venda"]))
        valor_a_prazo = valores_mes.get("Nota Promissória", 0.0)
        valor_em_aberto = float(r["valor_em_aberto"])
        indice_pct = (valor_em_aberto / valor_a_prazo * 100) if valor_a_prazo > 0 else None
        linhas.append({
            "ano_venda": int(r["ano_venda"]),
            "mes_venda": int(r["mes_venda"]),
            "valor_a_prazo": valor_a_prazo,
            "valor_em_aberto": valor_em_aberto,
            "indice_pct": indice_pct,
        })
    return pd.DataFrame(linhas)


def get_indice_inadimplencia_resumo_vendedor(vendedor_id):
    """Resumo do índice de inadimplência histórico do vendedor: média ponderada por
    R$ (soma valor em aberto ÷ soma valor a prazo de todos os meses com dado válido —
    mais justo que a média simples dos percentuais mensais, que trataria um mês
    pequeno com o mesmo peso de um mês grande), quantidade de meses considerados e
    o nível de risco resultante."""
    hist = get_inadimplencia_historico_vendedor(vendedor_id)
    hist_valida = hist[hist["valor_a_prazo"] > 0] if not hist.empty else hist
    if hist_valida.empty:
        return {
            "media_ponderada_pct": None,
            "n_meses": 0,
            "nivel_risco": nivel_risco_inadimplencia(None),
            "historico": hist,
        }
    total_prazo = float(hist_valida["valor_a_prazo"].sum())
    total_aberto = float(hist_valida["valor_em_aberto"].sum())
    media_pct = (total_aberto / total_prazo * 100) if total_prazo > 0 else None
    return {
        "media_ponderada_pct": media_pct,
        "n_meses": int(len(hist_valida)),
        "nivel_risco": nivel_risco_inadimplencia(media_pct),
        "historico": hist,
    }


# --------------------------------------------------------------------------
# Regras de negócio / KPIs
# --------------------------------------------------------------------------
def mes_anterior(ano, mes):
    if mes == 1:
        return ano - 1, 12
    return ano, mes - 1


def get_totais_mes(ano, mes, loja=None):
    """Meta total, realizado total (diário + manual) e pedidos totais (diário +
    manual) do mês (já filtrado por loja)."""
    metas_df = get_metas_mes(ano, mes, loja=loja)
    vendas_df = get_vendas_mes(ano, mes, loja=loja)
    manual_df = get_realizado_manual_mes(ano, mes, loja=loja)
    pedidos_manual_df = get_pedidos_manual_mes(ano, mes, loja=loja)
    meta_total = float(metas_df["valor_meta"].sum()) if not metas_df.empty else 0.0
    realizado_total = float(vendas_df["valor_realizado"].sum()) if not vendas_df.empty else 0.0
    realizado_total += float(manual_df["valor_realizado"].sum()) if not manual_df.empty else 0.0
    pedidos_total = int(vendas_df["qtd_pedidos"].sum()) if not vendas_df.empty else 0
    pedidos_total += int(pedidos_manual_df["qtd_pedidos"].sum()) if not pedidos_manual_df.empty else 0
    return {"meta": meta_total, "realizado": realizado_total, "pedidos": pedidos_total}


def get_indicadores_vendedores_mes(ano, mes, loja=None):
    """Uma linha por vendedor ativo com meta, realizado (diário + manual), pedidos
    (diário + manual), atingimento (%) e ticket médio individual do mês — já
    combinando todas as fontes de dado (lançamento diário e importações manuais)."""
    metas_df = get_metas_mes(ano, mes, loja=loja)
    vendas_df = get_vendas_mes(ano, mes, loja=loja)
    manual_df = get_realizado_manual_mes(ano, mes, loja=loja)
    pedidos_manual_df = get_pedidos_manual_mes(ano, mes, loja=loja)

    if vendas_df.empty:
        vendas_agg = pd.DataFrame(columns=["vendedor_id", "realizado", "pedidos"])
    else:
        vendas_agg = (
            vendas_df.groupby("vendedor_id")
            .agg(realizado=("valor_realizado", "sum"), pedidos=("qtd_pedidos", "sum"))
            .reset_index()
        )

    if not manual_df.empty:
        manual_agg = (
            manual_df.groupby("vendedor_id")["valor_realizado"].sum().reset_index()
            .rename(columns={"valor_realizado": "realizado_manual"})
        )
        vendas_agg = vendas_agg.merge(manual_agg, on="vendedor_id", how="outer").fillna(0)
        vendas_agg["realizado"] = vendas_agg["realizado"] + vendas_agg["realizado_manual"]
        vendas_agg = vendas_agg.drop(columns=["realizado_manual"])

    if not pedidos_manual_df.empty:
        cm_agg = (
            pedidos_manual_df.groupby("vendedor_id")["qtd_pedidos"].sum().reset_index()
            .rename(columns={"qtd_pedidos": "pedidos_manual"})
        )
        vendas_agg = vendas_agg.merge(cm_agg, on="vendedor_id", how="outer").fillna(0)
        vendas_agg["realizado"] = vendas_agg["realizado"].fillna(0.0)
        vendas_agg["pedidos"] = vendas_agg["pedidos"] + vendas_agg["pedidos_manual"]
        vendas_agg = vendas_agg.drop(columns=["pedidos_manual"])

    if "pedidos" in vendas_agg.columns and not vendas_agg.empty:
        vendas_agg["pedidos"] = vendas_agg["pedidos"].astype(int)

    resultado = metas_df.merge(vendas_agg, on="vendedor_id", how="left")
    resultado["realizado"] = resultado["realizado"].fillna(0.0)
    resultado["pedidos"] = resultado["pedidos"].fillna(0).astype(int)
    resultado["atingimento_pct"] = resultado.apply(
        lambda r: (r["realizado"] / r["valor_meta"] * 100) if r["valor_meta"] > 0 else 0.0, axis=1
    )
    resultado["ticket_medio"] = resultado.apply(
        lambda r: (r["realizado"] / r["pedidos"]) if r["pedidos"] > 0 else 0.0, axis=1
    )
    return resultado


def dias_uteis_transcorridos(ano, mes, dias_uteis_total=DIAS_UTEIS_PADRAO, referencia=None):
    """Conta dias úteis (segunda a sábado, domingo não conta) já transcorridos no mês,
    limitado ao total de dias úteis considerados para o mês (padrão 24)."""
    referencia = referencia or date.today()
    primeiro_dia = date(ano, mes, 1)
    ultimo_dia_mes = date(ano, mes, calendar.monthrange(ano, mes)[1])

    if (ano, mes) < (referencia.year, referencia.month):
        dia_fim = ultimo_dia_mes
    elif (ano, mes) == (referencia.year, referencia.month):
        dia_fim = referencia
    else:
        return 0

    if dia_fim < primeiro_dia:
        return 0

    count = 0
    d = primeiro_dia
    while d <= dia_fim:
        if d.weekday() != 6:  # 6 = domingo
            count += 1
        d += timedelta(days=1)
    return min(count, dias_uteis_total)


def cor_semaforo(pct):
    if pct < 70:
        return "#e74c3c"  # vermelho
    elif pct <= 90:
        return "#f39c12"  # amarelo
    else:
        return "#27ae60"  # verde


def label_semaforo(pct):
    if pct < 70:
        return "Crítico"
    elif pct <= 90:
        return "Atenção"
    else:
        return "No alvo"


def formatar_moeda(valor):
    try:
        return "R$ " + f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return "R$ 0,00"