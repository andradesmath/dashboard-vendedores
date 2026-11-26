"""Relatórios de vendas: por dia da semana, por mês e consolidado.

Só pandas (sem Streamlit nem banco), para poder testar isoladamente.
Os relatórios saem em ordem CRESCENTE (do mais fraco ao mais forte) quando
são ranking; o mensal sai em ordem cronológica.
"""
import io

import pandas as pd

DIAS_SEMANA = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]


def totais_por_dia(vendas: pd.DataFrame) -> pd.Series:
    """Soma vendedores e lojas por data. Dias zerados saem: loja fechada não é dia fraco."""
    if vendas.empty:
        return pd.Series(dtype=float)
    dia = vendas.groupby(pd.to_datetime(vendas["data"]))["valor_realizado"].sum().astype(float)
    return dia[dia > 0].sort_index()


def pedidos_por_dia(vendas: pd.DataFrame) -> pd.Series:
    """Pedidos fechados (Nº Ped do SGI) por data, nos mesmos dias de totais_por_dia."""
    datas = totais_por_dia(vendas).index
    if not len(datas):
        return pd.Series(dtype=int)
    qtd = vendas.groupby(pd.to_datetime(vendas["data"]))["qtd_pedidos"].sum()
    return qtd.reindex(datas).astype(int)


def _ticket(valor, pedidos):
    return valor / pedidos if pedidos else None


def _acumular(tabela: pd.DataFrame) -> pd.DataFrame:
    tabela["Pedidos acumulados"] = tabela["Pedidos"].cumsum()
    tabela["Acumulado"] = tabela["Total"].cumsum()
    soma = tabela["Total"].sum()
    tabela["% acumulado"] = tabela["Acumulado"] / soma * 100 if soma else 0.0
    return tabela.reset_index(drop=True)


def por_dia_semana(vendas: pd.DataFrame, ordenar_por: str = "Média por dia") -> pd.DataFrame:
    """Ranking crescente dos dias da semana, por média (padrão) ou por total."""
    colunas = ["Dia da semana", "Dias com venda", "Média por dia", "Total",
               "Pedidos", "Média de pedidos por dia", "Ticket médio"]
    dia = totais_por_dia(vendas)
    if dia.empty:
        return pd.DataFrame(columns=colunas + ["Pedidos acumulados", "Acumulado", "% acumulado"])
    t = dia.groupby(dia.index.dayofweek).agg(["size", "mean", "sum"]).reset_index()
    t.columns = colunas[:4]
    pedidos = pedidos_por_dia(vendas)
    q = pedidos.groupby(pedidos.index.dayofweek).agg(["sum", "mean"])
    t["Pedidos"] = t["Dia da semana"].map(q["sum"])
    t["Média de pedidos por dia"] = t["Dia da semana"].map(q["mean"])
    t["Ticket médio"] = [_ticket(v, n) for v, n in zip(t["Total"], t["Pedidos"])]
    t["Dia da semana"] = t["Dia da semana"].map(lambda i: DIAS_SEMANA[i])
    return _acumular(t.sort_values(ordenar_por, kind="stable"))


def por_mes(vendas: pd.DataFrame, manual: pd.DataFrame, pedidos_manual: pd.DataFrame) -> pd.DataFrame:
    """Total mensal = lançamentos diários + totais mensais importados
    (mesma regra de db.get_totais_mes, para bater com a aba Dashboard).
    `manual` tem as colunas ano, mes, valor_realizado."""
    dia = totais_por_dia(vendas)
    diario = dia.groupby(dia.index.to_period("M")).agg(["size", "sum"]) if not dia.empty \
        else pd.DataFrame(columns=["size", "sum"])
    if manual.empty:
        importado = pd.Series(dtype=float)
    else:
        periodos = pd.PeriodIndex(
            [pd.Period(year=int(a), month=int(m), freq="M") for a, m in zip(manual["ano"], manual["mes"])]
        )
        importado = manual["valor_realizado"].astype(float).groupby(periodos).sum()
    qtd = pedidos_por_dia(vendas)
    qtd = qtd.groupby(qtd.index.to_period("M")).sum() if len(qtd) else qtd
    if pedidos_manual.empty:
        qtd_importada = pd.Series(dtype=int)
    else:
        periodos_q = pd.PeriodIndex([
            pd.Period(year=int(a), month=int(m), freq="M")
            for a, m in zip(pedidos_manual["ano"], pedidos_manual["mes"])
        ])
        qtd_importada = pedidos_manual["qtd_pedidos"].groupby(periodos_q).sum()
    meses = sorted(set(diario.index) | set(importado.index) | set(qtd_importada.index))
    linhas = []
    for p in meses:
        n = int(diario["size"].get(p, 0))
        d = float(diario["sum"].get(p, 0.0))
        i = float(importado.get(p, 0.0))
        linhas.append({
            "Mês": f"{p.month:02d}/{p.year}", "Dias com venda": n,
            "Lançamento diário": d, "Importado mensal": i, "Total": d + i,
            # média só sobre o que tem data; o importado não sabe em quantos dias vendeu
            "Média por dia": d / n if n else None,
            "Pedidos": int(qtd.get(p, 0)) + int(qtd_importada.get(p, 0)),
            # só lançamento diário: valor e pedidos da mesma fonte
            "Ticket médio": _ticket(d, int(qtd.get(p, 0))),
        })
    colunas = ["Mês", "Dias com venda", "Lançamento diário", "Importado mensal", "Total",
               "Média por dia", "Pedidos", "Ticket médio"]
    return _acumular(pd.DataFrame(linhas, columns=colunas))


def por_data(vendas: pd.DataFrame, ordenar_por: str = "Total") -> pd.DataFrame:
    """Todas as datas, em ordem crescente de valor, pedidos ou ticket, com acumulado."""
    dia = totais_por_dia(vendas)
    qtd = pedidos_por_dia(vendas)
    t = pd.DataFrame({
        "Data": dia.index.strftime("%d/%m/%Y") if len(dia) else [],
        "Dia da semana": [DIAS_SEMANA[d.dayofweek] for d in dia.index],
        "Total": dia.values,
        "Pedidos": qtd.values,
    })
    t["Ticket médio"] = [_ticket(v, n) for v, n in zip(t["Total"], t["Pedidos"])]
    return _acumular(t.sort_values(ordenar_por, kind="stable"))


def consolidado(vendas: pd.DataFrame, manual: pd.DataFrame, pedidos_manual: pd.DataFrame) -> pd.DataFrame:
    """Uma linha por loja e uma linha TOTAL."""
    lojas = set(vendas["loja"]) if not vendas.empty else set()
    lojas |= set(manual["loja"]) if not manual.empty else set()
    lojas |= set(pedidos_manual["loja"]) if not pedidos_manual.empty else set()
    linhas = []
    for loja in sorted(lojas) + ["TOTAL"]:
        v = vendas if loja == "TOTAL" or vendas.empty else vendas[vendas["loja"] == loja]
        m = manual if loja == "TOTAL" or manual.empty else manual[manual["loja"] == loja]
        pm = pedidos_manual if loja == "TOTAL" or pedidos_manual.empty \
            else pedidos_manual[pedidos_manual["loja"] == loja]
        dia = totais_por_dia(v)
        qtd_diaria = int(pedidos_por_dia(v).sum())
        qtd_importada = int(pm["qtd_pedidos"].sum()) if not pm.empty else 0
        diario = float(dia.sum())
        importado = float(m["valor_realizado"].sum()) if not m.empty else 0.0
        linhas.append({
            "Loja": loja, "Dias com venda": len(dia), "Lançamento diário": diario,
            "Importado mensal": importado, "Total": diario + importado,
            "Média por dia": diario / len(dia) if len(dia) else None,
            "Pedidos": qtd_diaria + qtd_importada,
            # ticket só do lançamento diário: valor e pedidos da mesma fonte
            "Ticket médio": _ticket(diario, qtd_diaria),
        })
    return pd.DataFrame(linhas)


def para_excel(abas: dict) -> bytes:
    """Um .xlsx com uma aba por relatório; valores continuam numéricos."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        for nome, df in abas.items():
            df.to_excel(w, sheet_name=nome, index=False)
            ws = w.sheets[nome]
            for i, col in enumerate(df.columns, start=1):
                letra = ws.cell(1, i).column_letter
                ws.column_dimensions[letra].width = 20
                if col in ("Ticket médio", "Total", "Acumulado", "Média por dia", "Lançamento diário", "Importado mensal"):
                    for cel in ws[letra][1:]:
                        cel.number_format = "#,##0.00"
                elif col in ("% acumulado", "Média de pedidos por dia"):
                    for cel in ws[letra][1:]:
                        cel.number_format = "0.0"
    return buf.getvalue()
