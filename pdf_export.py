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
        "pct_np_mes": next(
            (l["pct_mes"] for l in mix_pagamento if l["modalidade"] == "Nota Promissória"), 0.0
        ) if mix_mes_total > 0 else None,
        "pct_np_historico": next(
            (l["media_historica_pct"] for l in mix_pagamento if l["modalidade"] == "Nota Promissória"), 0.0
        ) if mix_hist_total > 0 else None,
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
            f"⚠️ Risco de Nota Promissória: {kpis['pct_np_mes']:.1f}% do realizado do mês "
            f"({nivel_risco}){variacao_np_txt}",
            risco_estilo,
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