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


def _acumular(tabela: pd.DataFrame) -> pd.DataFrame:
    tabela["Acumulado"] = tabela["Total"].cumsum()
    soma = tabela["Total"].sum()
    tabela["% acumulado"] = tabela["Acumulado"] / soma * 100 if soma else 0.0
    return tabela.reset_index(drop=True)


def por_dia_semana(vendas: pd.DataFrame, ordenar_por: str = "Média por dia") -> pd.DataFrame:
    """Ranking crescente dos dias da semana, por média (padrão) ou por total."""
    colunas = ["Dia da semana", "Dias com venda", "Média por dia", "Total"]
    dia = totais_por_dia(vendas)
    if dia.empty:
        return pd.DataFrame(columns=colunas + ["Acumulado", "% acumulado"])
    t = dia.groupby(dia.index.dayofweek).agg(["size", "mean", "sum"]).reset_index()
    t.columns = colunas
    t["Dia da semana"] = t["Dia da semana"].map(lambda i: DIAS_SEMANA[i])
    return _acumular(t.sort_values(ordenar_por, kind="stable"))


def por_mes(vendas: pd.DataFrame, manual: pd.DataFrame) -> pd.DataFrame:
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
    meses = sorted(set(diario.index) | set(importado.index))
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
        })
    colunas = ["Mês", "Dias com venda", "Lançamento diário", "Importado mensal", "Total", "Média por dia"]
    return _acumular(pd.DataFrame(linhas, columns=colunas))


def por_data(vendas: pd.DataFrame) -> pd.DataFrame:
    """Todas as datas, da mais fraca à mais forte, com acumulado."""
    dia = totais_por_dia(vendas).sort_values(kind="stable")
    t = pd.DataFrame({
        "Data": dia.index.strftime("%d/%m/%Y") if len(dia) else [],
        "Dia da semana": [DIAS_SEMANA[d.dayofweek] for d in dia.index],
        "Total": dia.values,
    })
    return _acumular(t)


def consolidado(vendas: pd.DataFrame, manual: pd.DataFrame) -> pd.DataFrame:
    """Uma linha por loja e uma linha TOTAL."""
    lojas = set(vendas["loja"]) if not vendas.empty else set()
    lojas |= set(manual["loja"]) if not manual.empty else set()
    linhas = []
    for loja in sorted(lojas) + ["TOTAL"]:
        v = vendas if loja == "TOTAL" or vendas.empty else vendas[vendas["loja"] == loja]
        m = manual if loja == "TOTAL" or manual.empty else manual[manual["loja"] == loja]
        dia = totais_por_dia(v)
        diario = float(dia.sum())
        importado = float(m["valor_realizado"].sum()) if not m.empty else 0.0
        linhas.append({
            "Loja": loja, "Dias com venda": len(dia), "Lançamento diário": diario,
            "Importado mensal": importado, "Total": diario + importado,
            "Média por dia": diario / len(dia) if len(dia) else None,
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
                if col in ("Total", "Acumulado", "Média por dia", "Lançamento diário", "Importado mensal"):
                    for cel in ws[letra][1:]:
                        cel.number_format = "#,##0.00"
                elif col == "% acumulado":
                    for cel in ws[letra][1:]:
                        cel.number_format = "0.0"
    return buf.getvalue()
