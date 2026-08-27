"""
scripts/backfill_produtos.py - Importa o HISTÓRICO do relatório 'Totais de Vendas
Por Produto' do SGI de uma vez só (rodagem ÚNICA, disparada na mão — não é
agendada como scripts/sync_sgi.py):

  - De setembro/2025 até o mês anterior ao atual: um relatório por MÊS INTEIRO
    (período = 1º ao último dia do mês), guardado sob o dia 1º daquele mês — dá o
    total mensal por produto/vendedor. Não teria sentido puxar dia a dia um
    histórico tão longo (seriam centenas de relatórios), e as telas do painel que
    usam esse dado ("Vendas por Produto") já agregam por mês mesmo.
  - No mês atual: um relatório por DIA, do dia 1º até hoje — mesma granularidade
    diária da sincronização normal, assim o mês corrente fica com detalhe por dia
    desde o começo dele (não só a partir de quando essa automação foi ligada).

Reaproveita login/navegação/captura de tela de scripts/sync_sgi.py — só entra na
página do relatório UMA VEZ e fica gerando de novo (troca as datas, clica em
Imprimir de novo) pra não logar dezenas de vezes.

Uso:
    python scripts/backfill_produtos.py                 # todas as lojas, histórico completo
    python scripts/backfill_produtos.py --loja Porteira  # só uma loja
    python scripts/backfill_produtos.py --headed         # navegador visível (debug local)
    python scripts/backfill_produtos.py --ano 2026 --mes 8
        # reprocessa DIA A DIA só agosto/2026 (passado ou corrente)
    python scripts/backfill_produtos.py --ano 2026 --mes 6 --ano-fim 2026 --mes-fim 8
        # reprocessa DIA A DIA de junho/2026 até agosto/2026, tudo seguido

Mesmas variáveis de ambiente de scripts/sync_sgi.py (DATABASE_URL, SGI_URL_LOGIN,
SGI_LOGIN, SGI_SENHA, SGI_EMPRESA_PORTEIRA, SGI_EMPRESA_CASA_ADUBO). Rodagem
demorada (dezenas de relatórios por loja) — pensada pra rodar via o workflow
dedicado .github/workflows/backfill_produtos.yml (workflow_dispatch manual, sem
agendamento), não faz parte da sincronização automática de 12h/19h.
"""
import argparse
import calendar
import os
import sys
from datetime import date, timedelta

RAIZ_PROJETO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ_PROJETO)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(RAIZ_PROJETO, ".env"))
except ImportError:
    pass

import db  # noqa: E402
from sgi_relatorio import casar_produtos_vendedores, parse_relatorio_produtos_pdf  # noqa: E402
# Reaproveita as funções de login/navegação/captura já testadas em sync_sgi.py —
# como este arquivo também está em scripts/, o Python acha o módulo direto.
from sync_sgi import (  # noqa: E402
    LOJA_PARA_EMPRESA_ENV,
    _salvar_debug,
    fazer_login,
    gerar_pdf_totais_de_vendas_por_produto,
    navegar_ate_totais_de_vendas_por_produto,
    obter_lojas_configuradas,
)

from playwright.sync_api import sync_playwright  # noqa: E402


INICIO_HISTORICO_ANO = 2025
INICIO_HISTORICO_MES = 9  # setembro/2025 — início pedido pelo usuário


def _gerar_meses(ano_ini, mes_ini, ano_fim, mes_fim):
    """(ano, mes) de ano_ini/mes_ini até ano_fim/mes_fim, inclusive, em ordem
    cronológica. Não gera nada se o intervalo for vazio/invertido."""
    ano, mes = ano_ini, mes_ini
    while (ano, mes) <= (ano_fim, mes_fim):
        yield ano, mes
        ano, mes = (ano + 1, 1) if mes == 12 else (ano, mes + 1)


def _processar_pdf_produtos(loja, data_tag, pdf_bytes, rotulo):
    """Faz o parse do PDF de produtos e grava no banco sob a data `data_tag`
    (1º dia do mês pro histórico mensal, o próprio dia pro mês corrente) —
    tanto o detalhe por produto (vendas_produtos_diarias) quanto o resumo por
    vendedor (vendas_diarias: Realizado = soma de valor_total, Pedidos = soma
    da coluna 'Vendas' como aproximação), igual à sincronização diária em
    scripts/sync_sgi.py. Isso re-popula vendas_diarias com o histórico também,
    já que antes esse backfill só gravava vendas_produtos_diarias."""
    resultado = parse_relatorio_produtos_pdf(pdf_bytes)
    if not resultado["produtos"]:
        print(f"  [{loja}] {rotulo}: nenhum produto reconhecido (sem vendas nesse "
              "período, ou o PDF veio vazio/diferente do esperado).")
        return 0

    vendedores_df = db.get_vendedores(loja=loja, apenas_ativos=True)
    matched, nao_encontrados = casar_produtos_vendedores(resultado["produtos"], vendedores_df)
    if nao_encontrados:
        print(f"  [{loja}] {rotulo}: ignorados (sem correspondência no cadastro): "
              + ", ".join(nao_encontrados))

    por_vendedor = {}
    for item in matched:
        por_vendedor.setdefault(item["vendedor_id"], []).append(item)
    for vendedor_id, produtos in por_vendedor.items():
        db.upsert_vendas_produtos_dia(vendedor_id, data_tag, produtos)

        soma_valor = sum(p["valor_total"] or 0.0 for p in produtos)
        soma_vendas = sum(p["vendas"] or 0 for p in produtos)
        db.upsert_venda(vendedor_id, data_tag, float(soma_valor), int(round(soma_vendas)))

    total_valor = sum(p["valor_total"] or 0.0 for p in matched)
    print(f"  [{loja}] {rotulo}: {len(matched)} produto(s) de {len(por_vendedor)} "
          f"vendedor(es) — R$ {total_valor:,.2f}.")
    return len(matched)


def backfill_loja(playwright, loja, hoje, headed=False, intervalo_especifico=None):
    """Se `intervalo_especifico` for informado (tupla (ano_ini, mes_ini, ano_fim, mes_fim)),
    reprocessa DIA A DIA (mesma granularidade da sincronização normal — um relatório por
    dia, sobrescrevendo cada dia individualmente) todo o período do 1º dia de
    ano_ini/mes_ini até o último dia de ano_fim/mes_fim (truncado em `hoje`, se for o caso).
    Cobre tanto um único mês (ano_ini/mes_ini == ano_fim/mes_fim) quanto vários meses
    seguidos, passados ou o corrente — útil pra corrigir/completar um período sem rodar o
    backfill histórico completo de novo.

    Senão (intervalo_especifico=None), roda o backfill completo padrão (pensado pra
    importação inicial, de uma vez só): histórico mensal AGREGADO (um relatório por mês
    inteiro) de INICIO_HISTORICO_ANO/MES até o mês anterior ao atual, mais o mês atual dia
    a dia."""
    url_login = os.environ["SGI_URL_LOGIN"]
    login = os.environ["SGI_LOGIN"]
    senha = os.environ["SGI_SENHA"]
    empresa_texto = os.environ[LOJA_PARA_EMPRESA_ENV[loja]]

    print(f"[{loja}] Login...")
    browser, context, page = fazer_login(playwright, loja, url_login, login, senha, empresa_texto, headed=headed)
    try:
        navegar_ate_totais_de_vendas_por_produto(page)
        _salvar_debug(page, loja, "backfill_form_antes")

        if intervalo_especifico is not None:
            ano_ini, mes_ini, ano_fim, mes_fim = intervalo_especifico
            ultimo_dia_fim = calendar.monthrange(ano_fim, mes_fim)[1]
            dia = date(ano_ini, mes_ini, 1)
            dia_fim = min(date(ano_fim, mes_fim, ultimo_dia_fim), hoje)
            while dia <= dia_fim:
                data_str = dia.strftime("%d/%m/%Y")
                rotulo = f"{data_str} (dia)"
                print(f"  [{loja}] gerando relatório de {rotulo}...")
                pdf_bytes = gerar_pdf_totais_de_vendas_por_produto(page, context, data_str, data_str)
                _processar_pdf_produtos(loja, dia, pdf_bytes, rotulo)
                dia += timedelta(days=1)
            return

        ano_fim_hist, mes_fim_hist = db.mes_anterior(hoje.year, hoje.month)

        # ---- Histórico mensal: set/2025 até o mês anterior ao atual ----
        if (INICIO_HISTORICO_ANO, INICIO_HISTORICO_MES) <= (ano_fim_hist, mes_fim_hist):
            for ano, mes in _gerar_meses(INICIO_HISTORICO_ANO, INICIO_HISTORICO_MES, ano_fim_hist, mes_fim_hist):
                ultimo_dia = calendar.monthrange(ano, mes)[1]
                data_ini_str = date(ano, mes, 1).strftime("%d/%m/%Y")
                data_fim_str = date(ano, mes, ultimo_dia).strftime("%d/%m/%Y")
                rotulo = f"{db.MESES_PT[mes]}/{ano} (mês inteiro)"
                print(f"  [{loja}] gerando relatório de {rotulo}...")
                pdf_bytes = gerar_pdf_totais_de_vendas_por_produto(page, context, data_ini_str, data_fim_str)
                _processar_pdf_produtos(loja, date(ano, mes, 1), pdf_bytes, rotulo)

        # ---- Mês atual: dia a dia, do dia 1º até hoje ----
        dia = date(hoje.year, hoje.month, 1)
        while dia <= hoje:
            data_str = dia.strftime("%d/%m/%Y")
            rotulo = f"{data_str} (dia)"
            print(f"  [{loja}] gerando relatório de {rotulo}...")
            pdf_bytes = gerar_pdf_totais_de_vendas_por_produto(page, context, data_str, data_str)
            _processar_pdf_produtos(loja, dia, pdf_bytes, rotulo)
            dia += timedelta(days=1)

    except Exception:
        _salvar_debug(page, loja, "backfill_erro")
        raise
    finally:
        browser.close()


def main():
    parser = argparse.ArgumentParser(
        description="Importa o histórico de Vendas por Produto do SGI (rodagem única)."
    )
    parser.add_argument("--loja", choices=list(LOJA_PARA_EMPRESA_ENV.keys()), help="Rodar só uma loja.")
    parser.add_argument(
        "--ano", type=int,
        help="Ano inicial do período a reprocessar DIA A DIA (usar junto com --mes). "
             "Sem isso, roda o histórico completo (mensal agregado).",
    )
    parser.add_argument(
        "--mes", type=int,
        help="Mês inicial do período, 1-12 (usar junto com --ano) — reprocessa dia a dia a "
             "partir daqui, sem rodar o histórico inteiro.",
    )
    parser.add_argument(
        "--ano-fim", type=int,
        help="Ano final do período (default: igual a --ano, ou seja, só um mês).",
    )
    parser.add_argument(
        "--mes-fim", type=int,
        help="Mês final do período, 1-12 (default: igual a --mes).",
    )
    parser.add_argument("--headed", action="store_true", help="Abre o navegador visível (só faz sentido local).")
    args = parser.parse_args()

    intervalo_especifico = None
    if args.ano and args.mes:
        ano_fim = args.ano_fim or args.ano
        mes_fim = args.mes_fim or args.mes
        intervalo_especifico = (args.ano, args.mes, ano_fim, mes_fim)

    hoje = date.today()
    lojas = [args.loja] if args.loja else obter_lojas_configuradas()
    if not lojas:
        print(
            "Nenhuma loja configurada — defina SGI_EMPRESA_PORTEIRA e/ou "
            "SGI_EMPRESA_CASA_ADUBO (ou passe --loja)."
        )
        sys.exit(1)

    erros = []
    with sync_playwright() as playwright:
        for loja in lojas:
            try:
                backfill_loja(playwright, loja, hoje, headed=args.headed, intervalo_especifico=intervalo_especifico)
            except Exception as e:
                erros.append(f"{loja}: {e}")
                print(f"[{loja}] ERRO: {e}")

    if erros:
        print("\nFalhas nessa execução:")
        for erro in erros:
            print(f"  - {erro}")
        sys.exit(1)

    print("\nBackfill concluído sem erros.")


if __name__ == "__main__":
    main()