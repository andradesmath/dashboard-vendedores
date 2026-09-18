"""
pdf_export.py - Geração de relatórios em PDF com os indicadores de cada vendedor.

Usa reportlab (biblioteca 100% Python, sem dependência de binários externos como
Chromium/wkhtmltopdf), o que garante compatibilidade com o deploy no Streamlit
Community Cloud sem passos extras de instalação de sistema.

Cada PDF traz: dados do vendedor, KPIs do mês (meta, realizado, atingimento,
pedidos, ticket médio individual) e um gráfico de barras com o
realizado diário no período.
"""
import io
import re
from datetime import datetime

import numpy as np
import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable,
)
from reportlab.graphics.shapes import Drawing
from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.charts.piecharts import Pie
from reportlab.graphics.charts.legends import Legend

import db

AZUL = colors.HexColor("#1a5276")
VERDE = colors.HexColor("#1e8449")
CINZA = colors.HexColor("#555555")


def _md_para_reportlab(texto):
    """Converte negrito estilo Markdown (**texto**) pro markup que o Paragraph do
    reportlab entende (<b>texto</b>) — as recomendações em texto são escritas com
    **negrito** pensando na renderização em Markdown do Streamlit, e reaproveitadas
    aqui nos PDFs."""
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", texto)


def _calcular_kpis_vendedor(vendedor_id, ano, mes, dias_uteis_total=None):
    dias_uteis_total = dias_uteis_total or db.DIAS_UTEIS_PADRAO
    meta = db.get_meta_vendedor(vendedor_id, ano, mes)
    vendas = db.get_vendas_vendedor_mes(vendedor_id, ano, mes)
    realizado = float(vendas["valor_realizado"].sum()) if not vendas.empty else 0.0
    realizado += db.get_realizado_manual_vendedor(vendedor_id, ano, mes)
    pedidos = int(vendas["qtd_pedidos"].sum()) if not vendas.empty else 0
    pedidos += db.get_pedidos_manual_vendedor(vendedor_id, ano, mes)
    atingimento = (realizado / meta * 100) if meta > 0 else 0.0
    ticket = (realizado / pedidos) if pedidos > 0 else 0.0

    # Meta diária = meta do mês ÷ dias úteis do mês; média diária realizada considera
    # apenas os lançamentos diários (não inclui realizado importado como total mensal,
    # que não tem granularidade de dia).
    meta_diaria = (meta / dias_uteis_total) if dias_uteis_total > 0 else 0.0
    media_diaria_realizada = float(vendas["valor_realizado"].mean()) if not vendas.empty else 0.0
    atingimento_diario = (media_diaria_realizada / meta_diaria * 100) if meta_diaria > 0 else 0.0
    variacao_diaria_pct = atingimento_diario - 100.0

    # Mix de pagamento: % do mês atual, valor realizado por modalidade no mês e média
    # histórica ponderada (todo o histórico já lançado) por modalidade.
    mix_mes_valores, mix_mes_total = db.get_mix_pagamento_vendedor_mes(vendedor_id, ano, mes)
    mix_hist_pct, mix_hist_total = db.get_mix_pagamento_historico_vendedor(vendedor_id)
    mix_pagamento = []
    for modalidade in db.MODALIDADES_PAGAMENTO:
        valor_mes = mix_mes_valores.get(modalidade, 0.0)
        pct_mes = (valor_mes / mix_mes_total * 100) if mix_mes_total > 0 else 0.0
        mix_pagamento.append({
            "modalidade": modalidade,
            "pct_mes": pct_mes,
            "valor_mes": valor_mes,
            "media_historica_pct": mix_hist_pct.get(modalidade, 0.0),
        })

    # Risco de Nota Promissória: abordagem mensal considerando sempre o mês ANTERIOR
    # ao do relatório — o mix de pagamento de um mês só é lançado depois que ele
    # fecha, então usar o mês do próprio relatório aqui normalmente viria vazio.
    ano_np, mes_np = db.mes_anterior(ano, mes)
    mix_np_valores, mix_np_total = db.get_mix_pagamento_vendedor_mes(vendedor_id, ano_np, mes_np)
    pct_np_mes_anterior = (
        (mix_np_valores.get("Nota Promissória", 0.0) / mix_np_total * 100) if mix_np_total > 0 else None
    )

    return {
        "meta": meta,
        "realizado": realizado,
        "pedidos": pedidos,
        "atingimento": atingimento,
        "ticket": ticket,
        "vendas": vendas,
        "meta_diaria": meta_diaria,
        "media_diaria_realizada": media_diaria_realizada,
        "atingimento_diario": atingimento_diario,
        "variacao_diaria_pct": variacao_diaria_pct,
        "mix_pagamento": mix_pagamento,
        "mix_mes_total": mix_mes_total,
        "mix_hist_total": mix_hist_total,
        "ano_np": ano_np,
        "mes_np": mes_np,
        "pct_np_mes": pct_np_mes_anterior,
        "pct_np_historico": next(
            (l["media_historica_pct"] for l in mix_pagamento if l["modalidade"] == "Nota Promissória"), 0.0
        ) if mix_hist_total > 0 else None,
        **_kpis_inadimplencia(vendedor_id, ano, mes, mix_pagamento, mix_mes_total, mix_hist_total),
    }


def _kpis_inadimplencia(vendedor_id, ano, mes, mix_pagamento, mix_mes_total, mix_hist_total):
    """Risco de inadimplência das vendas a prazo (Nota Promissória), ponderando o
    ACUMULADO histórico já vendido a prazo contra o que ficou em atraso nos meses
    subsequentes — não só o mês específico do relatório (que costuma vir zerado
    quando o vendedor não vendeu a prazo naquele mês em particular)."""
    pct_np_hist = next(
        (l["media_historica_pct"] for l in mix_pagamento if l["modalidade"] == "Nota Promissória"), 0.0
    )
    # Total histórico vendido a prazo: TODO o mix já lançado (diário + manual), não só
    # os meses em que a inadimplência foi apurada — é a exposição acumulada real.
    total_vendido_prazo_historico = (pct_np_hist / 100 * mix_hist_total) if mix_hist_total > 0 else 0.0

    # Contexto do mês específico do relatório (pode vir zerado — normal se o vendedor
    # não vendeu a prazo naquele mês em particular).
    valor_a_prazo_mes = (
        next((l["valor_mes"] for l in mix_pagamento if l["modalidade"] == "Nota Promissória"), 0.0)
        if mix_mes_total > 0 else 0.0
    )
    valor_em_aberto_mes = db.get_inadimplencia_vendedor(vendedor_id, ano, mes)
    indice_mes_pct = (
        (valor_em_aberto_mes / valor_a_prazo_mes * 100)
        if (valor_em_aberto_mes is not None and valor_a_prazo_mes > 0) else None
    )

    resumo = db.get_indice_inadimplencia_resumo_vendedor(vendedor_id)
    hist = resumo["historico"]
    hist_avaliada = hist[hist["valor_a_prazo"] > 0] if not hist.empty else hist

    total_avaliado = float(hist_avaliada["valor_a_prazo"].sum()) if not hist_avaliada.empty else 0.0
    total_aberto_acumulado = float(hist_avaliada["valor_em_aberto"].sum()) if not hist_avaliada.empty else 0.0
    cobertura_pct = (
        (total_avaliado / total_vendido_prazo_historico * 100)
        if total_vendido_prazo_historico > 0 else None
    )

    hist_com_indice = (
        hist_avaliada[hist_avaliada["indice_pct"].notna()] if not hist_avaliada.empty else hist_avaliada
    )
    n_meses = len(hist_com_indice)

    slope = None
    if n_meses >= 3:
        xs = np.arange(n_meses, dtype=float)
        ys = hist_com_indice["indice_pct"].to_numpy(dtype=float)
        slope = float(np.polyfit(xs, ys, 1)[0])

    # Margem de confiabilidade: dispersão (desvio padrão amostral) dos índices mensais
    # em torno da média — quanto maior a dispersão e menor o nº de meses, menos
    # confiável é usar a média histórica como previsão do risco atual.
    desvio_pct = float(hist_com_indice["indice_pct"].std(ddof=1)) if n_meses >= 2 else None
    if n_meses < 3:
        confiabilidade = "Baixa (poucos meses de dado)"
    elif n_meses < 6:
        confiabilidade = "Média"
    else:
        confiabilidade = "Alta"

    media_pct = resumo["media_ponderada_pct"]
    # Risco atual estimado: projeta a média histórica um passo à frente usando a
    # tendência (regressão linear) — mais realista que só repetir a média histórica
    # "congelada" quando o índice está claramente subindo ou caindo.
    risco_atual_estimado = None
    if media_pct is not None:
        risco_atual_estimado = max(0.0, media_pct + (slope if slope is not None else 0.0))

    valor_esperado_perda = (
        total_vendido_prazo_historico * media_pct / 100 if media_pct is not None else None
    )

    return {
        "inadimp_total_vendido_prazo_historico": total_vendido_prazo_historico,
        "inadimp_total_avaliado_prazo": total_avaliado,
        "inadimp_total_aberto_acumulado": total_aberto_acumulado,
        "inadimp_cobertura_pct": cobertura_pct,
        "inadimp_valor_a_prazo_mes": valor_a_prazo_mes,
        "inadimp_valor_em_aberto_mes": valor_em_aberto_mes,
        "inadimp_indice_mes_pct": indice_mes_pct,
        "inadimp_media_historica_pct": media_pct,
        "inadimp_n_meses": n_meses,
        "inadimp_slope": slope,
        "inadimp_desvio_pct": desvio_pct,
        "inadimp_confiabilidade": confiabilidade,
        "inadimp_risco_atual_estimado_pct": risco_atual_estimado,
        "inadimp_nivel_risco": resumo["nivel_risco"],
        "inadimp_nivel_risco_atual": db.nivel_risco_inadimplencia(risco_atual_estimado),
        "inadimp_valor_esperado_perda": valor_esperado_perda,
    }


def _grafico_vendas_diarias(vendas_df):
    drawing = Drawing(440, 190)
    chart = VerticalBarChart()
    chart.x = 35
    chart.y = 25
    chart.height = 135
    chart.width = 390
    valores = [float(v) for v in vendas_df["valor_realizado"].tolist()]
    dias = [str(d.day) for d in vendas_df["data"]]
    chart.data = [valores]
    chart.categoryAxis.categoryNames = dias
    chart.categoryAxis.labels.fontSize = 6
    chart.categoryAxis.labels.angle = 0
    chart.valueAxis.valueMin = 0
    chart.bars[0].fillColor = VERDE
    chart.barWidth = 4
    drawing.add(chart)
    return drawing


def gerar_pdf_vendedor(vendedor_id, nome, loja, ano, mes, dias_uteis_total=None):
    """Gera o PDF de indicadores de um único vendedor e retorna os bytes do arquivo."""
    dias_uteis_total = dias_uteis_total or db.DIAS_UTEIS_PADRAO
    kpis = _calcular_kpis_vendedor(vendedor_id, ano, mes, dias_uteis_total)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, topMargin=1.6 * cm, bottomMargin=1.6 * cm,
        leftMargin=1.8 * cm, rightMargin=1.8 * cm,
    )
    styles = getSampleStyleSheet()
    titulo_style = ParagraphStyle("titulo", parent=styles["Heading1"], textColor=AZUL, spaceAfter=2)
    sub_style = ParagraphStyle("sub", parent=styles["Normal"], textColor=CINZA)
    secao_style = ParagraphStyle("secao", parent=styles["Heading3"], textColor=AZUL, spaceBefore=10)

    elementos = []
    elementos.append(Paragraph("Relatório de Desempenho Individual", titulo_style))
    elementos.append(Paragraph(f"{nome} — {loja}", styles["Heading2"]))
    elementos.append(Paragraph(f"Período de referência: {db.MESES_PT[mes]}/{ano}", sub_style))
    elementos.append(Spacer(1, 0.4 * cm))
    elementos.append(HRFlowable(width="100%", color=AZUL, thickness=1.2))
    elementos.append(Spacer(1, 0.5 * cm))

    variacao = kpis["variacao_diaria_pct"]
    variacao_txt = f"{'+' if variacao >= 0 else ''}{variacao:.1f}% ({'acima' if variacao >= 0 else 'abaixo'} da meta diária)"

    dados_tabela = [
        ["Indicador", "Valor"],
        ["Meta do mês (global)", db.formatar_moeda(kpis["meta"])],
        ["Realizado do mês", db.formatar_moeda(kpis["realizado"])],
        ["Atingimento da meta global (%)", f"{kpis['atingimento']:.1f}%  ({db.label_semaforo(kpis['atingimento'])})"],
        ["Meta diária (meta ÷ dias úteis)", db.formatar_moeda(kpis["meta_diaria"])],
        ["Média diária realizada", db.formatar_moeda(kpis["media_diaria_realizada"])],
        ["Atingimento da meta diária (%)", f"{kpis['atingimento_diario']:.1f}%"],
        ["Variação vs. meta diária", variacao_txt],
        ["Pedidos", str(kpis["pedidos"])],
        ["Ticket médio individual", db.formatar_moeda(kpis["ticket"])],
    ]
    tabela = Table(dados_tabela, colWidths=[8.5 * cm, 7.5 * cm])
    tabela.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), AZUL),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.lightgrey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f6f7")]),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]))
    elementos.append(tabela)

    if kpis["mix_mes_total"] > 0 or kpis["mix_hist_total"] > 0:
        elementos.append(Paragraph("Mix de pagamento", secao_style))
        elementos.append(Spacer(1, 0.2 * cm))
        dados_mix = [["Tipo de Pagamento", "% do Mês", "Média Histórica (%)", "Realizado no Mês"]]
        for linha in kpis["mix_pagamento"]:
            dados_mix.append([
                linha["modalidade"],
                f"{linha['pct_mes']:.1f}%" if kpis["mix_mes_total"] > 0 else "—",
                f"{linha['media_historica_pct']:.1f}%" if kpis["mix_hist_total"] > 0 else "—",
                db.formatar_moeda(linha["valor_mes"]) if kpis["mix_mes_total"] > 0 else "—",
            ])
        tabela_mix = Table(dados_mix, colWidths=[5 * cm, 3.5 * cm, 4 * cm, 3.5 * cm])
        tabela_mix.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), AZUL),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.lightgrey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f6f7")]),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ]))
        elementos.append(tabela_mix)
        if kpis["mix_mes_total"] == 0:
            elementos.append(Spacer(1, 0.15 * cm))
            elementos.append(Paragraph(
                "Nenhum mix de pagamento lançado neste mês — mostrando só a média histórica.",
                sub_style,
            ))
        elif kpis["mix_hist_total"] == 0:
            elementos.append(Spacer(1, 0.15 * cm))
            elementos.append(Paragraph(
                "Ainda sem histórico suficiente para calcular a média histórica.",
                sub_style,
            ))
        elementos.append(Spacer(1, 0.15 * cm))
        elementos.append(Paragraph(
            "Metodologia: % do Mês e Realizado no Mês são calculados a partir do mix de "
            "pagamento lançado nos formulários diário/mensal, cruzado com o valor "
            "efetivamente vendido. Média Histórica (%) usa todo o histórico já lançado "
            "para o vendedor, ponderado por R$ (não é a média simples dos meses).",
            sub_style,
        ))
        elementos.append(Spacer(1, 0.3 * cm))

    if kpis["pct_np_mes"] is not None:
        nivel_risco = db.nivel_risco_nota_promissoria(kpis["pct_np_mes"])
        variacao_np_txt = ""
        if kpis["pct_np_historico"] is not None:
            diff_np = kpis["pct_np_mes"] - kpis["pct_np_historico"]
            if diff_np > 5:
                variacao_np_txt = f" — piorando {('+' if diff_np >= 0 else '')}{diff_np:.1f}pp vs. a média histórica."
            elif diff_np < -5:
                variacao_np_txt = f" — melhorando {diff_np:.1f}pp vs. a média histórica."
            else:
                variacao_np_txt = " — estável frente à média histórica."
        risco_estilo = ParagraphStyle(
            "risco", parent=styles["Normal"], textColor=colors.HexColor("#7a3b00"),
            backColor=colors.HexColor("#fff3e0"), borderPadding=6,
        )
        elementos.append(Paragraph(
            f"⚠️ Risco de Nota Promissória ({db.MESES_PT[kpis['mes_np']]}/{kpis['ano_np']}): "
            f"{kpis['pct_np_mes']:.1f}% do realizado do mês ({nivel_risco}){variacao_np_txt}",
            risco_estilo,
        ))
        elementos.append(Spacer(1, 0.15 * cm))
        elementos.append(Paragraph(
            "Metodologia: abordagem mensal, sempre referente ao mês ANTERIOR ao do "
            "relatório (o mix de pagamento só é lançado depois que o mês fecha). % do "
            "realizado desse mês vendido em Nota Promissória. Faixas: "
            f"🟢 Baixo (&lt; {db.RISCO_NP_LIMIAR_BAIXO:.0f}%), 🟡 Moderado "
            f"({db.RISCO_NP_LIMIAR_BAIXO:.0f}–{db.RISCO_NP_LIMIAR_MODERADO:.0f}%), "
            f"🔴 Alto (&gt; {db.RISCO_NP_LIMIAR_MODERADO:.0f}%).",
            sub_style,
        ))
        elementos.append(Spacer(1, 0.3 * cm))

    if kpis["inadimp_total_vendido_prazo_historico"] > 0 or kpis["inadimp_n_meses"] > 0:
        elementos.append(Paragraph("Risco de Inadimplência em Vendas a Prazo (Nota Promissória)", secao_style))
        elementos.append(Spacer(1, 0.2 * cm))

        tendencia_txt = "—"
        if kpis["inadimp_slope"] is not None:
            slope = kpis["inadimp_slope"]
            rotulo_slope = "piorando" if slope > 0.5 else ("melhorando" if slope < -0.5 else "estável")
            tendencia_txt = f"{'+' if slope >= 0 else ''}{slope:.2f} pp/mês ({rotulo_slope})"

        margem_txt = "— (dados insuficientes)"
        if kpis["inadimp_desvio_pct"] is not None:
            margem_txt = (
                f"± {kpis['inadimp_desvio_pct']:.1f} pp — confiabilidade {kpis['inadimp_confiabilidade']} "
                f"({kpis['inadimp_n_meses']} meses de dado)"
            )
        elif kpis["inadimp_n_meses"] > 0:
            margem_txt = f"— confiabilidade {kpis['inadimp_confiabilidade']} ({kpis['inadimp_n_meses']} mês de dado)"

        cobertura_txt = "—"
        if kpis["inadimp_cobertura_pct"] is not None:
            cobertura_txt = (
                f"{kpis['inadimp_cobertura_pct']:.0f}% do total histórico "
                f"({db.formatar_moeda(kpis['inadimp_total_avaliado_prazo'])})"
            )

        risco_atual_txt = "—"
        if kpis["inadimp_risco_atual_estimado_pct"] is not None:
            risco_atual_txt = (
                f"{kpis['inadimp_risco_atual_estimado_pct']:.1f}% ({kpis['inadimp_nivel_risco_atual']})"
            )

        dados_inadimp = [
            ["Indicador", "Valor"],
            [
                "Total vendido a prazo (soma de tudo)",
                db.formatar_moeda(kpis["inadimp_total_vendido_prazo_historico"]),
            ],
            ["Cobertura da análise", cobertura_txt],
            [
                "Total em aberto acumulado",
                db.formatar_moeda(kpis["inadimp_total_aberto_acumulado"]),
            ],
            [
                "Índice de inadimplência histórico",
                f"{kpis['inadimp_media_historica_pct']:.1f}%"
                if kpis["inadimp_media_historica_pct"] is not None else "—",
            ],
            ["Margem de confiabilidade", margem_txt],
            ["Tendência", tendencia_txt],
            ["Risco atual estimado", risco_atual_txt],
        ]
        tabela_inadimp = Table(dados_inadimp, colWidths=[8.5 * cm, 7.5 * cm])
        tabela_inadimp.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), AZUL),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.lightgrey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f6f7")]),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ]))
        elementos.append(tabela_inadimp)
        elementos.append(Spacer(1, 0.15 * cm))

        if kpis["inadimp_valor_a_prazo_mes"] > 0:
            aberto_mes_txt = (
                db.formatar_moeda(kpis["inadimp_valor_em_aberto_mes"])
                if kpis["inadimp_valor_em_aberto_mes"] is not None else "não lançado ainda"
            )
            elementos.append(Paragraph(
                f"No mês do relatório: vendeu {db.formatar_moeda(kpis['inadimp_valor_a_prazo_mes'])} a "
                f"prazo, em aberto: {aberto_mes_txt}"
                + (f" (índice do mês: {kpis['inadimp_indice_mes_pct']:.1f}%)"
                   if kpis["inadimp_indice_mes_pct"] is not None else ""),
                sub_style,
            ))
            elementos.append(Spacer(1, 0.15 * cm))

        elementos.append(Paragraph(
            "Metodologia: pondera o ACUMULADO de vendas a prazo (Nota Promissória) contra o "
            "que ficou em atraso nos meses subsequentes, não só o mês do relatório. Total "
            "vendido a prazo = soma de toda a Nota Promissória já lançada no mix de pagamento "
            "(histórico completo). Cobertura = quanto desse total já tem um resultado de "
            "cobrança conhecido (valor em aberto apurado a partir de 30 dias). Índice de "
            "inadimplência histórico = total em aberto ÷ total avaliado, ponderado por R$. "
            "Margem de confiabilidade = desvio padrão dos índices mensais — quanto maior a "
            "dispersão e menos meses de dado, menos confiável é usar a média como previsão. "
            "Tendência = inclinação da regressão linear do índice mensal (mín. 3 meses). "
            "Risco atual estimado = índice histórico ajustado pela tendência (projeção de um "
            "mês à frente), não a média 'congelada'. Referência de risco: "
            f"{db.INADIMPLENCIA_LIMIAR_ACEITAVEL:.0f}% é a taxa cobrada pela maquininha de "
            f"cartão — até esse índice (🟢 Aceitável) o atraso fica dentro do custo que o "
            f"negócio já absorve normalmente; acima disso (🔴 Muito alto) a inadimplência "
            "está custando mais do que a maquininha custaria.",
            sub_style,
        ))
        elementos.append(Spacer(1, 0.3 * cm))

    vendas = kpis["vendas"]
    if not vendas.empty:
        elementos.append(Paragraph("Vendas diárias no período", secao_style))
        elementos.append(Spacer(1, 0.2 * cm))
        elementos.append(_grafico_vendas_diarias(vendas))
    else:
        elementos.append(Spacer(1, 0.5 * cm))
        elementos.append(Paragraph("Nenhum lançamento de vendas registrado no período.", styles["Normal"]))

    try:
        diagnostico_comercial = db.gerar_diagnostico_comercial_vendedor(vendedor_id, ano, mes, loja=loja)
    except Exception:
        diagnostico_comercial = []

    if diagnostico_comercial:
        elementos.append(Spacer(1, 0.3 * cm))
        elementos.append(Paragraph("Diagnóstico Comercial — Indicativos de Desenvolvimento", secao_style))
        elementos.append(Spacer(1, 0.2 * cm))
        elementos.append(Paragraph(
            "Comparação automática com os colegas ativos da mesma loja: concentração de "
            "portfólio de produtos vs. a média do grupo, e os produtos/fornecedores que os "
            "colegas vendem bem e este vendedor vende pouco ou nada.",
            sub_style,
        ))
        elementos.append(Spacer(1, 0.15 * cm))
        for rec in diagnostico_comercial:
            elementos.append(Paragraph(f"• {_md_para_reportlab(rec)}", styles["Normal"]))
            elementos.append(Spacer(1, 0.1 * cm))

    elementos.append(Spacer(1, 0.8 * cm))
    elementos.append(HRFlowable(width="100%", color=colors.lightgrey, thickness=0.5))
    elementos.append(Spacer(1, 0.2 * cm))
    elementos.append(Paragraph(
        f"Relatório gerado automaticamente em {datetime.now().strftime('%d/%m/%Y às %H:%M')}.",
        sub_style,
    ))

    doc.build(elementos)
    buffer.seek(0)
    return buffer.getvalue()


def gerar_zip_vendedores(vendedores_df, ano, mes, dias_uteis_total=None):
    """Gera um .zip contendo o PDF individual de cada vendedor informado.
    vendedores_df precisa ter as colunas: vendedor_id (ou id), nome, loja."""
    import zipfile

    buffer_zip = io.BytesIO()
    with zipfile.ZipFile(buffer_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for _, row in vendedores_df.iterrows():
            vendedor_id = row["vendedor_id"] if "vendedor_id" in row else row["id"]
            pdf_bytes = gerar_pdf_vendedor(
                vendedor_id, row["nome"], row["loja"], ano, mes, dias_uteis_total
            )
            nome_arquivo = f"{row['nome'].replace(' ', '_')}_{db.MESES_PT[mes]}_{ano}.pdf"
            zf.writestr(nome_arquivo, pdf_bytes)
    buffer_zip.seek(0)
    return buffer_zip.getvalue()


# ---------------------------------------------------------------------------
# Relatório de Mix de Produtos (por vendedor ou por loja)
# ---------------------------------------------------------------------------

_PALETA_PIZZA = [
    colors.HexColor("#1a5276"), colors.HexColor("#2e86c1"), colors.HexColor("#1e8449"),
    colors.HexColor("#f39c12"), colors.HexColor("#c0392b"), colors.HexColor("#8e44ad"),
    colors.HexColor("#16a085"), colors.HexColor("#7f8c8d"),
]


def _grafico_pizza_mix(mix_df, top_n=7):
    """Gráfico de pizza com a composição de um mix (marca ou fornecedor),
    agrupando o restante como 'Outros' além do top_n, com legenda em %."""
    df = mix_df.sort_values("valor_total", ascending=False).reset_index(drop=True)
    if len(df) > top_n:
        principais = df.head(top_n)
        outros_valor = float(df.iloc[top_n:]["valor_total"].sum())
        rotulos = principais["grupo"].tolist() + ["Outros"]
        valores = [float(v) for v in principais["valor_total"].tolist()] + [outros_valor]
    else:
        rotulos = df["grupo"].tolist()
        valores = [float(v) for v in df["valor_total"].tolist()]

    drawing = Drawing(460, 160)
    pie = Pie()
    pie.x = 20
    pie.y = 10
    pie.width = 140
    pie.height = 140
    pie.data = valores
    pie.labels = None
    pie.sideLabels = False
    pie.slices.strokeWidth = 0.5
    pie.slices.strokeColor = colors.white
    for i in range(len(valores)):
        pie.slices[i].fillColor = _PALETA_PIZZA[i % len(_PALETA_PIZZA)]
    drawing.add(pie)

    total = sum(valores) or 1
    legend = Legend()
    legend.x = 190
    legend.y = 140
    legend.dx = 8
    legend.dy = 8
    legend.fontName = "Helvetica"
    legend.fontSize = 8
    legend.alignment = "left"
    legend.columnMaximum = 8
    legend.colorNamePairs = [
        (_PALETA_PIZZA[i % len(_PALETA_PIZZA)], f"{str(rotulos[i])[:26]} ({valores[i] / total * 100:.1f}%)")
        for i in range(len(valores))
    ]
    drawing.add(legend)
    return drawing


def _grafico_serie_mensal_produtos(serie_df):
    """Gráfico de barras com a série MENSAL de faturamento em produtos (últimos meses)."""
    drawing = Drawing(440, 170)
    chart = VerticalBarChart()
    chart.x = 40
    chart.y = 25
    chart.height = 120
    chart.width = 380
    valores = [float(v) for v in serie_df["valor_total"].tolist()]
    rotulos = [f"{int(r['mes']):02d}/{str(int(r['ano']))[2:]}" for _, r in serie_df.iterrows()]
    chart.data = [valores]
    chart.categoryAxis.categoryNames = rotulos
    chart.categoryAxis.labels.fontSize = 7
    chart.valueAxis.valueMin = 0
    chart.bars[0].fillColor = AZUL
    chart.barWidth = 10
    drawing.add(chart)
    return drawing


def _fmt_pct_pdf(v):
    if v is None or pd.isna(v):
        return "—"
    sinal = "+" if v >= 0 else ""
    return f"{sinal}{v:.1f}%"


def gerar_pdf_mix_produtos(ano, mes, loja=None, vendedor_id=None, rotulo_escopo=None):
    """Gera o PDF de Mix de Produtos — curva ABC, mix completo por marca/fornecedor
    (com gráficos de pizza), concentração, consistência, tendência de 6 meses e
    comparativo de produtos MoM/YoY — de UM vendedor específico ou de UMA loja.
    Retorna os bytes do PDF."""
    rotulo_escopo = rotulo_escopo or (loja or "Geral")

    resumo = db.get_resumo_produtos_mes(ano, mes, loja=loja, vendedor_id=vendedor_id)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, topMargin=1.6 * cm, bottomMargin=1.6 * cm,
        leftMargin=1.8 * cm, rightMargin=1.8 * cm,
    )
    styles = getSampleStyleSheet()
    titulo_style = ParagraphStyle("titulo", parent=styles["Heading1"], textColor=AZUL, spaceAfter=2)
    sub_style = ParagraphStyle("sub", parent=styles["Normal"], textColor=CINZA)
    secao_style = ParagraphStyle("secao", parent=styles["Heading3"], textColor=AZUL, spaceBefore=10)

    elementos = []
    elementos.append(Paragraph("Relatório de Mix de Produtos", titulo_style))
    elementos.append(Paragraph(str(rotulo_escopo), styles["Heading2"]))
    elementos.append(Paragraph(f"Período de referência: {db.MESES_PT[mes]}/{ano}", sub_style))
    elementos.append(Spacer(1, 0.4 * cm))
    elementos.append(HRFlowable(width="100%", color=AZUL, thickness=1.2))
    elementos.append(Spacer(1, 0.5 * cm))

    if resumo["n_produtos"] == 0:
        elementos.append(Paragraph(
            "Nenhuma venda por produto lançada para este recorte no período.", styles["Normal"]
        ))
        doc.build(elementos)
        buffer.seek(0)
        return buffer.getvalue()

    concentracao = db.get_concentracao_portfolio(ano, mes, loja=loja, vendedor_id=vendedor_id)
    curva_abc = db.get_curva_abc_produtos(ano, mes, loja=loja, vendedor_id=vendedor_id)
    mix_forn = db.get_mix_marca_fornecedor(ano, mes, loja=loja, vendedor_id=vendedor_id, agrupar_por="fornecedor")
    mix_marca = db.get_mix_marca_fornecedor(ano, mes, loja=loja, vendedor_id=vendedor_id, agrupar_por="marca")
    comparativo = db.get_comparativo_produtos(ano, mes, loja=loja, vendedor_id=vendedor_id, top_n=20)
    serie_mensal = db.get_serie_mensal_produtos(loja=loja, vendedor_id=vendedor_id, meses=6)

    cv_mix = None
    if len(serie_mensal) >= 3:
        media_serie = float(serie_mensal["valor_total"].mean())
        desvio_serie = float(serie_mensal["valor_total"].std(ddof=0))
        cv_mix = (desvio_serie / media_serie * 100) if media_serie > 0 else None

    dados_kpi = [
        ["Indicador", "Valor"],
        ["Faturamento em produtos", db.formatar_moeda(resumo["faturamento_total"])],
        ["Itens vendidos", f"{resumo['qtd_total']:,.0f}".replace(",", ".")],
        ["Ticket médio por item", db.formatar_moeda(resumo["ticket_medio_item"])],
        ["SKUs distintos", str(resumo["n_produtos"])],
        ["Fornecedores / marcas distintos", f"{resumo['n_fornecedores']} / {resumo['n_marcas']}"],
        ["Concentração Top 5 SKUs", f"{concentracao['top5_pct']:.1f}%" if concentracao["top5_pct"] is not None else "—"],
        ["Concentração Top 10 SKUs", f"{concentracao['top10_pct']:.1f}%" if concentracao["top10_pct"] is not None else "—"],
        ["Consistência mensal (CV, 6m)", f"{cv_mix:.0f}%" if cv_mix is not None else "—"],
    ]
    tabela_kpi = Table(dados_kpi, colWidths=[9 * cm, 7 * cm])
    tabela_kpi.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), AZUL),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.lightgrey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f6f7")]),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))
    elementos.append(tabela_kpi)
    elementos.append(Spacer(1, 0.15 * cm))
    elementos.append(Paragraph(
        "Consistência (CV) = coeficiente de variação do faturamento mensal em produtos dos "
        "últimos 6 meses (desvio padrão ÷ média). Quanto menor, mais regular o volume mês a mês.",
        sub_style,
    ))
    elementos.append(Spacer(1, 0.3 * cm))

    if len(serie_mensal) >= 2:
        elementos.append(Paragraph("Tendência — faturamento mensal (últimos 6 meses)", secao_style))
        elementos.append(Spacer(1, 0.2 * cm))
        elementos.append(_grafico_serie_mensal_produtos(serie_mensal))
        if len(serie_mensal) >= 3:
            xs = np.arange(len(serie_mensal))
            ys = serie_mensal["valor_total"].to_numpy(dtype=float)
            slope = np.polyfit(xs, ys, 1)[0]
            media_serie_txt = ys.mean()
            if media_serie_txt > 0 and abs(slope) / media_serie_txt > 0.05:
                tendencia_txt = "📈 em alta" if slope > 0 else "📉 em queda"
            else:
                tendencia_txt = "➡️ estável"
            elementos.append(Spacer(1, 0.15 * cm))
            elementos.append(Paragraph(f"Tendência da série: {tendencia_txt}.", sub_style))
        elementos.append(Spacer(1, 0.3 * cm))

    if not curva_abc.empty:
        elementos.append(Paragraph("Curva ABC", secao_style))
        elementos.append(Spacer(1, 0.2 * cm))
        contagem_classe = curva_abc["classe"].value_counts()
        valor_classe = curva_abc.groupby("classe")["valor_total"].sum()
        dados_abc = [["Classe", "Nº de Produtos", "Faturamento"]]
        for classe in ["A", "B", "C"]:
            dados_abc.append([
                classe, str(int(contagem_classe.get(classe, 0))),
                db.formatar_moeda(float(valor_classe.get(classe, 0.0))),
            ])
        tabela_abc = Table(dados_abc, colWidths=[3 * cm, 5 * cm, 8 * cm])
        tabela_abc.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), AZUL),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.lightgrey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f6f7")]),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ]))
        elementos.append(tabela_abc)
        elementos.append(Spacer(1, 0.15 * cm))
        elementos.append(Paragraph(
            "A = primeiros 80% do faturamento acumulado (prioridade de estoque/negociação). "
            "B = de 80% a 95%. C = últimos 5% (cauda longa, candidatos a revisão de mix).",
            sub_style,
        ))
        elementos.append(Spacer(1, 0.3 * cm))

        top_abc = curva_abc.head(15)
        dados_top_abc = [["Cód.", "Produto", "Classe", "Faturamento", "% Acum."]]
        for _, r in top_abc.iterrows():
            dados_top_abc.append([
                str(r["cod_produto"]), str(r["descricao_produto"])[:38], r["classe"],
                db.formatar_moeda(r["valor_total"]), f"{r['pct_acumulado']:.1f}%",
            ])
        tabela_top_abc = Table(dados_top_abc, colWidths=[2 * cm, 7 * cm, 1.8 * cm, 3 * cm, 2.2 * cm])
        tabela_top_abc.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), AZUL),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 7.5),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.lightgrey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f6f7")]),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ]))
        elementos.append(Paragraph("Top 15 produtos (curva ABC)", secao_style))
        elementos.append(Spacer(1, 0.15 * cm))
        elementos.append(tabela_top_abc)
        elementos.append(Spacer(1, 0.3 * cm))

    if not mix_forn.empty:
        elementos.append(Paragraph("Mix por Fornecedor", secao_style))
        elementos.append(Spacer(1, 0.2 * cm))
        elementos.append(_grafico_pizza_mix(mix_forn))
        elementos.append(Spacer(1, 0.3 * cm))

    if not mix_marca.empty:
        elementos.append(Paragraph("Mix por Marca", secao_style))
        elementos.append(Spacer(1, 0.2 * cm))
        elementos.append(_grafico_pizza_mix(mix_marca))
        elementos.append(Spacer(1, 0.3 * cm))

    if not comparativo.empty:
        elementos.append(Paragraph("Comparativo de produtos — MoM e YoY", secao_style))
        elementos.append(Spacer(1, 0.2 * cm))
        dados_comp = [["Cód.", "Produto", "Faturamento", "MoM", "YoY"]]
        for _, r in comparativo.iterrows():
            dados_comp.append([
                str(r["cod_produto"]), str(r["descricao_produto"])[:34],
                db.formatar_moeda(r["valor_total"]),
                _fmt_pct_pdf(r["crescimento_mom_pct"]), _fmt_pct_pdf(r["crescimento_yoy_pct"]),
            ])
        tabela_comp = Table(dados_comp, colWidths=[2 * cm, 6.5 * cm, 3 * cm, 2.25 * cm, 2.25 * cm])
        tabela_comp.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), AZUL),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 7.5),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.lightgrey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f6f7")]),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ]))
        elementos.append(tabela_comp)
        elementos.append(Spacer(1, 0.15 * cm))
        elementos.append(Paragraph(
            "MoM = variação vs o mês anterior. YoY = variação vs o mesmo mês do ano anterior. "
            "Sem % quando o produto não vendeu no período de comparação.",
            sub_style,
        ))

    elementos.append(Spacer(1, 0.8 * cm))
    elementos.append(HRFlowable(width="100%", color=colors.lightgrey, thickness=0.5))
    elementos.append(Spacer(1, 0.2 * cm))
    elementos.append(Paragraph(
        f"Relatório gerado automaticamente em {datetime.now().strftime('%d/%m/%Y às %H:%M')}.",
        sub_style,
    ))

    doc.build(elementos)
    buffer.seek(0)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Relatório Estratégico Comercial por Loja
# ---------------------------------------------------------------------------

def _tabela_padrao(dados, col_widths, fonte=9):
    """Monta uma Table com o mesmo estilo visual usado em todo o arquivo (cabeçalho
    azul, linhas zebradas) — evita repetir o TableStyle inteiro em cada seção nova."""
    tabela = Table(dados, colWidths=col_widths)
    tabela.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), AZUL),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), fonte),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.lightgrey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f6f7")]),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return tabela


def gerar_pdf_estrategico_loja(loja, ano, mes, dias_uteis_total=None):
    """Gera o Relatório Estratégico Comercial de UMA loja: indicadores do mês
    (com comparativo MoM/YoY), situações a trabalhar (ritmo de meta por vendedor +
    comparativo com a outra loja), vendedores em risco de desempenho (com o
    indicativo de quando uma substituição passaria a fazer sentido), diagnóstico
    de concentração de portfólio por vendedor, e um PLANO DE AÇÃO pros próximos 3
    meses — tudo derivado dos indicadores já calculados no painel, sem IA gerando
    texto solto. Retorna os bytes do PDF."""
    dias_uteis_total = dias_uteis_total or db.dias_uteis_no_mes(ano, mes)
    dias_transcorridos = db.dias_uteis_transcorridos(ano, mes, dias_uteis_total)
    ano_ant, mes_ant = db.mes_anterior(ano, mes)

    totais_atual = db.get_totais_mes(ano, mes, loja=loja)
    totais_mes_anterior = db.get_totais_mes(ano_ant, mes_ant, loja=loja)
    totais_ano_anterior = db.get_totais_mes(ano - 1, mes, loja=loja)

    meta = totais_atual["meta"]
    realizado = totais_atual["realizado"]
    pedidos = totais_atual["pedidos"]
    atingimento = (realizado / meta * 100) if meta > 0 else 0.0
    projecao = (realizado / dias_transcorridos * dias_uteis_total) if dias_transcorridos > 0 else realizado
    ticket_medio = (realizado / pedidos) if pedidos > 0 else 0.0

    def _crescimento(atual_v, anterior_v):
        return ((atual_v - anterior_v) / anterior_v * 100) if anterior_v > 0 else None

    def _fmt_cresc(v):
        return "sem dado no período anterior" if v is None else f"{'+' if v >= 0 else ''}{v:.1f}%"

    cresc_mom = _crescimento(realizado, totais_mes_anterior["realizado"])
    cresc_yoy = _crescimento(realizado, totais_ano_anterior["realizado"])

    resumo_prod = db.get_resumo_produtos_mes(ano, mes, loja=loja)
    concentracao_prod = db.get_concentracao_portfolio(ano, mes, loja=loja)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, topMargin=1.6 * cm, bottomMargin=1.6 * cm,
        leftMargin=1.8 * cm, rightMargin=1.8 * cm,
    )
    styles = getSampleStyleSheet()
    titulo_style = ParagraphStyle("titulo", parent=styles["Heading1"], textColor=AZUL, spaceAfter=2)
    sub_style = ParagraphStyle("sub", parent=styles["Normal"], textColor=CINZA)
    secao_style = ParagraphStyle("secao", parent=styles["Heading3"], textColor=AZUL, spaceBefore=10)
    destaque_style = ParagraphStyle("destaque", parent=styles["Heading2"], textColor=VERDE, spaceBefore=10)

    elementos = []
    elementos.append(Paragraph("Relatório Estratégico Comercial", titulo_style))
    elementos.append(Paragraph(loja, styles["Heading2"]))
    elementos.append(Paragraph(f"Período de referência: {db.MESES_PT[mes]}/{ano}", sub_style))
    elementos.append(Spacer(1, 0.4 * cm))
    elementos.append(HRFlowable(width="100%", color=AZUL, thickness=1.2))
    elementos.append(Spacer(1, 0.5 * cm))

    # ---- Indicadores do mês ----
    dados_kpi = [
        ["Indicador", "Valor"],
        ["Meta do mês", db.formatar_moeda(meta)],
        ["Realizado do mês", db.formatar_moeda(realizado)],
        ["Atingimento da meta (%)", f"{atingimento:.1f}% ({db.label_semaforo(atingimento)})"],
        ["Projeção de fechamento", db.formatar_moeda(projecao)],
        ["Pedidos no mês", str(pedidos)],
        ["Ticket médio", db.formatar_moeda(ticket_medio)],
        [f"vs. {db.MESES_PT[mes_ant]}/{ano_ant} (realizado)", _fmt_cresc(cresc_mom)],
        [f"vs. {db.MESES_PT[mes]}/{ano - 1} (realizado)", _fmt_cresc(cresc_yoy)],
        ["Faturamento em produtos", db.formatar_moeda(resumo_prod["faturamento_total"])],
        ["SKUs distintos vendidos", str(resumo_prod["n_produtos"])],
        [
            "Concentração top 5 SKUs",
            f"{concentracao_prod['top5_pct']:.1f}%" if concentracao_prod["top5_pct"] is not None else "—",
        ],
    ]
    elementos.append(_tabela_padrao(dados_kpi, [9 * cm, 7 * cm]))
    elementos.append(Spacer(1, 0.3 * cm))

    # ---- Situações a trabalhar (ritmo de meta por vendedor + comparativo entre lojas) ----
    elementos.append(Paragraph("Situações a Trabalhar", secao_style))
    elementos.append(Spacer(1, 0.2 * cm))
    situacoes = []
    try:
        indicadores_loja = db.get_indicadores_vendedores_mes(ano, mes, loja=loja)
        if not indicadores_loja.empty and dias_transcorridos > 0:
            for row in indicadores_loja.itertuples():
                if row.valor_meta > 0:
                    projecao_vend = row.realizado / dias_transcorridos * dias_uteis_total
                    projecao_pct_vend = projecao_vend / row.valor_meta * 100
                    if projecao_pct_vend < 70:
                        situacoes.append(
                            f"{row.nome}: no ritmo atual, fecha o mês em {projecao_pct_vend:.0f}% da "
                            f"meta (projeção {db.formatar_moeda(projecao_vend)} de "
                            f"{db.formatar_moeda(row.valor_meta)})."
                        )
    except Exception:
        pass
    try:
        for _, texto_alerta in db.gerar_alertas_comparativo_lojas(ano, mes):
            situacoes.append(texto_alerta)
    except Exception:
        pass

    if not situacoes:
        elementos.append(Paragraph("Nenhuma situação crítica identificada neste período.", styles["Normal"]))
    else:
        for situacao in situacoes:
            elementos.append(Paragraph(f"• {_md_para_reportlab(situacao)}", styles["Normal"]))
            elementos.append(Spacer(1, 0.1 * cm))
    elementos.append(Spacer(1, 0.3 * cm))

    # ---- Vendedores em atenção / risco de desempenho ----
    elementos.append(Paragraph("Vendedores em Atenção — Risco de Desempenho", secao_style))
    elementos.append(Spacer(1, 0.2 * cm))
    elementos.append(Paragraph(
        "Sinal construído a partir de todo o histórico do vendedor somado à projeção do mês "
        "corrente — NÃO é uma decisão automática de desligamento, serve como gatilho pra um "
        "plano de ação (30/60/90 dias) antes de qualquer decisão sobre o vendedor.",
        sub_style,
    ))
    elementos.append(Spacer(1, 0.15 * cm))
    try:
        atencao_df = db.get_vendedores_em_atencao(ano, mes, loja=loja, dias_uteis_total=dias_uteis_total)
    except Exception:
        atencao_df = pd.DataFrame()
    if atencao_df.empty:
        elementos.append(Paragraph("Sem histórico suficiente para calcular os sinais.", styles["Normal"]))
    else:
        dados_atencao = [["Vendedor", "Zona", "Sinais", "Atingimento Atual (Proj.)"]]
        for _, r in atencao_df.iterrows():
            dados_atencao.append([
                r["nome"], r["zona"], str(int(r["criterios_atendidos"])),
                f"{r['atingimento_atual_proj']:.1f}%",
            ])
        elementos.append(_tabela_padrao(dados_atencao, [5.5 * cm, 3.5 * cm, 2 * cm, 5 * cm]))
    elementos.append(Spacer(1, 0.3 * cm))

    # ---- Concentração de portfólio por vendedor ----
    elementos.append(Paragraph("Concentração de Portfólio por Vendedor", secao_style))
    elementos.append(Spacer(1, 0.2 * cm))
    try:
        conc_vend_df = db.get_comparativo_concentracao_vendedores(ano, mes, loja=loja)
    except Exception:
        conc_vend_df = pd.DataFrame()
    if conc_vend_df.empty:
        elementos.append(Paragraph("Sem dado de produto suficiente para este comparativo.", styles["Normal"]))
    else:
        dados_conc = [["Vendedor", "Top 5", "Top 10", "Risco"]]
        for _, r in conc_vend_df.iterrows():
            dados_conc.append([
                r["nome"],
                f"{r['top5_pct']:.1f}%" if pd.notna(r["top5_pct"]) else "—",
                f"{r['top10_pct']:.1f}%" if pd.notna(r["top10_pct"]) else "—",
                r["classificacao"],
            ])
        elementos.append(_tabela_padrao(dados_conc, [6 * cm, 3 * cm, 3 * cm, 4 * cm]))
    elementos.append(Spacer(1, 0.3 * cm))

    # ---- Plano de Ação — Próximos 3 Meses ----
    elementos.append(Paragraph("Plano de Ação — Próximos 3 Meses", destaque_style))
    elementos.append(Spacer(1, 0.2 * cm))
    try:
        plano = db.gerar_plano_acao_loja(loja, ano, mes, dias_uteis_total=dias_uteis_total)
    except Exception as e_plano:
        plano = [f"Não foi possível montar o plano de ação automaticamente ({e_plano})."]
    for item in plano:
        elementos.append(Paragraph(f"• {_md_para_reportlab(item)}", styles["Normal"]))
        elementos.append(Spacer(1, 0.15 * cm))

    elementos.append(Spacer(1, 0.8 * cm))
    elementos.append(HRFlowable(width="100%", color=colors.lightgrey, thickness=0.5))
    elementos.append(Spacer(1, 0.2 * cm))
    elementos.append(Paragraph(
        f"Relatório gerado automaticamente em {datetime.now().strftime('%d/%m/%Y às %H:%M')}.",
        sub_style,
    ))

    doc.build(elementos)
    buffer.seek(0)
    return buffer.getvalue()