"""
pdf_export.py - Geração de relatórios em PDF com os indicadores de cada vendedor.

Usa reportlab (biblioteca 100% Python, sem dependência de binários externos como
Chromium/wkhtmltopdf), o que garante compatibilidade com o deploy no Streamlit
Community Cloud sem passos extras de instalação de sistema.

Cada PDF traz: dados do vendedor, KPIs do mês (meta, realizado, atingimento,
clientes atendidos, ticket médio individual) e um gráfico de barras com o
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


def _calcular_kpis_vendedor(vendedor_id, ano, mes):
    meta = db.get_meta_vendedor(vendedor_id, ano, mes)
    vendas = db.get_vendas_vendedor_mes(vendedor_id, ano, mes)
    realizado = float(vendas["valor_realizado"].sum()) if not vendas.empty else 0.0
    realizado += db.get_realizado_manual_vendedor(vendedor_id, ano, mes)
    clientes = int(vendas["qtd_clientes"].sum()) if not vendas.empty else 0
    clientes += db.get_clientes_manual_vendedor(vendedor_id, ano, mes)
    atingimento = (realizado / meta * 100) if meta > 0 else 0.0
    ticket = (realizado / clientes) if clientes > 0 else 0.0
    return {
        "meta": meta,
        "realizado": realizado,
        "clientes": clientes,
        "atingimento": atingimento,
        "ticket": ticket,
        "vendas": vendas,
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
    kpis = _calcular_kpis_vendedor(vendedor_id, ano, mes)

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

    dados_tabela = [
        ["Indicador", "Valor"],
        ["Meta do mês", db.formatar_moeda(kpis["meta"])],
        ["Realizado do mês", db.formatar_moeda(kpis["realizado"])],
        ["Atingimento (%)", f"{kpis['atingimento']:.1f}%  ({db.label_semaforo(kpis['atingimento'])})"],
        ["Clientes atendidos", str(kpis["clientes"])],
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