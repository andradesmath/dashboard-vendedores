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
from datetime import datetime

import numpy as np
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable,
)
from reportlab.graphics.shapes import Drawing
from reportlab.graphics.charts.barcharts import VerticalBarChart

import db

AZUL = colors.HexColor("#1a5276")
VERDE = colors.HexColor("#1e8449")
CINZA = colors.HexColor("#555555")


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
            f"mês à frente), não a média 'congelada'. Faixas de risco: 🟢 Baixo "
            f"(&lt; {db.INADIMPLENCIA_LIMIAR_BAIXO:.0f}%), 🟡 Moderado "
            f"({db.INADIMPLENCIA_LIMIAR_BAIXO:.0f}–{db.INADIMPLENCIA_LIMIAR_MODERADO:.0f}%), "
            f"🔴 Alto (&gt; {db.INADIMPLENCIA_LIMIAR_MODERADO:.0f}%).",
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