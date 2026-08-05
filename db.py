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
    ]
    with get_engine().begin() as conn:
        for stmt in migracoes_pre:
            conn.execute(text(stmt))
        for stmt in ddl:
            conn.execute(text(stmt))
        # Migração: remove a coluna email de bancos criados com a versão anterior do schema.
        conn.execute(text("ALTER TABLE vendedores DROP COLUMN IF EXISTS email"))
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