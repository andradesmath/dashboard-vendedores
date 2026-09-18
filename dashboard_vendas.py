"""
====================================================================
 DASHBOARD DE DESEMPENHO DE VENDEDORES - Porteira & Casa de Adubo
 (versão com banco PostgreSQL na nuvem + export em PDF)
====================================================================

VISÃO GERAL DA STACK
--------------------------------------------------------------------
   Interface .......... Streamlit
   Dados ............... Pandas
   Banco ............... PostgreSQL (recomendado: Supabase, plano gratuito)
   Export individual ... PDF (reportlab, sem dependências de binário)
   Deploy .............. Streamlit Community Cloud (gratuito)

ARQUIVOS DO PROJETO
--------------------------------------------------------------------
   db.py                 -> camada de dados (Postgres/SQLAlchemy), CRUD e regras de negócio
   pdf_export.py          -> geração dos relatórios em PDF por vendedor
   dashboard_vendas.py    -> este arquivo, interface Streamlit
   seed_data.py           -> script opcional de dados fictícios
   requirements.txt       -> dependências
   .env.example            -> modelo de variável de ambiente para rodar localmente
   secrets.toml.example    -> modelo de secrets para o Streamlit Cloud (copie o
                              conteúdo para uma pasta .streamlit/secrets.toml)
   .gitignore

PASSO 1 - CRIAR O BANCO POSTGRES GRATUITO (Supabase)
--------------------------------------------------------------------
   1) Crie uma conta em https://supabase.com e um novo projeto (gratuito).
   2) Em "Project Settings" > "Database" > "Connection string", copie a URI no
      formato "Connection pooling" (porta 6543, modo transaction) — é a
      recomendada para apps como o Streamlit que abrem várias conexões curtas.
      Ela tem este formato:
         postgresql://postgres.[ref]:[SENHA]@aws-x-region.pooler.supabase.com:6543/postgres
   3) Guarde essa URL — ela será usada como DATABASE_URL nos passos abaixo.

PASSO 2 - RODAR LOCALMENTE
--------------------------------------------------------------------
   1) Crie um ambiente virtual (opcional, recomendado):
         python -m venv venv
         source venv/bin/activate        (Linux/Mac)
         venv\\Scripts\\activate           (Windows)

   2) Instale as dependências:
         pip install -r requirements.txt

   3) Copie .env.example para .env e cole sua DATABASE_URL do Supabase:
         cp .env.example .env

   4) (Opcional, recomendado na 1a vez) Popule o banco com dados fictícios:
         python seed_data.py

   5) Rode o app:
         streamlit run dashboard_vendas.py

      Acesse em http://localhost:8501

PASSO 3 - DEPLOY GRATUITO NO STREAMLIT COMMUNITY CLOUD
--------------------------------------------------------------------
   1) Suba os arquivos do projeto para um repositório no GitHub
      (NÃO suba o .env nem .streamlit/secrets.toml reais — o .gitignore já
      cuida disso).
   2) Acesse https://share.streamlit.io, conecte sua conta GitHub e clique em
      "New app", selecionando o repositório e o arquivo dashboard_vendas.py.
   3) Antes (ou depois) do deploy, vá em "Settings" > "Secrets" do app e cole:
         DATABASE_URL = "postgresql://postgres.[ref]:[SENHA]@aws-x-region.pooler.supabase.com:6543/postgres"
   4) Clique em "Deploy". O app ficará disponível em uma URL pública gratuita
      do tipo https://SEU-APP.streamlit.app

   Observação: no plano gratuito do Streamlit Cloud o app "dorme" após um
   período sem acessos e reinicia ao ser aberto de novo — como o banco agora é
   externo (Postgres/Supabase), os dados NÃO se perdem nesse processo.
====================================================================
"""
import calendar
import re
from datetime import date

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import db
import github_actions
import pdf_export
from sgi_relatorio import casar_vendedores, parse_relatorio_vendas_pdf, parse_valor_brl

# --------------------------------------------------------------------------
# Configuração da página e estilo corporativo (azul/verde)
# --------------------------------------------------------------------------
st.set_page_config(page_title="Desempenho de Vendedores", page_icon="📊", layout="wide")

AZUL = "#1a5276"
AZUL_CLARO = "#2e86c1"
VERDE = "#1e8449"

st.markdown(
    f"""
    <style>
        .main {{ background-color: #f4f6f7; }}
        h1, h2, h3 {{ color: {AZUL}; }}
        div.stButton > button, div.stDownloadButton > button {{
            background-color: {AZUL}; color: white; border-radius: 6px; border: none;
        }}
        div.stButton > button:hover, div.stDownloadButton > button:hover {{
            background-color: {AZUL_CLARO}; color: white;
        }}
        div[data-testid="stForm"] {{
            background-color: white; padding: 1.2rem; border-radius: 10px;
            border: 1px solid #e0e0e0;
        }}
        .kpi-card {{
            background-color: white; border-radius: 10px; padding: 1rem 1.2rem;
            border-left: 6px solid {AZUL}; box-shadow: 0 1px 4px rgba(0,0,0,0.08);
            height: 100%;
        }}
        .kpi-title {{ font-size: 0.85rem; color: #555; margin-bottom: 0.2rem; }}
        .kpi-value {{ font-size: 1.5rem; font-weight: 700; color: {AZUL}; }}
    </style>
    """,
    unsafe_allow_html=True,
)

try:
    db.init_db()
except Exception as e:
    st.error(
        "Não foi possível conectar ao banco PostgreSQL. Verifique se DATABASE_URL está "
        "configurada corretamente (arquivo .env local ou Secrets no Streamlit Cloud)."
    )
    st.exception(e)
    st.stop()

if "versao_dados" not in st.session_state:
    st.session_state.versao_dados = 0

st.title("📊 Painel de Desempenho de Vendedores")
st.caption("Porteira & Casa de Adubo — controle de metas, realizado e produtividade comercial")

def parse_linha_importacao(linha):
    """Aceita 'Nome<TAB>Loja', 'Nome  Loja' (2+ espaços) ou 'Nome,Loja'."""
    partes = linha.split("\t")
    if len(partes) < 2:
        partes = re.split(r"\s{2,}", linha.strip())
    if len(partes) < 2:
        partes = linha.split(",")
    if len(partes) < 2:
        return None
    nome = partes[0].strip()
    loja = partes[1].strip()
    if not nome or not loja:
        return None
    return nome, loja


def normalizar_loja(texto):
    texto_norm = texto.strip().lower()
    for loja_valida in db.LOJAS:
        if loja_valida.lower() == texto_norm:
            return loja_valida
    return None


MESES_ABREV = {
    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
    "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
}


def parse_linha_historico(linha):
    """Espera: Nome<TAB>mes/ano<TAB>Meta<TAB>Realizado (aceita 2+ espaços como separador
    também). A coluna Realizado é opcional — se ausente (ex.: metas de meses futuros/atuais
    ainda sem resultado), assume 0."""
    partes = linha.split("\t")
    if len(partes) < 3:
        partes = re.split(r"\s{2,}", linha.strip())
    if len(partes) < 3:
        return None
    nome = partes[0].strip()
    mes_ano_bruto = partes[1].strip().lower()
    meta_bruta = partes[2].strip()
    realizado_bruto = partes[3].strip() if len(partes) > 3 else "0"

    m = re.match(r"([a-zç]{3})/(\d{4})", mes_ano_bruto)
    if not m:
        return None
    abrev, ano_str = m.groups()
    mes_num = MESES_ABREV.get(abrev)
    if not mes_num or not nome:
        return None

    meta_valor = parse_valor_brl(meta_bruta)
    realizado_valor = parse_valor_brl(realizado_bruto)
    if meta_valor is None or realizado_valor is None:
        return None

    return {"nome": nome, "ano": int(ano_str), "mes": mes_num, "meta": meta_valor, "realizado": realizado_valor}


def parse_linha_venda_diaria(linha):
    """Espera: Nome<TAB>DD/MM/AAAA<TAB>Valor (aceita 2+ espaços como separador também)."""
    partes = linha.split("\t")
    if len(partes) < 3:
        partes = re.split(r"\s{2,}", linha.strip())
    if len(partes) < 3:
        return None
    nome = partes[0].strip()
    data_bruta = partes[1].strip()
    valor_bruto = partes[2].strip()

    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", data_bruta)
    if not m:
        return None
    dia, mes, ano = (int(x) for x in m.groups())
    try:
        data_obj = date(ano, mes, dia)
    except ValueError:
        return None

    valor = parse_valor_brl(valor_bruto)
    if valor is None or not nome:
        return None

    return {"nome": nome, "data": data_obj, "valor": valor}


def campos_mix_pagamento(key_prefix):
    """Renderiza um número de entrada em R$ por modalidade de pagamento, em colunas.
    Devolve (percentuais_dict, soma_informada_rs) — o percentual de cada modalidade
    é calculado automaticamente dividindo o valor em R$ informado pela soma de
    todos os valores informados (não precisa bater com o realizado do dia/mês até
    o centavo, só precisa refletir a proporção certa entre as modalidades). Deve
    ser chamada dentro de um `with st.form(...)`."""
    valores_brutos = {}
    cols = st.columns(len(db.MODALIDADES_PAGAMENTO))
    for col, modalidade in zip(cols, db.MODALIDADES_PAGAMENTO):
        with col:
            valores_brutos[modalidade] = st.number_input(
                modalidade, min_value=0.0, step=50.0, format="%.2f",
                key=f"{key_prefix}_{modalidade}",
            )
    soma_informada = sum(valores_brutos.values())
    percentuais = {
        m: (v / soma_informada * 100 if soma_informada > 0 else 0.0)
        for m, v in valores_brutos.items()
    }
    return percentuais, soma_informada


tab_cadastros, tab_metas, tab_lancamentos, tab_dashboard = st.tabs(
    ["📋 Cadastros", "🎯 Metas", "📝 Lançamentos Diários", "📊 Dashboard"]
)

# ==========================================================================
# ABA 1 - CADASTROS
# ==========================================================================
with tab_cadastros:
    st.subheader("Vendedores cadastrados")
    vendedores_df = db.get_vendedores()

    if vendedores_df.empty:
        st.info("Nenhum vendedor cadastrado ainda. Use o formulário abaixo para adicionar.")
    else:
        st.dataframe(
            vendedores_df[["id", "nome", "loja", "ativo"]].rename(
                columns={"id": "ID", "nome": "Nome", "loja": "Loja", "ativo": "Ativo"}
            ),
            use_container_width=True,
            hide_index=True,
        )

    col_add, col_edit = st.columns(2)

    with col_add:
        st.markdown("##### ➕ Adicionar vendedor")
        with st.form("form_add_vendedor", clear_on_submit=True):
            nome = st.text_input("Nome completo *")
            loja = st.selectbox("Loja *", db.LOJAS)
            enviado = st.form_submit_button("Adicionar vendedor")
            if enviado:
                if not nome.strip():
                    st.error("O nome completo é obrigatório.")
                else:
                    db.add_vendedor(nome.strip(), loja)
                    st.success(f"Vendedor '{nome}' adicionado com sucesso!")
                    st.session_state.versao_dados += 1
                    st.rerun()

    with col_edit:
        st.markdown("##### ✏️ Editar / Excluir vendedor")
        if vendedores_df.empty:
            st.caption("Cadastre um vendedor primeiro.")
        else:
            opcoes = {
                f"{row['nome']} ({row['loja']})": row["id"] for _, row in vendedores_df.iterrows()
            }
            escolha = st.selectbox("Selecione o vendedor", list(opcoes.keys()), key="sel_editar")
            vendedor_id = opcoes[escolha]
            vendedor_atual = vendedores_df[vendedores_df["id"] == vendedor_id].iloc[0]

            with st.form("form_edit_vendedor"):
                novo_nome = st.text_input("Nome completo *", value=vendedor_atual["nome"])
                st.caption(
                    f"Loja atual: **{vendedor_atual['loja']}**. Pra mudar de loja, use "
                    "\"🔄 Transferir vendedor de loja\" logo abaixo — mantém o histórico de "
                    "vendas correto na loja antiga."
                )
                novo_ativo = st.checkbox("Ativo", value=bool(vendedor_atual["ativo"]))
                col_salvar, col_excluir = st.columns(2)
                salvar = col_salvar.form_submit_button("💾 Salvar alterações")
                excluir = col_excluir.form_submit_button("🗑️ Excluir vendedor")

                if salvar:
                    if not novo_nome.strip():
                        st.error("O nome completo é obrigatório.")
                    else:
                        db.update_vendedor(vendedor_id, novo_nome.strip(), vendedor_atual["loja"], novo_ativo)
                        st.success("Vendedor atualizado com sucesso!")
                        st.session_state.versao_dados += 1
                        st.rerun()

                if excluir:
                    db.delete_vendedor(vendedor_id)
                    st.warning(f"Vendedor '{vendedor_atual['nome']}' excluído (metas e vendas associadas também foram removidas).")
                    st.session_state.versao_dados += 1
                    st.rerun()

    st.markdown("---")
    with st.expander("🔄 Transferir vendedor de loja", expanded=False):
        st.caption(
            "Muda a loja de um vendedor a partir de uma data — as vendas ANTERIORES a essa "
            "data continuam contando pra loja antiga em todos os relatórios (Comparativo "
            "entre Lojas, rankings, histórico); as vendas dessa data em diante passam a "
            "contar pra loja nova, mesmo que já estivessem sincronizadas ou sejam "
            "resincronizadas depois."
        )
        if vendedores_df.empty:
            st.caption("Cadastre um vendedor primeiro.")
        else:
            opcoes_transf = {
                f"{row['nome']} ({row['loja']})": row["id"] for _, row in vendedores_df.iterrows()
            }
            escolha_transf = st.selectbox(
                "Selecione o vendedor", list(opcoes_transf.keys()), key="sel_transferir"
            )
            vendedor_id_transf = opcoes_transf[escolha_transf]
            vendedor_atual_transf = vendedores_df[vendedores_df["id"] == vendedor_id_transf].iloc[0]
            loja_atual_transf = vendedor_atual_transf["loja"]
            lojas_destino = [loja for loja in db.LOJAS if loja != loja_atual_transf]

            with st.form("form_transferir_vendedor"):
                col_t1, col_t2 = st.columns(2)
                with col_t1:
                    st.text_input("Loja atual", value=loja_atual_transf, disabled=True)
                with col_t2:
                    loja_nova_transf = st.selectbox("Nova loja *", lojas_destino)
                data_efetiva_transf = st.date_input(
                    "A partir de qual data (inclusive)?", value=date.today(),
                    max_value=date.today(), key="data_efetiva_transf",
                )
                confirmar_transf = st.form_submit_button("🔄 Transferir vendedor")

                if confirmar_transf:
                    try:
                        resultado_transf = db.transferir_vendedor_loja(
                            vendedor_id_transf, loja_nova_transf, data_efetiva_transf
                        )
                        st.success(
                            f"'{resultado_transf['nome']}' transferido(a) de "
                            f"{resultado_transf['loja_anterior']} para {resultado_transf['loja_nova']} "
                            f"a partir de {data_efetiva_transf.strftime('%d/%m/%Y')}. O histórico "
                            "anterior a essa data continua na loja antiga."
                        )
                        st.session_state.versao_dados += 1
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))

            historico_transf = db.get_transferencias_vendedor()
            if not historico_transf.empty:
                st.markdown("###### Histórico de transferências")
                hist_fmt = historico_transf.copy()
                hist_fmt["data_efetiva"] = pd.to_datetime(hist_fmt["data_efetiva"]).dt.strftime("%d/%m/%Y")
                st.dataframe(
                    hist_fmt[["nome", "loja_anterior", "loja_nova", "data_efetiva"]].rename(
                        columns={
                            "nome": "Vendedor", "loja_anterior": "De", "loja_nova": "Para",
                            "data_efetiva": "A partir de",
                        }
                    ),
                    use_container_width=True,
                    hide_index=True,
                )

    st.markdown("---")
    with st.expander("📥 Importar vendedores em lote"):
        st.caption(
            "Cole uma lista com um vendedor por linha, no formato 'Nome' seguido da loja "
            "(separados por TAB, vírgula, ou copiados direto de uma planilha/Excel). "
            "Lojas aceitas: Porteira, Casa de Adubo."
        )
        texto_import = st.text_area(
            "Lista de vendedores", height=200, key="texto_import_vendedores",
            placeholder="FELIPE\tPorteira\nELIZANGELA BATISTA\tPorteira\nADRIELY\tCasa de Adubo",
        )

        if st.button("🔍 Pré-visualizar importação"):
            linhas = [l for l in texto_import.splitlines() if l.strip()]
            registros_validos = []
            erros = []
            for linha in linhas:
                resultado = parse_linha_importacao(linha)
                if not resultado:
                    erros.append(f"Linha ignorada (formato não reconhecido): '{linha}'")
                    continue
                nome_bruto, loja_bruta = resultado
                loja_normalizada = normalizar_loja(loja_bruta)
                if not loja_normalizada:
                    erros.append(
                        f"Loja inválida para '{nome_bruto}': '{loja_bruta}' "
                        "(use Porteira ou Casa de Adubo)"
                    )
                    continue
                registros_validos.append({"nome": nome_bruto.title(), "loja": loja_normalizada})
            st.session_state["import_preview"] = registros_validos
            st.session_state["import_erros"] = erros

        erros_preview = st.session_state.get("import_erros", [])
        preview = st.session_state.get("import_preview", [])

        for erro in erros_preview:
            st.warning(erro)

        if preview:
            nomes_existentes = {n.strip().lower() for n in db.get_vendedores()["nome"].tolist()}
            novos = [r for r in preview if r["nome"].strip().lower() not in nomes_existentes]
            ja_existentes = [r for r in preview if r["nome"].strip().lower() in nomes_existentes]

            if novos:
                st.write(f"**{len(novos)} vendedor(es) novo(s) para importar:**")
                st.dataframe(
                    pd.DataFrame(novos).rename(columns={"nome": "Nome", "loja": "Loja"}),
                    use_container_width=True, hide_index=True,
                )
            else:
                st.info("Nenhum vendedor novo para importar (todos já cadastrados).")

            if ja_existentes:
                st.caption(
                    f"{len(ja_existentes)} já cadastrado(s) e serão ignorados: "
                    + ", ".join(r["nome"] for r in ja_existentes)
                )

            if novos and st.button("✅ Confirmar importação"):
                for registro in novos:
                    db.add_vendedor(registro["nome"], registro["loja"])
                st.success(f"{len(novos)} vendedor(es) importado(s) com sucesso!")
                st.session_state.pop("import_preview", None)
                st.session_state.pop("import_erros", None)
                st.session_state.versao_dados += 1
                st.rerun()

# ==========================================================================
# ABA 2 - METAS
# ==========================================================================
with tab_metas:
    st.subheader("Lançamento de metas mensais")
    vendedores_df = db.get_vendedores(apenas_ativos=True)

    if vendedores_df.empty:
        st.info("Cadastre vendedores na aba 'Cadastros' antes de lançar metas.")
    else:
        with st.form("form_meta"):
            col1, col2, col3, col4 = st.columns(4)
            opcoes_vend = {
                f"{row['nome']} ({row['loja']})": row["id"] for _, row in vendedores_df.iterrows()
            }
            with col1:
                escolha_vend = st.selectbox("Vendedor *", list(opcoes_vend.keys()))
            with col2:
                ano_meta = st.number_input(
                    "Ano *", min_value=2020, max_value=2035, value=date.today().year, step=1
                )
            with col3:
                mes_meta = st.selectbox(
                    "Mês *", list(db.MESES_PT.keys()), format_func=lambda m: db.MESES_PT[m],
                    index=date.today().month - 1,
                )
            with col4:
                valor_meta = st.number_input("Meta de vendas (R$) *", min_value=0.0, step=100.0, format="%.2f")

            enviado_meta = st.form_submit_button("💾 Salvar meta")
            if enviado_meta:
                vendedor_id = opcoes_vend[escolha_vend]
                db.upsert_meta(vendedor_id, int(ano_meta), int(mes_meta), float(valor_meta))
                st.success(
                    f"Meta de {db.formatar_moeda(valor_meta)} lançada para {escolha_vend} "
                    f"em {db.MESES_PT[mes_meta]}/{ano_meta}."
                )
                st.session_state.versao_dados += 1
                st.rerun()

    st.markdown("---")
    st.subheader("🧾 Pedidos do mês")
    st.caption(
        "Total de pedidos no mês, quando não há quebra diária (ex.: meses "
        "históricos ou fechamento mensal). Some ao total já lançado dia a dia, se houver."
    )
    vendedores_df_pedidos_mes = db.get_vendedores(apenas_ativos=True)
    if vendedores_df_pedidos_mes.empty:
        st.caption("Cadastre vendedores na aba 'Cadastros' primeiro.")
    else:
        with st.form("form_pedidos_mes"):
            colm1, colm2, colm3, colm4 = st.columns(4)
            opcoes_vend_cm = {
                f"{row['nome']} ({row['loja']})": row["id"]
                for _, row in vendedores_df_pedidos_mes.iterrows()
            }
            with colm1:
                escolha_vend_cm = st.selectbox("Vendedor *", list(opcoes_vend_cm.keys()), key="sel_pedidos_mes")
            with colm2:
                ano_cm = st.number_input(
                    "Ano *", min_value=2020, max_value=2035, value=date.today().year, step=1, key="ano_pedidos_mes"
                )
            with colm3:
                mes_cm = st.selectbox(
                    "Mês *", list(db.MESES_PT.keys()), format_func=lambda m: db.MESES_PT[m],
                    index=date.today().month - 1, key="mes_pedidos_mes",
                )
            with colm4:
                qtd_pedidos_mes = st.number_input(
                    "Pedidos *", min_value=0, step=1, format="%d", key="qtd_pedidos_mes"
                )

            enviado_pedidos_mes = st.form_submit_button("💾 Salvar pedidos do mês")
            if enviado_pedidos_mes:
                vendedor_id_cm = opcoes_vend_cm[escolha_vend_cm]
                db.upsert_pedidos_mensal(vendedor_id_cm, int(ano_cm), int(mes_cm), int(qtd_pedidos_mes))
                st.success(
                    f"{qtd_pedidos_mes} pedido(s) lançado(s) para {escolha_vend_cm} "
                    f"em {db.MESES_PT[mes_cm]}/{ano_cm}."
                )
                st.session_state.versao_dados += 1
                st.rerun()

    st.markdown("---")
    st.subheader("💳 Mix de pagamento do mês (dado agregado)")
    st.caption(
        "Use quando só se sabe o valor por modalidade do mês inteiro, sem quebra diária "
        "— útil para meses passados (histórico) ou para completar dias sem mix lançado. "
        "Informe o valor em R$ de cada modalidade; o percentual de cada uma é calculado "
        "automaticamente a partir da soma informada. Funciona com qualquer mês que já "
        "tenha realizado registrado (lançamento diário ou importação de histórico): o "
        "percentual resultante é aplicado sobre a parte do realizado do mês que ainda "
        "não tem mix diário informado."
    )
    vendedores_df_pgto_mes = db.get_vendedores(apenas_ativos=True)
    if vendedores_df_pgto_mes.empty:
        st.caption("Cadastre vendedores na aba 'Cadastros' primeiro.")
    else:
        with st.form("form_pagamento_mes", clear_on_submit=False):
            colp1, colp2, colp3 = st.columns(3)
            opcoes_vend_pg = {
                f"{row['nome']} ({row['loja']})": row["id"]
                for _, row in vendedores_df_pgto_mes.iterrows()
            }
            with colp1:
                escolha_vend_pg = st.selectbox("Vendedor *", list(opcoes_vend_pg.keys()), key="sel_pgto_mes")
            with colp2:
                ano_pg = st.number_input(
                    "Ano *", min_value=2020, max_value=2035, value=date.today().year, step=1, key="ano_pgto_mes"
                )
            with colp3:
                mes_pg = st.selectbox(
                    "Mês *", list(db.MESES_PT.keys()), format_func=lambda m: db.MESES_PT[m],
                    index=date.today().month - 1, key="mes_pgto_mes",
                )

            percentuais_mes, soma_mes = campos_mix_pagamento("pgto_mes")
            st.caption(
                f"Soma informada: {db.formatar_moeda(soma_mes)} — essa soma vira a base de "
                "100% para calcular o percentual de cada modalidade (não precisa bater com o "
                "realizado do mês até o centavo, só refletir a proporção certa)."
            )

            enviado_pgto_mes = st.form_submit_button("💾 Salvar mix de pagamento do mês")
            if enviado_pgto_mes:
                if soma_mes <= 0:
                    st.error("Informe pelo menos um valor em R$ maior que zero.")
                else:
                    vendedor_id_pg = opcoes_vend_pg[escolha_vend_pg]
                    db.upsert_pagamento_mensal(vendedor_id_pg, int(ano_pg), int(mes_pg), percentuais_mes)
                    st.success(
                        f"Mix de pagamento salvo para {escolha_vend_pg} em {db.MESES_PT[mes_pg]}/{ano_pg}."
                    )
                    st.session_state.versao_dados += 1
                    st.rerun()

    st.markdown("---")
    st.subheader("💸 Valores em aberto (vendas a prazo / Nota Promissória)")
    st.caption(
        "O risco de um mês só se confirma nos meses seguintes: o valor vendido a prazo "
        "(Nota Promissória) num mês deveria ser recebido a partir do mês seguinte. Lance "
        "aqui o valor que ainda está em aberto (não recebido) daquele mês de VENDA, "
        "a partir de 30 dias — ex.: vendeu R$ 100 mil a prazo em fevereiro, recebeu R$ 98 "
        "mil, lance R$ 2.000,00 em aberto para fevereiro. Pode atualizar o mesmo mês "
        "conforme a cobrança evolui."
    )
    vendedores_df_inadimp = db.get_vendedores(apenas_ativos=True)
    if vendedores_df_inadimp.empty:
        st.caption("Cadastre vendedores na aba 'Cadastros' primeiro.")
    else:
        with st.form("form_inadimplencia", clear_on_submit=False):
            coli1, coli2, coli3, coli4 = st.columns(4)
            opcoes_vend_ina = {
                f"{row['nome']} ({row['loja']})": row["id"]
                for _, row in vendedores_df_inadimp.iterrows()
            }
            with coli1:
                escolha_vend_ina = st.selectbox("Vendedor *", list(opcoes_vend_ina.keys()), key="sel_inadimp")
            with coli2:
                ano_ina = st.number_input(
                    "Ano da venda *", min_value=2020, max_value=2035, value=date.today().year,
                    step=1, key="ano_inadimp",
                )
            with coli3:
                mes_ina = st.selectbox(
                    "Mês da venda *", list(db.MESES_PT.keys()), format_func=lambda m: db.MESES_PT[m],
                    index=date.today().month - 1, key="mes_inadimp",
                )
            with coli4:
                valor_aberto_ina = st.number_input(
                    "Valor em aberto (R$) *", min_value=0.0, step=100.0, format="%.2f", key="valor_inadimp"
                )

            enviado_inadimp = st.form_submit_button("💾 Salvar valor em aberto")
            if enviado_inadimp:
                vendedor_id_ina = opcoes_vend_ina[escolha_vend_ina]
                db.upsert_inadimplencia(vendedor_id_ina, int(ano_ina), int(mes_ina), float(valor_aberto_ina))
                st.success(
                    f"Valor em aberto salvo: {escolha_vend_ina} — vendas de "
                    f"{db.MESES_PT[mes_ina]}/{ano_ina} — {db.formatar_moeda(valor_aberto_ina)} em aberto."
                )
                st.session_state.versao_dados += 1
                st.rerun()

    st.markdown("---")
    with st.expander("📥 Importar histórico de Meta x Realizado (meses anteriores)"):
        st.caption(
            "Cole uma linha por lançamento, no formato: Vendedor [TAB] Mês/Ano [TAB] Meta (R$) "
            "[TAB] Realizado (R$) — como copiado de uma planilha ou tabela de PDF. Mês/Ano no "
            "formato 'mai/2026'. A coluna Realizado é opcional: para metas de meses futuros ou "
            "do mês atual (ainda sem resultado), basta colar só Vendedor/Mês-Ano/Meta. Quando "
            "houver Realizado, ele entra nos totais e no ranking, mas sem granularidade diária."
        )
        texto_hist = st.text_area(
            "Dados históricos", height=220, key="texto_import_historico",
            placeholder="ADRIELY\tmai/2026\tR$ 115.000,00\tR$ 119.665,85",
        )

        if st.button("🔍 Pré-visualizar histórico"):
            linhas = [l for l in texto_hist.splitlines() if l.strip()]
            registros = []
            erros = []
            for linha in linhas:
                if "vendedor" in linha.lower() and "meta" in linha.lower():
                    continue  # cabeçalho da tabela, ignora
                resultado = parse_linha_historico(linha)
                if not resultado:
                    erros.append(f"Linha ignorada (formato não reconhecido): '{linha}'")
                    continue
                registros.append(resultado)
            st.session_state["import_hist_preview"] = registros
            st.session_state["import_hist_erros"] = erros

        erros_hist = st.session_state.get("import_hist_erros", [])
        registros_hist = st.session_state.get("import_hist_preview", [])

        for erro in erros_hist:
            st.warning(erro)

        if registros_hist:
            vendedores_todos = db.get_vendedores()
            nomes_existentes_map = {
                row["nome"].strip().lower(): row["nome"] for _, row in vendedores_todos.iterrows()
            }
            nomes_unicos = sorted({r["nome"] for r in registros_hist})
            nomes_nao_mapeados = [n for n in nomes_unicos if n.strip().lower() not in nomes_existentes_map]

            mapeamento = {}
            lojas_novos = {}
            if nomes_nao_mapeados:
                st.markdown(
                    "**Estes nomes do histórico não batem com o cadastro atual. "
                    "Diga a quem correspondem:**"
                )
                opcoes_vendedor = ["— Criar novo vendedor —"] + sorted(vendedores_todos["nome"].tolist())
                for nome_bruto in nomes_nao_mapeados:
                    mapeamento[nome_bruto] = st.selectbox(
                        f"'{nome_bruto}' corresponde a:", opcoes_vendedor, key=f"map_{nome_bruto}",
                    )
                    if mapeamento[nome_bruto] == "— Criar novo vendedor —":
                        lojas_novos[nome_bruto] = st.selectbox(
                            f"Loja para o novo vendedor '{nome_bruto}':", db.LOJAS,
                            key=f"loja_novo_{nome_bruto}",
                        )

            preview_df = pd.DataFrame(registros_hist)
            preview_df["Mês/Ano"] = preview_df.apply(lambda r: f"{db.MESES_PT[r['mes']]}/{r['ano']}", axis=1)
            preview_df["Meta"] = preview_df["meta"].apply(db.formatar_moeda)
            preview_df["Realizado"] = preview_df["realizado"].apply(db.formatar_moeda)
            st.write(f"**{len(registros_hist)} lançamento(s) no histórico colado:**")
            st.dataframe(
                preview_df[["nome", "Mês/Ano", "Meta", "Realizado"]].rename(columns={"nome": "Vendedor"}),
                use_container_width=True, hide_index=True,
            )

            if st.button("✅ Confirmar importação do histórico"):
                nomes_ids = {
                    row["nome"].strip().lower(): int(row["id"]) for _, row in vendedores_todos.iterrows()
                }
                importados = 0
                for registro in registros_hist:
                    chave = registro["nome"].strip().lower()
                    vendedor_id = nomes_ids.get(chave)
                    if vendedor_id is None:
                        escolha_map = mapeamento.get(registro["nome"])
                        if escolha_map == "— Criar novo vendedor —":
                            loja_novo = lojas_novos.get(registro["nome"], db.LOJAS[0])
                            db.add_vendedor(registro["nome"].title(), loja_novo)
                            atualizados = db.get_vendedores()
                            vendedor_id = int(
                                atualizados[atualizados["nome"] == registro["nome"].title()]["id"].iloc[-1]
                            )
                        elif escolha_map:
                            vendedor_id = int(
                                vendedores_todos[vendedores_todos["nome"] == escolha_map]["id"].iloc[0]
                            )
                        else:
                            continue
                        nomes_ids[chave] = vendedor_id

                    db.upsert_meta(vendedor_id, registro["ano"], registro["mes"], registro["meta"])
                    if registro["realizado"] != 0:
                        db.upsert_realizado_mensal(
                            vendedor_id, registro["ano"], registro["mes"], registro["realizado"]
                        )
                    importados += 1

                st.success(f"{importados} registro(s) de meta/realizado importado(s) com sucesso!")
                st.session_state.pop("import_hist_preview", None)
                st.session_state.pop("import_hist_erros", None)
                st.session_state.versao_dados += 1
                st.rerun()

    st.markdown("---")
    st.subheader("Histórico consolidado: Meta x Realizado")
    st.caption(
        "Um bloco por mês, com o realizado somado a partir dos lançamentos diários (e do "
        "histórico importado, quando não houver detalhamento diário) de cada vendedor, e o "
        "total do mês ao final de cada bloco."
    )
    filtro_loja_metas = st.selectbox("Filtrar por loja", ["Ambas"] + db.LOJAS, key="filtro_loja_metas")
    historico = db.get_historico_meta_realizado(loja=filtro_loja_metas)

    if historico.empty:
        st.info("Nenhuma meta ou venda lançada ainda.")
    else:
        historico["atingimento_pct"] = historico.apply(
            lambda r: (r["realizado"] / r["valor_meta"] * 100) if r["valor_meta"] > 0 else 0.0, axis=1
        )
        historico["ticket_medio"] = historico.apply(
            lambda r: (r["realizado"] / r["pedidos"]) if r["pedidos"] > 0 else 0.0, axis=1
        )

        periodos = (
            historico[["ano", "mes"]]
            .drop_duplicates()
            .sort_values(["ano", "mes"], ascending=[False, False])
        )

        for _, per in periodos.iterrows():
            ano_h, mes_h = int(per["ano"]), int(per["mes"])
            bloco = historico[(historico["ano"] == ano_h) & (historico["mes"] == mes_h)].sort_values(
                "realizado", ascending=False
            )

            st.markdown(f"##### {db.MESES_PT[mes_h]}/{ano_h}")

            sem_meta_bloco = bloco[bloco["valor_meta"] == 0]
            if not sem_meta_bloco.empty:
                st.caption(f"⚠️ Meta não lançada para: {', '.join(sem_meta_bloco['nome'].tolist())}")

            bloco_fmt = bloco.copy()
            bloco_fmt["Meta"] = bloco_fmt["valor_meta"].apply(db.formatar_moeda)
            bloco_fmt["Realizado"] = bloco_fmt["realizado"].apply(db.formatar_moeda)
            bloco_fmt["Atingimento (%)"] = bloco_fmt["atingimento_pct"].apply(lambda v: f"{v:.1f}%")
            bloco_fmt["Ticket Médio"] = bloco_fmt["ticket_medio"].apply(db.formatar_moeda)
            st.dataframe(
                bloco_fmt[
                    ["nome", "loja", "Meta", "Realizado", "Atingimento (%)", "pedidos", "Ticket Médio"]
                ].rename(columns={"nome": "Vendedor", "loja": "Loja", "pedidos": "Pedidos"}),
                use_container_width=True,
                hide_index=True,
            )

            meta_total_mes = float(bloco["valor_meta"].sum())
            realizado_total_mes = float(bloco["realizado"].sum())
            pedidos_total_mes = int(bloco["pedidos"].sum())
            atingimento_total_mes = (
                (realizado_total_mes / meta_total_mes * 100) if meta_total_mes > 0 else 0.0
            )
            ticket_total_mes = (
                (realizado_total_mes / pedidos_total_mes) if pedidos_total_mes > 0 else 0.0
            )

            st.markdown(
                f"**Total do mês:** Meta {db.formatar_moeda(meta_total_mes)} · "
                f"Realizado {db.formatar_moeda(realizado_total_mes)} · "
                f"Atingimento {atingimento_total_mes:.1f}% · "
                f"Pedidos {pedidos_total_mes} · "
                f"Ticket médio {db.formatar_moeda(ticket_total_mes)}"
            )
            st.markdown("---")

# ==========================================================================
# ABA 3 - LANÇAMENTOS DIÁRIOS
# ==========================================================================
with tab_lancamentos:
    with st.expander("🔄 Sincronizar com o SGI agora", expanded=False):
        st.caption(
            "Dispara a mesma automação que roda sozinha às 12h e 19h: loga no SGI, gera os "
            "relatórios do dia escolhido (Totais de Vendas e Totais de Vendas Por Produto) e "
            "grava no painel. Escolha uma data anterior pra reprocessar um dia específico. Roda "
            "no GitHub Actions (não aqui no navegador), então leva de 1 a 3 minutos — a tela "
            "atualiza sozinha quando terminar."
        )
        col_sync1, col_sync2, col_sync3 = st.columns([1, 1, 1.4])
        with col_sync1:
            loja_sync = st.selectbox("Loja", ["Ambas"] + db.LOJAS, key="loja_sync_manual")
        with col_sync2:
            data_sync = st.date_input(
                "Data", value=date.today(), max_value=date.today(), key="data_sync_manual"
            )
        with col_sync3:
            st.write("")
            disparar_sync = st.button("🔄 Puxar dados do SGI agora", key="btn_sync_manual")

        if disparar_sync:
            try:
                loja_param = None if loja_sync == "Ambas" else loja_sync
                data_param = None if data_sync == date.today() else data_sync.strftime("%d/%m/%Y")
                disparado_em = github_actions.disparar_sincronizacao(loja=loja_param, data=data_param)
                status_placeholder = st.empty()
                with st.spinner("Sincronizando com o SGI — isso pode levar alguns minutos..."):
                    sucesso, url_run = github_actions.aguardar_conclusao(
                        disparado_em, callback_status=status_placeholder.caption
                    )
                if sucesso:
                    db.limpar_cache()
                    st.success("Sincronização concluída! Atualizando os números...")
                    st.session_state.versao_dados += 1
                    st.rerun()
                elif sucesso is False:
                    st.error(f"A sincronização terminou com erro. Veja os detalhes/logs em: {url_run}")
                else:
                    st.warning(
                        f"Ainda não terminou depois de alguns minutos — pode continuar rodando. "
                        f"Acompanhe em: {url_run}"
                    )
            except github_actions.SincronizacaoIndisponivel as e:
                st.error(str(e))

    with st.expander("📦 Reprocessar um período de Vendas por Produto (dia a dia)", expanded=False):
        st.caption(
            "Puxa de novo o relatório 'Totais de Vendas Por Produto' **dia a dia** — um relatório "
            "por dia, sobrescrevendo cada dia individualmente — para o período escolhido (pode ser "
            "só um mês, ou vários meses seguidos, passados ou o corrente). Útil pra corrigir/"
            "completar um período sem rodar o backfill inteiro de novo. Quanto maior o período, "
            "mais relatórios precisam ser gerados — pode demorar bastante (veja a estimativa "
            "abaixo). Roda no GitHub Actions."
        )
        col_bf1, col_bf2, col_bf3, col_bf4, col_bf5 = st.columns([1, 0.9, 1.3, 0.9, 1.3])
        with col_bf1:
            loja_bf = st.selectbox("Loja", ["Ambas"] + db.LOJAS, key="loja_backfill_mes")
        with col_bf2:
            ano_ini_bf = st.number_input(
                "De (ano)", min_value=2024, max_value=date.today().year, value=date.today().year,
                step=1, key="ano_ini_backfill",
            )
        with col_bf3:
            mes_ini_bf = st.selectbox(
                "De (mês)", list(db.MESES_PT.keys()), format_func=lambda m: db.MESES_PT[m],
                index=date.today().month - 1, key="mes_ini_backfill",
            )
        with col_bf4:
            ano_fim_bf = st.number_input(
                "Até (ano)", min_value=2024, max_value=date.today().year, value=date.today().year,
                step=1, key="ano_fim_backfill",
            )
        with col_bf5:
            mes_fim_bf = st.selectbox(
                "Até (mês)", list(db.MESES_PT.keys()), format_func=lambda m: db.MESES_PT[m],
                index=date.today().month - 1, key="mes_fim_backfill",
            )

        hoje_bf = date.today()
        intervalo_invalido_bf = (int(ano_ini_bf), int(mes_ini_bf)) > (int(ano_fim_bf), int(mes_fim_bf))
        loja_bf_label = "Porteira e Casa de Adubo" if loja_bf == "Ambas" else loja_bf
        n_lojas_bf = 2 if loja_bf == "Ambas" else 1

        if intervalo_invalido_bf:
            st.error("O período \"De\" precisa ser antes (ou igual a) o período \"Até\".")
            disparar_bf = st.button("📦 Puxar esse período agora", key="btn_backfill_mes", disabled=True)
        else:
            data_ini_bf = date(int(ano_ini_bf), int(mes_ini_bf), 1)
            ultimo_dia_fim_bf = calendar.monthrange(int(ano_fim_bf), int(mes_fim_bf))[1]
            data_fim_bf = min(date(int(ano_fim_bf), int(mes_fim_bf), ultimo_dia_fim_bf), hoje_bf)
            n_dias_bf = (data_fim_bf - data_ini_bf).days + 1
            n_relatorios_bf = n_dias_bf * n_lojas_bf
            estimativa_min_bf = max(1, round(n_relatorios_bf * 8 / 60))  # ~8s por relatório

            # ---- Confirmação visual do que será reprocessado, pra evitar reprocessar o
            # período errado por engano (ex.: seletor ficou num valor de um teste anterior) ----
            if data_ini_bf.strftime("%Y-%m") == data_fim_bf.strftime("%Y-%m"):
                rotulo_periodo_bf = f"**{db.MESES_PT[int(mes_ini_bf)]}/{int(ano_ini_bf)}**"
            else:
                rotulo_periodo_bf = (
                    f"**{db.MESES_PT[int(mes_ini_bf)]}/{int(ano_ini_bf)}** até "
                    f"**{db.MESES_PT[data_fim_bf.month]}/{data_fim_bf.year}**"
                )
            msg_bf = (
                f"➡️ Vai reprocessar {rotulo_periodo_bf} para **{loja_bf_label}**, dia a dia "
                f"({n_dias_bf} dia(s) × {n_lojas_bf} loja(s) = {n_relatorios_bf} relatório(s) — "
                f"estimativa de ~{estimativa_min_bf} min)."
            )
            if n_relatorios_bf > 90:
                st.warning(msg_bf + " Período grande — considere rodar em partes menores se puder.")
            else:
                st.info(msg_bf)

            col_bf6, _ = st.columns([1.4, 3])
            with col_bf6:
                disparar_bf = st.button("📦 Puxar esse período agora", key="btn_backfill_mes")

        if not intervalo_invalido_bf and disparar_bf:
            try:
                loja_bf_param = None if loja_bf == "Ambas" else loja_bf
                disparado_em_bf = github_actions.disparar_backfill_mes(
                    int(ano_ini_bf), int(mes_ini_bf),
                    loja=loja_bf_param,
                    ano_fim=int(ano_fim_bf), mes_fim=int(mes_fim_bf),
                )
                status_placeholder_bf = st.empty()
                with st.spinner(f"Puxando {rotulo_periodo_bf.replace('**', '')} do SGI, dia a dia..."):
                    sucesso_bf, url_run_bf = github_actions.aguardar_conclusao(
                        disparado_em_bf,
                        workflow_arquivo=github_actions.WORKFLOW_BACKFILL_PRODUTOS,
                        callback_status=status_placeholder_bf.caption,
                    )
                if sucesso_bf:
                    db.limpar_cache()
                    st.success("Período reprocessado! Atualizando os números...")
                    st.session_state.versao_dados += 1
                    st.rerun()
                elif sucesso_bf is False:
                    st.error(f"Terminou com erro. Veja os detalhes/logs em: {url_run_bf}")
                else:
                    st.warning(
                        f"Ainda não terminou depois de alguns minutos — pode continuar rodando. "
                        f"Acompanhe em: {url_run_bf}"
                    )
            except github_actions.SincronizacaoIndisponivel as e:
                st.error(str(e))

    st.markdown("---")
    st.subheader("Lançamento diário de vendas")
    vendedores_df = db.get_vendedores(apenas_ativos=True)

    if vendedores_df.empty:
        st.info("Cadastre vendedores na aba 'Cadastros' antes de lançar vendas.")
    else:
        with st.form("form_venda", clear_on_submit=True):
            col1, col2, col3, col4 = st.columns(4)
            opcoes_vend = {
                f"{row['nome']} ({row['loja']})": row["id"] for _, row in vendedores_df.iterrows()
            }
            with col1:
                escolha_vend_v = st.selectbox("Vendedor *", list(opcoes_vend.keys()), key="sel_venda")
            with col2:
                data_venda = st.date_input("Data *", value=date.today(), max_value=date.today())
            with col3:
                valor_realizado = st.number_input(
                    "Realizado do dia (R$) *", min_value=0.0, step=50.0, format="%.2f"
                )
            with col4:
                qtd_pedidos = st.number_input(
                    "Pedidos *", min_value=0, step=1, format="%d"
                )

            enviado_venda = st.form_submit_button("💾 Salvar lançamento")
            if enviado_venda:
                vendedor_id = opcoes_vend[escolha_vend_v]
                db.upsert_venda(vendedor_id, data_venda, float(valor_realizado), int(qtd_pedidos))
                st.success(
                    f"Lançamento salvo: {escolha_vend_v} — {data_venda.strftime('%d/%m/%Y')} — "
                    f"{db.formatar_moeda(valor_realizado)} — {qtd_pedidos} pedido(s)."
                )
                st.session_state.versao_dados += 1
                st.rerun()

    st.markdown("---")
    st.subheader("🧾 Lançar pedidos por dia")
    st.caption(
        "Use isto quando o valor vendido do dia já foi lançado (ou importado) e falta só "
        "informar os pedidos — não sobrescreve o valor já registrado."
    )
    if vendedores_df.empty:
        st.caption("Cadastre vendedores na aba 'Cadastros' primeiro.")
    else:
        with st.form("form_pedidos_dia", clear_on_submit=True):
            colc1, colc2, colc3 = st.columns(3)
            opcoes_vend_c = {
                f"{row['nome']} ({row['loja']})": row["id"] for _, row in vendedores_df.iterrows()
            }
            with colc1:
                escolha_vend_c = st.selectbox("Vendedor *", list(opcoes_vend_c.keys()), key="sel_pedidos_dia")
            with colc2:
                data_pedidos = st.date_input(
                    "Data *", value=date.today(), max_value=date.today(), key="data_pedidos_dia"
                )
            with colc3:
                qtd_pedidos_dia = st.number_input(
                    "Pedidos *", min_value=0, step=1, format="%d", key="qtd_pedidos_dia"
                )
            enviado_pedidos_dia = st.form_submit_button("💾 Salvar pedidos do dia")
            if enviado_pedidos_dia:
                vendedor_id_c = opcoes_vend_c[escolha_vend_c]
                db.upsert_pedidos_dia(vendedor_id_c, data_pedidos, int(qtd_pedidos_dia))
                st.success(
                    f"Pedidos salvos: {escolha_vend_c} — "
                    f"{data_pedidos.strftime('%d/%m/%Y')} — {qtd_pedidos_dia} pedido(s)."
                )
                st.session_state.versao_dados += 1
                st.rerun()

    st.markdown("---")
    with st.expander("📥 Importar relatório de vendas em PDF (SGI)", expanded=False):
        st.caption(
            "Envie o PDF do 'Relatório de Totais de Vendas' do SGI. Pega a data do período, "
            "o Nº Ped e o T. Liq. de cada vendedor — só importa quem já está cadastrado; "
            "nomes do PDF sem correspondência no cadastro são listados e ignorados (não cria "
            "vendedor novo automaticamente)."
        )
        arquivo_pdf_vendas = st.file_uploader("Arquivo PDF", type=["pdf"], key="upload_pdf_vendas")

        if arquivo_pdf_vendas is not None and st.button("🔍 Pré-visualizar PDF"):
            try:
                st.session_state["pdf_vendas_parse"] = parse_relatorio_vendas_pdf(arquivo_pdf_vendas.read())
            except Exception as e:
                st.session_state.pop("pdf_vendas_parse", None)
                st.error(f"Não foi possível ler o PDF: {e}")

        resultado_pdf_vendas = st.session_state.get("pdf_vendas_parse")
        if resultado_pdf_vendas is not None:
            if not resultado_pdf_vendas["linhas"]:
                st.warning("Nenhuma linha de vendedor reconhecida nesse PDF.")
            else:
                col_pdfv1, col_pdfv2 = st.columns(2)
                with col_pdfv1:
                    data_default_pdf = (
                        resultado_pdf_vendas["data_fim"] or resultado_pdf_vendas["data_ini"] or date.today()
                    )
                    if (
                        resultado_pdf_vendas["data_ini"] and resultado_pdf_vendas["data_fim"]
                        and resultado_pdf_vendas["data_ini"] != resultado_pdf_vendas["data_fim"]
                    ):
                        st.caption(
                            f"⚠️ O PDF cobre um período de "
                            f"{resultado_pdf_vendas['data_ini'].strftime('%d/%m/%Y')} a "
                            f"{resultado_pdf_vendas['data_fim'].strftime('%d/%m/%Y')} — todos os "
                            "valores serão lançados na data única escolhida abaixo."
                        )
                    data_confirmada_pdf = st.date_input(
                        "Data do lançamento", value=data_default_pdf, max_value=date.today(),
                        key="data_pdf_vendas",
                    )
                with col_pdfv2:
                    indice_loja_pdf = (
                        db.LOJAS.index(resultado_pdf_vendas["loja_detectada"])
                        if resultado_pdf_vendas["loja_detectada"] in db.LOJAS else 0
                    )
                    loja_confirmada_pdf = st.selectbox(
                        "Loja", db.LOJAS, index=indice_loja_pdf, key="loja_pdf_vendas"
                    )

                vendedores_loja_pdf_df = db.get_vendedores(loja=loja_confirmada_pdf, apenas_ativos=True)
                pdf_matched, pdf_nao_encontrados = casar_vendedores(
                    resultado_pdf_vendas["linhas"], vendedores_loja_pdf_df
                )

                if pdf_matched:
                    st.write(f"**{len(pdf_matched)} vendedor(es) do PDF batem com o cadastro:**")
                    preview_pdf_df = pd.DataFrame(pdf_matched)
                    preview_pdf_fmt = preview_pdf_df.copy()
                    preview_pdf_fmt["T. Liq."] = preview_pdf_fmt["t_liq"].apply(db.formatar_moeda)
                    st.dataframe(
                        preview_pdf_fmt[["nome", "loja", "n_ped", "T. Liq."]].rename(
                            columns={"nome": "Nome", "loja": "Loja", "n_ped": "Nº Ped"}
                        ),
                        use_container_width=True, hide_index=True,
                    )
                else:
                    st.warning("Nenhum vendedor do PDF bate com o cadastro da loja selecionada.")

                if pdf_nao_encontrados:
                    st.caption(
                        "⚠️ Ignorados (sem correspondência no cadastro): " + ", ".join(pdf_nao_encontrados)
                    )

                if pdf_matched and st.button("✅ Confirmar importação do PDF"):
                    for item_pdf in pdf_matched:
                        db.upsert_venda(
                            item_pdf["vendedor_id"], data_confirmada_pdf,
                            float(item_pdf["t_liq"]), int(item_pdf["n_ped"]),
                        )
                    st.success(
                        f"{len(pdf_matched)} lançamento(s) importado(s) para "
                        f"{data_confirmada_pdf.strftime('%d/%m/%Y')}."
                    )
                    st.session_state.pop("pdf_vendas_parse", None)
                    st.session_state.versao_dados += 1
                    st.rerun()

    st.markdown("---")
    with st.expander("📥 Importar vendas diárias em lote (sem pedidos)"):
        st.caption(
            "Cole uma linha por lançamento, no formato: Vendedor [TAB] Data (DD/MM/AAAA) [TAB] "
            "Valor Vendido (R$) — como copiado de uma planilha. Não sobrescreve os pedidos "
            "já lançados para o mesmo dia; se o dia ainda não existir, entra com 0 "
            "pedidos (lance depois em '🧾 Lançar pedidos por dia' acima)."
        )
        texto_venda_lote = st.text_area(
            "Vendas diárias", height=220, key="texto_import_vendas",
            placeholder="TAINA SANTOS\t01/08/2026\tR$ 2.388,17",
        )

        if st.button("🔍 Pré-visualizar vendas"):
            linhas = [l for l in texto_venda_lote.splitlines() if l.strip()]
            registros_v = []
            erros_v = []
            for linha in linhas:
                if "vendedor" in linha.lower() and "data" in linha.lower():
                    continue  # cabeçalho da tabela, ignora
                resultado = parse_linha_venda_diaria(linha)
                if not resultado:
                    erros_v.append(f"Linha ignorada (formato não reconhecido): '{linha}'")
                    continue
                registros_v.append(resultado)
            st.session_state["import_vendas_preview"] = registros_v
            st.session_state["import_vendas_erros"] = erros_v

        erros_vendas = st.session_state.get("import_vendas_erros", [])
        registros_vendas = st.session_state.get("import_vendas_preview", [])

        for erro in erros_vendas:
            st.warning(erro)

        if registros_vendas:
            vendedores_todos_v = db.get_vendedores()
            nomes_map_v = {
                row["nome"].strip().lower(): row["nome"] for _, row in vendedores_todos_v.iterrows()
            }
            nomes_unicos_v = sorted({r["nome"] for r in registros_vendas})
            nomes_nao_mapeados_v = [n for n in nomes_unicos_v if n.strip().lower() not in nomes_map_v]

            mapeamento_v = {}
            lojas_novos_v = {}
            if nomes_nao_mapeados_v:
                st.markdown(
                    "**Estes nomes não batem com o cadastro atual. Diga a quem correspondem:**"
                )
                opcoes_vendedor_v = ["— Criar novo vendedor —"] + sorted(vendedores_todos_v["nome"].tolist())
                for nome_bruto in nomes_nao_mapeados_v:
                    mapeamento_v[nome_bruto] = st.selectbox(
                        f"'{nome_bruto}' corresponde a:", opcoes_vendedor_v, key=f"map_venda_{nome_bruto}",
                    )
                    if mapeamento_v[nome_bruto] == "— Criar novo vendedor —":
                        lojas_novos_v[nome_bruto] = st.selectbox(
                            f"Loja para o novo vendedor '{nome_bruto}':", db.LOJAS,
                            key=f"loja_novo_venda_{nome_bruto}",
                        )

            preview_v_df = pd.DataFrame(registros_vendas)
            preview_v_df["Data"] = preview_v_df["data"].apply(lambda d: d.strftime("%d/%m/%Y"))
            preview_v_df["Valor"] = preview_v_df["valor"].apply(db.formatar_moeda)
            st.write(f"**{len(registros_vendas)} lançamento(s) no lote colado:**")
            st.dataframe(
                preview_v_df[["nome", "Data", "Valor"]].rename(columns={"nome": "Vendedor"}),
                use_container_width=True, hide_index=True,
            )

            if st.button("✅ Confirmar importação de vendas"):
                nomes_ids_v = {
                    row["nome"].strip().lower(): int(row["id"]) for _, row in vendedores_todos_v.iterrows()
                }
                importados_v = 0
                for registro in registros_vendas:
                    chave = registro["nome"].strip().lower()
                    vendedor_id_v = nomes_ids_v.get(chave)
                    if vendedor_id_v is None:
                        escolha_map = mapeamento_v.get(registro["nome"])
                        if escolha_map == "— Criar novo vendedor —":
                            loja_novo = lojas_novos_v.get(registro["nome"], db.LOJAS[0])
                            db.add_vendedor(registro["nome"].title(), loja_novo)
                            atualizados_v = db.get_vendedores()
                            vendedor_id_v = int(
                                atualizados_v[atualizados_v["nome"] == registro["nome"].title()]["id"].iloc[-1]
                            )
                        elif escolha_map:
                            vendedor_id_v = int(
                                vendedores_todos_v[vendedores_todos_v["nome"] == escolha_map]["id"].iloc[0]
                            )
                        else:
                            continue
                        nomes_ids_v[chave] = vendedor_id_v

                    db.upsert_venda_valor(vendedor_id_v, registro["data"], registro["valor"])
                    importados_v += 1

                st.success(f"{importados_v} lançamento(s) de venda importado(s) com sucesso!")
                st.session_state.pop("import_vendas_preview", None)
                st.session_state.pop("import_vendas_erros", None)
                st.session_state.versao_dados += 1
                st.rerun()

    st.markdown("---")
    st.subheader("Lançamentos recentes")
    filtro_loja_lanc = st.selectbox("Filtrar por loja", ["Ambas"] + db.LOJAS, key="filtro_loja_lanc")
    recentes = db.get_lancamentos_recentes(limite=30, loja=filtro_loja_lanc)

    if recentes.empty:
        st.info("Nenhum lançamento registrado ainda.")
    else:
        recentes_fmt = recentes.copy()
        recentes_fmt["Realizado (R$)"] = recentes_fmt["valor_realizado"].apply(db.formatar_moeda)
        st.dataframe(
            recentes_fmt[["id", "nome", "loja", "data", "Realizado (R$)", "qtd_pedidos"]].rename(
                columns={"id": "ID", "nome": "Vendedor", "loja": "Loja", "data": "Data", "qtd_pedidos": "Pedidos"}
            ),
            use_container_width=True,
            hide_index=True,
        )

        with st.expander("🗑️ Excluir um lançamento"):
            id_para_excluir = st.number_input("ID do lançamento", min_value=0, step=1, format="%d")
            if st.button("Excluir lançamento"):
                if id_para_excluir in recentes["id"].values:
                    db.delete_venda(int(id_para_excluir))
                    st.warning(f"Lançamento ID {id_para_excluir} excluído.")
                    st.session_state.versao_dados += 1
                    st.rerun()
                else:
                    st.error("ID não encontrado na lista de lançamentos recentes acima.")

# ==========================================================================
# ABA 4 - DASHBOARD
# ==========================================================================
with tab_dashboard:
    st.subheader("Filtros")
    fcol1, fcol2, fcol3, fcol4 = st.columns([2, 1.3, 1.3, 1.4])
    with fcol1:
        loja_filtro = st.selectbox("Loja", ["Ambas"] + db.LOJAS, key="loja_dash")
    with fcol2:
        ano_filtro = st.number_input(
            "Ano", min_value=2020, max_value=2035, value=date.today().year, step=1, key="ano_dash"
        )
    with fcol3:
        mes_filtro = st.selectbox(
            "Mês", list(db.MESES_PT.keys()), format_func=lambda m: db.MESES_PT[m],
            index=date.today().month - 1, key="mes_dash",
        )
    with fcol4:
        # Chave inclui ano/mês pra recalcular o padrão (dias úteis reais do mês
        # escolhido — pode ser 24, 25, 26 ou 27 dependendo de quantos domingos ele
        # tem) sempre que o filtro de mês mudar, em vez de ficar preso num valor de
        # outro mês. Value só é usado a primeira vez que essa chave aparece — depois
        # disso o usuário pode ajustar livremente pra descontar feriados.
        dias_uteis_total = st.number_input(
            "Dias úteis no mês (confirme feriados)", min_value=1, max_value=31,
            value=db.dias_uteis_no_mes(int(ano_filtro), int(mes_filtro)), step=1,
            key=f"dias_uteis_dash_{int(ano_filtro)}_{int(mes_filtro)}",
        )

    ano_filtro = int(ano_filtro)
    mes_filtro = int(mes_filtro)
    dias_uteis_total = int(dias_uteis_total)

    metas_df = db.get_metas_mes(ano_filtro, mes_filtro, loja=loja_filtro)
    vendas_df = db.get_vendas_mes(ano_filtro, mes_filtro, loja=loja_filtro)
    manual_df = db.get_realizado_manual_mes(ano_filtro, mes_filtro, loja=loja_filtro)
    pedidos_manual_df = db.get_pedidos_manual_mes(ano_filtro, mes_filtro, loja=loja_filtro)

    # Alerta de metas não lançadas
    sem_meta = metas_df[metas_df["valor_meta"] == 0]
    if not sem_meta.empty:
        nomes_sem_meta = ", ".join(sem_meta["nome"].tolist())
        st.warning(f"⚠️ Meta não lançada para: {nomes_sem_meta} (considerando Meta = R$ 0,00).")

    if not manual_df.empty:
        st.caption(
            "ℹ️ Este mês inclui realizado importado como total mensal (sem lançamento diário) "
            "para: " + ", ".join(sorted(manual_df["nome"].unique().tolist())) +
            ". Esses valores entram nos totais e no ranking, mas não aparecem na evolução diária "
            "nem no mapa de calor por dia da semana."
        )

    # ---- KPIs ----
    totais_mes_atual = db.get_totais_mes(ano_filtro, mes_filtro, loja=loja_filtro)
    meta_total = totais_mes_atual["meta"]
    realizado_total = totais_mes_atual["realizado"]
    pedidos_total = totais_mes_atual["pedidos"]

    atingimento_pct = (realizado_total / meta_total * 100) if meta_total > 0 else 0.0
    dias_transcorridos = db.dias_uteis_transcorridos(ano_filtro, mes_filtro, dias_uteis_total)
    run_rate_diario = (realizado_total / dias_transcorridos) if dias_transcorridos > 0 else 0.0
    projecao_fechamento = run_rate_diario * dias_uteis_total
    ticket_medio = (realizado_total / pedidos_total) if pedidos_total > 0 else 0.0
    produtividade_diaria = run_rate_diario

    cor_pct = db.cor_semaforo(atingimento_pct)
    label_pct = db.label_semaforo(atingimento_pct)

    st.markdown("### Indicadores do mês")
    k1, k2, k3, k4, k5, k6 = st.columns(6)

    def kpi_card(col, titulo, valor, cor=AZUL):
        col.markdown(
            f"""
            <div class="kpi-card" style="border-left-color:{cor};">
                <div class="kpi-title">{titulo}</div>
                <div class="kpi-value" style="color:{cor};">{valor}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    kpi_card(k1, "Meta Total do Mês", db.formatar_moeda(meta_total))
    kpi_card(k2, "Realizado Total do Mês", db.formatar_moeda(realizado_total), cor=VERDE)
    kpi_card(k3, f"Atingimento Geral ({label_pct})", f"{atingimento_pct:.1f}%", cor=cor_pct)
    kpi_card(k4, "Projeção de Fechamento", db.formatar_moeda(projecao_fechamento))
    kpi_card(k5, "Ticket Médio do Mês", db.formatar_moeda(ticket_medio), cor=VERDE)
    kpi_card(k6, "Produtividade Diária Média", db.formatar_moeda(produtividade_diaria))

    st.caption(
        f"Dias úteis transcorridos: {dias_transcorridos} de {dias_uteis_total} | "
        f"Pedidos no mês: {pedidos_total}"
    )
    st.caption(
        "ℹ️ *Pedidos* (e por consequência o *Ticket Médio*) é aproximado pela soma da coluna "
        "'Vendas' do relatório 'Totais de Vendas Por Produto' nos dias sincronizados "
        "automaticamente pelo SGI — pode superestimar levemente pedidos com mais de um item, o "
        "que subestima um pouco o ticket médio nesses dias. Lançamentos manuais com Nº de Pedidos "
        "exato não são afetados."
    )

    st.markdown("---")

    # ---- Central de Alertas: ritmo de meta (projeção) por vendedor/loja e quedas vs. mês anterior ----
    ano_ant, mes_ant = db.mes_anterior(ano_filtro, mes_filtro)

    indicadores_atual = db.get_indicadores_vendedores_mes(ano_filtro, mes_filtro, loja=loja_filtro)
    if not indicadores_atual.empty:
        if dias_transcorridos > 0:
            indicadores_atual["projecao_fechamento"] = (
                indicadores_atual["realizado"] / dias_transcorridos * dias_uteis_total
            )
        else:
            indicadores_atual["projecao_fechamento"] = indicadores_atual["realizado"]
        indicadores_atual["projecao_pct"] = indicadores_atual.apply(
            lambda r: (r["projecao_fechamento"] / r["valor_meta"] * 100) if r["valor_meta"] > 0 else None,
            axis=1,
        )

    LIMIAR_PROJECAO_CRITICA = 70.0   # projeção de fechamento abaixo disso = alerta (mesmo corte do semáforo)
    LIMIAR_QUEDA_MOM_ALERTA = -20.0  # queda de realizado vs. mês anterior considerada alerta

    st.markdown("### 🔔 Central de Alertas")
    st.caption(
        "Recalculado a cada carregamento da tela: ritmo de meta (projeção de fechamento no ritmo "
        "atual, considerando só dias úteis já transcorridos) por vendedor e por loja, e quedas de "
        f"realizado vs. o mês anterior acima de {abs(LIMIAR_QUEDA_MOM_ALERTA):.0f}%."
    )

    alertas = []

    if not indicadores_atual.empty and dias_transcorridos > 0:
        for row in indicadores_atual.itertuples():
            if row.valor_meta > 0 and pd.notna(row.projecao_pct) and row.projecao_pct < LIMIAR_PROJECAO_CRITICA:
                alertas.append((
                    "🔴",
                    f"**{row.nome}** ({row.loja}): no ritmo atual, fecha o mês em "
                    f"**{row.projecao_pct:.0f}%** da meta (projeção {db.formatar_moeda(row.projecao_fechamento)} "
                    f"de {db.formatar_moeda(row.valor_meta)})."
                ))

    indicadores_mes_anterior = db.get_indicadores_vendedores_mes(ano_ant, mes_ant, loja=loja_filtro)
    if not indicadores_atual.empty and not indicadores_mes_anterior.empty:
        comp_mom = indicadores_atual[["vendedor_id", "nome", "loja", "realizado"]].merge(
            indicadores_mes_anterior[["vendedor_id", "realizado"]].rename(columns={"realizado": "realizado_ant"}),
            on="vendedor_id", how="left",
        )
        for row in comp_mom.itertuples():
            if row.realizado_ant and row.realizado_ant > 0:
                variacao = (row.realizado - row.realizado_ant) / row.realizado_ant * 100
                if variacao <= LIMIAR_QUEDA_MOM_ALERTA:
                    alertas.append((
                        "🟡",
                        f"**{row.nome}** ({row.loja}): realizado caiu **{variacao:.0f}%** vs. "
                        f"{db.MESES_PT[mes_ant]}/{ano_ant} ({db.formatar_moeda(row.realizado_ant)} → "
                        f"{db.formatar_moeda(row.realizado)})."
                    ))

    if dias_transcorridos > 0:
        for loja_nome in db.LOJAS:
            totais_loja_alerta = db.get_totais_mes(ano_filtro, mes_filtro, loja=loja_nome)
            if totais_loja_alerta["meta"] > 0:
                projecao_loja = totais_loja_alerta["realizado"] / dias_transcorridos * dias_uteis_total
                projecao_loja_pct = projecao_loja / totais_loja_alerta["meta"] * 100
                if projecao_loja_pct < LIMIAR_PROJECAO_CRITICA:
                    alertas.append((
                        "🔴",
                        f"Loja **{loja_nome}**: no ritmo atual, fecha o mês em "
                        f"**{projecao_loja_pct:.0f}%** da meta (projeção "
                        f"{db.formatar_moeda(projecao_loja)} de {db.formatar_moeda(totais_loja_alerta['meta'])})."
                    ))

    if not alertas:
        st.success(
            "✅ Nenhum alerta no momento — ritmo de meta e comparativo com o mês anterior dentro "
            "do esperado."
        )
    else:
        for icone, texto in sorted(alertas, key=lambda a: a[0] != "🔴"):
            (st.error if icone == "🔴" else st.warning)(texto)

    st.markdown("---")

    # ---- Comparativo mês a mês e ano a ano ----
    st.markdown("### 🔄 Comparativo Mês a Mês e Ano a Ano")

    totais_mes_anterior = db.get_totais_mes(ano_ant, mes_ant, loja=loja_filtro)
    totais_ano_anterior = db.get_totais_mes(ano_filtro - 1, mes_filtro, loja=loja_filtro)

    def calcular_crescimento(atual, anterior):
        if anterior > 0:
            return (atual - anterior) / anterior * 100
        return None

    crescimento_mom = calcular_crescimento(realizado_total, totais_mes_anterior["realizado"])
    crescimento_yoy = calcular_crescimento(realizado_total, totais_ano_anterior["realizado"])

    def cor_crescimento(valor):
        if valor is None:
            return "#888888"
        return VERDE if valor >= 0 else "#e74c3c"

    def texto_crescimento(valor):
        if valor is None:
            return "Sem dados no período anterior"
        seta = "▲" if valor >= 0 else "▼"
        return f"{seta} {valor:.1f}%"

    cc1, cc2, cc3, cc4 = st.columns(4)
    kpi_card(
        cc1, f"vs. {db.MESES_PT[mes_ant]}/{ano_ant} (Realizado)",
        db.formatar_moeda(totais_mes_anterior["realizado"]),
    )
    kpi_card(
        cc2, "Crescimento vs. mês anterior", texto_crescimento(crescimento_mom),
        cor=cor_crescimento(crescimento_mom),
    )
    kpi_card(
        cc3, f"vs. {db.MESES_PT[mes_filtro]}/{ano_filtro - 1} (Realizado)",
        db.formatar_moeda(totais_ano_anterior["realizado"]),
    )
    kpi_card(
        cc4, "Crescimento vs. mesmo mês ano passado", texto_crescimento(crescimento_yoy),
        cor=cor_crescimento(crescimento_yoy),
    )

    st.markdown("---")

    # ---- Ranking de vendedores ----
    st.markdown("### 🏆 Ranking de Vendedores")
    ranking = indicadores_atual.rename(columns={"ticket_medio": "ticket_medio_ind"}).sort_values(
        "realizado", ascending=False
    )

    if ranking.empty:
        st.info("Nenhum vendedor ativo para o filtro selecionado.")
    else:
        ranking_fmt = ranking.copy()
        ranking_fmt["Meta"] = ranking_fmt["valor_meta"].apply(db.formatar_moeda)
        ranking_fmt["Realizado"] = ranking_fmt["realizado"].apply(db.formatar_moeda)
        ranking_fmt["Atingimento (%)"] = ranking_fmt["atingimento_pct"].apply(lambda v: f"{v:.1f}%")
        ranking_fmt["Ticket Médio"] = ranking_fmt["ticket_medio_ind"].apply(db.formatar_moeda)
        ranking_fmt["Projeção de Fechamento"] = ranking_fmt["projecao_fechamento"].apply(db.formatar_moeda)
        ranking_fmt["Ritmo de Meta (%)"] = ranking_fmt["projecao_pct"].apply(
            lambda v: "—" if pd.isna(v) else f"{v:.0f}%"
        )
        st.dataframe(
            ranking_fmt[
                ["nome", "loja", "Meta", "Realizado", "Atingimento (%)", "pedidos", "Ticket Médio",
                 "Projeção de Fechamento", "Ritmo de Meta (%)"]
            ].rename(columns={"nome": "Nome", "loja": "Loja", "pedidos": "Pedidos"}),
            use_container_width=True,
            hide_index=True,
        )
        st.caption(
            "Ritmo de Meta (%) = projeção de fechamento (realizado ÷ dias úteis transcorridos × "
            "dias úteis do mês) ÷ meta do mês — em outras palavras, \"no ritmo atual, fecha o mês "
            "em X% da meta\". Considera só dias úteis já transcorridos; sem meta lançada aparece "
            "como \"—\"."
        )

    st.markdown("---")

    # ---- Atingimento Médio por Vendedor (meses finalizados) ----
    st.markdown("### 📊 Atingimento Médio por Vendedor — Meses Finalizados")
    st.caption(
        f"Média do atingimento (%) mensal de cada vendedor, considerando só meses já FECHADOS "
        f"(com meta lançada) — de {db.MESES_PT[db.BASE_INICIO_MES]}/{db.BASE_INICIO_ANO} (início "
        "da base de dados) até o mês anterior ao atual. Independe do filtro de mês acima e nunca "
        "inclui o mês corrente, que ainda está em andamento."
    )

    resumo_ating = db.get_atingimento_medio_vendedor(loja=loja_filtro)

    if resumo_ating.empty:
        st.info("Sem meses finalizados com meta lançada para calcular a média.")
    else:
        resumo_ating_fmt = resumo_ating.copy()
        resumo_ating_fmt["Atingimento Médio (%)"] = resumo_ating_fmt["atingimento_medio_pct"].apply(
            lambda v: f"{v:.1f}%"
        )
        resumo_ating_fmt["Meses Finalizados"] = resumo_ating_fmt["n_meses"]
        st.dataframe(
            resumo_ating_fmt[
                ["nome", "loja", "Atingimento Médio (%)", "Meses Finalizados"]
            ].rename(columns={"nome": "Nome", "loja": "Loja"}),
            use_container_width=True,
            hide_index=True,
        )

        fig_ating_medio = go.Figure(go.Bar(
            x=resumo_ating["atingimento_medio_pct"], y=resumo_ating["nome"], orientation="h",
            marker_color=[VERDE if v >= 100 else AZUL_CLARO for v in resumo_ating["atingimento_medio_pct"]],
            text=resumo_ating["atingimento_medio_pct"].apply(lambda v: f"{v:.1f}%"),
            textposition="outside",
        ))
        fig_ating_medio.update_layout(
            xaxis_title="Atingimento Médio (%)",
            yaxis=dict(autorange="reversed"),
            margin=dict(l=10, r=10, t=20, b=10), height=max(280, 32 * len(resumo_ating)),
        )
        fig_ating_medio.add_vline(x=100, line_dash="dash", line_color="grey")
        st.plotly_chart(fig_ating_medio, use_container_width=True)

    st.markdown("---")

    # ---- Evolução diária: acumulado realizado vs meta diária projetada ----
    st.markdown("### 📈 Evolução Diária: Realizado Acumulado vs. Meta Projetada")
    ultimo_dia_mes = calendar.monthrange(ano_filtro, mes_filtro)[1]
    hoje = date.today()
    if (ano_filtro, mes_filtro) < (hoje.year, hoje.month):
        dia_limite = ultimo_dia_mes
    elif (ano_filtro, mes_filtro) == (hoje.year, hoje.month):
        dia_limite = hoje.day
    else:
        dia_limite = 0

    meta_diaria = (meta_total / dias_uteis_total) if dias_uteis_total > 0 else 0.0

    datas, realizado_acum, meta_acum = [], [], []
    acumulado_real = 0.0
    contador_dia_util = 0
    for dia in range(1, dia_limite + 1):
        data_atual = date(ano_filtro, mes_filtro, dia)
        if not vendas_df.empty:
            realizado_dia = vendas_df.loc[vendas_df["data"] == data_atual, "valor_realizado"].sum()
        else:
            realizado_dia = 0.0
        acumulado_real += realizado_dia
        if data_atual.weekday() != 6 and contador_dia_util < dias_uteis_total:
            contador_dia_util += 1
        datas.append(data_atual)
        realizado_acum.append(acumulado_real)
        meta_acum.append(meta_diaria * contador_dia_util)

    if not datas:
        st.info("Sem dados suficientes para exibir a evolução diária deste mês.")
    else:
        fig_evolucao = go.Figure()
        fig_evolucao.add_trace(
            go.Scatter(x=datas, y=realizado_acum, mode="lines+markers", name="Realizado Acumulado",
                       line=dict(color=VERDE, width=3))
        )
        fig_evolucao.add_trace(
            go.Scatter(x=datas, y=meta_acum, mode="lines", name="Meta Diária Projetada (Acum.)",
                       line=dict(color=AZUL, width=2, dash="dash"))
        )
        fig_evolucao.update_layout(
            xaxis_title="Data", yaxis_title="R$", legend=dict(orientation="h", y=-0.2),
            margin=dict(l=10, r=10, t=30, b=10), height=380,
        )
        st.plotly_chart(fig_evolucao, use_container_width=True)

    st.markdown("---")

    # ---- Vendas por dia da semana (heatmap + ranking de dias) ----
    st.markdown("### 🔥 Vendas por Dia da Semana — Picos de Movimento")

    dias_semana_nomes = {
        0: "Segunda", 1: "Terça", 2: "Quarta", 3: "Quinta", 4: "Sexta", 5: "Sábado", 6: "Domingo",
    }

    if vendas_df.empty:
        st.info("Sem lançamentos no período para montar o mapa de calor.")
    else:
        vendas_semana = vendas_df.copy()
        vendas_semana["dia_semana"] = vendas_semana["data"].apply(lambda d: d.weekday())
        vendas_semana["semana_mes"] = vendas_semana["data"].apply(lambda d: (d.day - 1) // 7 + 1)

        col_heat, col_rank_dia = st.columns([2, 1])

        with col_heat:
            agrupado = (
                vendas_semana.groupby(["dia_semana", "semana_mes"])["valor_realizado"]
                .sum()
                .reset_index()
            )
            pivot = agrupado.pivot(index="dia_semana", columns="semana_mes", values="valor_realizado")
            pivot = pivot.reindex(range(7))
            colunas_semana = sorted(vendas_semana["semana_mes"].unique())
            pivot = pivot.reindex(columns=colunas_semana).fillna(0.0)

            texto_hover = [
                [db.formatar_moeda(v) for v in linha] for linha in pivot.values
            ]

            fig_heatmap = go.Figure(
                data=go.Heatmap(
                    z=pivot.values,
                    x=[f"Semana {c}" for c in pivot.columns],
                    y=[dias_semana_nomes[i] for i in pivot.index],
                    colorscale=[[0, "#f4f6f7"], [1, VERDE]],
                    text=texto_hover,
                    texttemplate="%{text}",
                    textfont={"size": 10},
                    hoverinfo="skip",
                )
            )
            fig_heatmap.update_layout(
                title="Realizado por dia da semana x semana do mês",
                margin=dict(l=10, r=10, t=40, b=10), height=380,
            )
            st.plotly_chart(fig_heatmap, use_container_width=True)

        with col_rank_dia:
            total_por_dia = (
                vendas_semana.groupby("dia_semana")["valor_realizado"].sum().reindex(range(7)).fillna(0.0)
            )
            dia_pico = total_por_dia.idxmax() if total_por_dia.sum() > 0 else None
            fig_dia_semana = go.Figure(
                go.Bar(
                    x=total_por_dia.values,
                    y=[dias_semana_nomes[i] for i in total_por_dia.index],
                    orientation="h",
                    marker_color=[
                        VERDE if i == dia_pico else AZUL_CLARO for i in total_por_dia.index
                    ],
                    text=[db.formatar_moeda(v) for v in total_por_dia.values],
                    textposition="outside",
                )
            )
            fig_dia_semana.update_layout(
                title="Total por dia da semana",
                margin=dict(l=10, r=10, t=40, b=10), height=380,
                xaxis_title="R$",
            )
            st.plotly_chart(fig_dia_semana, use_container_width=True)

            if dia_pico is not None:
                st.caption(f"📌 Dia de maior movimento no período: **{dias_semana_nomes[dia_pico]}**.")

    st.markdown("---")

    # ---- Comparativo entre lojas ----
    st.markdown("### 🏬 Comparativo entre Lojas — Porteira vs. Casa de Adubo")
    comparativo = []
    for loja_nome in db.LOJAS:
        totais_loja = db.get_totais_mes(ano_filtro, mes_filtro, loja=loja_nome)
        meta_l = totais_loja["meta"]
        realizado_l = totais_loja["realizado"]
        pedidos_l = totais_loja["pedidos"]
        atingimento_l = (realizado_l / meta_l * 100) if meta_l > 0 else 0.0
        ticket_l = (realizado_l / pedidos_l) if pedidos_l > 0 else 0.0
        n_vendedores_l = len(db.get_vendedores(loja=loja_nome, apenas_ativos=True))
        meta_per_capita_l = (meta_l / n_vendedores_l) if n_vendedores_l > 0 else 0.0
        comparativo.append(
            {
                "loja": loja_nome, "atingimento": atingimento_l, "ticket_medio": ticket_l,
                "n_vendedores": n_vendedores_l, "meta_per_capita": meta_per_capita_l,
            }
        )
    comp_df = pd.DataFrame(comparativo)

    col_g1, col_g2 = st.columns(2)
    with col_g1:
        fig_ating = go.Figure(
            data=[
                go.Bar(
                    x=comp_df["loja"], y=comp_df["atingimento"],
                    marker_color=[db.cor_semaforo(v) for v in comp_df["atingimento"]],
                    text=[f"{v:.1f}%" for v in comp_df["atingimento"]], textposition="outside",
                )
            ]
        )
        fig_ating.update_layout(title="Atingimento (%) por Loja", yaxis_title="%",
                                margin=dict(l=10, r=10, t=40, b=10), height=350)
        st.plotly_chart(fig_ating, use_container_width=True)

    with col_g2:
        fig_ticket = go.Figure(
            data=[
                go.Bar(
                    x=comp_df["loja"], y=comp_df["ticket_medio"], marker_color=[AZUL, VERDE],
                    text=[db.formatar_moeda(v) for v in comp_df["ticket_medio"]], textposition="outside",
                )
            ]
        )
        fig_ticket.update_layout(title="Ticket Médio por Loja", yaxis_title="R$",
                                 margin=dict(l=10, r=10, t=40, b=10), height=350)
        st.plotly_chart(fig_ticket, use_container_width=True)

    st.caption(
        "Regra de negócio: mês comercial com "
        f"{dias_uteis_total} dias úteis (segunda a sábado, editável acima para refletir feriados). "
        "Semáforo: 🔴 abaixo de 70% · 🟡 70% a 90% · 🟢 acima de 90%."
    )

    cap1, cap2 = st.columns(2)
    for col_capita, row_capita in zip([cap1, cap2], comp_df.itertuples()):
        kpi_card(
            col_capita,
            f"Meta per Capita — {row_capita.loja} ({row_capita.n_vendedores} vendedor(es))",
            db.formatar_moeda(row_capita.meta_per_capita),
        )

    st.markdown("---")

    # ---- Indicadores Avançados por Vendedor ----
    st.markdown("### 📐 Indicadores Avançados por Vendedor")
    st.caption(
        "Consistência e comparação individual dentro do mês selecionado. Métricas que dependem "
        "de granularidade diária (dias sem lançamento, desvio padrão, % de dias com meta batida, "
        "melhor dia) usam só os lançamentos diários — não incluem realizado/pedidos importados "
        "como total mensal."
    )

    avancado_df = pd.DataFrame(columns=["vendedor_id", "cv", "gap_ticket_pct"])

    if vendas_df.empty or dias_transcorridos == 0:
        st.info("Sem lançamentos diários suficientes no período para calcular estes indicadores.")
    else:
        meta_diaria_map = (
            indicadores_atual.set_index("vendedor_id")["valor_meta"] / dias_uteis_total
            if dias_uteis_total > 0 else indicadores_atual.set_index("vendedor_id")["valor_meta"] * 0
        )

        ticket_medio_loja_map = (
            indicadores_atual[indicadores_atual["pedidos"] > 0]
            .groupby("loja")
            .apply(lambda g: g["realizado"].sum() / g["pedidos"].sum() if g["pedidos"].sum() > 0 else 0.0)
        )

        resumo_ina_lookup_avancado = db.get_indice_inadimplencia_resumo_todos_vendedores(loja=loja_filtro)

        linhas_avancado = []
        for row in indicadores_atual.itertuples():
            dados_vend = vendas_df[vendas_df["vendedor_id"] == row.vendedor_id]
            dias_com_lancamento = dados_vend["data"].nunique() if not dados_vend.empty else 0
            dias_sem_lancamento = max(dias_transcorridos - dias_com_lancamento, 0)

            desvio_padrao = float(dados_vend["valor_realizado"].std(ddof=0)) if len(dados_vend) > 1 else 0.0
            media_diaria = float(dados_vend["valor_realizado"].mean()) if len(dados_vend) > 1 else 0.0
            cv = (desvio_padrao / media_diaria * 100) if media_diaria > 0 and len(dados_vend) > 1 else None

            meta_diaria_ind = float(meta_diaria_map.get(row.vendedor_id, 0.0))
            dias_bateram_meta = (
                int((dados_vend["valor_realizado"] >= meta_diaria_ind).sum())
                if not dados_vend.empty and meta_diaria_ind > 0 else 0
            )
            pct_dias_meta = (dias_bateram_meta / dias_transcorridos * 100) if dias_transcorridos > 0 else 0.0

            if not dados_vend.empty:
                idx_melhor = dados_vend["valor_realizado"].idxmax()
                melhor_dia_data = dados_vend.loc[idx_melhor, "data"]
                melhor_dia_valor = dados_vend.loc[idx_melhor, "valor_realizado"]
            else:
                melhor_dia_data = None
                melhor_dia_valor = 0.0

            ticket_medio_loja = float(ticket_medio_loja_map.get(row.loja, 0.0))
            gap_ticket = row.ticket_medio - ticket_medio_loja
            gap_ticket_pct = (gap_ticket / ticket_medio_loja * 100) if ticket_medio_loja > 0 else None

            linhas_avancado.append({
                "vendedor_id": row.vendedor_id,
                "nome": row.nome,
                "loja": row.loja,
                "dias_sem_lancamento": dias_sem_lancamento,
                "desvio_padrao": desvio_padrao,
                "cv": cv,
                "pct_dias_meta": pct_dias_meta,
                "melhor_dia_data": melhor_dia_data,
                "melhor_dia_valor": melhor_dia_valor,
                "gap_ticket": gap_ticket,
                "gap_ticket_pct": gap_ticket_pct,
            })

        def _fmt_cv(v):
            if v is None or pd.isna(v):
                return "—"
            if v <= 30:
                rotulo = "Alta regularidade"
            elif v <= 60:
                rotulo = "Regularidade média"
            else:
                rotulo = "Baixa regularidade"
            return f"{v:.1f}% ({rotulo})"

        avancado_df = pd.DataFrame(linhas_avancado)
        avancado_fmt = avancado_df.copy()
        avancado_fmt["Dias sem Lançamento"] = avancado_fmt["dias_sem_lancamento"]
        avancado_fmt["CV (Regularidade)"] = avancado_fmt["cv"].apply(_fmt_cv)
        avancado_fmt["% Dias com Meta Batida"] = avancado_fmt["pct_dias_meta"].apply(lambda v: f"{v:.1f}%")
        avancado_fmt["Melhor Dia"] = avancado_fmt.apply(
            lambda r: (
                f"{r['melhor_dia_data'].strftime('%d/%m')} ({db.formatar_moeda(r['melhor_dia_valor'])})"
                if r["melhor_dia_data"] is not None else "—"
            ),
            axis=1,
        )
        avancado_fmt["Ticket vs. Média da Loja"] = avancado_fmt["gap_ticket"].apply(
            lambda v: f"{'+' if v >= 0 else ''}{db.formatar_moeda(v)}"
        )
        st.dataframe(
            avancado_fmt[
                ["nome", "loja", "Dias sem Lançamento", "CV (Regularidade)",
                 "% Dias com Meta Batida", "Melhor Dia", "Ticket vs. Média da Loja"]
            ].rename(columns={"nome": "Nome", "loja": "Loja"}),
            use_container_width=True,
            hide_index=True,
        )
        st.caption(
            "Dias sem lançamento: dias úteis transcorridos sem nenhum registro de venda para o "
            "vendedor. CV (Coeficiente de Variação) = desvio padrão ÷ média do realizado diário — "
            "mede regularidade independente do volume vendido, por isso compara de forma justa "
            "vendedores de portes diferentes (quanto menor, mais regular). % Dias com Meta Batida "
            "compara o realizado de cada dia com a meta diária proporcional do vendedor (meta ÷ "
            "dias úteis do mês). Ticket vs. Média da Loja: positivo (verde) = ticket acima da "
            "média da loja."
        )

    st.markdown("---")

    # ---- Tendência: aceleração na quinzena e evolução de 3 meses ----
    st.markdown("### 📉 Tendência")

    st.markdown("##### Aceleração dentro do mês (1ª quinzena x 2ª quinzena)")
    if vendas_df.empty:
        st.info("Sem lançamentos diários no período para calcular a aceleração quinzenal.")
    else:
        vendas_quinzena = vendas_df.copy()
        vendas_quinzena["quinzena"] = vendas_quinzena["data"].apply(
            lambda d: "1ª Quinzena" if d.day <= 15 else "2ª Quinzena"
        )
        pivot_quinzena = (
            vendas_quinzena.groupby(["vendedor_id", "quinzena"])["valor_realizado"]
            .sum()
            .reset_index()
            .pivot(index="vendedor_id", columns="quinzena", values="valor_realizado")
            .reset_index()
        )
        for col in ["1ª Quinzena", "2ª Quinzena"]:
            if col not in pivot_quinzena.columns:
                pivot_quinzena[col] = 0.0
        pivot_quinzena = pivot_quinzena.fillna(0.0)

        quinzena_df = indicadores_atual[["vendedor_id", "nome", "loja"]].merge(
            pivot_quinzena, on="vendedor_id", how="left"
        ).fillna(0.0)
        quinzena_df["variacao_pct"] = quinzena_df.apply(
            lambda r: (
                (r["2ª Quinzena"] - r["1ª Quinzena"]) / r["1ª Quinzena"] * 100
            ) if r["1ª Quinzena"] > 0 else None,
            axis=1,
        )
        quinzena_df = quinzena_df.sort_values("2ª Quinzena", ascending=False)

        quinzena_fmt = quinzena_df.copy()
        quinzena_fmt["1ª Quinzena (R$)"] = quinzena_fmt["1ª Quinzena"].apply(db.formatar_moeda)
        quinzena_fmt["2ª Quinzena (R$)"] = quinzena_fmt["2ª Quinzena"].apply(db.formatar_moeda)
        quinzena_fmt["Variação"] = quinzena_fmt["variacao_pct"].apply(
            lambda v: "—" if v is None else f"{'▲' if v >= 0 else '▼'} {v:.1f}%"
        )
        st.dataframe(
            quinzena_fmt[
                ["nome", "loja", "1ª Quinzena (R$)", "2ª Quinzena (R$)", "Variação"]
            ].rename(columns={"nome": "Nome", "loja": "Loja"}),
            use_container_width=True,
            hide_index=True,
        )

    st.markdown("##### Tendência de atingimento (últimos 3 meses)")
    ano_m1, mes_m1 = db.mes_anterior(ano_filtro, mes_filtro)
    ano_m2, mes_m2 = db.mes_anterior(ano_m1, mes_m1)

    ind_m1 = db.get_indicadores_vendedores_mes(ano_m1, mes_m1, loja=loja_filtro)[
        ["vendedor_id", "atingimento_pct"]
    ].rename(columns={"atingimento_pct": "m1"})
    ind_m2 = db.get_indicadores_vendedores_mes(ano_m2, mes_m2, loja=loja_filtro)[
        ["vendedor_id", "atingimento_pct"]
    ].rename(columns={"atingimento_pct": "m2"})

    tendencia_df = indicadores_atual[["vendedor_id", "nome", "loja", "atingimento_pct"]].rename(
        columns={"atingimento_pct": "atual"}
    )
    tendencia_df = tendencia_df.merge(ind_m1, on="vendedor_id", how="left").merge(
        ind_m2, on="vendedor_id", how="left"
    ).fillna(0.0)

    def calcular_slope(row):
        xs = np.array([0.0, 1.0, 2.0])
        ys = np.array([row["m2"], row["m1"], row["atual"]])
        return float(np.polyfit(xs, ys, 1)[0])

    def rotular_slope(slope):
        if slope > 2:
            return "📈 Alta"
        if slope < -2:
            return "📉 Queda"
        return "➡️ Estável"

    tendencia_df["slope"] = tendencia_df.apply(calcular_slope, axis=1)
    tendencia_df["Tendência"] = tendencia_df["slope"].apply(rotular_slope)
    tendencia_fmt = tendencia_df.copy()
    tendencia_fmt[f"{db.MESES_PT[mes_m2]}/{ano_m2}"] = tendencia_fmt["m2"].apply(lambda v: f"{v:.1f}%")
    tendencia_fmt[f"{db.MESES_PT[mes_m1]}/{ano_m1}"] = tendencia_fmt["m1"].apply(lambda v: f"{v:.1f}%")
    tendencia_fmt[f"{db.MESES_PT[mes_filtro]}/{ano_filtro} (atual)"] = tendencia_fmt["atual"].apply(
        lambda v: f"{v:.1f}%"
    )
    tendencia_fmt["Inclinação (pp/mês)"] = tendencia_fmt["slope"].apply(
        lambda v: f"{'+' if v >= 0 else ''}{v:.1f} pp/mês"
    )
    st.dataframe(
        tendencia_fmt[
            ["nome", "loja", f"{db.MESES_PT[mes_m2]}/{ano_m2}", f"{db.MESES_PT[mes_m1]}/{ano_m1}",
             f"{db.MESES_PT[mes_filtro]}/{ano_filtro} (atual)", "Inclinação (pp/mês)", "Tendência"]
        ].rename(columns={"nome": "Nome", "loja": "Loja"}),
        use_container_width=True,
        hide_index=True,
    )
    st.caption(
        "Atingimento (%) = realizado ÷ meta do mês. Tendência calculada por regressão linear "
        "simples sobre os 3 meses (inclinação em pontos percentuais de atingimento por mês, via "
        "numpy.polyfit) — Alta/Queda exige inclinação acima de ±2 pp/mês; entre isso, considera-se "
        "Estável. Mais sensível que exigir 3 meses seguidos estritamente na mesma direção."
    )

    st.markdown("---")

    # ---- Score Composto de Performance (0-100) ----
    st.markdown("### 🏅 Score de Performance (0–100)")
    st.caption(
        "Índice único por vendedor: 50% Atingimento da meta, 20% Consistência (regularidade das "
        "vendas diárias, via CV), 15% Tendência (inclinação dos últimos 3 meses) e 15% Ticket "
        "médio vs. média da loja. Quando faltam dados diários para calcular consistência ou "
        "tendência, esse componente recebe nota neutra (50 pontos) em vez de penalizar o vendedor."
    )

    if indicadores_atual.empty:
        st.info("Sem vendedores no filtro selecionado para calcular o score.")
    else:
        score_df = indicadores_atual[["vendedor_id", "nome", "loja", "atingimento_pct"]].copy()
        score_df = score_df.merge(
            avancado_df[["vendedor_id", "cv", "gap_ticket_pct"]], on="vendedor_id", how="left",
        )
        score_df = score_df.merge(
            tendencia_df[["vendedor_id", "slope"]], on="vendedor_id", how="left"
        )

        def _score_atingimento(pct):
            return max(0.0, min(float(pct), 150.0)) / 150.0 * 100.0

        def _score_consistencia(cv):
            if cv is None or pd.isna(cv):
                return 50.0
            return max(0.0, min(100.0 - float(cv), 100.0))

        def _score_tendencia(slope):
            if slope is None or pd.isna(slope):
                return 50.0
            return max(0.0, min(50.0 + float(slope) * 5.0, 100.0))

        def _score_ticket(gap_pct):
            if gap_pct is None or pd.isna(gap_pct):
                return 50.0
            return max(0.0, min(50.0 + float(gap_pct), 100.0))

        score_df["score_atingimento"] = score_df["atingimento_pct"].apply(_score_atingimento)
        score_df["score_consistencia"] = score_df["cv"].apply(_score_consistencia)
        score_df["score_tendencia"] = score_df["slope"].apply(_score_tendencia)
        score_df["score_ticket"] = score_df["gap_ticket_pct"].apply(_score_ticket)
        score_df["score_final"] = (
            score_df["score_atingimento"] * 0.50
            + score_df["score_consistencia"] * 0.20
            + score_df["score_tendencia"] * 0.15
            + score_df["score_ticket"] * 0.15
        )
        score_df = score_df.sort_values("score_final", ascending=False).reset_index(drop=True)

        medalhas = {0: "🥇", 1: "🥈", 2: "🥉"}
        score_fmt = score_df.copy()
        score_fmt["#"] = score_fmt.index.map(lambda i: medalhas.get(i, str(i + 1)))
        score_fmt["Score"] = score_fmt["score_final"].apply(lambda v: f"{v:.1f}")
        score_fmt["Atingimento"] = score_fmt["score_atingimento"].apply(lambda v: f"{v:.0f}")
        score_fmt["Consistência"] = score_fmt["score_consistencia"].apply(lambda v: f"{v:.0f}")
        score_fmt["Tendência"] = score_fmt["score_tendencia"].apply(lambda v: f"{v:.0f}")
        score_fmt["Ticket"] = score_fmt["score_ticket"].apply(lambda v: f"{v:.0f}")

        st.dataframe(
            score_fmt[
                ["#", "nome", "loja", "Score", "Atingimento", "Consistência", "Tendência", "Ticket"]
            ].rename(columns={"nome": "Nome", "loja": "Loja"}),
            use_container_width=True,
            hide_index=True,
        )

        fig_score = go.Figure(go.Bar(
            x=score_df["score_final"], y=score_df["nome"], orientation="h",
            marker_color=AZUL, text=score_df["score_final"].apply(lambda v: f"{v:.1f}"),
            textposition="outside",
        ))
        fig_score.update_layout(
            xaxis=dict(title="Score (0-100)", range=[0, 108]),
            yaxis=dict(autorange="reversed"),
            margin=dict(l=10, r=10, t=20, b=10), height=max(280, 32 * len(score_df)),
        )
        st.plotly_chart(fig_score, use_container_width=True)

    st.markdown("---")

    # ---- Vendedores em Atenção (sinal para plano de ação / troca) ----
    st.markdown("### ⚠️ Vendedores em Atenção")
    st.caption(
        "Sinal construído a partir de TODO o histórico do vendedor (todos os meses com meta "
        "lançada) somado à projeção do mês corrente. Não é uma decisão automática de troca — "
        "serve como gatilho para um plano de ação (30/60/90 dias) antes de qualquer decisão."
    )

    historico_full = db.get_historico_meta_realizado(loja=loja_filtro)

    if historico_full.empty or indicadores_atual.empty:
        st.info("Sem histórico suficiente para calcular os sinais de atenção.")
    else:
        LIMIAR_CRITICO = 70.0
        LIMIAR_ATUAL_CRITICO = 60.0
        LIMIAR_CV_ALTO = 40.0
        LIMIAR_MEDIA_BAIXA = 85.0
        LIMIAR_SLOPE_NEGATIVO = -2.0

        linhas_atencao = []
        for row in indicadores_atual.itertuples():
            hist_vend = historico_full[historico_full["vendedor_id"] == row.vendedor_id].copy()
            hist_vend = hist_vend[hist_vend["valor_meta"] > 0]
            hist_vend["atingimento_pct"] = hist_vend["realizado"] / hist_vend["valor_meta"] * 100
            hist_vend = hist_vend.sort_values(["ano", "mes"])
            n_meses_hist = len(hist_vend)

            # Projeção do mês corrente (mesmo run-rate usado no KPI "Projeção de Fechamento" da loja)
            if dias_transcorridos > 0 and dias_transcorridos < dias_uteis_total and row.realizado > 0 and row.valor_meta > 0:
                atingimento_atual_proj = (
                    (row.realizado / dias_transcorridos * dias_uteis_total) / row.valor_meta * 100
                )
            else:
                atingimento_atual_proj = row.atingimento_pct

            # Histórico excluindo o mês atual (evita duplicar o efeito da projeção)
            hist_sem_atual = hist_vend[
                ~((hist_vend["ano"] == ano_filtro) & (hist_vend["mes"] == mes_filtro))
            ]

            ultimos_3 = hist_sem_atual.tail(3)
            n_ultimos_3 = len(ultimos_3)
            n_criticos_ultimos_3 = int((ultimos_3["atingimento_pct"] < LIMIAR_CRITICO).sum())
            criterio_persistencia = n_ultimos_3 >= 2 and n_criticos_ultimos_3 >= 2

            if len(hist_sem_atual) >= 3:
                xs = np.arange(len(hist_sem_atual), dtype=float)
                ys = hist_sem_atual["atingimento_pct"].to_numpy(dtype=float)
                slope_hist = float(np.polyfit(xs, ys, 1)[0])
                media_hist = float(hist_sem_atual["atingimento_pct"].mean())
                std_hist = float(hist_sem_atual["atingimento_pct"].std(ddof=0))
                cv_hist = (std_hist / media_hist * 100) if media_hist > 0 else None
            else:
                slope_hist, media_hist, cv_hist = None, None, None

            criterio_tendencia = slope_hist is not None and slope_hist < LIMIAR_SLOPE_NEGATIVO
            criterio_atual = row.valor_meta > 0 and atingimento_atual_proj < LIMIAR_ATUAL_CRITICO
            criterio_consistencia = (
                cv_hist is not None and media_hist is not None
                and cv_hist > LIMIAR_CV_ALTO and media_hist < LIMIAR_MEDIA_BAIXA
            )

            criterios_atendidos = sum(
                [criterio_persistencia, criterio_tendencia, criterio_atual, criterio_consistencia]
            )
            if criterios_atendidos >= 3:
                zona = "🔴 Crítico"
            elif criterios_atendidos >= 1:
                zona = "🟡 Atenção"
            else:
                zona = "🟢 Ok"

            dias_sem_lanc_row = avancado_df[avancado_df["vendedor_id"] == row.vendedor_id]
            dias_sem_lanc = (
                int(dias_sem_lanc_row["dias_sem_lancamento"].iloc[0])
                if not dias_sem_lanc_row.empty and "dias_sem_lancamento" in dias_sem_lanc_row.columns
                else None
            )

            linhas_atencao.append({
                "vendedor_id": row.vendedor_id,
                "nome": row.nome,
                "loja": row.loja,
                "zona": zona,
                "criterios_atendidos": criterios_atendidos,
                "n_meses_hist": n_meses_hist,
                "atingimento_atual_proj": atingimento_atual_proj,
                "slope_hist": slope_hist,
                "cv_hist": cv_hist,
                "persistencia": criterio_persistencia,
                "tendencia": criterio_tendencia,
                "atual_critico": criterio_atual,
                "consistencia": criterio_consistencia,
                "dias_sem_lancamento": dias_sem_lanc,
            })

        atencao_df = pd.DataFrame(linhas_atencao)
        ordem_zona = {"🔴 Crítico": 0, "🟡 Atenção": 1, "🟢 Ok": 2}
        atencao_df["ordem"] = atencao_df["zona"].map(ordem_zona)
        atencao_df = atencao_df.sort_values(
            ["ordem", "criterios_atendidos"], ascending=[True, False]
        ).drop(columns=["ordem"])

        n_criticos = int((atencao_df["zona"] == "🔴 Crítico").sum())
        n_atencao = int((atencao_df["zona"] == "🟡 Atenção").sum())
        if n_criticos > 0:
            st.error(
                f"🔴 {n_criticos} vendedor(es) em zona crítica — recomenda-se plano de ação "
                "antes de qualquer decisão de troca."
            )
        elif n_atencao > 0:
            st.warning(f"🟡 {n_atencao} vendedor(es) em zona de atenção.")
        else:
            st.success("🟢 Nenhum vendedor em zona de atenção ou crítica no momento.")

        def _fmt_criterio(v):
            return "🔴" if v else "—"

        atencao_fmt = atencao_df.copy()
        atencao_fmt["Meses no Histórico"] = atencao_fmt["n_meses_hist"]
        atencao_fmt["Atingimento Atual (Projetado)"] = atencao_fmt["atingimento_atual_proj"].apply(
            lambda v: f"{v:.1f}%"
        )
        atencao_fmt["Persistência (2 de 3 últimos < 70%)"] = atencao_fmt["persistencia"].apply(_fmt_criterio)
        atencao_fmt["Tendência Histórica"] = atencao_fmt.apply(
            lambda r: (
                (f"{'+' if r['slope_hist'] >= 0 else ''}{r['slope_hist']:.1f} pp/mês")
                if r["slope_hist"] is not None else "—"
            ) + (" 🔴" if r["tendencia"] else ""),
            axis=1,
        )
        atencao_fmt["Mês Atual Crítico"] = atencao_fmt["atual_critico"].apply(_fmt_criterio)
        atencao_fmt["Consistência Histórica"] = atencao_fmt.apply(
            lambda r: (
                (f"CV {r['cv_hist']:.0f}%") if r["cv_hist"] is not None else "—"
            ) + (" 🔴" if r["consistencia"] else ""),
            axis=1,
        )
        atencao_fmt["Dias sem Lanç. (mês atual)"] = atencao_fmt["dias_sem_lancamento"].apply(
            lambda v: str(v) if v is not None else "—"
        )

        st.dataframe(
            atencao_fmt[
                ["nome", "loja", "zona", "Meses no Histórico", "Atingimento Atual (Projetado)",
                 "Persistência (2 de 3 últimos < 70%)", "Tendência Histórica", "Mês Atual Crítico",
                 "Consistência Histórica", "Dias sem Lanç. (mês atual)"]
            ].rename(columns={"nome": "Nome", "loja": "Loja", "zona": "Zona"}),
            use_container_width=True,
            hide_index=True,
        )
        st.caption(
            "Regras (cada uma soma 1 ponto; 3-4 pontos = 🔴 Crítico, 1-2 = 🟡 Atenção, 0 = 🟢 Ok): "
            "Persistência = pelo menos 2 dos últimos 3 meses do histórico (excluindo o mês atual) "
            "abaixo de 70% de atingimento • Tendência = inclinação negativa (< -2 pp/mês) na "
            "regressão linear sobre todo o histórico • Mês Atual Crítico = atingimento projetado "
            "do mês corrente (run-rate ÷ dias transcorridos × dias úteis do mês) abaixo de 60% • "
            "Consistência Histórica = alta variação do atingimento mês a mês (CV > 40%) combinada "
            "com média histórica abaixo de 85%. Vendedores com menos de 3 meses de histórico só são "
            "avaliados pelo critério do Mês Atual — os demais exigem dados insuficientes e não "
            "contam contra o vendedor (evita punir quem é novo por falta de histórico)."
        )

    st.markdown("---")

    # ---- Curva de concentração (Pareto 80/20) ----
    st.markdown("### 🎯 Curva de Concentração (Pareto 80/20)")
    if indicadores_atual.empty or indicadores_atual["realizado"].sum() <= 0:
        st.info("Sem realizado no período para montar a curva de concentração.")
    else:
        pareto_df = indicadores_atual.sort_values("realizado", ascending=False).reset_index(drop=True)
        total_pareto = pareto_df["realizado"].sum()
        pareto_df["cum_pct"] = pareto_df["realizado"].cumsum() / total_pareto * 100

        fig_pareto = go.Figure()
        fig_pareto.add_trace(
            go.Bar(x=pareto_df["nome"], y=pareto_df["realizado"], name="Realizado",
                   marker_color=AZUL_CLARO, yaxis="y1")
        )
        fig_pareto.add_trace(
            go.Scatter(x=pareto_df["nome"], y=pareto_df["cum_pct"], name="% Acumulado",
                       mode="lines+markers", line=dict(color=VERDE, width=2), yaxis="y2")
        )
        fig_pareto.update_layout(
            yaxis=dict(title="Realizado (R$)"),
            yaxis2=dict(title="% Acumulado", overlaying="y", side="right", range=[0, 110]),
            shapes=[dict(
                type="line", xref="paper", x0=0, x1=1, y0=80, y1=80, yref="y2",
                line=dict(color="#e74c3c", dash="dot", width=1.5),
            )],
            legend=dict(orientation="h", y=-0.3),
            margin=dict(l=10, r=10, t=30, b=10), height=420,
        )
        st.plotly_chart(fig_pareto, use_container_width=True)

        n_concentra_80 = int((pareto_df["cum_pct"] < 80).sum()) + 1
        n_concentra_80 = min(n_concentra_80, len(pareto_df))
        st.caption(
            f"📌 {n_concentra_80} de {len(pareto_df)} vendedor(es) concentram 80% do realizado "
            f"do filtro selecionado (linha vermelha pontilhada = 80% acumulado)."
        )

    st.markdown("---")

    # ---- Vendas por Produto ----
    st.markdown("### 🛒 Vendas por Produto")
    st.caption(
        "Baseado no relatório 'Totais de Vendas Por Produto' do SGI, sincronizado "
        "automaticamente junto com as vendas diárias (12h e 19h). Sem dado até a primeira "
        "sincronização desse relatório rodar."
    )

    def _fmt_qtd(v):
        return f"{v:,.0f}".replace(",", ".")

    def _fmt_pct_sinal(v):
        if v is None or pd.isna(v):
            return "—"
        seta = "▲" if v >= 0 else "▼"
        return f"{seta} {v:.1f}%"

    resumo_prod = db.get_resumo_produtos_mes(ano_filtro, mes_filtro, loja=loja_filtro)

    if resumo_prod["n_produtos"] == 0:
        st.info("Nenhuma venda por produto lançada para o filtro selecionado.")
    else:
        # ---- Indicadores do mês (+ projeção de fechamento, mesmo ritmo usado no
        # topo do painel pro realizado geral) ----
        st.markdown("##### Indicadores do mês")
        if dias_transcorridos > 0:
            projecao_produtos = resumo_prod["faturamento_total"] / dias_transcorridos * dias_uteis_total
        else:
            projecao_produtos = resumo_prod["faturamento_total"]

        p1, p2, p3, p4, p5, p6 = st.columns(6)
        kpi_card(p1, "Faturamento em Produtos", db.formatar_moeda(resumo_prod["faturamento_total"]), cor=VERDE)
        kpi_card(p2, "Projeção de Fechamento", db.formatar_moeda(projecao_produtos))
        kpi_card(p3, "Itens Vendidos", _fmt_qtd(resumo_prod["qtd_total"]))
        kpi_card(p4, "Ticket Médio por Item", db.formatar_moeda(resumo_prod["ticket_medio_item"]))
        kpi_card(p5, "Produtos Distintos (SKUs)", str(resumo_prod["n_produtos"]))
        kpi_card(p6, "Fornecedores / Marcas", f"{resumo_prod['n_fornecedores']} / {resumo_prod['n_marcas']}")
        st.caption(
            "Projeção de Fechamento = mesmo cálculo de ritmo de meta do topo do painel "
            "(realizado em produtos ÷ dias úteis transcorridos × dias úteis do mês), aplicado "
            "ao faturamento em produtos."
        )

        # ---- Concentração de portfólio ----
        concentracao = db.get_concentracao_portfolio(ano_filtro, mes_filtro, loja=loja_filtro)
        c1, c2 = st.columns(2)
        kpi_card(
            c1, "Concentração — Top 5 produtos",
            f"{concentracao['top5_pct']:.1f}%" if concentracao["top5_pct"] is not None else "—",
        )
        kpi_card(
            c2, "Concentração — Top 10 produtos",
            f"{concentracao['top10_pct']:.1f}%" if concentracao["top10_pct"] is not None else "—",
        )
        st.caption(
            "% do faturamento em produtos do mês que vem só dos 5/10 SKUs mais vendidos — "
            "quanto maior, mais o negócio depende de poucos produtos (risco de ruptura de "
            "estoque ou de um fornecedor específico)."
        )

        # ---- Tendência do portfólio (série histórica já importada) ----
        st.markdown("##### Tendência do faturamento em produtos (últimos 6 meses)")
        serie_portfolio = db.get_serie_mensal_produtos(loja=loja_filtro, meses=6)
        if len(serie_portfolio) < 2:
            st.info("Ainda não há histórico suficiente (pelo menos 2 meses) pra montar a tendência.")
        else:
            serie_portfolio = serie_portfolio.copy()
            serie_portfolio["rotulo"] = serie_portfolio.apply(
                lambda r: f"{db.MESES_PT[int(r['mes'])][:3]}/{int(r['ano'])}", axis=1
            )
            fig_serie_prod = go.Figure(go.Scatter(
                x=serie_portfolio["rotulo"], y=serie_portfolio["valor_total"],
                mode="lines+markers", line=dict(color=AZUL, width=3), marker=dict(size=7),
                fill="tozeroy", fillcolor="rgba(26, 82, 118, 0.08)",
            ))
            fig_serie_prod.update_layout(
                margin=dict(l=10, r=10, t=20, b=10), height=280,
                yaxis=dict(title="Faturamento em Produtos (R$)"),
            )
            st.plotly_chart(fig_serie_prod, use_container_width=True)

            if len(serie_portfolio) >= 3:
                xs_prod = np.arange(len(serie_portfolio), dtype=float)
                ys_prod = serie_portfolio["valor_total"].to_numpy(dtype=float)
                slope_prod = float(np.polyfit(xs_prod, ys_prod, 1)[0])
                media_prod = float(ys_prod.mean())
                slope_pct_prod = (slope_prod / media_prod * 100) if media_prod > 0 else 0.0
                if slope_pct_prod > 5:
                    rotulo_tend_prod = "📈 em alta"
                elif slope_pct_prod < -5:
                    rotulo_tend_prod = "📉 em queda"
                else:
                    rotulo_tend_prod = "➡️ estável"
                st.caption(
                    f"Tendência (regressão linear dos últimos {len(serie_portfolio)} meses): "
                    f"{rotulo_tend_prod} — {'+' if slope_pct_prod >= 0 else ''}{slope_pct_prod:.1f}% "
                    "ao mês, em média."
                )

        st.markdown("---")

        st.markdown("##### Curva ABC de produtos")
        st.caption(
            "Classificação clássica de portfólio (Pareto): produtos ordenados por faturamento, "
            "com % acumulado. Classe A = os produtos que juntos somam os primeiros 80% do "
            "faturamento (prioridade de estoque/negociação); B = de 80% a 95%; C = os últimos "
            "5% (cauda longa — muitos itens com pouca representatividade individual)."
        )
        curva_abc_df = db.get_curva_abc_produtos(ano_filtro, mes_filtro, loja=loja_filtro)
        if curva_abc_df.empty:
            st.info("Sem dado suficiente para montar a curva ABC.")
        else:
            contagem_classe = curva_abc_df["classe"].value_counts()
            valor_classe = curva_abc_df.groupby("classe")["valor_total"].sum()
            ca1, ca2, ca3 = st.columns(3)
            for col, classe, cor_classe in [(ca1, "A", VERDE), (ca2, "B", "#f39c12"), (ca3, "C", "#888888")]:
                n_itens = int(contagem_classe.get(classe, 0))
                valor_itens = float(valor_classe.get(classe, 0.0))
                kpi_card(
                    col, f"Classe {classe}",
                    f"{n_itens} produto(s) — {db.formatar_moeda(valor_itens)}",
                    cor=cor_classe,
                )
            with st.expander("Ver tabela completa da Curva ABC", expanded=False):
                abc_fmt = curva_abc_df.copy()
                abc_fmt["Qtd"] = abc_fmt["qtd_total"].apply(_fmt_qtd)
                abc_fmt["Valor Total"] = abc_fmt["valor_total"].apply(db.formatar_moeda)
                abc_fmt["Participação"] = abc_fmt["pct_participacao"].apply(lambda v: f"{v:.2f}%")
                abc_fmt["Acumulado"] = abc_fmt["pct_acumulado"].apply(lambda v: f"{v:.2f}%")
                st.dataframe(
                    abc_fmt[[
                        "cod_produto", "descricao_produto", "marca", "fornecedor",
                        "Qtd", "Valor Total", "Participação", "Acumulado", "classe",
                    ]].rename(columns={
                        "cod_produto": "Cód.", "descricao_produto": "Produto",
                        "marca": "Marca", "fornecedor": "Fornecedor", "classe": "Classe",
                    }),
                    use_container_width=True, hide_index=True,
                )

        st.markdown("---")

        # ---- Produtos novos e parados (churn de portfólio) ----
        st.markdown("##### Produtos novos e parados")
        st.caption(
            "Compara o portfólio vendido este mês com os 3 meses anteriores (sem contar o mês "
            "atual). Novos = venderam este mês e não vendiam há pelo menos 3 meses. "
            "Parados = vendiam nesses últimos 3 meses e não venderam nada este mês — candidatos "
            "a revisão de estoque/mix."
        )
        novos_parados = db.get_produtos_novos_e_parados(ano_filtro, mes_filtro, loja=loja_filtro, janela_meses=3)
        col_np1, col_np2 = st.columns(2)
        with col_np1:
            df_novos = novos_parados["novos"]
            st.write(f"**🆕 Novos ({len(df_novos)})**")
            if df_novos.empty:
                st.caption("Nenhum produto novo neste mês.")
            else:
                novos_fmt = df_novos.copy()
                novos_fmt["Valor no Mês"] = novos_fmt["valor_total"].apply(db.formatar_moeda)
                st.dataframe(
                    novos_fmt[["cod_produto", "descricao_produto", "fornecedor", "Valor no Mês"]]
                    .rename(columns={"cod_produto": "Cód.", "descricao_produto": "Produto", "fornecedor": "Fornecedor"}),
                    use_container_width=True, hide_index=True,
                )
        with col_np2:
            df_parados = novos_parados["parados"]
            st.write(f"**⏸️ Parados ({len(df_parados)})**")
            if df_parados.empty:
                st.caption("Nenhum produto parou de vender nesse período.")
            else:
                parados_fmt = df_parados.sort_values("valor_total", ascending=False).copy()
                parados_fmt["Valor (na janela)"] = parados_fmt["valor_total"].apply(db.formatar_moeda)
                parados_fmt["Última Venda"] = pd.to_datetime(parados_fmt["ultima_venda"]).dt.strftime("%d/%m/%Y")
                parados_fmt["Dias Parado"] = parados_fmt["dias_parado"]
                st.dataframe(
                    parados_fmt[["cod_produto", "descricao_produto", "fornecedor", "Valor (na janela)", "Última Venda", "Dias Parado"]]
                    .rename(columns={"cod_produto": "Cód.", "descricao_produto": "Produto", "fornecedor": "Fornecedor"}),
                    use_container_width=True, hide_index=True,
                )

        st.markdown("---")

        st.markdown("##### Produtos mais vendidos (geral) — com crescimento vs mês anterior")
        produtos_top_df = db.get_comparativo_produtos(ano_filtro, mes_filtro, loja=loja_filtro, top_n=15)
        if produtos_top_df.empty:
            st.info("Nenhuma venda por produto lançada para o filtro selecionado.")
        else:
            produtos_fmt = produtos_top_df.copy()
            produtos_fmt["Qtd"] = produtos_fmt["qtd_total"].apply(_fmt_qtd)
            produtos_fmt["Valor Total"] = produtos_fmt["valor_total"].apply(db.formatar_moeda)
            produtos_fmt["Crescimento"] = produtos_fmt["crescimento_pct"].apply(_fmt_pct_sinal)
            st.dataframe(
                produtos_fmt[["cod_produto", "descricao_produto", "marca", "fornecedor", "Qtd", "Valor Total", "Crescimento"]]
                .rename(columns={
                    "cod_produto": "Cód.", "descricao_produto": "Produto",
                    "marca": "Marca", "fornecedor": "Fornecedor",
                }),
                use_container_width=True, hide_index=True,
            )

        st.markdown("##### 🔍 Detalhar um produto")
        st.caption(
            "Busque um produto pelo código ou nome pra ver a série histórica mensal (com base "
            "em toda a série já importada), tendência, e quais vendedores mais vendem ele."
        )
        termo_busca_produto = st.text_input(
            "Buscar produto (código ou nome)", key="termo_busca_produto",
            placeholder="Ex.: ureia, 1234, adubo...",
        )
        if termo_busca_produto and termo_busca_produto.strip():
            resultados_busca = db.buscar_produtos(termo_busca_produto, loja=loja_filtro, limite=30)
            if resultados_busca.empty:
                st.info("Nenhum produto encontrado com esse termo.")
            else:
                opcoes_busca = {
                    f"{row['cod_produto']} — {row['descricao_produto']}": row["cod_produto"]
                    for _, row in resultados_busca.iterrows()
                }
                escolha_busca = st.selectbox(
                    "Selecione o produto", list(opcoes_busca.keys()), key="sel_produto_detalhe"
                )
                cod_produto_sel = opcoes_busca[escolha_busca]

                serie_prod_sel = db.get_serie_produto(cod_produto_sel, loja=loja_filtro, meses=12)
                if serie_prod_sel.empty:
                    st.info("Sem série histórica pra esse produto no filtro selecionado.")
                else:
                    serie_prod_sel = serie_prod_sel.copy()
                    serie_prod_sel["rotulo"] = serie_prod_sel.apply(
                        lambda r: f"{db.MESES_PT[int(r['mes'])][:3]}/{int(r['ano'])}", axis=1
                    )
                    dcol1, dcol2 = st.columns([1.4, 1])
                    with dcol1:
                        fig_prod_sel = go.Figure(go.Bar(
                            x=serie_prod_sel["rotulo"], y=serie_prod_sel["valor_total"], marker_color=AZUL_CLARO,
                        ))
                        fig_prod_sel.update_layout(
                            margin=dict(l=10, r=10, t=20, b=10), height=300,
                            yaxis=dict(title="Faturamento (R$)"),
                        )
                        st.plotly_chart(fig_prod_sel, use_container_width=True)
                    with dcol2:
                        total_hist_prod = float(serie_prod_sel["valor_total"].sum())
                        qtd_hist_prod = float(serie_prod_sel["qtd_total"].sum())
                        media_mensal_prod = total_hist_prod / len(serie_prod_sel)
                        kpi_card(st, "Faturamento (período)", db.formatar_moeda(total_hist_prod))
                        kpi_card(st, "Qtd. Vendida (período)", _fmt_qtd(qtd_hist_prod))
                        kpi_card(st, "Média Mensal", db.formatar_moeda(media_mensal_prod))
                        if len(serie_prod_sel) >= 3:
                            xs_ps = np.arange(len(serie_prod_sel), dtype=float)
                            ys_ps = serie_prod_sel["valor_total"].to_numpy(dtype=float)
                            slope_ps = float(np.polyfit(xs_ps, ys_ps, 1)[0])
                            slope_pct_ps = (slope_ps / media_mensal_prod * 100) if media_mensal_prod > 0 else 0.0
                            if slope_pct_ps > 5:
                                rot_ps = "📈 em alta"
                            elif slope_pct_ps < -5:
                                rot_ps = "📉 em queda"
                            else:
                                rot_ps = "➡️ estável"
                            kpi_card(st, "Tendência", rot_ps)

                    st.write("**Quem mais vende esse produto (mês do filtro)**")
                    vend_prod_df = db.get_vendedores_por_produto(
                        cod_produto_sel, ano_filtro, mes_filtro, loja=loja_filtro
                    )
                    if vend_prod_df.empty:
                        st.caption("Ninguém vendeu esse produto no mês selecionado.")
                    else:
                        vend_prod_fmt = vend_prod_df.copy()
                        vend_prod_fmt["Qtd"] = vend_prod_fmt["qtd_total"].apply(_fmt_qtd)
                        vend_prod_fmt["Valor Total"] = vend_prod_fmt["valor_total"].apply(db.formatar_moeda)
                        st.dataframe(
                            vend_prod_fmt[["nome", "loja", "Qtd", "Valor Total"]]
                            .rename(columns={"nome": "Vendedor", "loja": "Loja"}),
                            use_container_width=True, hide_index=True,
                        )

        st.markdown("---")

        st.markdown("##### Produtos mais vendidos por vendedor")
        top_vend_df = db.get_produtos_mais_vendidos_por_vendedor(ano_filtro, mes_filtro, loja=loja_filtro, top_n=5)
        if top_vend_df.empty:
            st.info("Nenhuma venda por produto lançada para o filtro selecionado.")
        else:
            opcoes_vend_prod = sorted(top_vend_df["nome"].unique().tolist())
            vendedor_sel_prod = st.selectbox("Vendedor", opcoes_vend_prod, key="sel_vendedor_produtos")
            sub_vend = top_vend_df[top_vend_df["nome"] == vendedor_sel_prod].copy()
            total_vend_prod = float(sub_vend["valor_total"].sum())
            sub_vend["% da Carteira (top 5)"] = sub_vend["valor_total"].apply(
                lambda v: f"{(v / total_vend_prod * 100):.1f}%" if total_vend_prod > 0 else "—"
            )
            sub_vend["Qtd"] = sub_vend["qtd_total"].apply(_fmt_qtd)
            sub_vend["Valor Total"] = sub_vend["valor_total"].apply(db.formatar_moeda)
            st.dataframe(
                sub_vend[[
                    "cod_produto", "descricao_produto", "marca", "fornecedor",
                    "Qtd", "Valor Total", "% da Carteira (top 5)",
                ]].rename(columns={
                    "cod_produto": "Cód.", "descricao_produto": "Produto",
                    "marca": "Marca", "fornecedor": "Fornecedor",
                }),
                use_container_width=True, hide_index=True,
            )
            st.caption(
                "% da Carteira (top 5) = participação do produto dentro dos 5 produtos mais "
                "vendidos DESSE vendedor no mês (não do faturamento total dele)."
            )

        st.markdown("---")

        st.markdown("##### Ranking por marca / fornecedor — com crescimento e tendência")
        col_rk1, col_rk2 = st.columns(2)

        def _tendencia_grupo(serie_grupo_df, grupo):
            sub = serie_grupo_df[serie_grupo_df["grupo"] == grupo].sort_values(["ano", "mes"])
            if len(sub) < 3:
                return "—"
            xs_g = np.arange(len(sub), dtype=float)
            ys_g = sub["valor_total"].to_numpy(dtype=float)
            slope_g = float(np.polyfit(xs_g, ys_g, 1)[0])
            media_g = float(ys_g.mean())
            slope_pct_g = (slope_g / media_g * 100) if media_g > 0 else 0.0
            if slope_pct_g > 5:
                return "📈"
            if slope_pct_g < -5:
                return "📉"
            return "➡️"

        with col_rk1:
            st.write("**Por Fornecedor**")
            rank_forn_df = db.get_comparativo_marca_fornecedor(
                ano_filtro, mes_filtro, loja=loja_filtro, agrupar_por="fornecedor", top_n=10
            )
            if rank_forn_df.empty:
                st.info("Sem dado de fornecedor no período.")
            else:
                serie_forn = db.get_serie_mensal_grupo(
                    loja=loja_filtro, agrupar_por="fornecedor", meses=6, top_n=10
                )
                rank_forn_fmt = rank_forn_df.copy()
                rank_forn_fmt["Valor Total"] = rank_forn_fmt["valor_total"].apply(db.formatar_moeda)
                rank_forn_fmt["Crescimento"] = rank_forn_fmt["crescimento_pct"].apply(_fmt_pct_sinal)
                rank_forn_fmt["Tend. 6m"] = rank_forn_fmt["grupo"].apply(
                    lambda g: _tendencia_grupo(serie_forn, g)
                )
                st.dataframe(
                    rank_forn_fmt[["grupo", "Valor Total", "Crescimento", "Tend. 6m"]].rename(columns={"grupo": "Fornecedor"}),
                    use_container_width=True, hide_index=True,
                )
        with col_rk2:
            st.write("**Por Marca**")
            rank_marca_df = db.get_comparativo_marca_fornecedor(
                ano_filtro, mes_filtro, loja=loja_filtro, agrupar_por="marca", top_n=10
            )
            if rank_marca_df.empty:
                st.info("Sem dado de marca no período.")
            else:
                serie_marca = db.get_serie_mensal_grupo(
                    loja=loja_filtro, agrupar_por="marca", meses=6, top_n=10
                )
                rank_marca_fmt = rank_marca_df.copy()
                rank_marca_fmt["Valor Total"] = rank_marca_fmt["valor_total"].apply(db.formatar_moeda)
                rank_marca_fmt["Crescimento"] = rank_marca_fmt["crescimento_pct"].apply(_fmt_pct_sinal)
                rank_marca_fmt["Tend. 6m"] = rank_marca_fmt["grupo"].apply(
                    lambda g: _tendencia_grupo(serie_marca, g)
                )
                st.dataframe(
                    rank_marca_fmt[["grupo", "Valor Total", "Crescimento", "Tend. 6m"]].rename(columns={"grupo": "Marca"}),
                    use_container_width=True, hide_index=True,
                )
        st.caption(
            "Crescimento = variação do faturamento do grupo (fornecedor/marca) vs o mesmo grupo "
            "no mês anterior. Tend. 6m = direção da regressão linear do faturamento mensal do "
            "grupo nos últimos 6 meses (📈 alta / 📉 queda / ➡️ estável, precisa de pelo menos "
            "3 meses com venda). Sem % de crescimento quando o grupo não teve venda no mês "
            "anterior (nada pra comparar)."
        )

        # ---- Diagnóstico Comercial — Comparativo de Concentração entre Vendedores ----
        st.markdown("---")
        st.markdown("##### 📊 Diagnóstico Comercial — Concentração de Portfólio por Vendedor")
        st.caption(
            "Compara todos os vendedores ativos do filtro entre si: quanto do faturamento em "
            "produtos de cada um está concentrado nos 5/10 SKUs mais vendidos. Quanto maior a "
            "concentração, maior o risco de depender de poucos produtos (ruptura de estoque, "
            "negociação com um único fornecedor, etc.) — e maior a oportunidade de trabalhar "
            "ampliação de mix com esse vendedor."
        )
        comparativo_concentracao = db.get_comparativo_concentracao_vendedores(ano_filtro, mes_filtro, loja=loja_filtro)
        if comparativo_concentracao.empty:
            st.info("Sem dado de produto suficiente para montar o comparativo neste período.")
        else:
            fig_concentracao = go.Figure(go.Bar(
                x=comparativo_concentracao["top5_pct"],
                y=comparativo_concentracao["nome"],
                orientation="h",
                marker_color=[
                    "#c0392b" if c.startswith("🔴") else ("#f39c12" if c.startswith("🟡") else "#1e8449")
                    for c in comparativo_concentracao["classificacao"]
                ],
                text=comparativo_concentracao["classificacao"],
                textposition="outside",
            ))
            media_top5_geral = float(comparativo_concentracao["top5_pct"].mean())
            fig_concentracao.add_vline(x=media_top5_geral, line_dash="dash", line_color="#888888")
            fig_concentracao.update_layout(
                margin=dict(l=10, r=60, t=20, b=10),
                height=max(220, 32 * len(comparativo_concentracao)),
                xaxis=dict(title="% do faturamento nos top 5 SKUs"),
                yaxis=dict(autorange="reversed"),
            )
            st.plotly_chart(fig_concentracao, use_container_width=True)
            st.caption(
                f"Linha pontilhada = média do grupo no filtro atual ({media_top5_geral:.0f}%). "
                f"🔴 Alta = ≥{db.CONCENTRACAO_LIMIAR_ALTA:.0f}%. 🟡 Moderada = entre "
                f"{db.CONCENTRACAO_LIMIAR_MODERADA:.0f}% e {db.CONCENTRACAO_LIMIAR_ALTA:.0f}%. "
                f"🟢 Diversificada = <{db.CONCENTRACAO_LIMIAR_MODERADA:.0f}%."
            )
            with st.expander("Ver tabela completa do comparativo", expanded=False):
                comp_conc_fmt = comparativo_concentracao.copy()
                comp_conc_fmt["Faturamento"] = comp_conc_fmt["faturamento_total"].apply(db.formatar_moeda)
                comp_conc_fmt["Top 5"] = comp_conc_fmt["top5_pct"].apply(lambda v: f"{v:.1f}%" if pd.notna(v) else "—")
                comp_conc_fmt["Top 10"] = comp_conc_fmt["top10_pct"].apply(lambda v: f"{v:.1f}%" if pd.notna(v) else "—")
                st.dataframe(
                    comp_conc_fmt[[
                        "nome", "loja", "Faturamento", "n_produtos", "n_fornecedores", "n_marcas",
                        "Top 5", "Top 10", "classificacao",
                    ]].rename(columns={
                        "nome": "Vendedor", "loja": "Loja", "n_produtos": "SKUs",
                        "n_fornecedores": "Fornec.", "n_marcas": "Marcas", "classificacao": "Risco",
                    }),
                    use_container_width=True, hide_index=True,
                )

        # ---- Mix de Produtos — por Vendedor / por Loja ----
        st.markdown("---")
        st.markdown("##### 🧬 Mix de Vendas Completo — por Vendedor ou por Loja")
        st.caption(
            "Analytics completo de portfólio para UM vendedor específico ou UMA loja: curva "
            "ABC própria, mix completo por marca/fornecedor (não só o top N), concentração, "
            "consistência mês a mês, e comparativos de produtos mês a mês (MoM) e ano a ano "
            "(YoY) em %."
        )
        modo_mix = st.radio(
            "Analisar por", ["Vendedor", "Loja"], horizontal=True, key="modo_mix_produtos"
        )

        vendedor_id_mix = None
        loja_escopo_mix = None
        rotulo_escopo_mix = None
        if modo_mix == "Vendedor":
            vendedores_ativos_mix = db.get_vendedores(loja=loja_filtro, apenas_ativos=True)
            if vendedores_ativos_mix.empty:
                st.info("Nenhum vendedor ativo no filtro selecionado.")
            else:
                opcoes_mix_vend = {
                    f"{r['nome']} ({r['loja']})": int(r["id"]) for _, r in vendedores_ativos_mix.iterrows()
                }
                escolha_mix_vend = st.selectbox(
                    "Vendedor", list(opcoes_mix_vend.keys()), key="sel_mix_vendedor"
                )
                vendedor_id_mix = opcoes_mix_vend[escolha_mix_vend]
                rotulo_escopo_mix = escolha_mix_vend
        else:
            loja_escopo_mix = st.selectbox("Loja", db.LOJAS, key="sel_mix_loja")
            rotulo_escopo_mix = loja_escopo_mix

        if vendedor_id_mix or loja_escopo_mix:
            resumo_mix = db.get_resumo_produtos_mes(
                ano_filtro, mes_filtro, loja=loja_escopo_mix, vendedor_id=vendedor_id_mix
            )
            if resumo_mix["n_produtos"] == 0:
                st.info(f"Nenhuma venda por produto lançada para {rotulo_escopo_mix} no mês selecionado.")
            else:
                concentracao_mix = db.get_concentracao_portfolio(
                    ano_filtro, mes_filtro, loja=loja_escopo_mix, vendedor_id=vendedor_id_mix
                )
                serie_mix = db.get_serie_mensal_produtos(
                    loja=loja_escopo_mix, vendedor_id=vendedor_id_mix, meses=6
                )
                cv_mix = None
                if len(serie_mix) >= 3:
                    media_serie_mix = float(serie_mix["valor_total"].mean())
                    desvio_serie_mix = float(serie_mix["valor_total"].std(ddof=0))
                    cv_mix = (desvio_serie_mix / media_serie_mix * 100) if media_serie_mix > 0 else None

                m1, m2, m3, m4, m5 = st.columns(5)
                kpi_card(m1, "Faturamento em Produtos", db.formatar_moeda(resumo_mix["faturamento_total"]), cor=VERDE)
                kpi_card(m2, "SKUs Distintos", str(resumo_mix["n_produtos"]))
                kpi_card(
                    m3, "Concentração Top 5",
                    f"{concentracao_mix['top5_pct']:.1f}%" if concentracao_mix["top5_pct"] is not None else "—",
                )
                kpi_card(
                    m4, "Concentração Top 10",
                    f"{concentracao_mix['top10_pct']:.1f}%" if concentracao_mix["top10_pct"] is not None else "—",
                )
                kpi_card(
                    m5, "Consistência (CV, 6m)",
                    f"{cv_mix:.0f}%" if cv_mix is not None else "—",
                )
                st.caption(
                    "Consistência (CV) = coeficiente de variação do faturamento mensal em produtos "
                    "nos últimos 6 meses (desvio padrão ÷ média) — quanto MENOR, mais regular o "
                    "volume mês a mês; valores altos indicam meses de pico intercalados com meses fracos."
                )

                st.write(f"**Curva ABC — {rotulo_escopo_mix}**")
                curva_abc_mix = db.get_curva_abc_produtos(
                    ano_filtro, mes_filtro, loja=loja_escopo_mix, vendedor_id=vendedor_id_mix
                )
                if curva_abc_mix.empty:
                    st.caption("Sem dado suficiente.")
                else:
                    contagem_classe_mix = curva_abc_mix["classe"].value_counts()
                    valor_classe_mix = curva_abc_mix.groupby("classe")["valor_total"].sum()
                    cma1, cma2, cma3 = st.columns(3)
                    for col_classe, classe, cor_classe in [(cma1, "A", VERDE), (cma2, "B", "#f39c12"), (cma3, "C", "#888888")]:
                        n_itens_mix = int(contagem_classe_mix.get(classe, 0))
                        valor_itens_mix = float(valor_classe_mix.get(classe, 0.0))
                        kpi_card(
                            col_classe, f"Classe {classe}",
                            f"{n_itens_mix} produto(s) — {db.formatar_moeda(valor_itens_mix)}",
                            cor=cor_classe,
                        )
                    with st.expander(f"Ver Curva ABC completa — {rotulo_escopo_mix}", expanded=False):
                        abc_mix_fmt = curva_abc_mix.copy()
                        abc_mix_fmt["Qtd"] = abc_mix_fmt["qtd_total"].apply(_fmt_qtd)
                        abc_mix_fmt["Valor Total"] = abc_mix_fmt["valor_total"].apply(db.formatar_moeda)
                        abc_mix_fmt["Participação"] = abc_mix_fmt["pct_participacao"].apply(lambda v: f"{v:.2f}%")
                        abc_mix_fmt["Acumulado"] = abc_mix_fmt["pct_acumulado"].apply(lambda v: f"{v:.2f}%")
                        st.dataframe(
                            abc_mix_fmt[[
                                "cod_produto", "descricao_produto", "marca", "fornecedor",
                                "Qtd", "Valor Total", "Participação", "Acumulado", "classe",
                            ]].rename(columns={
                                "cod_produto": "Cód.", "descricao_produto": "Produto",
                                "marca": "Marca", "fornecedor": "Fornecedor", "classe": "Classe",
                            }),
                            use_container_width=True, hide_index=True,
                        )

                st.write(f"**Mix completo por Fornecedor e Marca — {rotulo_escopo_mix}**")
                mcol1, mcol2 = st.columns(2)
                with mcol1:
                    mix_forn = db.get_mix_marca_fornecedor(
                        ano_filtro, mes_filtro, loja=loja_escopo_mix, vendedor_id=vendedor_id_mix, agrupar_por="fornecedor"
                    )
                    if mix_forn.empty:
                        st.caption("Sem dado de fornecedor.")
                    else:
                        fig_mix_forn = go.Figure(go.Bar(
                            x=mix_forn["pct_participacao"], y=mix_forn["grupo"], orientation="h", marker_color=AZUL_CLARO,
                        ))
                        fig_mix_forn.update_layout(
                            margin=dict(l=10, r=10, t=20, b=10), height=max(250, 24 * len(mix_forn)),
                            xaxis=dict(title="% do faturamento"), yaxis=dict(autorange="reversed"),
                        )
                        st.plotly_chart(fig_mix_forn, use_container_width=True)
                        with st.expander("Ver tabela completa — Fornecedores", expanded=False):
                            mix_forn_fmt = mix_forn.copy()
                            mix_forn_fmt["Valor Total"] = mix_forn_fmt["valor_total"].apply(db.formatar_moeda)
                            mix_forn_fmt["Participação"] = mix_forn_fmt["pct_participacao"].apply(lambda v: f"{v:.2f}%")
                            st.dataframe(
                                mix_forn_fmt[["grupo", "Valor Total", "Participação"]].rename(columns={"grupo": "Fornecedor"}),
                                use_container_width=True, hide_index=True,
                            )
                with mcol2:
                    mix_marca = db.get_mix_marca_fornecedor(
                        ano_filtro, mes_filtro, loja=loja_escopo_mix, vendedor_id=vendedor_id_mix, agrupar_por="marca"
                    )
                    if mix_marca.empty:
                        st.caption("Sem dado de marca.")
                    else:
                        fig_mix_marca = go.Figure(go.Bar(
                            x=mix_marca["pct_participacao"], y=mix_marca["grupo"], orientation="h", marker_color=VERDE,
                        ))
                        fig_mix_marca.update_layout(
                            margin=dict(l=10, r=10, t=20, b=10), height=max(250, 24 * len(mix_marca)),
                            xaxis=dict(title="% do faturamento"), yaxis=dict(autorange="reversed"),
                        )
                        st.plotly_chart(fig_mix_marca, use_container_width=True)
                        with st.expander("Ver tabela completa — Marcas", expanded=False):
                            mix_marca_fmt = mix_marca.copy()
                            mix_marca_fmt["Valor Total"] = mix_marca_fmt["valor_total"].apply(db.formatar_moeda)
                            mix_marca_fmt["Participação"] = mix_marca_fmt["pct_participacao"].apply(lambda v: f"{v:.2f}%")
                            st.dataframe(
                                mix_marca_fmt[["grupo", "Valor Total", "Participação"]].rename(columns={"grupo": "Marca"}),
                                use_container_width=True, hide_index=True,
                            )

                st.write(f"**Comparativo de produtos — mês a mês (MoM) e ano a ano (YoY) — {rotulo_escopo_mix}**")
                comp_mix = db.get_comparativo_produtos(
                    ano_filtro, mes_filtro, loja=loja_escopo_mix, vendedor_id=vendedor_id_mix, top_n=20
                )
                if comp_mix.empty:
                    st.caption("Sem dado suficiente pra montar o comparativo.")
                else:
                    comp_mix_fmt = comp_mix.copy()
                    comp_mix_fmt["Qtd"] = comp_mix_fmt["qtd_total"].apply(_fmt_qtd)
                    comp_mix_fmt["Valor Total"] = comp_mix_fmt["valor_total"].apply(db.formatar_moeda)
                    comp_mix_fmt["MoM"] = comp_mix_fmt["crescimento_mom_pct"].apply(_fmt_pct_sinal)
                    comp_mix_fmt["YoY"] = comp_mix_fmt["crescimento_yoy_pct"].apply(_fmt_pct_sinal)
                    st.dataframe(
                        comp_mix_fmt[[
                            "cod_produto", "descricao_produto", "marca", "fornecedor", "Qtd", "Valor Total", "MoM", "YoY",
                        ]].rename(columns={
                            "cod_produto": "Cód.", "descricao_produto": "Produto",
                            "marca": "Marca", "fornecedor": "Fornecedor",
                        }),
                        use_container_width=True, hide_index=True,
                    )
                    st.caption(
                        "MoM = variação vs o mês anterior. YoY = variação vs o mesmo mês do ano "
                        "anterior. Sem % quando o produto não vendeu no período de comparação "
                        "(nada pra comparar)."
                    )

                if vendedor_id_mix:
                    st.write(f"**💡 Diagnóstico Comercial — Indicativos de Desenvolvimento ({rotulo_escopo_mix})**")
                    st.caption(
                        "Comparação automática com os colegas ativos do filtro atual (mesma loja "
                        "selecionada no topo do painel) — concentração de portfólio vs. a média do "
                        "grupo, e os produtos/fornecedores que os colegas vendem bem e este vendedor "
                        "vende pouco ou nada (oportunidade = média dos colegas − o que ele já vende)."
                    )
                    diagnostico_mix = db.gerar_diagnostico_comercial_vendedor(
                        vendedor_id_mix, ano_filtro, mes_filtro, loja=loja_filtro
                    )
                    for rec in diagnostico_mix:
                        st.info(rec)

                    oport_produto_mix = db.get_oportunidades_foco_vendedor(
                        vendedor_id_mix, ano_filtro, mes_filtro, loja=loja_filtro,
                        agrupar_por="produto", top_n=10,
                    )
                    oport_forn_mix = db.get_oportunidades_foco_vendedor(
                        vendedor_id_mix, ano_filtro, mes_filtro, loja=loja_filtro,
                        agrupar_por="fornecedor", top_n=10,
                    )
                    ocol1, ocol2 = st.columns(2)
                    with ocol1:
                        st.write("**Produtos com maior oportunidade**")
                        if oport_produto_mix.empty:
                            st.caption("Sem oportunidade relevante identificada (precisa de pelo menos 2 colegas vendendo o item).")
                        else:
                            oport_prod_fmt = oport_produto_mix.copy()
                            oport_prod_fmt["Você"] = oport_prod_fmt["valor_dele"].apply(db.formatar_moeda)
                            oport_prod_fmt["Média Colegas"] = oport_prod_fmt["media_colegas"].apply(db.formatar_moeda)
                            oport_prod_fmt["Oportunidade"] = oport_prod_fmt["oportunidade"].apply(db.formatar_moeda)
                            st.dataframe(
                                oport_prod_fmt[["descricao_produto", "Você", "Média Colegas", "Oportunidade"]].rename(
                                    columns={"descricao_produto": "Produto"}
                                ),
                                use_container_width=True, hide_index=True,
                            )
                    with ocol2:
                        st.write("**Fornecedores com maior oportunidade**")
                        if oport_forn_mix.empty:
                            st.caption("Sem oportunidade relevante identificada (precisa de pelo menos 2 colegas vendendo do fornecedor).")
                        else:
                            oport_forn_fmt = oport_forn_mix.copy()
                            oport_forn_fmt["Você"] = oport_forn_fmt["valor_dele"].apply(db.formatar_moeda)
                            oport_forn_fmt["Média Colegas"] = oport_forn_fmt["media_colegas"].apply(db.formatar_moeda)
                            oport_forn_fmt["Oportunidade"] = oport_forn_fmt["oportunidade"].apply(db.formatar_moeda)
                            st.dataframe(
                                oport_forn_fmt[["grupo", "Você", "Média Colegas", "Oportunidade"]].rename(
                                    columns={"grupo": "Fornecedor"}
                                ),
                                use_container_width=True, hide_index=True,
                            )
                    st.caption(
                        "Oportunidade = média de faturamento dos colegas nesse item − o que este "
                        "vendedor já vende dele. Só considera itens vendidos por pelo menos 2 outros "
                        "vendedores (evita recomendação baseada no resultado de uma pessoa só)."
                    )

                st.write("**Exportar esse recorte em PDF**")
                pdf_mix_bytes = pdf_export.gerar_pdf_mix_produtos(
                    ano_filtro, mes_filtro, loja=loja_escopo_mix, vendedor_id=vendedor_id_mix,
                    rotulo_escopo=rotulo_escopo_mix,
                )
                nome_pdf_mix = (
                    f"Mix_Produtos_{rotulo_escopo_mix.replace(' ', '_').replace('(', '').replace(')', '')}"
                    f"_{db.MESES_PT[mes_filtro]}_{ano_filtro}.pdf"
                )
                st.download_button(
                    "⬇️ Baixar PDF do mix de produtos", data=pdf_mix_bytes, file_name=nome_pdf_mix,
                    mime="application/pdf", key="btn_pdf_mix_produtos",
                )

    st.markdown("---")

    # ---- Exportação em PDF ----
    st.markdown("### 📄 Exportar indicadores em PDF")
    st.caption(
        "Gere um relatório em PDF com os indicadores individuais de um vendedor, ou baixe "
        "um .zip com o PDF de todos os vendedores do filtro atual."
    )

    if metas_df.empty:
        st.info("Nenhum vendedor ativo para exportar no filtro selecionado.")
    else:
        col_pdf1, col_pdf2 = st.columns(2)

        with col_pdf1:
            opcoes_pdf = {
                f"{row['nome']} ({row['loja']})": row for _, row in metas_df.iterrows()
            }
            escolha_pdf = st.selectbox("Vendedor", list(opcoes_pdf.keys()), key="sel_pdf")
            linha_sel = opcoes_pdf[escolha_pdf]
            pdf_bytes = pdf_export.gerar_pdf_vendedor(
                linha_sel["vendedor_id"], linha_sel["nome"], linha_sel["loja"],
                ano_filtro, mes_filtro, dias_uteis_total,
            )
            nome_arquivo_pdf = f"{linha_sel['nome'].replace(' ', '_')}_{db.MESES_PT[mes_filtro]}_{ano_filtro}.pdf"
            st.download_button(
                "⬇️ Baixar PDF individual", data=pdf_bytes, file_name=nome_arquivo_pdf,
                mime="application/pdf",
            )

        with col_pdf2:
            st.write("")
            st.write("")
            zip_bytes = pdf_export.gerar_zip_vendedores(metas_df, ano_filtro, mes_filtro, dias_uteis_total)
            nome_zip = f"Relatorios_{loja_filtro.replace(' ', '_')}_{db.MESES_PT[mes_filtro]}_{ano_filtro}.zip"
            st.download_button(
                "⬇️ Baixar PDFs de todos os vendedores (.zip)", data=zip_bytes, file_name=nome_zip,
                mime="application/zip",
            )