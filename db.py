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

# Início da base de dados do painel (primeiro mês com lançamentos confiáveis) —
# usado para calcular médias históricas só a partir daqui.
BASE_INICIO_ANO = 2026
BASE_INICIO_MES = 2

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


# --------------------------------------------------------------------------
# Cache de leitura (performance). O painel cresceu bastante em número de
# indicadores e várias funções fazem várias consultas por vendedor (ex.:
# histórico de mix de pagamento, índice de inadimplência) — sem cache, o
# Streamlit reexecuta TODAS as consultas de TODAS as seções a cada interação
# na página (clique, filtro, etc.), o que fica lento rápido conforme o
# histórico de meses e o número de vendedores crescem.
#
# `@cache_leitura()` usa st.cache_data quando o Streamlit está disponível
# (é um no-op fora do Streamlit, ex.: seed_data.py, scripts de importação) e
# guarda o resultado por um tempo limite (TTL) como rede de segurança. Toda
# função que grava dado (upsert_*, add_vendedor, delete_*) chama
# `limpar_cache()` no final, o que invalida IMEDIATAMENTE tudo que estiver em
# cache — garante que a tela sempre mostra o dado atualizado logo após salvar,
# sem esperar o TTL expirar.
# --------------------------------------------------------------------------
def cache_leitura(ttl=600):
    try:
        import streamlit as st
        return st.cache_data(ttl=ttl, show_spinner=False)
    except Exception:
        return lambda func: func


def limpar_cache():
    try:
        import streamlit as st
        st.cache_data.clear()
    except Exception:
        pass


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
        """
        CREATE TABLE IF NOT EXISTS vendas_produtos_diarias (
            id SERIAL PRIMARY KEY,
            vendedor_id INTEGER NOT NULL REFERENCES vendedores(id) ON DELETE CASCADE,
            data DATE NOT NULL,
            cod_produto TEXT NOT NULL,
            descricao_produto TEXT NOT NULL,
            marca TEXT,
            fornecedor TEXT,
            posit NUMERIC(10, 2),
            vendas NUMERIC(10, 2),
            qtd NUMERIC(12, 2),
            qtd_cx NUMERIC(12, 2),
            valor_total NUMERIC(14, 2) NOT NULL DEFAULT 0,
            pct NUMERIC(6, 2),
            UNIQUE (vendedor_id, data, cod_produto)
        )
        """,
    ]
    with get_engine().begin() as conn:
        # Trava (só durante esta transação) pra impedir que duas instâncias rodem as
        # migrações do schema ao mesmo tempo — ex.: o painel no Streamlit Cloud
        # acordando de "sleep" e o script de sincronização (scripts/sync_sgi.py, que
        # também chama db.init_db() ao importar db) batendo no banco juntos. Sem essa
        # trava, "CREATE TABLE IF NOT EXISTS" concorrente pode disparar uma condição
        # de corrida conhecida do Postgres (erro de chave duplicada em pg_type) na
        # primeira vez que uma tabela nova é criada. pg_advisory_xact_lock libera
        # sozinho no fim da transação, sem risco de ficar "preso" com o pool de
        # conexões reaproveitando conexões.
        conn.execute(text("SELECT pg_advisory_xact_lock(727271)"))
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
    limpar_cache()


def update_vendedor(vendedor_id, nome, loja, ativo=True):
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "UPDATE vendedores SET nome=:nome, loja=:loja, ativo=:ativo WHERE id=:id"
            ),
            {"nome": nome, "loja": loja, "ativo": bool(ativo), "id": vendedor_id},
        )
    limpar_cache()


def delete_vendedor(vendedor_id):
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM vendedores WHERE id=:id"), {"id": vendedor_id})
    limpar_cache()


@cache_leitura()
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


@cache_leitura()
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
    limpar_cache()


@cache_leitura()
def get_meta_vendedor(vendedor_id, ano, mes):
    df = pd.read_sql_query(
        text("SELECT valor_meta FROM metas WHERE vendedor_id=:vid AND ano=:ano AND mes=:mes"),
        get_engine(),
        params={"vid": vendedor_id, "ano": ano, "mes": mes},
    )
    return float(df.iloc[0]["valor_meta"]) if not df.empty else 0.0


@cache_leitura()
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


@cache_leitura()
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


@cache_leitura()
def get_atingimento_medio_vendedor(loja=None):
    """Atingimento médio (%) por vendedor, considerando só os meses já FINALIZADOS
    (fechados): do início da base de dados (BASE_INICIO_ANO/BASE_INICIO_MES) até o
    mês anterior ao atual — nunca inclui o mês corrente, que ainda está em
    andamento. Só entram na média os meses em que o vendedor tinha meta lançada
    (> 0); meses sem meta não contam. Retorna uma linha por vendedor com
    atingimento_medio_pct e n_meses (quantos meses fechados entraram na conta)."""
    hoje = date.today()
    ano_ult, mes_ult = mes_anterior(hoje.year, hoje.month)

    hist = get_historico_meta_realizado(loja=loja)
    cols = ["vendedor_id", "nome", "loja", "atingimento_medio_pct", "n_meses"]
    if hist.empty:
        return pd.DataFrame(columns=cols)

    inicio_ok = (hist["ano"] > BASE_INICIO_ANO) | ((hist["ano"] == BASE_INICIO_ANO) & (hist["mes"] >= BASE_INICIO_MES))
    fim_ok = (hist["ano"] < ano_ult) | ((hist["ano"] == ano_ult) & (hist["mes"] <= mes_ult))
    hist_periodo = hist[inicio_ok & fim_ok]
    hist_com_meta = hist_periodo[hist_periodo["valor_meta"] > 0].copy()
    if hist_com_meta.empty:
        return pd.DataFrame(columns=cols)

    hist_com_meta["atingimento_pct"] = hist_com_meta["realizado"] / hist_com_meta["valor_meta"] * 100
    resumo = (
        hist_com_meta.groupby(["vendedor_id", "nome", "loja"])["atingimento_pct"]
        .agg(atingimento_medio_pct="mean", n_meses="count")
        .reset_index()
        .sort_values("atingimento_medio_pct", ascending=False)
    )
    return resumo


@cache_leitura()
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
    limpar_cache()


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
    limpar_cache()


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
    limpar_cache()


def delete_venda(venda_id):
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM vendas_diarias WHERE id=:id"), {"id": venda_id})
    limpar_cache()


@cache_leitura()
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


@cache_leitura()
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


@cache_leitura()
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
    limpar_cache()


@cache_leitura()
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


@cache_leitura()
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
    limpar_cache()


@cache_leitura()
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


@cache_leitura()
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
    limpar_cache()


@cache_leitura()
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
    limpar_cache()


@cache_leitura()
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


@cache_leitura()
def _mix_pagamento_lote_mes(ano, mes, loja=None, apenas_ativos=True):
    """Versão EM LOTE (poucas queries no total, não uma por vendedor) do mix de
    pagamento de todos os vendedores do filtro num mês específico. Substitui o
    padrão antigo de chamar get_mix_pagamento_vendedor_mes uma vez por vendedor
    (N queries) — aqui são sempre ~4 queries, não importa quantos vendedores.
    Retorna um DataFrame com uma linha por vendedor: realizado_mes, total_coberto
    e uma coluna em R$ por modalidade de pagamento."""
    ini = date(ano, mes, 1)
    ultimo_dia = calendar.monthrange(ano, mes)[1]
    fim = date(ano, mes, ultimo_dia)
    colunas = list(COLUNAS_PAGAMENTO.values())

    vendedores_df = get_vendedores(loja=loja, apenas_ativos=apenas_ativos)
    if vendedores_df.empty:
        return pd.DataFrame(columns=["vendedor_id", "nome", "loja", "realizado_mes", "total_coberto"] + MODALIDADES_PAGAMENTO)

    vendas_df = get_vendas_mes(ano, mes, loja=loja)
    realizado_diario = (
        vendas_df.groupby("vendedor_id")["valor_realizado"].sum() if not vendas_df.empty else pd.Series(dtype=float)
    )
    manual_df = get_realizado_manual_mes(ano, mes, loja=loja)
    realizado_manual = (
        manual_df.groupby("vendedor_id")["valor_realizado"].sum() if not manual_df.empty else pd.Series(dtype=float)
    )

    soma_cols = ", ".join(f"SUM(vd.valor_realizado * pg.{c} / 100.0) AS {c}" for c in colunas)
    dia_df = pd.read_sql_query(
        text(
            f"""
            SELECT pg.vendedor_id, SUM(vd.valor_realizado) AS total_coberto_diario, {soma_cols}
            FROM pagamentos_diarios pg
            JOIN vendas_diarias vd ON vd.vendedor_id = pg.vendedor_id AND vd.data = pg.data
            WHERE pg.data BETWEEN :ini AND :fim
            GROUP BY pg.vendedor_id
            """
        ),
        get_engine(),
        params={"ini": ini, "fim": fim},
    )
    dia_idx = dia_df.set_index("vendedor_id") if not dia_df.empty else None

    mensal_df = pd.read_sql_query(
        text(f"SELECT vendedor_id, {', '.join(colunas)} FROM pagamentos_mensal_manual WHERE ano=:ano AND mes=:mes"),
        get_engine(),
        params={"ano": ano, "mes": mes},
    )
    mensal_idx = mensal_df.set_index("vendedor_id") if not mensal_df.empty else None

    linhas = []
    for _, v in vendedores_df.iterrows():
        vid = int(v["id"])
        realizado_mes = float(realizado_diario.get(vid, 0.0)) + float(realizado_manual.get(vid, 0.0))

        valores = {m: 0.0 for m in MODALIDADES_PAGAMENTO}
        total_coberto_diario = 0.0
        if dia_idx is not None and vid in dia_idx.index:
            row_dia = dia_idx.loc[vid]
            total_coberto_diario = float(row_dia["total_coberto_diario"] or 0.0)
            for modalidade, coluna in COLUNAS_PAGAMENTO.items():
                valores[modalidade] = float(row_dia[coluna] or 0.0)

        realizado_restante = max(realizado_mes - total_coberto_diario, 0.0)
        total_coberto = total_coberto_diario
        if mensal_idx is not None and vid in mensal_idx.index and realizado_restante > 0:
            row_mensal = mensal_idx.loc[vid]
            for modalidade, coluna in COLUNAS_PAGAMENTO.items():
                valores[modalidade] += realizado_restante * float(row_mensal[coluna] or 0.0) / 100.0
            total_coberto += realizado_restante

        linha = {
            "vendedor_id": vid, "nome": v["nome"], "loja": v["loja"],
            "realizado_mes": realizado_mes, "total_coberto": total_coberto,
        }
        linha.update(valores)
        linhas.append(linha)

    return pd.DataFrame(linhas)


@cache_leitura()
def _mix_pagamento_lote_historico(loja=None, apenas_ativos=True):
    """Versão EM LOTE do mix de pagamento histórico (todos os meses já lançados,
    de qualquer vendedor) de todos os vendedores do filtro. Em vez de uma query
    por vendedor por mês (o que causava lentidão/timeout com vários vendedores e
    vários meses de histórico), faz uma passada por MÊS DISTINTO do sistema
    (tipicamente bem menos que vendedores × meses) reusando _mix_pagamento_lote_mes,
    que já é cacheado. Retorna um DataFrame com uma linha por vendedor: total_geral
    (R$ com mix lançado, soma de todos os meses) e uma coluna por modalidade."""
    vendedores_df = get_vendedores(loja=loja, apenas_ativos=apenas_ativos)
    if vendedores_df.empty:
        return pd.DataFrame(columns=["vendedor_id", "nome", "loja", "total_geral"] + MODALIDADES_PAGAMENTO)

    meses_df = pd.read_sql_query(
        text(
            """
            SELECT DISTINCT ano, mes FROM (
                SELECT EXTRACT(YEAR FROM data)::int AS ano, EXTRACT(MONTH FROM data)::int AS mes
                FROM pagamentos_diarios
                UNION
                SELECT ano, mes FROM pagamentos_mensal_manual
            ) t
            """
        ),
        get_engine(),
    )

    ids_validos = set(vendedores_df["id"].astype(int))
    acumulado = {vid: {m: 0.0 for m in MODALIDADES_PAGAMENTO} for vid in ids_validos}

    for _, row in meses_df.iterrows():
        mes_df = _mix_pagamento_lote_mes(int(row["ano"]), int(row["mes"]), loja=loja, apenas_ativos=apenas_ativos)
        for _, vr in mes_df.iterrows():
            vid = int(vr["vendedor_id"])
            if vid not in acumulado:
                continue
            for modalidade in MODALIDADES_PAGAMENTO:
                acumulado[vid][modalidade] += float(vr[modalidade])

    nomes = {int(v["id"]): (v["nome"], v["loja"]) for _, v in vendedores_df.iterrows()}
    linhas = []
    for vid, valores in acumulado.items():
        total_geral = sum(valores.values())
        nome, loja_v = nomes[vid]
        linha = {"vendedor_id": vid, "nome": nome, "loja": loja_v, "total_geral": total_geral}
        linha.update(valores)
        linhas.append(linha)
    return pd.DataFrame(linhas)


@cache_leitura()
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


@cache_leitura()
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


@cache_leitura()
def get_mix_pagamento_historico_vendedor(vendedor_id):
    """Média histórica ponderada por modalidade, somando todos os meses em que o
    vendedor tem mix de pagamento lançado (diário e/ou mensal agregado). Retorna
    (medias_pct_dict, total_com_mix). Implementado em cima da versão em lote
    (_mix_pagamento_lote_historico), que resolve o histórico de TODOS os
    vendedores de uma vez (poucas queries) em vez de uma query por mês por
    vendedor — aqui só filtramos a linha do vendedor pedido."""
    lote = _mix_pagamento_lote_historico(loja=None, apenas_ativos=False)
    linha = lote[lote["vendedor_id"] == vendedor_id]
    if linha.empty:
        return {m: 0.0 for m in MODALIDADES_PAGAMENTO}, 0.0
    row = linha.iloc[0]
    total_geral = float(row["total_geral"])
    medias_pct = {
        m: (float(row[m]) / total_geral * 100 if total_geral > 0 else 0.0) for m in MODALIDADES_PAGAMENTO
    }
    return medias_pct, total_geral


@cache_leitura()
def get_mix_pagamento_mes(ano, mes, loja=None):
    """Combina o mix diário + mensal agregado de TODOS os vendedores do filtro num
    DataFrame "longo" (uma linha por vendedor/modalidade, com o valor em R$), mais a
    soma do realizado que teve mix informado (cobertura em relação ao realizado
    total do mês). Implementado em cima da versão em lote (_mix_pagamento_lote_mes),
    que faz sempre ~4 queries no total, não uma por vendedor."""
    lote = _mix_pagamento_lote_mes(ano, mes, loja=loja, apenas_ativos=True)
    linhas = []
    realizado_com_mix = float(lote["total_coberto"].sum()) if not lote.empty else 0.0
    for _, v in lote.iterrows():
        for modalidade in MODALIDADES_PAGAMENTO:
            valor = float(v[modalidade])
            if valor == 0.0:
                continue
            linhas.append({
                "vendedor_id": int(v["vendedor_id"]),
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


@cache_leitura()
def get_risco_nota_promissoria_mes(ano, mes, loja=None):
    """Por vendedor ativo do filtro: exposição em Nota Promissória no mês (R$ e %),
    média histórica (%) de Nota Promissória e nível de risco resultante. Cruza o mix
    do mês com todo o histórico já lançado — ambos calculados em lote
    (_mix_pagamento_lote_mes / _mix_pagamento_lote_historico), sem loop de query
    por vendedor."""
    mes_df = _mix_pagamento_lote_mes(ano, mes, loja=loja, apenas_ativos=True)
    if mes_df.empty:
        return pd.DataFrame(columns=[
            "vendedor_id", "nome", "loja", "valor_np_mes", "total_com_mix_mes",
            "pct_np_mes", "pct_np_historico", "nivel_risco",
        ])
    hist_df = _mix_pagamento_lote_historico(loja=loja, apenas_ativos=True)
    hist_idx = hist_df.set_index("vendedor_id") if not hist_df.empty else None

    linhas = []
    for _, v in mes_df.iterrows():
        vendedor_id = int(v["vendedor_id"])
        valor_np_mes = float(v["Nota Promissória"])
        total_mes = float(v["total_coberto"])
        pct_np_mes = (valor_np_mes / total_mes * 100) if total_mes > 0 else None

        pct_np_hist = None
        if hist_idx is not None and vendedor_id in hist_idx.index:
            row_hist = hist_idx.loc[vendedor_id]
            total_hist = float(row_hist["total_geral"])
            if total_hist > 0:
                pct_np_hist = float(row_hist["Nota Promissória"]) / total_hist * 100

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
# Referência: 7% é a taxa cobrada pela maquininha de cartão — até esse índice, o
# atraso fica dentro do custo que o negócio já absorve normalmente. Acima disso, o
# índice de inadimplência é considerado muito alto.
INADIMPLENCIA_LIMIAR_ACEITAVEL = 7.0


def nivel_risco_inadimplencia(pct):
    """Classifica o índice de inadimplência (%): até o limiar aceitável (taxa da
    maquininha de cartão) ou muito alto acima dele."""
    if pct is None:
        return "— sem dado"
    if pct <= INADIMPLENCIA_LIMIAR_ACEITAVEL:
        return "🟢 Aceitável"
    return "🔴 Muito alto"


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
    limpar_cache()


@cache_leitura()
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


@cache_leitura()
def _inadimplencia_historico_lote(loja=None, apenas_ativos=True):
    """Versão EM LOTE da série histórica de inadimplência de TODOS os vendedores do
    filtro (uma linha por vendedor/mês de venda com valor em aberto lançado),
    evitando uma query de mix de pagamento por vendedor por mês: busca todos os
    lançamentos de inadimplência de uma vez e resolve o valor a prazo fazendo uma
    passada por MÊS DISTINTO (não por vendedor) usando _mix_pagamento_lote_mes."""
    vendedores_df = get_vendedores(loja=loja, apenas_ativos=apenas_ativos)
    cols = ["vendedor_id", "nome", "loja", "ano_venda", "mes_venda", "valor_a_prazo", "valor_em_aberto", "indice_pct"]
    if vendedores_df.empty:
        return pd.DataFrame(columns=cols)
    ids_validos = set(vendedores_df["id"].astype(int))
    nomes = {int(v["id"]): (v["nome"], v["loja"]) for _, v in vendedores_df.iterrows()}

    df = pd.read_sql_query(
        text(
            "SELECT vendedor_id, ano_venda, mes_venda, valor_em_aberto FROM inadimplencia_mensal "
            "ORDER BY vendedor_id, ano_venda, mes_venda"
        ),
        get_engine(),
    )
    if df.empty:
        return pd.DataFrame(columns=cols)
    df = df[df["vendedor_id"].astype(int).isin(ids_validos)]
    if df.empty:
        return pd.DataFrame(columns=cols)

    pares = df[["ano_venda", "mes_venda"]].drop_duplicates()
    valor_np_lookup = {}
    for _, p in pares.iterrows():
        ano_v, mes_v = int(p["ano_venda"]), int(p["mes_venda"])
        mes_df = _mix_pagamento_lote_mes(ano_v, mes_v, loja=loja, apenas_ativos=apenas_ativos)
        for _, vr in mes_df.iterrows():
            valor_np_lookup[(int(vr["vendedor_id"]), ano_v, mes_v)] = float(vr["Nota Promissória"])

    linhas = []
    for _, r in df.iterrows():
        vid = int(r["vendedor_id"])
        ano_v, mes_v = int(r["ano_venda"]), int(r["mes_venda"])
        valor_a_prazo = valor_np_lookup.get((vid, ano_v, mes_v), 0.0)
        valor_em_aberto = float(r["valor_em_aberto"])
        indice_pct = (valor_em_aberto / valor_a_prazo * 100) if valor_a_prazo > 0 else None
        nome, loja_v = nomes[vid]
        linhas.append({
            "vendedor_id": vid, "nome": nome, "loja": loja_v,
            "ano_venda": ano_v, "mes_venda": mes_v,
            "valor_a_prazo": valor_a_prazo, "valor_em_aberto": valor_em_aberto,
            "indice_pct": indice_pct,
        })
    return pd.DataFrame(linhas)


@cache_leitura()
def get_inadimplencia_mes(ano_venda, mes_venda, loja=None):
    """Por vendedor ativo do filtro: valor vendido a prazo (Nota Promissória) no mês
    de venda informado, valor em aberto lançado e o índice de inadimplência (%).
    Sempre ~2 queries no total (mais o mix em lote do mês), não uma por vendedor."""
    vendedores_df = get_vendedores(loja=loja, apenas_ativos=True)
    cols = ["vendedor_id", "nome", "loja", "valor_a_prazo", "valor_em_aberto", "indice_pct", "nivel_risco"]
    if vendedores_df.empty:
        return pd.DataFrame(columns=cols)

    aberto_df = pd.read_sql_query(
        text("SELECT vendedor_id, valor_em_aberto FROM inadimplencia_mensal WHERE ano_venda=:ano AND mes_venda=:mes"),
        get_engine(),
        params={"ano": ano_venda, "mes": mes_venda},
    )
    aberto_lookup = {int(r["vendedor_id"]): float(r["valor_em_aberto"]) for _, r in aberto_df.iterrows()}

    mix_df = _mix_pagamento_lote_mes(ano_venda, mes_venda, loja=loja, apenas_ativos=True)
    prazo_lookup = {int(r["vendedor_id"]): float(r["Nota Promissória"]) for _, r in mix_df.iterrows()} if not mix_df.empty else {}

    linhas = []
    for _, v in vendedores_df.iterrows():
        vid = int(v["id"])
        valor_aberto = aberto_lookup.get(vid)
        valor_a_prazo = prazo_lookup.get(vid, 0.0)
        indice_pct = (
            (valor_aberto / valor_a_prazo * 100)
            if (valor_aberto is not None and valor_a_prazo > 0) else None
        )
        linhas.append({
            "vendedor_id": vid,
            "nome": v["nome"],
            "loja": v["loja"],
            "valor_a_prazo": valor_a_prazo,
            "valor_em_aberto": valor_aberto,
            "indice_pct": indice_pct,
            "nivel_risco": nivel_risco_inadimplencia(indice_pct),
        })
    return pd.DataFrame(linhas)


@cache_leitura()
def get_inadimplencia_historico_vendedor(vendedor_id):
    """Série histórica (uma linha por mês de venda com valor em aberto lançado) do
    vendedor: valor vendido a prazo, valor em aberto e o índice de inadimplência (%)
    de cada mês, ordenada cronologicamente — base para calcular tendência.
    Implementado em cima da versão em lote (_inadimplencia_historico_lote)."""
    hist = _inadimplencia_historico_lote(loja=None, apenas_ativos=False)
    cols = ["ano_venda", "mes_venda", "valor_a_prazo", "valor_em_aberto", "indice_pct"]
    if hist.empty:
        return pd.DataFrame(columns=cols)
    sub = hist[hist["vendedor_id"] == vendedor_id][cols].sort_values(["ano_venda", "mes_venda"])
    return sub.reset_index(drop=True)


@cache_leitura()
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


@cache_leitura()
def get_indice_inadimplencia_resumo_todos_vendedores(loja=None):
    """Versão EM LOTE de get_indice_inadimplencia_resumo_vendedor para todos os
    vendedores ativos do filtro de uma vez — usada no Dashboard para não fazer um
    loop de query por vendedor. Retorna um dict {vendedor_id: {..., "historico":
    DataFrame, "nome": str, "loja": str}}."""
    hist_todos = _inadimplencia_historico_lote(loja=loja, apenas_ativos=True)
    vendedores_df = get_vendedores(loja=loja, apenas_ativos=True)
    cols = ["ano_venda", "mes_venda", "valor_a_prazo", "valor_em_aberto", "indice_pct"]
    resultado = {}
    for _, v in vendedores_df.iterrows():
        vid = int(v["id"])
        if not hist_todos.empty:
            hist_v = hist_todos[hist_todos["vendedor_id"] == vid][cols].sort_values(["ano_venda", "mes_venda"]).reset_index(drop=True)
        else:
            hist_v = pd.DataFrame(columns=cols)

        hist_valida = hist_v[hist_v["valor_a_prazo"] > 0] if not hist_v.empty else hist_v
        if hist_valida.empty:
            media_pct, n_meses = None, 0
        else:
            total_prazo = float(hist_valida["valor_a_prazo"].sum())
            total_aberto = float(hist_valida["valor_em_aberto"].sum())
            media_pct = (total_aberto / total_prazo * 100) if total_prazo > 0 else None
            n_meses = int(len(hist_valida))

        resultado[vid] = {
            "nome": v["nome"],
            "loja": v["loja"],
            "media_ponderada_pct": media_pct,
            "n_meses": n_meses,
            "nivel_risco": nivel_risco_inadimplencia(media_pct),
            "historico": hist_v,
        }
    return resultado


@cache_leitura()
def get_indice_inadimplencia_geral_loja():
    """Índice de inadimplência histórico agregado por loja: soma de todo o valor em
    aberto já lançado ÷ soma de todo o valor vendido a prazo, considerando TODOS os
    vendedores ativos de cada loja (ponderado por R$, mesma lógica do índice por
    vendedor) — mais um índice "Geral" com as duas lojas juntas. Retorna
    {"Porteira": {...}, "Casa de Adubo": {...}, "Geral": {...}}, cada valor com
    media_ponderada_pct, n_meses e nivel_risco."""
    hist = _inadimplencia_historico_lote(loja=None, apenas_ativos=True)
    hist_valida = hist[hist["valor_a_prazo"] > 0] if not hist.empty else hist

    def _resumo(sub):
        if sub.empty:
            return {"media_ponderada_pct": None, "n_meses": 0, "nivel_risco": nivel_risco_inadimplencia(None)}
        total_prazo = float(sub["valor_a_prazo"].sum())
        total_aberto = float(sub["valor_em_aberto"].sum())
        media_pct = (total_aberto / total_prazo * 100) if total_prazo > 0 else None
        return {
            "media_ponderada_pct": media_pct,
            "n_meses": int(len(sub)),
            "nivel_risco": nivel_risco_inadimplencia(media_pct),
        }

    resultado = {}
    for loja in LOJAS:
        sub_loja = hist_valida[hist_valida["loja"] == loja] if not hist_valida.empty else hist_valida
        resultado[loja] = _resumo(sub_loja)
    resultado["Geral"] = _resumo(hist_valida)
    return resultado


# --------------------------------------------------------------------------
# Vendas por produto (relatório "Totais de Vendas Por Produto" do SGI) - uma
# linha por vendedor/dia/produto. Alimentado pela mesma automação (login +
# GitHub Actions) que alimenta vendas_diarias, e também pode ser importado via
# PDF manual no futuro seguindo o mesmo padrão. Cada sincronização de um
# vendedor/dia SUBSTITUI por completo os produtos daquele vendedor/dia (delete +
# insert), em vez de UPSERT por produto — evita registro "fantasma" de um
# produto que vendeu ontem mas não vende mais hoje.
# --------------------------------------------------------------------------
def upsert_vendas_produtos_dia(vendedor_id, data_venda, produtos):
    """`produtos`: lista de dicts com as chaves cod_produto, descricao_produto,
    marca, fornecedor, posit, vendas, qtd, qtd_cx, valor_total, pct (todas exceto
    cod_produto/descricao_produto/valor_total são opcionais/podem vir None)."""
    data_str = data_venda.isoformat() if hasattr(data_venda, "isoformat") else data_venda
    with get_engine().begin() as conn:
        conn.execute(
            text("DELETE FROM vendas_produtos_diarias WHERE vendedor_id=:vid AND data=:data"),
            {"vid": vendedor_id, "data": data_str},
        )
        if produtos:
            conn.execute(
                text(
                    """
                    INSERT INTO vendas_produtos_diarias
                        (vendedor_id, data, cod_produto, descricao_produto, marca, fornecedor,
                         posit, vendas, qtd, qtd_cx, valor_total, pct)
                    VALUES
                        (:vendedor_id, :data, :cod_produto, :descricao_produto, :marca, :fornecedor,
                         :posit, :vendas, :qtd, :qtd_cx, :valor_total, :pct)
                    """
                ),
                [
                    {
                        "vendedor_id": vendedor_id,
                        "data": data_str,
                        "cod_produto": p.get("cod_produto"),
                        "descricao_produto": p.get("descricao_produto"),
                        "marca": p.get("marca"),
                        "fornecedor": p.get("fornecedor"),
                        "posit": p.get("posit"),
                        "vendas": p.get("vendas"),
                        "qtd": p.get("qtd"),
                        "qtd_cx": p.get("qtd_cx"),
                        "valor_total": p.get("valor_total") or 0.0,
                        "pct": p.get("pct"),
                    }
                    for p in produtos
                ],
            )
    limpar_cache()


@cache_leitura()
def get_produtos_mais_vendidos(ano, mes, loja=None, vendedor_id=None, top_n=15):
    """Ranking de produtos mais vendidos (por valor R$) no mês, somando os
    lançamentos diários. Filtra por loja e/ou por um vendedor específico."""
    cols = ["cod_produto", "descricao_produto", "marca", "fornecedor", "qtd_total", "valor_total"]
    ini = date(ano, mes, 1)
    ultimo_dia = calendar.monthrange(ano, mes)[1]
    fim = date(ano, mes, ultimo_dia)
    query = """
        SELECT vp.cod_produto, vp.descricao_produto, vp.marca, vp.fornecedor,
               SUM(vp.qtd) AS qtd_total, SUM(vp.valor_total) AS valor_total
        FROM vendas_produtos_diarias vp
        JOIN vendedores v ON v.id = vp.vendedor_id
        WHERE vp.data BETWEEN :ini AND :fim
    """
    params = {"ini": ini, "fim": fim}
    if loja and loja != "Ambas":
        query += " AND v.loja = :loja"
        params["loja"] = loja
    if vendedor_id:
        query += " AND vp.vendedor_id = :vendedor_id"
        params["vendedor_id"] = vendedor_id
    query += """
        GROUP BY vp.cod_produto, vp.descricao_produto, vp.marca, vp.fornecedor
        ORDER BY valor_total DESC
        LIMIT :top_n
    """
    params["top_n"] = top_n
    df = pd.read_sql_query(text(query), get_engine(), params=params)
    if df.empty:
        return pd.DataFrame(columns=cols)
    df["qtd_total"] = df["qtd_total"].astype(float)
    df["valor_total"] = df["valor_total"].astype(float)
    return df


@cache_leitura()
def get_produtos_mais_vendidos_por_vendedor(ano, mes, loja=None, top_n=5):
    """Top N produtos (por valor R$) de CADA vendedor ativo no mês, numa única
    consulta (não uma por vendedor) — o ranking por vendedor é feito em pandas
    (groupby + head) em cima do total já agregado por produto/vendedor."""
    cols = ["vendedor_id", "nome", "loja", "cod_produto", "descricao_produto", "marca", "fornecedor", "qtd_total", "valor_total"]
    ini = date(ano, mes, 1)
    ultimo_dia = calendar.monthrange(ano, mes)[1]
    fim = date(ano, mes, ultimo_dia)
    query = """
        SELECT vp.vendedor_id, v.nome, v.loja, vp.cod_produto, vp.descricao_produto,
               vp.marca, vp.fornecedor, SUM(vp.qtd) AS qtd_total, SUM(vp.valor_total) AS valor_total
        FROM vendas_produtos_diarias vp
        JOIN vendedores v ON v.id = vp.vendedor_id
        WHERE vp.data BETWEEN :ini AND :fim AND v.ativo = TRUE
    """
    params = {"ini": ini, "fim": fim}
    if loja and loja != "Ambas":
        query += " AND v.loja = :loja"
        params["loja"] = loja
    query += (
        " GROUP BY vp.vendedor_id, v.nome, v.loja, vp.cod_produto, vp.descricao_produto, vp.marca, vp.fornecedor"
    )
    df = pd.read_sql_query(text(query), get_engine(), params=params)
    if df.empty:
        return pd.DataFrame(columns=cols)
    df["qtd_total"] = df["qtd_total"].astype(float)
    df["valor_total"] = df["valor_total"].astype(float)
    df = df.sort_values(["vendedor_id", "valor_total"], ascending=[True, False])
    return df.groupby("vendedor_id").head(top_n).reset_index(drop=True)


@cache_leitura()
def get_ranking_marca_fornecedor(ano, mes, loja=None, agrupar_por="fornecedor", top_n=15):
    """Ranking por marca OU fornecedor (agrupar_por: 'marca' ou 'fornecedor'), somando
    o valor R$ vendido no mês por todos os vendedores do filtro."""
    if agrupar_por not in ("marca", "fornecedor"):
        raise ValueError("agrupar_por deve ser 'marca' ou 'fornecedor'")
    cols = ["grupo", "qtd_total", "valor_total"]
    ini = date(ano, mes, 1)
    ultimo_dia = calendar.monthrange(ano, mes)[1]
    fim = date(ano, mes, ultimo_dia)
    query = f"""
        SELECT vp.{agrupar_por} AS grupo, SUM(vp.qtd) AS qtd_total, SUM(vp.valor_total) AS valor_total
        FROM vendas_produtos_diarias vp
        JOIN vendedores v ON v.id = vp.vendedor_id
        WHERE vp.data BETWEEN :ini AND :fim AND vp.{agrupar_por} IS NOT NULL AND vp.{agrupar_por} <> ''
    """
    params = {"ini": ini, "fim": fim}
    if loja and loja != "Ambas":
        query += " AND v.loja = :loja"
        params["loja"] = loja
    query += f" GROUP BY vp.{agrupar_por} ORDER BY valor_total DESC LIMIT :top_n"
    params["top_n"] = top_n
    df = pd.read_sql_query(text(query), get_engine(), params=params)
    if df.empty:
        return pd.DataFrame(columns=cols)
    df["qtd_total"] = df["qtd_total"].astype(float)
    df["valor_total"] = df["valor_total"].astype(float)
    return df


@cache_leitura()
def get_resumo_produtos_mes(ano, mes, loja=None):
    """KPIs gerais de Vendas por Produto no mês: faturamento total, quantidade
    total de itens vendidos, ticket médio por item (faturamento ÷ qtd) e a
    contagem de produtos/fornecedores/marcas distintos que tiveram venda no
    período — a base de qualquer análise de portfólio de produtos."""
    ini = date(ano, mes, 1)
    ultimo_dia = calendar.monthrange(ano, mes)[1]
    fim = date(ano, mes, ultimo_dia)
    query = """
        SELECT
            COALESCE(SUM(vp.valor_total), 0) AS faturamento_total,
            COALESCE(SUM(vp.qtd), 0) AS qtd_total,
            COUNT(DISTINCT vp.cod_produto) AS n_produtos,
            COUNT(DISTINCT NULLIF(vp.fornecedor, '')) AS n_fornecedores,
            COUNT(DISTINCT NULLIF(vp.marca, '')) AS n_marcas
        FROM vendas_produtos_diarias vp
        JOIN vendedores v ON v.id = vp.vendedor_id
        WHERE vp.data BETWEEN :ini AND :fim
    """
    params = {"ini": ini, "fim": fim}
    if loja and loja != "Ambas":
        query += " AND v.loja = :loja"
        params["loja"] = loja
    df = pd.read_sql_query(text(query), get_engine(), params=params)
    row = df.iloc[0]
    faturamento_total = float(row["faturamento_total"])
    qtd_total = float(row["qtd_total"])
    return {
        "faturamento_total": faturamento_total,
        "qtd_total": qtd_total,
        "ticket_medio_item": (faturamento_total / qtd_total) if qtd_total > 0 else 0.0,
        "n_produtos": int(row["n_produtos"]),
        "n_fornecedores": int(row["n_fornecedores"]),
        "n_marcas": int(row["n_marcas"]),
    }


@cache_leitura()
def get_curva_abc_produtos(ano, mes, loja=None):
    """Curva ABC dos produtos vendidos no mês — a régua clássica de gestão de
    portfólio/estoque (princípio de Pareto aplicado a produto): ordena por
    faturamento decrescente, calcula o % de participação e o % acumulado de cada
    produto, e classifica em:
      A — produtos que, somados, respondem pelos primeiros 80% do faturamento
          (o "poucos produtos que fazem a diferença", merecem atenção prioritária
          de estoque/negociação com fornecedor);
      B — de 80% a 95% acumulado (relevância intermediária);
      C — os últimos 5% (cauda longa, muitos produtos com pouca representatividade
          individual — candidatos a revisão de mix)."""
    cols = [
        "cod_produto", "descricao_produto", "marca", "fornecedor",
        "qtd_total", "valor_total", "pct_participacao", "pct_acumulado", "classe",
    ]
    df = get_produtos_mais_vendidos(ano, mes, loja=loja, top_n=100000)
    if df.empty:
        return pd.DataFrame(columns=cols)

    df = df.sort_values("valor_total", ascending=False).reset_index(drop=True)
    total = df["valor_total"].sum()
    if total <= 0:
        return pd.DataFrame(columns=cols)

    df["pct_participacao"] = df["valor_total"] / total * 100
    df["pct_acumulado"] = df["pct_participacao"].cumsum()

    def _classificar(pct_acum):
        if pct_acum <= 80:
            return "A"
        elif pct_acum <= 95:
            return "B"
        return "C"

    df["classe"] = df["pct_acumulado"].apply(_classificar)
    return df[cols]


@cache_leitura()
def get_comparativo_marca_fornecedor(ano, mes, loja=None, agrupar_por="fornecedor", top_n=10):
    """Ranking por marca/fornecedor do mês (ver get_ranking_marca_fornecedor), com
    o faturamento do mês anterior ao lado e o % de crescimento — identifica quem
    está em alta ou em queda, não só o tamanho absoluto."""
    ano_ant, mes_ant = mes_anterior(ano, mes)
    atual = get_ranking_marca_fornecedor(ano, mes, loja=loja, agrupar_por=agrupar_por, top_n=top_n)
    cols = list(atual.columns) + ["valor_total_anterior", "crescimento_pct"]
    if atual.empty:
        return pd.DataFrame(columns=cols)

    anterior = get_ranking_marca_fornecedor(ano_ant, mes_ant, loja=loja, agrupar_por=agrupar_por, top_n=100000)
    anterior_lookup = anterior.set_index("grupo")["valor_total"].to_dict() if not anterior.empty else {}

    atual = atual.copy()
    atual["valor_total_anterior"] = atual["grupo"].map(anterior_lookup).fillna(0.0)
    atual["crescimento_pct"] = atual.apply(
        lambda r: (
            (r["valor_total"] - r["valor_total_anterior"]) / r["valor_total_anterior"] * 100
        ) if r["valor_total_anterior"] > 0 else None,
        axis=1,
    )
    return atual


# --------------------------------------------------------------------------
# Regras de negócio / KPIs
# --------------------------------------------------------------------------
def mes_anterior(ano, mes):
    if mes == 1:
        return ano - 1, 12
    return ano, mes - 1


@cache_leitura()
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


@cache_leitura()
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