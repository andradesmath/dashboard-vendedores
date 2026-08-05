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
    ddl = [
        """
        CREATE TABLE IF NOT EXISTS vendedores (
            id SERIAL PRIMARY KEY,
            nome TEXT NOT NULL,
            email TEXT,
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
            qtd_clientes INTEGER NOT NULL DEFAULT 0,
            UNIQUE (vendedor_id, data)
        )
        """,
    ]
    with get_engine().begin() as conn:
        for stmt in ddl:
            conn.execute(text(stmt))


# --------------------------------------------------------------------------
# Vendedores
# --------------------------------------------------------------------------
def add_vendedor(nome, email, loja):
    with get_engine().begin() as conn:
        conn.execute(
            text("INSERT INTO vendedores (nome, email, loja) VALUES (:nome, :email, :loja)"),
            {"nome": nome, "email": email, "loja": loja},
        )


def update_vendedor(vendedor_id, nome, email, loja, ativo=True):
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "UPDATE vendedores SET nome=:nome, email=:email, loja=:loja, ativo=:ativo "
                "WHERE id=:id"
            ),
            {"nome": nome, "email": email, "loja": loja, "ativo": bool(ativo), "id": vendedor_id},
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
def upsert_venda(vendedor_id, data_venda, valor_realizado, qtd_clientes):
    with get_engine().begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO vendas_diarias (vendedor_id, data, valor_realizado, qtd_clientes)
                VALUES (:vendedor_id, :data, :valor_realizado, :qtd_clientes)
                ON CONFLICT (vendedor_id, data)
                DO UPDATE SET valor_realizado = EXCLUDED.valor_realizado,
                              qtd_clientes = EXCLUDED.qtd_clientes
                """
            ),
            {
                "vendedor_id": vendedor_id,
                "data": data_venda,
                "valor_realizado": valor_realizado,
                "qtd_clientes": qtd_clientes,
            },
        )


def delete_venda(venda_id):
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM vendas_diarias WHERE id=:id"), {"id": venda_id})


def get_vendas_mes(ano, mes, loja=None):
    ini = date(ano, mes, 1)
    ultimo_dia = calendar.monthrange(ano, mes)[1]
    fim = date(ano, mes, ultimo_dia)
    query = """
        SELECT vd.id, v.id as vendedor_id, v.nome, v.loja, vd.data, vd.valor_realizado, vd.qtd_clientes
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
            "SELECT data, valor_realizado, qtd_clientes FROM vendas_diarias "
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
        SELECT vd.id, v.nome, v.loja, vd.data, vd.valor_realizado, vd.qtd_clientes
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
# Regras de negócio / KPIs
# --------------------------------------------------------------------------
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
