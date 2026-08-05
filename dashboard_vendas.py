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
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import db
import pdf_export

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
                nova_loja = st.selectbox(
                    "Loja *", db.LOJAS, index=db.LOJAS.index(vendedor_atual["loja"])
                )
                novo_ativo = st.checkbox("Ativo", value=bool(vendedor_atual["ativo"]))
                col_salvar, col_excluir = st.columns(2)
                salvar = col_salvar.form_submit_button("💾 Salvar alterações")
                excluir = col_excluir.form_submit_button("🗑️ Excluir vendedor")

                if salvar:
                    if not novo_nome.strip():
                        st.error("O nome completo é obrigatório.")
                    else:
                        db.update_vendedor(vendedor_id, novo_nome.strip(), nova_loja, novo_ativo)
                        st.success("Vendedor atualizado com sucesso!")
                        st.session_state.versao_dados += 1
                        st.rerun()

                if excluir:
                    db.delete_vendedor(vendedor_id)
                    st.warning(f"Vendedor '{vendedor_atual['nome']}' excluído (metas e vendas associadas também foram removidas).")
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
    st.subheader("Histórico consolidado: Meta x Realizado")
    st.caption(
        "Um bloco por mês, com o realizado somado a partir dos lançamentos diários de cada "
        "vendedor, e o total do mês ao final de cada bloco."
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
            lambda r: (r["realizado"] / r["clientes"]) if r["clientes"] > 0 else 0.0, axis=1
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
                    ["nome", "loja", "Meta", "Realizado", "Atingimento (%)", "clientes", "Ticket Médio"]
                ].rename(columns={"nome": "Vendedor", "loja": "Loja", "clientes": "Clientes Atendidos"}),
                use_container_width=True,
                hide_index=True,
            )

            meta_total_mes = float(bloco["valor_meta"].sum())
            realizado_total_mes = float(bloco["realizado"].sum())
            clientes_total_mes = int(bloco["clientes"].sum())
            atingimento_total_mes = (
                (realizado_total_mes / meta_total_mes * 100) if meta_total_mes > 0 else 0.0
            )
            ticket_total_mes = (
                (realizado_total_mes / clientes_total_mes) if clientes_total_mes > 0 else 0.0
            )

            st.markdown(
                f"**Total do mês:** Meta {db.formatar_moeda(meta_total_mes)} · "
                f"Realizado {db.formatar_moeda(realizado_total_mes)} · "
                f"Atingimento {atingimento_total_mes:.1f}% · "
                f"Clientes atendidos {clientes_total_mes} · "
                f"Ticket médio {db.formatar_moeda(ticket_total_mes)}"
            )
            st.markdown("---")

# ==========================================================================
# ABA 3 - LANÇAMENTOS DIÁRIOS
# ==========================================================================
with tab_lancamentos:
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
                qtd_clientes = st.number_input(
                    "Clientes atendidos *", min_value=0, step=1, format="%d"
                )

            enviado_venda = st.form_submit_button("💾 Salvar lançamento")
            if enviado_venda:
                vendedor_id = opcoes_vend[escolha_vend_v]
                db.upsert_venda(vendedor_id, data_venda, float(valor_realizado), int(qtd_clientes))
                st.success(
                    f"Lançamento salvo: {escolha_vend_v} — {data_venda.strftime('%d/%m/%Y')} — "
                    f"{db.formatar_moeda(valor_realizado)} — {qtd_clientes} cliente(s)."
                )
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
            recentes_fmt[["id", "nome", "loja", "data", "Realizado (R$)", "qtd_clientes"]].rename(
                columns={"id": "ID", "nome": "Vendedor", "loja": "Loja", "data": "Data", "qtd_clientes": "Clientes"}
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
        dias_uteis_total = st.number_input(
            "Dias úteis no mês (confirme feriados)", min_value=1, max_value=31,
            value=db.DIAS_UTEIS_PADRAO, step=1, key="dias_uteis_dash",
        )

    ano_filtro = int(ano_filtro)
    mes_filtro = int(mes_filtro)
    dias_uteis_total = int(dias_uteis_total)

    metas_df = db.get_metas_mes(ano_filtro, mes_filtro, loja=loja_filtro)
    vendas_df = db.get_vendas_mes(ano_filtro, mes_filtro, loja=loja_filtro)

    # Alerta de metas não lançadas
    sem_meta = metas_df[metas_df["valor_meta"] == 0]
    if not sem_meta.empty:
        nomes_sem_meta = ", ".join(sem_meta["nome"].tolist())
        st.warning(f"⚠️ Meta não lançada para: {nomes_sem_meta} (considerando Meta = R$ 0,00).")

    # ---- KPIs ----
    meta_total = float(metas_df["valor_meta"].sum()) if not metas_df.empty else 0.0
    realizado_total = float(vendas_df["valor_realizado"].sum()) if not vendas_df.empty else 0.0
    clientes_total = int(vendas_df["qtd_clientes"].sum()) if not vendas_df.empty else 0

    atingimento_pct = (realizado_total / meta_total * 100) if meta_total > 0 else 0.0
    dias_transcorridos = db.dias_uteis_transcorridos(ano_filtro, mes_filtro, dias_uteis_total)
    run_rate_diario = (realizado_total / dias_transcorridos) if dias_transcorridos > 0 else 0.0
    projecao_fechamento = run_rate_diario * dias_uteis_total
    ticket_medio = (realizado_total / clientes_total) if clientes_total > 0 else 0.0
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
        f"Clientes atendidos no mês: {clientes_total}"
    )

    st.markdown("---")

    # ---- Comparativo mês a mês e ano a ano ----
    st.markdown("### 🔄 Comparativo Mês a Mês e Ano a Ano")

    ano_ant, mes_ant = db.mes_anterior(ano_filtro, mes_filtro)
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
    if vendas_df.empty:
        vendas_agg = pd.DataFrame(columns=["vendedor_id", "realizado", "clientes"])
    else:
        vendas_agg = (
            vendas_df.groupby("vendedor_id")
            .agg(realizado=("valor_realizado", "sum"), clientes=("qtd_clientes", "sum"))
            .reset_index()
        )

    ranking = metas_df.merge(vendas_agg, on="vendedor_id", how="left")
    ranking["realizado"] = ranking["realizado"].fillna(0.0)
    ranking["clientes"] = ranking["clientes"].fillna(0).astype(int)
    ranking["atingimento_pct"] = ranking.apply(
        lambda r: (r["realizado"] / r["valor_meta"] * 100) if r["valor_meta"] > 0 else 0.0, axis=1
    )
    ranking["ticket_medio_ind"] = ranking.apply(
        lambda r: (r["realizado"] / r["clientes"]) if r["clientes"] > 0 else 0.0, axis=1
    )
    ranking = ranking.sort_values("realizado", ascending=False)

    if ranking.empty:
        st.info("Nenhum vendedor ativo para o filtro selecionado.")
    else:
        ranking_fmt = ranking.copy()
        ranking_fmt["Meta"] = ranking_fmt["valor_meta"].apply(db.formatar_moeda)
        ranking_fmt["Realizado"] = ranking_fmt["realizado"].apply(db.formatar_moeda)
        ranking_fmt["Atingimento (%)"] = ranking_fmt["atingimento_pct"].apply(lambda v: f"{v:.1f}%")
        ranking_fmt["Ticket Médio"] = ranking_fmt["ticket_medio_ind"].apply(db.formatar_moeda)
        st.dataframe(
            ranking_fmt[
                ["nome", "loja", "Meta", "Realizado", "Atingimento (%)", "clientes", "Ticket Médio"]
            ].rename(columns={"nome": "Nome", "loja": "Loja", "clientes": "Clientes Atendidos"}),
            use_container_width=True,
            hide_index=True,
        )

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
        metas_loja = db.get_metas_mes(ano_filtro, mes_filtro, loja=loja_nome)
        vendas_loja = db.get_vendas_mes(ano_filtro, mes_filtro, loja=loja_nome)
        meta_l = float(metas_loja["valor_meta"].sum()) if not metas_loja.empty else 0.0
        realizado_l = float(vendas_loja["valor_realizado"].sum()) if not vendas_loja.empty else 0.0
        clientes_l = int(vendas_loja["qtd_clientes"].sum()) if not vendas_loja.empty else 0
        atingimento_l = (realizado_l / meta_l * 100) if meta_l > 0 else 0.0
        ticket_l = (realizado_l / clientes_l) if clientes_l > 0 else 0.0
        comparativo.append(
            {"loja": loja_nome, "atingimento": atingimento_l, "ticket_medio": ticket_l}
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