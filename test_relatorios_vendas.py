import io
from datetime import date

import pandas as pd

import relatorios_vendas as rv

VENDAS = pd.DataFrame(
    [("Porteira", date(2026, 3, 2), 100.0),       # segunda
     ("Casa de Adubo", date(2026, 3, 2), 50.0),   # mesma data, outra loja
     ("Porteira", date(2026, 3, 6), 400.0),       # sexta
     ("Porteira", date(2026, 3, 8), 0.0),         # domingo zerado
     ("Porteira", date(2026, 4, 6), 250.0)],      # segunda
    columns=["loja", "data", "valor_realizado"],
).assign(qtd_pedidos=[3, 1, 2, 0, 5])
MANUAL = pd.DataFrame([("Porteira", 2026, 2, 1000.0)], columns=["loja", "ano", "mes", "valor_realizado"])
PEDIDOS = pd.DataFrame([("Porteira", 2026, 2, 7)], columns=["loja", "ano", "mes", "qtd_pedidos"])
VAZIO_P = PEDIDOS.iloc[0:0]
VAZIO_V, VAZIO_M = VENDAS.iloc[0:0], MANUAL.iloc[0:0]


def test_dia_semana_crescente_com_acumulado():
    t = rv.por_dia_semana(VENDAS)
    assert list(t["Dia da semana"]) == ["Segunda", "Sexta"]  # médias 200 e 400; domingo fora
    assert list(t["Dias com venda"]) == [2, 1]
    assert list(t["Acumulado"]) == [400.0, 800.0]
    assert t["% acumulado"].iloc[-1] == 100


def test_mes_soma_importado():
    t = rv.por_mes(VENDAS, MANUAL, PEDIDOS)
    assert list(t["Mês"]) == ["02/2026", "03/2026", "04/2026"]
    assert list(t["Total"]) == [1000.0, 550.0, 250.0]
    assert t["Acumulado"].iloc[-1] == 1800.0
    assert pd.isna(t["Média por dia"].iloc[0])


def test_consolidado_e_datas_batem():
    g = rv.consolidado(VENDAS, MANUAL, PEDIDOS).set_index("Loja")
    assert g.loc["TOTAL", "Total"] == 1800.0
    assert g.drop("TOTAL")["Total"].sum() == 1800.0
    d = rv.por_data(VENDAS)
    assert list(d["Total"]) == [150.0, 250.0, 400.0]


def test_vazios():
    assert rv.por_dia_semana(VAZIO_V).empty
    assert rv.por_data(VAZIO_V).empty
    assert rv.por_mes(VAZIO_V, VAZIO_M, VAZIO_P).empty
    assert rv.por_mes(VAZIO_V, MANUAL, VAZIO_P)["Total"].sum() == 1000.0


def test_excel_numerico():
    abas = pd.read_excel(io.BytesIO(rv.para_excel({"Por mês": rv.por_mes(VENDAS, MANUAL, PEDIDOS)})), sheet_name=None)
    assert abas["Por mês"]["Total"].sum() == 1800.0


def test_pedidos():
    s = rv.por_dia_semana(VENDAS, "Média de pedidos por dia")
    assert list(s["Dia da semana"]) == ["Sexta", "Segunda"]  # 2 contra 4,5
    assert list(s["Pedidos acumulados"]) == [2, 11]
    assert list(rv.por_data(VENDAS, "Pedidos")["Pedidos"]) == [2, 4, 5]
    assert list(rv.por_mes(VENDAS, MANUAL, PEDIDOS)["Pedidos"]) == [7, 6, 5]
    g = rv.consolidado(VENDAS, MANUAL, PEDIDOS).set_index("Loja")
    assert g.loc["TOTAL", "Pedidos"] == 18
    assert g.loc["TOTAL", "Ticket médio"] == 800 / 11
